"""X2b. System B's intraday scanner.

Every few minutes between 09:45 and 15:30 New York time:

1. Cheap rules on live prices for the whole S&P 500 (no Claude, no cost):
     breakout   above its 20-day high, up today, on heavy volume, above its 50-day average
     momentum   up at least one average daily range today, on heavy volume, near the day's high
     pullback   in an uptrend, down at least 1.5 average daily ranges, already off the low
   "Heavy volume" compares today's pace with yesterday's on the same feed.
2. The best few go to a quick screen (Claude Haiku) that passes on most of them.
3. What survives goes to the decision (Claude Sonnet): buy or not, with an entry zone,
   stop, target, expected holding time, confidence and plain-language reasons. Code then
   checks the plan (prices near the market, reward at least 1.5x the risk, a stop
   outside normal noise, a sane holding time) and sizes it like B's daily trades: lose at
   most `risk_per_trade_pct` of the account at the stop.
4. A Telegram message announces it. In live mode the order is placed after the /skip
   window unless you send /skip TICKER; in shadow mode nothing is ever placed and the
   plan is scored after the close on the full market's minute bars, costs included.

Hard limits: at most `max_calls_per_day` Claude calls, `max_decisions_per_day` decisions
and `max_trades_per_day` trades, the scanner's own monthly dollar cap, and every risk
limit of the daily trades (positions, sectors, pause, kill switch). A time stop closes
a trade once its expected holding time has clearly passed.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.context import clip
from papertrader.agents.snapshot import SECTOR_ETF
from papertrader.b.live import Candle, LiveData, Quote
from papertrader.b.models import IntradayPlan
from papertrader.b.prompts import DECIDE_SHARED, DECIDE_SPECIFIC, SCREEN_SHARED, SCREEN_SPECIFIC
from papertrader.config import AppConfig, IntradaySwitch
from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError
from papertrader.notify.messages import money, signed_pct
from papertrader.notify.outbox import queue

log = logging.getLogger(__name__)

SCREEN = SystemPrompt(SCREEN_SHARED, SCREEN_SPECIFIC)
DECIDE = SystemPrompt(DECIDE_SHARED, DECIDE_SPECIFIC)
PURPOSES = ("intraday_screen", "intraday_decide")
OPEN, CLOSE = time(9, 30), time(16, 0)
SESSION_MINUTES = 390
FUNDS = frozenset({"SPY", *SECTOR_ETF.values()})
DECIDE_PER_SCAN = 2
LATE_AFTER = timedelta(minutes=20)  # a planned order not placed by then has expired


# --------------------------------------------------------------------------- daily facts


@dataclass(frozen=True)
class DayFacts:
    """A stock's history up to yesterday's close (the full market's daily bars)."""

    ticker: str
    sector: str | None
    last_close: float
    atr_pct: float
    high20: float
    sma50: float
    ret20: float

    @property
    def uptrend(self) -> bool:
        return self.last_close > self.sma50 and self.ret20 > 0


def day_facts(session: Session, config: AppConfig, day: date) -> dict[str, DayFacts]:
    members = dict(session.execute(
        select(m.UniverseMember.ticker, m.UniverseMember.gics_sector)
        .where(m.UniverseMember.index_name == config.universe.index, m.UniverseMember.is_active.is_(True))
    ).all())  # fmt: skip
    rows = session.execute(
        select(m.PriceBar.ticker, m.PriceBar.high, m.PriceBar.low, m.PriceBar.close)
        .where(m.PriceBar.timeframe == "1Day", m.PriceBar.ticker.in_(sorted(members)),
               m.PriceBar.ts >= at_new_york(day - timedelta(days=110), time(0)),
               m.PriceBar.ts < at_new_york(day, time(0)))
        .order_by(m.PriceBar.ticker, m.PriceBar.ts)
    ).all()  # fmt: skip
    series: dict[str, list[tuple[float, float, float]]] = {}
    for ticker, high, low, close in rows:
        series.setdefault(ticker, []).append((float(high), float(low), float(close)))
    out = {}
    for ticker, bars in series.items():
        if len(bars) < 50 or bars[-1][2] <= 0:
            continue
        closes = [b[2] for b in bars]
        ranges = [
            max(h - lo, abs(h - pc), abs(lo - pc))
            for (h, lo, _), pc in zip(bars[-14:], closes[-15:-1], strict=True)
        ]
        out[ticker] = DayFacts(
            ticker=ticker, sector=members.get(ticker), last_close=closes[-1],
            atr_pct=sum(ranges) / 14 / closes[-1], high20=max(b[0] for b in bars[-20:]),
            sma50=sum(closes[-50:]) / 50, ret20=closes[-1] / closes[-21] - 1,
        )  # fmt: skip
    return out


# --------------------------------------------------------------------------- the rules


@dataclass(frozen=True)
class Candidate:
    ticker: str
    rule: str
    score: float
    facts: dict[str, float] = field(default_factory=dict)


def minutes_open(now: datetime) -> int:
    local = now.astimezone(NEW_YORK)
    return max(0, (local.hour * 60 + local.minute) - (OPEN.hour * 60 + OPEN.minute))


def volume_share(minutes: int) -> float:
    """Roughly what share of a day's volume trades in the first `minutes` (busy open)."""
    return float(min(1.0, max(minutes, 5) / SESSION_MINUTES) ** 0.75)


def find_candidates(quotes: Mapping[str, Quote], facts: Mapping[str, DayFacts], now: datetime,
                    cfg: IntradaySwitch, exclude: set[str]) -> list[Candidate]:  # fmt: skip
    today = new_york_date(now).isoformat()
    share = volume_share(minutes_open(now))
    found: list[Candidate] = []
    for ticker, q in quotes.items():
        f = facts.get(ticker)
        if (
            f is None
            or ticker in exclude
            or ticker in FUNDS
            or q.day != today
            or q.price < cfg.min_price
        ):
            continue
        if q.prev_close <= 0 or f.atr_pct <= 0 or q.high <= q.low:
            continue
        change = q.price / q.prev_close - 1
        in_ranges = change / f.atr_pct
        pace = q.volume / (q.prev_volume * share) if q.prev_volume > 0 else 0.0
        place = (q.price - q.low) / (q.high - q.low)
        numbers = {"price": q.price, "change": round(change, 4), "ranges": round(in_ranges, 2),
                   "volume_pace": round(pace, 2), "place_in_day": round(place, 2),
                   "atr_pct": round(f.atr_pct, 4), "high20": f.high20, "sma50": round(f.sma50, 2)}  # fmt: skip
        heavy = pace >= cfg.min_relative_volume
        if q.price > f.high20 and change > 0 and heavy and q.price > f.sma50:
            found.append(Candidate(ticker, "breakout", pace * max(in_ranges, 0.5), numbers))
        elif in_ranges >= cfg.big_move_atr and heavy and place >= 0.75:
            found.append(Candidate(ticker, "momentum", pace * in_ranges, numbers))
        elif f.uptrend and in_ranges <= -1.5 * cfg.big_move_atr and place >= 0.3:
            found.append(Candidate(ticker, "pullback", abs(in_ranges), numbers))
    return sorted(found, key=lambda c: -c.score)[: cfg.candidates_per_scan]


# --------------------------------------------------------------------------- Claude


class ScreenPick(BaseModel):
    ticker: str
    look_closer: bool
    reason: str


class ScreenOutput(BaseModel):
    picks: list[ScreenPick]


class DecideOutput(BaseModel):
    trade: bool
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    expected_hold_minutes: int
    confidence: float
    why: str
    invalidation: str


RULE_WORDS = {
    "breakout": "above its 20-day high on heavy volume",
    "momentum": "a big move up on heavy volume, near the day's high",
    "pullback": "a sharp dip inside an uptrend, already off the low",
}


def describe(c: Candidate) -> str:
    n = c.facts
    return (f"{c.ticker}: {RULE_WORDS[c.rule]}; {n['change']:+.1%} today ({n['ranges']:+.1f} typical "
            f"daily ranges), volume pace {n['volume_pace']:.1f}x yesterday's, price {n['price']:.2f}")  # fmt: skip


def check_plan(out: DecideOutput, price: float, facts: DayFacts, cfg: IntradaySwitch) -> str | None:
    """None if the plan is sane; otherwise why not, in plain words."""
    if not out.trade:
        return "the analyst passed"
    low, high = sorted((out.entry_low, out.entry_high))
    if out.confidence < cfg.min_confidence:
        return f"confidence {out.confidence:.2f} is below {cfg.min_confidence:.2f}"
    if not (price * 0.99 <= low and high <= price * 1.005):
        return f"entry zone {low:.2f}-{high:.2f} is not close to the price {price:.2f}"
    if not out.stop_price < low * 0.999 or not out.target_price > high * 1.001:
        return "the stop must be below the entry zone and the target above it"
    mid = (low + high) / 2
    risk, reward = mid - out.stop_price, out.target_price - mid
    if reward < cfg.min_reward_risk * risk:
        return f"reward is only {reward / risk:.1f}x the risk (needs {cfg.min_reward_risk:g}x)"
    daily_range = facts.atr_pct * price
    if risk < 0.25 * daily_range:
        return "the stop is inside the stock's normal noise"
    if risk > 3 * daily_range:
        return "the stop is too far away (more than three typical daily ranges)"
    if not 15 <= out.expected_hold_minutes <= cfg.max_hold_days * SESSION_MINUTES:
        return f"expected holding time {out.expected_hold_minutes} minutes is out of range"
    return None


def add_market_minutes(start: datetime, minutes: int, stop_before_close: int) -> datetime:
    """`minutes` of trading time after `start`, weekdays 09:30-close (holidays ignored)."""
    local = start.astimezone(NEW_YORK)
    last = (
        datetime.combine(local.date(), CLOSE, tzinfo=NEW_YORK)
        - timedelta(minutes=stop_before_close)
    ).time()
    remaining = minutes
    day, clock = local.date(), local.time()
    while True:
        if day.weekday() < 5:
            begin = max(clock, OPEN)
            available = (last.hour * 60 + last.minute) - (begin.hour * 60 + begin.minute)
            if available >= remaining:
                return datetime.combine(day, begin, tzinfo=NEW_YORK) + timedelta(minutes=remaining)
            remaining -= max(available, 0)
        day, clock = day + timedelta(days=1), OPEN


def held_text(minutes: int) -> str:
    if minutes < 90:
        return f"about {minutes} minutes"
    if minutes < SESSION_MINUTES:
        return f"about {minutes / 60:.0f} hours"
    return f"about {minutes / SESSION_MINUTES:.1f} trading days"


# --------------------------------------------------------------------------- the scanner


@dataclass
class ScanReport:
    candidates: int = 0
    screened: int = 0
    decided: int = 0
    planned: list[str] = field(default_factory=list)
    passed: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def details(self) -> dict[str, Any]:
        return {"candidates": self.candidates, "screened": self.screened, "decided": self.decided,
                "planned": self.planned, "passed": self.passed, "note": self.note}  # fmt: skip


class Scanner:
    def __init__(self, config: AppConfig, sessions: sessionmaker[Session], live: LiveData,
                 claude: Callable[[], ClaudeClient], *, equity: Callable[[], float | None] = lambda: None) -> None:  # fmt: skip
        self.config = config
        self.cfg = config.decision.intraday
        self.sessions = sessions
        self.live = live
        self.claude = claude
        self.equity = equity
        self._facts: tuple[date, dict[str, DayFacts]] | None = None
        self.looked_at: dict[str, datetime] = {}  # cooldown, per ticker

    # ------------------------------------------------------------------ budgets

    def usage_today(self, session: Session, now: datetime) -> tuple[int, int, float]:
        """(Claude calls today, decisions today, dollars this month) of the scanner."""
        local = now.astimezone(NEW_YORK)
        day_start = at_new_york(local.date(), time(0))
        month_start = at_new_york(local.date().replace(day=1), time(0))
        calls = session.scalar(select(func.count(m.ApiUsage.id)).where(
            m.ApiUsage.purpose.in_(PURPOSES), m.ApiUsage.ts >= day_start)) or 0  # fmt: skip
        decisions = session.scalar(select(func.count(m.ApiUsage.id)).where(
            m.ApiUsage.purpose == "intraday_decide", m.ApiUsage.ts >= day_start)) or 0  # fmt: skip
        spent = session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0)).where(
            m.ApiUsage.purpose.in_(PURPOSES), m.ApiUsage.ts >= month_start)) or 0.0  # fmt: skip
        return int(calls), int(decisions), float(spent)

    def plans_today(self, session: Session, day: date) -> list[IntradayPlan]:
        return list(session.scalars(select(IntradayPlan).where(IntradayPlan.day == day)))

    # ------------------------------------------------------------------ one scan

    def facts(self, day: date) -> dict[str, DayFacts]:
        if self._facts is None or self._facts[0] != day:
            with self.sessions() as session:
                self._facts = (day, day_facts(session, self.config, day))
        return self._facts[1]

    async def scan(self, now: datetime) -> ScanReport:
        cfg, report = self.cfg, ScanReport()
        local = now.astimezone(NEW_YORK).time()
        if not cfg.start <= local <= cfg.end:
            report.note = "outside the scanning window"
            return report
        day = new_york_date(now)
        with self.sessions() as session:
            calls, decisions, spent = self.usage_today(session, now)
            today = self.plans_today(session, day)
            held = set(
                session.scalars(select(m.Trade.ticker).where(m.Trade.status == m.TradeStatus.OPEN))
            )
        counted = [p for p in today if p.status not in ("skipped", "refused", "expired")]
        if len(counted) >= cfg.max_trades_per_day:
            report.note = f"today's maximum of {cfg.max_trades_per_day} trades is reached"
            return report
        if calls >= cfg.max_calls_per_day or decisions >= cfg.max_decisions_per_day:
            report.note = "today's Claude call limit is reached"
            return report
        if spent >= cfg.monthly_usd:
            report.note = (
                f"the scanner's monthly budget is used up (${spent:.2f} of ${cfg.monthly_usd:.2f})"
            )
            return report

        facts = self.facts(day)
        quotes = self.live.quotes([*facts, "SPY", *sorted(set(SECTOR_ETF.values()))])
        cooling = {
            t
            for t, at in self.looked_at.items()
            if now - at < timedelta(minutes=cfg.cooldown_minutes)
        }
        exclude = held | {p.ticker for p in today} | cooling
        candidates = find_candidates(quotes, facts, now, cfg, exclude)
        report.candidates = len(candidates)
        if not candidates:
            return report
        for c in candidates:
            self.looked_at[c.ticker] = now

        claude = self.claude()
        headlines = self.live.headlines(
            [c.ticker for c in candidates], now - timedelta(hours=24), now
        )
        market = self._market_line(quotes)
        lines = [f"AS OF: {now.astimezone(NEW_YORK):%Y-%m-%d %H:%M} New York time", market, ""]
        for c in candidates:
            lines.append(describe(c))
            lines += [f"    news: {h}" for h in headlines.get(c.ticker, [])[:3]] or [
                "    news: none in the last 24 hours"
            ]
        try:
            screened = await claude.structured(
                purpose="intraday_screen", model=cfg.screen_model, effort="low", max_tokens=4000,
                system=SCREEN, user="\n".join(lines), output=ScreenOutput,
            )  # fmt: skip
        except (LLMError, BudgetExceededError) as exc:
            report.note = f"the screen did not run ({exc})"
            return report
        report.screened = len(candidates)
        by_ticker = {c.ticker: c for c in candidates}
        picks = [
            p
            for p in screened.output.picks
            if p.look_closer and p.ticker.strip().upper() in by_ticker
        ]
        for pick in screened.output.picks:
            if not pick.look_closer and pick.ticker in by_ticker:
                report.passed[pick.ticker] = "screen: " + clip(pick.reason, 160)
        room = min(DECIDE_PER_SCAN, cfg.max_decisions_per_day - decisions, cfg.max_calls_per_day - calls - 1,
                   cfg.max_trades_per_day - len(counted))  # fmt: skip
        for pick in picks[: max(room, 0)]:
            candidate = by_ticker[pick.ticker.strip().upper()]
            await self._decide(candidate, quotes, facts[candidate.ticker], headlines.get(candidate.ticker, []),
                               market, claude, now, report)  # fmt: skip
        return report

    def _market_line(self, quotes: Mapping[str, Quote]) -> str:
        spy = quotes.get("SPY")
        if spy is None or spy.prev_close <= 0:
            return "Market: SPY not available"
        return f"Market: SPY {spy.price / spy.prev_close - 1:+.2%} today"

    async def _decide(self, c: Candidate, quotes: Mapping[str, Quote], f: DayFacts, headlines: Sequence[str],
                      market: str, claude: ClaudeClient, now: datetime, report: ScanReport) -> None:  # fmt: skip
        cfg = self.cfg
        q = quotes[c.ticker]
        fund = SECTOR_ETF.get(f.sector or "")
        sector = quotes.get(fund or "")
        open_at = at_new_york(new_york_date(now), OPEN)
        bars: list[Candle] = self.live.candles([c.ticker], "5Min", open_at, now).get(c.ticker, [])
        research = self._research(c.ticker, now)
        lines = [
            f"AS OF: {now.astimezone(NEW_YORK):%Y-%m-%d %H:%M} New York time",
            f"STOCK: {c.ticker} ({f.sector or 'sector unknown'}), price {q.price:.2f}",
            f"Why it was flagged: {RULE_WORDS[c.rule]}",
            f"Today: open {q.open:.2f}, high {q.high:.2f}, low {q.low:.2f}, {q.price / q.prev_close - 1:+.2%} vs "
            f"yesterday's close {q.prev_close:.2f}; volume pace {c.facts['volume_pace']:.1f}x yesterday's",
            f"Typical daily range (14 days): {f.atr_pct:.2%} = ${f.atr_pct * q.price:.2f}",
            f"20-day high {f.high20:.2f}, 50-day average {f.sma50:.2f}, 20-day return {f.ret20:+.1%}",
            market + (f"; sector fund {fund} {sector.price / sector.prev_close - 1:+.2%}" if sector and sector.prev_close else ""),
            "", "5-minute bars today (New York time, open, high, low, close, volume):",
        ]  # fmt: skip
        lines += [f"{b.at.astimezone(NEW_YORK):%H:%M} {b.open:.2f} {b.high:.2f} {b.low:.2f} {b.close:.2f} {b.volume:.0f}"
                  for b in bars[-48:]] or ["none"]  # fmt: skip
        lines += ["", "Headlines (last 24 hours):", *(headlines or ["none"])]
        lines += ["", "This morning's research on it:", *(research or ["none"])]
        lines += ["", f"If bought, the size is set so the stop loses at most {self.config.decision.risk.risk_per_trade_pct:.1%} "
                  f"of the account; the trade is closed when its time has clearly passed (at most {cfg.max_hold_days} trading days)."]  # fmt: skip
        try:
            answer = await claude.structured(
                purpose="intraday_decide", model=cfg.decide_model, effort=cfg.decide_effort, max_tokens=12000,
                system=DECIDE, user="\n".join(lines), output=DecideOutput,
            )  # fmt: skip
        except (LLMError, BudgetExceededError) as exc:
            report.passed[c.ticker] = f"decision did not run ({exc})"
            return
        report.decided += 1
        out = answer.output
        problem = check_plan(out, q.price, f, cfg)
        if problem is not None:
            report.passed[c.ticker] = clip(problem + (f": {out.why}" if not out.trade else ""), 300)
            return
        self._plan(c, q, f, out, now)
        report.planned.append(c.ticker)

    def _research(self, ticker: str, now: datetime) -> list[str]:
        day = new_york_date(now)
        with self.sessions() as session:
            rows = session.scalars(
                select(m.AgentReport).where(m.AgentReport.ticker == ticker,
                                            m.AgentReport.as_of >= at_new_york(day, time(0)),
                                            m.AgentReport.as_of <= now)
            ).all()  # fmt: skip
        return [
            f"[{r.agent_name}] {r.direction.value} {r.confidence:.2f}: {clip(r.thesis, 250)}"
            for r in rows[:6]
        ]

    def _plan(self, c: Candidate, q: Quote, f: DayFacts, out: DecideOutput, now: datetime) -> None:
        cfg = self.cfg
        low, high = sorted((round(out.entry_low, 2), round(out.entry_high, 2)))
        mid = (low + high) / 2
        size = min(self.config.decision.risk.risk_per_trade_pct * mid / (mid - out.stop_price),
                   self.config.risk.max_position_pct)  # fmt: skip
        hold = out.expected_hold_minutes
        stop_minutes = self.config.trading.time_stop_minutes_before_close
        time_stop = add_market_minutes(
            now, min(math.ceil(hold * 1.5), cfg.max_hold_days * SESSION_MINUTES), stop_minutes
        )
        place_after = now + timedelta(minutes=cfg.skip_window_minutes)
        live = cfg.mode == "live"
        plan = IntradayPlan(
            day=new_york_date(now), created_at=now, mode=cfg.mode, status="planned" if live else "shadow",
            ticker=c.ticker, rule=c.rule, facts=c.facts, price_at_decision=q.price, entry_low=low, entry_high=high,
            target_price=round(out.target_price, 2), stop_price=round(out.stop_price, 2), size_pct=round(size, 4),
            confidence=round(out.confidence, 3), expected_hold_minutes=hold, time_stop_at=time_stop,
            place_after=place_after, why=clip(out.why, 600), invalidation=clip(out.invalidation, 300),
        )  # fmt: skip
        from papertrader.notify.warnings import Gate

        gate = Gate(self.sessions, self.config.telegram.warnings, now=lambda: now)
        with_buttons = live and gate.enabled  # X6: a heads-up with Skip / Go now
        with self.sessions() as session:
            session.add(plan)
            session.flush()
            if not with_buttons:
                queue(
                    session,
                    "intraday",
                    announce(plan, self.equity()),
                    dedup_key=f"intraday-plan:{plan.id}",
                )
            session.commit()
        if with_buttons:
            gate.warn([intraday_pending(plan, self.equity())], due=place_after, early=True)


