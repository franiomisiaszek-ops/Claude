"""X2c. "Why did it move?" post-mortems for system B, and careful lessons from them.

Every morning (07:00 New York) B looks back at the previous trading day and picks at
most `max_cases_per_day` cases, most important first:

    closed_trade  a trade that closed
    big_move      a held stock that moved more than `big_move_ranges` daily ranges
                  beyond its sector fund
    missed        an idea B passed on (or blocked) that then went the predicted way
    dodged        an idea B passed on (or blocked) that then went against it

For each, a cheap model (Haiku) writes the likely causes with their sources and
likelihoods (market, sector and noise always among them), why B acted as it did,
whether that was a good decision WITH WHAT WAS KNOWN THEN (sources are marked "known
then" or "after"), what would have changed it, and a conditional lesson. A Sonnet digest
"Why it moved" goes to Telegram; /why TICKER shows the latest case.

How strong a lesson is, is decided in code, never by the model: one case is an
"anecdote"; 3+ similar cases pointing the same way (60%+) are "emerging"; 8+ (70%+)
are "supported".

Lessons at the judge (when `apply_to_judge` is on): for each of today's trades, the
most similar past cases are shown to a reviewer together with their explicit
differences from today, and it decides whether each is really comparable. Comparable
cases may nudge the confidence by at most 0.03 (anecdote, emerging) or 0.07 (supported),
only in the direction of their lesson. A past case can lower a trade below the bar, but
never lift one above it, and never changes a size, a stop or any risk limit. Every use
is recorded, and once a month the same ideas are scored with and without the lessons.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.context import clip
from papertrader.agents.snapshot import SECTOR_ETF
from papertrader.b.models import Backtest, DecisionLog, LessonUse, PostMortem
from papertrader.b.outcomes import IdeaSample, resolve
from papertrader.b.prompts import (
    CASE_SHARED,
    CASE_SPECIFIC,
    COMPARE_SHARED,
    COMPARE_SPECIFIC,
    DIGEST_SHARED,
    DIGEST_SPECIFIC,
)
from papertrader.config import AppConfig, PostMortemSwitch
from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
from papertrader.learning.outcomes import _daily_bars
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError
from papertrader.notify.messages import signed_pct
from papertrader.notify.outbox import queue

log = logging.getLogger(__name__)

CASE = SystemPrompt(CASE_SHARED, CASE_SPECIFIC)
DIGEST = SystemPrompt(DIGEST_SHARED, DIGEST_SPECIFIC)
COMPARE = SystemPrompt(COMPARE_SHARED, COMPARE_SPECIFIC)
PURPOSES = ("postmortem_case", "postmortem_digest", "lesson_compare")
PRIORITY = {"closed_trade": 3.0, "big_move": 2.0, "missed": 1.0, "dodged": 1.0}
OUTCOME_GOOD = {"dodged": "good", "missed": "bad"}


# --------------------------------------------------------------------------- picking cases


@dataclass
class Seed:
    kind: str
    ticker: str
    sector: str | None
    decided_at: datetime | None
    start: date
    end: date
    what_b_did: str
    outcome: str
    record: list[str] = field(default_factory=list)
    conditions: dict[str, Any] = field(default_factory=dict)
    priority: float = 0.0


def vol_bucket(atr_pct: float | None) -> str:
    if atr_pct is None:
        return "unknown"
    return "low" if atr_pct < 0.015 else "medium" if atr_pct < 0.03 else "high"


def _sectors(session: Session, config: AppConfig) -> dict[str, str | None]:
    return dict(session.execute(
        select(m.UniverseMember.ticker, m.UniverseMember.gics_sector)
        .where(m.UniverseMember.index_name == config.universe.index)
    ).all())  # fmt: skip


def _move(
    bars: dict[str, Any], ticker: str, start: date, end: date, *, from_open: bool
) -> float | None:
    series = bars.get(ticker, {})
    days = sorted(d for d in series if start <= d <= end)
    if not days:
        return None
    first = series[days[0]].open if from_open else series[days[0]].close
    if not first:
        return None
    return float(series[days[-1]].close / first - 1)


def _atr(bars: dict[str, Any], ticker: str, before: date) -> float | None:
    series = bars.get(ticker, {})
    days = sorted(d for d in series if d < before)[-15:]
    if len(days) < 6:
        return None
    moves = [abs(series[b].close / series[a].close - 1) for a, b in pairwise(days)]
    return float(
        sum(moves) / len(moves) * 1.4
    )  # close-to-close moves understate the full daily range


def _event_types(session: Session, idea_id: int | None) -> list[str]:
    if idea_id is None:
        return []
    rows = session.execute(
        select(m.AgentReport.event_type).join(m.TradeIdeaReport, m.TradeIdeaReport.agent_report_id == m.AgentReport.id)
        .where(m.TradeIdeaReport.trade_idea_id == idea_id, m.TradeIdeaReport.stance == m.Stance.SUPPORT)
    ).scalars().all()  # fmt: skip
    return sorted({e for e in rows if e})


def _idea_lines(session: Session, idea: m.TradeIdea | None) -> list[str]:
    if idea is None:
        return []
    lines = [f"the judge's case: {clip(idea.rationale, 500)}"]
    if idea.dissent:
        lines.append(f"the judge's doubts: {clip(idea.dissent, 300)}")
    for link in idea.report_links:
        report = session.get(m.AgentReport, link.agent_report_id)
        if report is not None:
            lines.append(f"{link.stance.value}: [{report.agent_name}, {report.direction.value} {report.confidence:.2f}] "
                         f"{clip(report.thesis, 250)}")  # fmt: skip
    entry = session.scalar(select(DecisionLog).where(DecisionLog.idea_id == idea.id))
    if entry is not None:
        lines.append(
            f"system A's rules: {entry.a_status}; B's switches: "
            + ("; ".join(entry.steps) or "no change")
        )
    return lines


def select_cases(session: Session, config: AppConfig, day: date) -> list[Seed]:
    pm = config.decision.postmortems
    sectors = _sectors(session, config)
    start = day - timedelta(days=pm.lookback_days * 2 + 40)
    now = at_new_york(day + timedelta(days=1), time(0))
    tickers: set[str] = set()
    seeds: list[Seed] = []
    day_start, day_end = at_new_york(day, time(0)), at_new_york(day + timedelta(days=1), time(0))

    closed = session.scalars(select(m.Trade).where(
        m.Trade.status == m.TradeStatus.CLOSED, m.Trade.is_test.is_(False),
        m.Trade.exited_at >= day_start, m.Trade.exited_at < day_end)).all()  # fmt: skip
    held = session.scalars(select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN,
                                                 m.Trade.is_test.is_(False))).all()  # fmt: skip
    recent = session.scalars(select(DecisionLog).where(
        DecisionLog.as_of >= at_new_york(day - timedelta(days=pm.lookback_days * 7 // 5 + 1), time(0)),
        DecisionLog.as_of < day_end, DecisionLog.b_status != "proposed",
        DecisionLog.a_status != "rejected_by_risk")).all()  # fmt: skip
    tickers |= {t.ticker for t in [*closed, *held]} | {r.ticker for r in recent}
    funds = {SECTOR_ETF[sec] for t in tickers if (sec := sectors.get(t) or "") in SECTOR_ETF}
    bars = _daily_bars(session, tickers | funds | {config.universe.benchmark}, start, now)

    def versus(
        ticker: str, a: date, b: date, *, from_open: bool
    ) -> tuple[float | None, float | None, float | None]:
        stock = _move(bars, ticker, a, b, from_open=from_open)
        spy = _move(bars, config.universe.benchmark, a, b, from_open=from_open)
        fund = SECTOR_ETF.get(sectors.get(ticker) or "")
        sect = _move(bars, fund, a, b, from_open=from_open) if fund else None
        return stock, (stock - spy if stock is not None and spy is not None else None), (
            stock - sect if stock is not None and sect is not None else None)  # fmt: skip

    for trade in closed:
        idea = session.get(m.TradeIdea, trade.trade_idea_id) if trade.trade_idea_id else None
        a, b = new_york_date(trade.entered_at), day
        pnl = trade.pnl_pct or 0.0
        seeds.append(Seed(
            "closed_trade", trade.ticker, sectors.get(trade.ticker), idea.decision.as_of if idea else trade.entered_at,
            a, b, f"bought {trade.qty:g} at {trade.entry_price:.2f} on {a:%d %b}, sold at {(trade.exit_price or 0):.2f} "
                  f"({trade.exit_trigger.value if trade.exit_trigger else 'closed'}) on {b:%d %b}: {signed_pct(pnl)}",
            "good" if pnl > 0 else "bad", _idea_lines(session, idea),
            {"idea_id": trade.trade_idea_id, "event_types": _event_types(session, trade.trade_idea_id)},
            PRIORITY["closed_trade"] + abs(pnl) * 10,
        ))  # fmt: skip
    for trade in held:
        stock, _, sect = versus(trade.ticker, day, day, from_open=False)
        prev = sorted(d for d in bars.get(trade.ticker, {}) if d < day)
        if not prev or stock is None:
            continue
        stock, _, sect = versus(trade.ticker, prev[-1], day, from_open=False)
        atr = _atr(bars, trade.ticker, day)
        if sect is None or atr is None or abs(sect) < pm.big_move_ranges * atr:
            continue
        idea = session.get(m.TradeIdea, trade.trade_idea_id) if trade.trade_idea_id else None
        seeds.append(Seed(
            "big_move", trade.ticker, sectors.get(trade.ticker), idea.decision.as_of if idea else trade.entered_at,
            prev[-1], day, f"holds {trade.qty:g} shares bought at {trade.entry_price:.2f} on "
                           f"{trade.entered_at.astimezone(NEW_YORK):%d %b}",
            "good" if (stock or 0) > 0 else "bad", _idea_lines(session, idea),
            {"idea_id": trade.trade_idea_id, "event_types": _event_types(session, trade.trade_idea_id)},
            PRIORITY["big_move"] + abs(sect) * 10,
        ))  # fmt: skip
    seen: set[tuple[str, int]] = set()
    for entry in recent:
        a = new_york_date(entry.as_of)
        stock, _, sect = versus(entry.ticker, a, day, from_open=True)
        atr = _atr(bars, entry.ticker, a)
        if sect is None or atr is None:
            continue
        days = max(1, len([d for d in bars.get(entry.ticker, {}) if a <= d <= day]))
        if abs(sect) < pm.big_move_ranges * atr * math.sqrt(days):
            continue  # an ordinary move for that many days
        favourable = sect > 0 if entry.action == "buy" else sect < 0
        kind = "missed" if favourable else "dodged"
        if (entry.ticker, entry.idea_id) in seen:
            continue
        seen.add((entry.ticker, entry.idea_id))
        idea = session.get(m.TradeIdea, entry.idea_id)
        seeds.append(Seed(
            kind, entry.ticker, sectors.get(entry.ticker), entry.as_of, a, day,
            f"did not trade the judge's {entry.action} idea of {a:%d %b} ({entry.b_status}: {entry.b_reason or 'no reason'})",
            OUTCOME_GOOD[kind], _idea_lines(session, idea),
            {"idea_id": entry.idea_id, "event_types": _event_types(session, entry.idea_id),
             "market_trend": entry.market_trend, "vol": vol_bucket(entry.atr_pct)},
            PRIORITY[kind] + abs(sect) * 10,
        ))  # fmt: skip

    done = {(p.kind, (p.conditions or {}).get("idea_id"), p.ticker) for p in session.scalars(
        select(PostMortem).where(PostMortem.day >= day - timedelta(days=pm.lookback_days * 2)))}  # fmt: skip
    fresh = [
        s
        for s in seeds
        if (s.kind, s.conditions.get("idea_id"), s.ticker) not in done or s.kind == "big_move"
    ]
    for s in fresh:
        stock, spy, sect = versus(
            s.ticker, s.start, s.end, from_open=s.kind in ("closed_trade", "missed", "dodged")
        )
        s.conditions |= {"kind": s.kind, "sector": s.sector, "direction": "long",
                         "move": stock, "vs_spy": spy, "vs_sector": sect}  # fmt: skip
        s.conditions.setdefault("vol", vol_bucket(_atr(bars, s.ticker, s.start)))
    return sorted(fresh, key=lambda s: -s.priority)[: pm.max_cases_per_day]


# --------------------------------------------------------------------------- writing a case


class Cause(BaseModel):
    cause: str
    likelihood: float
    sources: list[str]


class CaseOutput(BaseModel):
    causes: list[Cause]
    why_b_acted: str
    decision_quality: Literal["good", "poor", "unclear"]
    what_would_change: str
    lesson: str
    adjust: Literal["more_cautious", "more_confident", "no_change"]


def sources(session: Session, seed: Seed) -> list[tuple[str, bool, str]]:
    """(id, known at the decision time, text) for the news, filings and earnings around it."""
    decided = seed.decided_at or at_new_york(seed.start, time(9, 30))
    since = decided - timedelta(days=2)
    until = at_new_york(seed.end + timedelta(days=1), time(0))
    out: list[tuple[str, bool, str]] = []
    news = session.scalars(
        select(m.NewsItem).join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
        .where(m.NewsTicker.ticker == seed.ticker, m.NewsItem.duplicate_of_id.is_(None),
               m.NewsItem.available_at >= since, m.NewsItem.available_at < until)
        .order_by(m.NewsItem.available_at).limit(15)
    ).all()  # fmt: skip
    for n in news:
        out.append((f"news:{n.id}", n.available_at <= decided,
                    f"{n.published_at.astimezone(NEW_YORK):%d %b %H:%M} {clip(n.headline, 200)}"))  # fmt: skip
    for f in session.scalars(select(m.Filing).where(m.Filing.ticker == seed.ticker, m.Filing.available_at >= since,
                                                    m.Filing.available_at < until).limit(5)):  # fmt: skip
        what = (
            f"{f.form_type}"
            + (f" items {f.items}" if f.items else "")
            + (f": {clip(f.text_excerpt, 160)}" if f.text_excerpt else "")
        )
        out.append(
            (
                f"filing:{f.id}",
                f.available_at <= decided,
                f"{f.published_at.astimezone(NEW_YORK):%d %b} {what}",
            )
        )
    for e in session.scalars(select(m.EarningsEvent).where(
            m.EarningsEvent.ticker == seed.ticker, m.EarningsEvent.report_date >= seed.start - timedelta(days=3),
            m.EarningsEvent.report_date <= seed.end)):  # fmt: skip
        actual = (
            f", EPS {e.eps_actual} vs {e.eps_estimate} expected"
            if e.eps_actual is not None
            else ", not reported then"
        )
        out.append(
            (
                f"earnings:{e.id}",
                e.available_at <= decided,
                f"earnings on {e.report_date:%d %b}{actual}",
            )
        )
    return out


def render_case(seed: Seed, found: Sequence[tuple[str, bool, str]]) -> str:
    c = seed.conditions
    lines = [
        f"STOCK: {seed.ticker} ({seed.sector or 'sector unknown'}); case: {seed.kind.replace('_', ' ')}",
        f"Period: {seed.start:%a %d %b} to {seed.end:%a %d %b}; move {signed_pct(c.get('move'))}, "
        f"vs SPY {signed_pct(c.get('vs_spy'))}, vs its sector fund {signed_pct(c.get('vs_sector'))}",
        f"What system B did: {seed.what_b_did}",
        f"B decided at: {seed.decided_at.astimezone(NEW_YORK):%a %d %b %H:%M} New York" if seed.decided_at else "",
        "", "B's decision record:", *(seed.record or ["(none kept)"]),
        "", "Sources (known then = public before B decided):",
    ]  # fmt: skip
    lines += [
        f"[{sid}] ({'known then' if known else 'after the decision'}) {text}"
        for sid, known, text in found
    ]
    if not found:
        lines.append("none found: no news, filing or earnings around it")
    return "\n".join(line for line in lines if line is not None)


async def write_case(
    session: Session, seed: Seed, claude: ClaudeClient, cfg: PostMortemSwitch, now: datetime
) -> PostMortem | None:
    found = sources(session, seed)
    try:
        answer = await claude.structured(
            purpose="postmortem_case", model=cfg.case_model, effort="low", max_tokens=6000,
            system=CASE, user=render_case(seed, found), output=CaseOutput,
        )  # fmt: skip
    except (LLMError, BudgetExceededError) as exc:
        log.warning("post-mortem not written", extra={"ticker": seed.ticker, "error": str(exc)})
        return None
    out = answer.output
    valid = {sid for sid, _, _ in found}
    causes: list[dict[str, Any]] = [{"cause": clip(c.cause, 240), "likelihood": round(min(max(c.likelihood, 0.0), 1.0), 2),
               "sources": [s for s in c.sources if s in valid]} for c in out.causes[:5]]  # fmt: skip
    if not any(
        w in c["cause"].lower() for c in causes for w in ("market", "sector", "noise", "random")
    ):
        causes.append({"cause": "the market, the sector or noise (not assessed in the write-up)", "likelihood": None,
                       "sources": []})  # fmt: skip
    case = PostMortem(
        day=seed.end, created_at=now, kind=seed.kind, ticker=seed.ticker, sector=seed.sector, decided_at=seed.decided_at,
        period_start=seed.start, period_end=seed.end, move_pct=seed.conditions.get("move") or 0.0,
        move_vs_spy=seed.conditions.get("vs_spy"), move_vs_sector=seed.conditions.get("vs_sector"),
        what_b_did=seed.what_b_did, causes=causes, why_b_acted=clip(out.why_b_acted, 400),
        decision_quality=out.decision_quality, outcome=seed.outcome, what_would_change=clip(out.what_would_change, 400),
        lesson=clip(out.lesson, 400), adjust=out.adjust, conditions=seed.conditions, model=answer.model,
    )  # fmt: skip
    session.add(case)
    session.flush()
    case.evidence = evidence(session, case)
    return case


# --------------------------------------------------------------------------- similarity and evidence


def similarity(
    a: dict[str, Any], b: dict[str, Any], ticker_a: str, ticker_b: str
) -> tuple[float, list[str]]:
    """How alike two situations are, and the differences worth saying out loud."""
    score, differences = 0.0, []
    if ticker_a == ticker_b:
        score += 3
    if set(a.get("event_types") or []) & set(b.get("event_types") or []):
        score += 2
    elif a.get("event_types") or b.get("event_types"):
        differences.append(f"kind of news: {', '.join(a.get('event_types') or ['none'])} then, "
                           f"{', '.join(b.get('event_types') or ['none'])} now")  # fmt: skip
    if a.get("sector") and a.get("sector") == b.get("sector"):
        score += 1
    for key, label in (
        ("market_trend", "SPY vs its 200-day average"),
        ("vol", "the stock's volatility"),
    ):
        if a.get(key) and b.get(key):
            if a[key] == b[key]:
                score += 1 if key == "market_trend" else 0.5
            else:
                differences.append(f"{label}: {a[key]} then, {b[key]} now")
    return score, differences


def similar(session: Session, cfg: PostMortemSwitch, ticker: str, conditions: dict[str, Any], before: datetime,
            *, limit: int | None = None) -> list[tuple[PostMortem, float, list[str]]]:  # fmt: skip
    cases = session.scalars(select(PostMortem).where(
        PostMortem.retired.is_(False), PostMortem.created_at < before,
        PostMortem.day >= new_york_date(before) - timedelta(days=cfg.case_max_age_days))).all()  # fmt: skip
    scored = []
    for case in cases:
        score, differences = similarity(case.conditions or {}, conditions, case.ticker, ticker)
        if score >= 3 and (case.ticker == ticker or set(case.conditions.get("event_types") or [])
                           & set(conditions.get("event_types") or [])):  # fmt: skip
            scored.append((case, score, differences))
    scored.sort(key=lambda x: (-x[1], -x[0].id))
    return scored[: limit or cfg.max_similar]


def evidence(session: Session, case: PostMortem) -> str:
    if case.adjust == "no_change":
        return "anecdote"
    others = session.scalars(
        select(PostMortem).where(PostMortem.id != case.id, PostMortem.retired.is_(False))
    ).all()
    alike = [
        o
        for o in others
        if similarity(o.conditions or {}, case.conditions or {}, o.ticker, case.ticker)[0] >= 3
    ]
    agree = sum(1 for o in alike if o.adjust == case.adjust) + 1
    share = agree / (len(alike) + 1)
    if agree >= 8 and share >= 0.7:
        return "supported"
    if agree >= 3 and share >= 0.6:
        return "emerging"
    return "anecdote"


# --------------------------------------------------------------------------- the daily job


def spent_this_month(session: Session, now: datetime) -> float:
    start = at_new_york(new_york_date(now).replace(day=1), time(0))
    return float(session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0)).where(
        m.ApiUsage.purpose.in_(PURPOSES), m.ApiUsage.ts >= start)) or 0.0)  # fmt: skip


def why_text(case: PostMortem) -> str:
    causes = "\n".join(
        f"  {i}. {c['cause']}" + (f" ({c['likelihood']:.0%})" if c.get("likelihood") is not None else "")
        + (f" [{', '.join(c['sources'])}]" if c.get("sources") else "")
        for i, c in enumerate(case.causes, start=1)
    )  # fmt: skip
    return "\n".join([
        f"WHY IT MOVED · {case.ticker} ({case.day:%a %d %b}, {case.kind.replace('_', ' ')})",
        f"Move {signed_pct(case.move_pct)} · vs SPY {signed_pct(case.move_vs_spy)} · vs sector {signed_pct(case.move_vs_sector)}",
        "Likely causes:", causes,
        f"What B did: {case.what_b_did}",
        f"With what was known then, the decision was {case.decision_quality}: {case.why_b_acted}",
        f"Would have changed it: {case.what_would_change}",
        f"Lesson ({case.evidence}): {case.lesson}",
    ])  # fmt: skip


async def run_day(sessions: sessionmaker[Session], config: AppConfig, claude: ClaudeClient, day: date,
                  now: datetime) -> dict[str, Any]:  # fmt: skip
    cfg = config.decision.postmortems
    with sessions() as session:
        retired = prune(session, cfg, now)
        if spent_this_month(session, now) >= cfg.monthly_usd:
            session.commit()
            return {
                "cases": 0,
                "note": f"the post-mortems' monthly budget (${cfg.monthly_usd:.2f}) is used up",
            }
        seeds = select_cases(session, config, day)
        written = []
        for seed in seeds:
            case = await write_case(session, seed, claude, cfg, now)
            if case is not None:
                written.append(case)
        session.commit()
        if written:
            digest_lines = [why_text(c) for c in written]
            try:
                answer = await claude.structured(
                    purpose="postmortem_digest", model=cfg.summary_model, effort="low", max_tokens=6000,
                    system=DIGEST, user="\n\n".join(digest_lines), output=DigestOutput,
                )  # fmt: skip
                text = answer.output.text
            except (LLMError, BudgetExceededError):
                text = "\n".join(f"- {c.ticker}: {signed_pct(c.move_pct)}; {c.causes[0]['cause'] if c.causes else 'unclear'}"
                                 for c in written)  # fmt: skip
            queue(session, "digest", f"WHY IT MOVED · {day:%a %d %b}\n{text}\nDetails: /why TICKER",
                  dedup_key=f"why-it-moved:{day}")  # fmt: skip
            session.commit()
        return {
            "cases": len(written),
            "picked": [f"{s.ticker} ({s.kind})" for s in seeds],
            "retired": retired,
        }


class DigestOutput(BaseModel):
    text: str


def prune(session: Session, cfg: PostMortemSwitch, now: datetime) -> int:
    """Retire cases that are too old, or keep being judged not comparable."""
    count = 0
    oldest = new_york_date(now) - timedelta(days=cfg.case_max_age_days)
    for case in session.scalars(select(PostMortem).where(PostMortem.retired.is_(False))):
        if case.day < oldest or (
            case.times_shown >= 10 and case.times_comparable / case.times_shown < 0.1
        ):
            case.retired = True
            count += 1
    return count


def latest_why(sessions: sessionmaker[Session], ticker: str) -> str:
    ticker = ticker.strip().upper()
    with sessions() as session:
        case = session.scalars(select(PostMortem).where(PostMortem.ticker == ticker)
                               .order_by(PostMortem.day.desc(), PostMortem.id.desc()).limit(1)).first()  # fmt: skip
        return why_text(case) if case else f"No post-mortem for {ticker} yet."


# --------------------------------------------------------------------------- lessons at the judge


class CompareVerdict(BaseModel):
    ticker: str
    case_id: int
    comparable: bool
    change: float
    why: str


class CompareOutput(BaseModel):
    verdicts: list[CompareVerdict]


@dataclass
class Shown:
    ticker: str
    conditions: dict[str, Any]
    cases: list[tuple[PostMortem, float, list[str]]]


def render_compare(items: Sequence[tuple[Any, Shown]]) -> str:
    lines = []
    for trade, shown in items:
        c = shown.conditions
        lines.append(f"## Today: {trade.ticker} {trade.action}, confidence {trade.final_confidence:.2f}, "
                     f"news kind {', '.join(c.get('event_types') or ['none'])}, SPY {c.get('market_trend') or 'unknown'} "
                     f"its 200-day average, volatility {c.get('vol')}")  # fmt: skip
        lines.append(f"   the judge's case: {clip(trade.rationale, 300)}")
        for case, _, differences in shown.cases:
            lines.append(f"- case {case.id} ({case.day:%d %b}, {case.ticker}, {case.kind.replace('_', ' ')}, evidence "
                         f"{case.evidence}): {case.what_b_did}; move vs sector {signed_pct(case.move_vs_sector)}; "
                         f"lesson ({case.adjust.replace('_', ' ')}): {case.lesson}")  # fmt: skip
            lines.append(
                "  differences from today: "
                + ("; ".join(differences) or "none found by the program")
            )
    return "\n".join(lines)


def adjustment(cfg: PostMortemSwitch, case: PostMortem, change: float) -> float:
    cap = cfg.cap_supported if case.evidence == "supported" else cfg.cap_anecdote
    delta = max(-1.0, min(1.0, change)) * cap
    if case.adjust == "more_cautious":
        return min(delta, 0.0)
    if case.adjust == "more_confident":
        return max(delta, 0.0)
    return 0.0


# --------------------------------------------------------------------------- does it help?


def evaluate(
    session: Session, config: AppConfig, now: datetime, *, force: bool = False
) -> Backtest | None:
    """Once a month: score the same ideas with and without the lessons.

    Better means a lower Brier score (more honest confidence) and, among the trades the
    lessons stopped, more losers than winners. After three months without help the
    recommendation is to switch `apply_to_judge` off.
    """
    last = session.scalars(select(Backtest).where(Backtest.kind == "lessons_eval")
                           .order_by(Backtest.ran_at.desc()).limit(1)).first()  # fmt: skip
    if last is not None and not force and now - last.ran_at < timedelta(days=28):
        return None
    uses = session.scalars(select(LessonUse).order_by(LessonUse.as_of)).all()
    if not uses:
        return None
    pairs = [
        (u, IdeaSample(u.as_of, u.ticker, u.action, u.horizon_days, u.confidence_without, "B"))
        for u in uses
    ]
    by_sample = {id(sample): use for use, sample in pairs}
    done = resolve(session, config, [sample for _, sample in pairs], now,
                   sector_relative=config.decision.sector_relative.enabled)  # fmt: skip
    rows = [(by_sample[id(r.sample)], r.worked) for r in done]
    result: dict[str, Any] = {"uses": len(uses), "scored": len(rows),
                              "since": new_york_date(uses[0].as_of).isoformat()}  # fmt: skip
    if rows:

        def brier(values: list[tuple[float, bool]]) -> float:
            return round(sum((p - (1.0 if w else 0.0)) ** 2 for p, w in values) / len(values), 5)

        result["brier_without"] = brier([(u.confidence_without, w) for u, w in rows])
        result["brier_with"] = brier([(u.confidence_with, w) for u, w in rows])
        stopped = [
            w for u, w in rows if u.status_without == "proposed" and u.status_with != "proposed"
        ]
        result["stopped_losers"] = sum(1 for w in stopped if not w)
        result["stopped_winners"] = sum(1 for w in stopped if w)
    months = (now - uses[0].as_of).days / 30
    helped = bool(rows) and (result["brier_with"] < result["brier_without"]
                             or result["stopped_losers"] > result["stopped_winners"])  # fmt: skip
    if not rows:
        verdict = "no lesson-touched idea has a known result yet"
    elif helped:
        verdict = (f"the lessons helped so far (Brier {result['brier_with']:.3f} with vs {result['brier_without']:.3f} "
                   f"without; stopped {result['stopped_losers']} losers, {result['stopped_winners']} winners)")  # fmt: skip
    elif months >= 3:
        verdict = ("after three months the lessons have not helped: recommend switching "
                   "decision: postmortems: apply_to_judge to false (the post-mortems themselves can stay)")  # fmt: skip
    else:
        verdict = (f"no help yet (Brier {result['brier_with']:.3f} with vs {result['brier_without']:.3f} without); "
                   "too early to judge")  # fmt: skip
    row = Backtest(
        kind="lessons_eval",
        ran_at=now,
        params={"months": round(months, 1)},
        result=result,
        verdict=verdict,
    )
    session.add(row)
    queue(
        session,
        "weekly",
        f"LESSONS CHECK (system B, monthly): {verdict}.",
        dedup_key=f"lessons-eval:{new_york_date(now)}",
    )
    session.flush()
    return row