def intraday_pending(plan: IntradayPlan, equity: float | None) -> Any:
    """The heads-up for a live intraday plan (X6), with Skip and Go now."""
    from papertrader.notify.ahead import sentences
    from papertrader.notify.warnings import Pending

    shares = float(math.floor(equity * plan.size_pct / plan.entry_high)) if equity else None
    return Pending(ref=f"intraday:{plan.id}", side="BUY", ticker=plan.ticker, source="intraday", shares=shares or None,
                   price=f"limit {money(plan.entry_high)} (zone {money(plan.entry_low)}-{money(plan.entry_high)})",
                   target=plan.target_price, stop=plan.stop_price,
                   hold=f"{held_text(plan.expected_hold_minutes)}, sold by "
                        f"{plan.time_stop_at.astimezone(NEW_YORK):%a %H:%M} at the latest",
                   confidence=plan.confidence,
                   reasons=(*sentences(plan.why, 3), f"found by the {RULE_WORDS[plan.rule]} rule"),
                   against=f"wrong if {plan.invalidation}" if plan.invalidation else "",
                   details={"plan_id": plan.id})  # fmt: skip


def announce(plan: IntradayPlan, equity: float | None) -> str:
    risk = plan.entry_high - plan.stop_price
    shares = ""
    if equity:
        qty = math.floor(equity * plan.size_pct / plan.entry_high)
        shares = f"{qty} shares (about {money(qty * plan.entry_high)}, {plan.size_pct:.1%} of the account), "
    head = (
        "PLANNED BUY · " if plan.mode == "live" else "SHADOW (no order) · WOULD BUY "
    ) + f"{plan.ticker} (intraday scanner)"
    lines = [
        head,
        f"Buy {shares}limit {money(plan.entry_high)} (zone {money(plan.entry_low)}-{money(plan.entry_high)})",
        f"Target {money(plan.target_price)} ({signed_pct(plan.target_price / plan.entry_high - 1)}) · "
        f"stop {money(plan.stop_price)} ({signed_pct(plan.stop_price / plan.entry_high - 1)}) · "
        f"reward/risk {(plan.target_price - plan.entry_high) / risk:.1f}" if risk > 0 else "",
        f"Expected hold: {held_text(plan.expected_hold_minutes)} · time stop "
        f"{plan.time_stop_at.astimezone(NEW_YORK):%a %H:%M} New York",
        f"Confidence {plan.confidence:.0%}",
        f"Why: {plan.why}",
    ]  # fmt: skip
    if plan.invalidation:
        lines.append(f"Wrong if: {plan.invalidation}")
    if plan.mode == "live":
        lines.append(
            f"Placing at {plan.place_after.astimezone(NEW_YORK):%H:%M} New York unless you send /skip {plan.ticker}"
        )
    else:
        lines.append("Shadow mode: nothing is placed; this plan is scored after the close.")
    return "\n".join(line for line in lines if line)


# --------------------------------------------------------------------------- live trades


def _tell(session: Session, plan: IntradayPlan, text: str) -> None:
    queue(session, "intraday", text, dedup_key=f"intraday-{plan.status}:{plan.id}")


def skip(sessions: sessionmaker[Session], ticker: str, now: datetime) -> str:
    """The /skip command: cancel today's planned trade in `ticker` before it is placed."""
    ticker = ticker.strip().upper()
    with sessions() as session:
        plans = session.scalars(
            select(IntradayPlan).where(IntradayPlan.ticker == ticker, IntradayPlan.day == new_york_date(now))
            .order_by(IntradayPlan.id.desc())
        ).all()  # fmt: skip
        plan = next((p for p in plans if p.status == "planned"), plans[0] if plans else None)
        if plan is None:
            return f"There is no planned trade in {ticker} today."
        if plan.status == "shadow":
            return f"{ticker} is a shadow plan: nothing is placed anyway."
        if plan.status != "planned":
            return f"Too late for {ticker}: the plan is already {plan.status}."
        plan.status = "skipped"
        plan.note = f"skipped by you at {now.astimezone(NEW_YORK):%H:%M}"
        from papertrader.notify.warnings import GO, SKIPPED, WAITING, OrderWarning

        warning = session.scalar(
            select(OrderWarning).where(OrderWarning.ref == f"intraday:{plan.id}")
        )
        if warning is not None and warning.status in (WAITING, GO):  # its Telegram buttons go away
            warning.status, warning.decided_at, warning.decided_by = SKIPPED, now, "you"
        session.commit()
        return f"Skipped {ticker}: it will not be bought."


def place_due(
    sessions: sessionmaker[Session], config: AppConfig, engine: Any, now: datetime
) -> list[str]:
    """Place the planned trades whose /skip window is over (live mode), within every limit."""
    from papertrader.notify.warnings import SKIPPED
    from papertrader.trading.engine import TradingRefused

    cfg = config.decision.intraday
    broker = engine.broker
    gate = getattr(engine, "gate", None)
    done: list[str] = []
    go_now = (
        {int(w.ref.split(":")[1]) for w in gate.due("intraday:")} if gate is not None else set()
    )
    with sessions() as session:
        due = [p.id for p in session.scalars(select(IntradayPlan).where(IntradayPlan.status == "planned"))
               if p.place_after <= now or p.id in go_now]  # fmt: skip
    warnings = gate.rows(f"intraday:{i}" for i in due) if gate is not None else {}
    for plan_id in due:
        with sessions() as session:
            plan = session.get(IntradayPlan, plan_id)
            assert plan is not None
            warning = warnings.get(f"intraday:{plan_id}")
            if warning is not None and warning.status == SKIPPED:
                plan.status, plan.note = "skipped", "skipped by you on Telegram"
                session.commit()
                done.append(f"{plan.ticker}: skipped by you")
                continue
            problem: str | None = None
            placed_today = session.scalar(select(func.count(IntradayPlan.id)).where(
                IntradayPlan.day == plan.day, IntradayPlan.status.in_(("placed", "done")))) or 0  # fmt: skip
            if now - plan.place_after > LATE_AFTER:
                problem = "too late: the market moved on while waiting"
            elif placed_today >= cfg.max_trades_per_day:
                problem = f"today's maximum of {cfg.max_trades_per_day} intraday trades is reached"
            else:
                price = broker.latest_prices([plan.ticker]).get(plan.ticker)
                account = broker.account()
                if price is None or not plan.entry_low * 0.998 <= price <= plan.entry_high * 1.002:
                    problem = f"the price ({price}) has left the entry zone"
                elif (account.daytrade_count or 0) >= 3 and account.equity < 25_000:
                    problem = (
                        "US day-trading rule: 3 day trades in 5 days on an account under $25,000"
                    )
            if problem is not None:
                plan.status, plan.note = (
                    ("expired" if problem.startswith("too late") else "refused"),
                    problem,
                )
                _tell(session, plan, f"NOT PLACED · {plan.ticker} (intraday scanner): {problem}")
                session.commit()
                done.append(f"{plan.ticker}: {problem}")
                continue
            cycle = m.DecisionCycle(kind=m.CycleKind.DAILY, as_of=now, status=m.RunStatus.SUCCEEDED,
                                    notes={"stage": "done", "intraday_plan": plan.id})  # fmt: skip
            session.add(cycle)
            session.flush()
            decision = m.JudgeDecision(cycle_id=cycle.id, as_of=now, model=cfg.decide_model,
                                       market_summary=f"Intraday scanner: {plan.ticker} ({RULE_WORDS[plan.rule]})",
                                       payload={"intraday_plan": plan.id, "trades": []})  # fmt: skip
            idea = m.TradeIdea(
                rank=1, ticker=plan.ticker, action=m.TradeAction.BUY, judge_confidence=plan.confidence,
                model_probability=None, agent_agreement=None, final_confidence=plan.confidence,
                entry_low=plan.entry_low, entry_high=plan.entry_high, target_price=plan.target_price,
                stop_price=plan.stop_price, size_pct=plan.size_pct,
                horizon_days=max(1, math.ceil(plan.expected_hold_minutes * 1.5 / SESSION_MINUTES)),
                rationale=f"[intraday scanner] {plan.why}", status=m.IdeaStatus.PROPOSED,
            )  # fmt: skip
            decision.ideas.append(idea)
            session.add(decision)
            session.flush()
            plan.idea_id, decision_id = idea.id, decision.id
            session.commit()
        try:
            results = engine.place_orders(decision_id, gated=False)  # its own heads-up came first
            outcome = results[0] if results else None
        except TradingRefused as exc:
            outcome = None
            refused = str(exc)
        with sessions() as session:
            plan = session.get(IntradayPlan, plan_id)
            assert plan is not None
            if outcome is not None and outcome.status == "submitted":
                plan.status, plan.qty = "placed", outcome.qty
                done.append(f"{plan.ticker}: placed")
                if gate is not None:
                    gate.mark_sent([f"intraday:{plan_id}"])
            else:
                reason = outcome.detail if outcome is not None else refused
                plan.status, plan.note = "refused", reason
                idea_row = session.get(m.TradeIdea, plan.idea_id) if plan.idea_id else None
                if idea_row is not None and idea_row.status == m.IdeaStatus.PROPOSED:
                    idea_row.status, idea_row.status_reason = (
                        m.IdeaStatus.REJECTED_BY_RISK,
                        reason[:500],
                    )
                _tell(session, plan, f"NOT PLACED · {plan.ticker} (intraday scanner): {reason}")
                done.append(f"{plan.ticker}: {reason}")
            session.commit()
    return done


def manage_open(sessions: sessionmaker[Session], engine: Any, now: datetime) -> list[str]:
    """Follow placed trades: record fills and results, and apply the time stops."""
    actions: list[str] = []
    to_close: list[tuple[int, str]] = []
    with sessions() as session:
        for plan in session.scalars(select(IntradayPlan).where(IntradayPlan.status == "placed")):
            trade = (
                session.scalar(select(m.Trade).where(m.Trade.trade_idea_id == plan.idea_id))
                if plan.idea_id
                else None
            )
            if trade is None:
                idea = session.get(m.TradeIdea, plan.idea_id) if plan.idea_id else None
                if idea is not None and idea.status in (
                    m.IdeaStatus.EXPIRED,
                    m.IdeaStatus.REJECTED_BY_RISK,
                ):
                    plan.status, plan.exit_reason, plan.note = (
                        "done",
                        "no_fill",
                        "the limit order did not fill",
                    )
                continue
            plan.trade_id, plan.entry_price, plan.entered_at = (
                trade.id,
                trade.entry_price,
                trade.entered_at,
            )
            if trade.status == m.TradeStatus.CLOSED:
                plan.status, plan.exit_price, plan.exited_at = (
                    "done",
                    trade.exit_price,
                    trade.exited_at,
                )
                plan.exit_reason = trade.exit_trigger.value if trade.exit_trigger else None
                plan.net_return_pct = trade.pnl_pct
                actions.append(f"{plan.ticker}: closed ({plan.exit_reason})")
            elif now >= plan.time_stop_at:
                to_close.append((trade.id, plan.ticker))
        session.commit()
    for trade_id, ticker in to_close:
        result = engine.exit_trade(trade_id, m.ExitTrigger.TIME,
                                   "intraday time stop: the expected move did not come in time")  # fmt: skip
        actions.append(f"{ticker}: time stop ({result})")
    return actions


# --------------------------------------------------------------------------- shadow results


@dataclass(frozen=True)
class Result:
    entry_price: float | None
    entered_at: datetime | None
    exit_price: float | None
    exited_at: datetime | None
    reason: str  # target | stop | time | no_fill
    net: float | None


def simulate(plan: IntradayPlan, candles: Sequence[Candle], cost_bps: float) -> Result:
    """What the plan would have done, on the full market's one-minute bars.

    The order goes in at `place_after` only if the price is still in the entry zone;
    the limit (top of the zone) fills at the first bar that trades at or below it; then
    the stop wins any bar that also touched the target (the cautious assumption), and
    the time stop sells at the last bar's close.
    """
    start = plan.place_after.replace(second=0, microsecond=0)
    bars = [c for c in candles if c.at >= start]
    no_fill = Result(None, None, None, None, "no_fill", None)
    if not bars or not plan.entry_low * 0.998 <= bars[0].open <= plan.entry_high * 1.002:
        return no_fill
    day_end = at_new_york(plan.day, CLOSE)
    fill_index = next(
        (i for i, c in enumerate(bars) if c.at < day_end and c.low <= plan.entry_high), None
    )
    if fill_index is None:
        return no_fill
    entry_bar = bars[fill_index]
    entry = min(entry_bar.open, plan.entry_high)
    exit_price, exited_at, reason = None, None, "time"
    last = entry_bar
    for c in bars[fill_index + 1 :]:
        if c.at > plan.time_stop_at:
            break
        last = c
        if c.low <= plan.stop_price:
            exit_price, exited_at, reason = min(c.open, plan.stop_price), c.at, "stop"
            break
        if c.high >= plan.target_price:
            exit_price, exited_at, reason = max(c.open, plan.target_price), c.at, "target"
            break
    if exit_price is None:
        exit_price, exited_at = last.close, last.at
    net = exit_price / entry - 1 - cost_bps / 10_000
    return Result(entry, entry_bar.at, exit_price, exited_at, reason, net)


def score_plans(
    sessions: sessionmaker[Session], config: AppConfig, live: LiveData, now: datetime
) -> int:
    """After the close: score finished shadow plans, and measure the live feed's price gap."""
    cfg = config.decision.intraday
    ripe = now - timedelta(minutes=16)  # the full market's bars are free once 15 minutes old
    scored = 0
    with sessions() as session:
        plans = session.scalars(select(IntradayPlan).where(
            ((IntradayPlan.status == "shadow") & (IntradayPlan.time_stop_at <= ripe))
            | (IntradayPlan.feed_gap_bps.is_(None) & (IntradayPlan.created_at <= ripe)))).all()  # fmt: skip
        for plan in plans:
            end = (
                min(plan.time_stop_at, ripe)
                if plan.status == "shadow"
                else plan.created_at + timedelta(minutes=1)
            )
            start = plan.created_at.replace(second=0, microsecond=0) - timedelta(minutes=1)
            candles = live.candles([plan.ticker], "1Min", start, end + timedelta(minutes=1),
                                   full_market=True).get(plan.ticker, [])  # fmt: skip
            if plan.feed_gap_bps is None:
                minute = next(
                    (c for c in candles if c.at <= plan.created_at < c.at + timedelta(minutes=1)),
                    None,
                )
                if minute is not None and minute.close > 0:
                    plan.feed_gap_bps = round(
                        (plan.price_at_decision / minute.close - 1) * 10_000, 2
                    )
            if plan.status == "shadow" and plan.time_stop_at <= ripe:
                result = simulate(plan, candles, cfg.cost_bps)
                plan.entry_price, plan.entered_at = result.entry_price, result.entered_at
                plan.exit_price, plan.exited_at = result.exit_price, result.exited_at
                plan.exit_reason, plan.net_return_pct = result.reason, result.net
                plan.status = "done"
                scored += 1
        session.commit()
    return scored


def results(session: Session, since: date) -> dict[str, Any]:
    """The scanner's own track record (shadow and live), for the report and the decision
    whether to go live: judged on its own profit after costs."""
    plans = session.scalars(select(IntradayPlan).where(IntradayPlan.day >= since)).all()
    out: dict[str, Any] = {}
    for mode in ("shadow", "live"):
        mine = [p for p in plans if p.mode == mode]
        traded = [p for p in mine if p.net_return_pct is not None]
        gaps = sorted(abs(p.feed_gap_bps) for p in mine if p.feed_gap_bps is not None)
        out[mode] = {
            "plans": len(mine), "traded": len(traded),
            "no_fill": sum(1 for p in mine if p.exit_reason == "no_fill"),
            "skipped": sum(1 for p in mine if p.status == "skipped"),
            "wins": sum(1 for p in traded if (p.net_return_pct or 0) > 0),
            "average_net": round(sum(p.net_return_pct or 0 for p in traded) / len(traded), 5) if traded else None,
            "sum_net": round(sum(p.net_return_pct or 0 for p in traded), 5),
            "median_feed_gap_bps": gaps[len(gaps) // 2] if gaps else None,
        }  # fmt: skip
    return out
