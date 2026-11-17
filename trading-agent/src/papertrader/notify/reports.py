"""Longer messages built from the database: morning brief, weekly summary, and the
answers to the bot's /status, /pnl and /positions commands.

Each has a `format_...` function that only arranges text (used for test messages
too) and a builder that gathers the numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papertrader.agents.context import clip
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.state import HEARTBEAT_KEY, KILL_SWITCH_KEY, TRADING_PAUSED_KEY, get_state
from papertrader.ingest.timeutil import NEW_YORK, new_york_date
from papertrader.notify.messages import money, signed_money, signed_pct

ACTION_WORDS = {"buy": "BUY", "sell_short": "SHORT"}
NOT_TRADED = {"below_threshold": "below the bar", "rejected_by_risk": "refused by risk rules",
              "expired": "not filled"}  # fmt: skip


@dataclass(frozen=True)
class BriefTrade:
    rank: int
    action: str
    ticker: str
    confidence: float
    entry_low: float | None
    entry_high: float | None
    target: float
    stop: float
    reason: str
    status: str


def first_sentence(text: str, limit: int = 140) -> str:
    sentence = text.split(". ")[0].strip().rstrip(".")
    return clip(sentence, limit)


def format_brief(
    day: date, summary: str, trades: Sequence[BriefTrade], positions: Sequence[str]
) -> str:
    lines = [f"Morning brief · {day:%a %d %b}", f"Market: {clip(summary, 500)}", ""]
    if not trades:
        lines.append("No trades today: nothing passed the bar.")
    else:
        lines.append("Top ideas:")
        for t in trades:
            zone = (
                f"{t.entry_low:.2f}-{t.entry_high:.2f}"
                if t.entry_low and t.entry_high
                else "market"
            )
            lines.append(f"{t.rank}. {ACTION_WORDS.get(t.action, t.action.upper())} {t.ticker} · "
                         f"{t.confidence:.0%} · entry {zone} · target {t.target:.2f} · stop {t.stop:.2f}")  # fmt: skip
            note = f" ({NOT_TRADED[t.status]})" if t.status in NOT_TRADED else ""
            lines.append(f"   {first_sentence(t.reason)}{note}")
    lines += ["", "Open positions: " + (", ".join(positions) if positions else "none")]
    return "\n".join(lines)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=NEW_YORK)


def latest_daily_decision(session: Session, day: date) -> m.JudgeDecision | None:
    return session.scalars(
        select(m.JudgeDecision)
        .join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
        .where(m.DecisionCycle.kind == m.CycleKind.DAILY, m.JudgeDecision.as_of >= _day_start(day))
        .order_by(m.JudgeDecision.id.desc())
    ).first()


def open_trade_labels(session: Session, prices: dict[str, float] | None = None) -> list[str]:
    labels = []
    for trade in session.scalars(
        select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN).order_by(m.Trade.ticker)
    ):
        price = (prices or {}).get(trade.ticker)
        move = (
            f" {signed_pct(price / trade.entry_price - 1)}" if price and trade.entry_price else ""
        )
        labels.append(f"{trade.ticker}{move}{' [test]' if trade.is_test else ''}")
    return labels


def build_brief(
    session: Session, config: AppConfig, now: datetime, prices: dict[str, float] | None = None
) -> str | None:
    day = new_york_date(now)
    decision = latest_daily_decision(session, day)
    if decision is None:
        return None
    ideas = sorted(decision.ideas, key=lambda i: i.rank)[: config.telegram.brief_max_trades]
    trades = [
        BriefTrade(i.rank, i.action.value, i.ticker, i.final_confidence, i.entry_low, i.entry_high,
                   i.target_price, i.stop_price, i.rationale, i.status.value)
        for i in ideas
    ]  # fmt: skip
    return format_brief(day, decision.market_summary, trades, open_trade_labels(session, prices))


# --------------------------------------------------------------------------- weekly


def format_weekly(*, end: date, closed: int, wins: int, pnl: float, excess: float | None,
                  leaderboard: Sequence[tuple[str, int, float | None]], model_line: str,
                  spend_week: float, spend_parts: dict[str, float], spend_month: float, budget: float) -> str:  # fmt: skip
    lines = [f"Weekly summary · week to {end:%a %d %b}",
             f"Trades closed: {closed} · won {wins} · P&L {signed_money(pnl)} · avg vs SPY {signed_pct(excess)}", "",
             "Agent leaderboard (hit rate, resolved calls):"]  # fmt: skip
    if leaderboard:
        for rank, (agent, n, hit) in enumerate(leaderboard, start=1):
            lines.append(f"{rank}. {agent} · {'n/a' if hit is None else f'{hit:.0%}'} · {n} calls")
    else:
        lines.append("no resolved predictions yet")
    parts = ", ".join(f"{k} {money(v)}" for k, v in spend_parts.items() if v > 0) or "nothing"
    lines += ["", f"Model: {model_line}",
              f"API spend: this week {money(spend_week)} ({parts}); month {money(spend_month)} of {money(budget)}"]  # fmt: skip
    return "\n".join(lines)


def _spend_group(purpose: str) -> str:
    if purpose in ("judge", "thesis:judge", "lessons"):
        return "judge"
    if purpose == "text_features":
        return "text labels"
    return "agents"


def build_weekly(
    session: Session, config: AppConfig, now: datetime, *, extra: Sequence[str] = ()
) -> str:
    since = now - timedelta(days=7)
    closed = session.scalars(
        select(m.Trade).where(m.Trade.status == m.TradeStatus.CLOSED, m.Trade.exited_at >= since,
                              m.Trade.is_test.is_(False))
    ).all()  # fmt: skip
    excess = [t.excess_return_pct for t in closed if t.excess_return_pct is not None]

    latest = session.scalar(select(func.max(m.AgentStat.as_of_date)))
    leaderboard: list[tuple[str, int, float | None]] = []
    if latest is not None:
        stats = session.scalars(
            select(m.AgentStat).where(m.AgentStat.as_of_date == latest, m.AgentStat.sector == "",
                                      m.AgentStat.event_type == "")
        ).all()  # fmt: skip
        best: dict[str, m.AgentStat] = {}
        for stat in stats:  # the longest window per agent
            if stat.agent_name not in best or stat.window_days > best[stat.agent_name].window_days:
                best[stat.agent_name] = stat
        leaderboard = sorted(((s.agent_name, s.n, s.hit_rate) for s in best.values()),
                             key=lambda row: (row[2] is None, -(row[2] or 0)))  # fmt: skip

    model = session.scalars(select(m.ModelVersion).order_by(m.ModelVersion.id.desc())).first()
    if model is None:
        model_line = "not trained yet"
    else:
        ic = (model.metrics or {}).get("ic_daily")
        state = "ACTIVE" if model.is_active else "not promoted"
        model_line = f"{model.version} ({model.created_at:%d %b}) {state}, daily IC {'n/a' if ic is None else f'{ic:.3f}'}"

    parts: dict[str, float] = {"agents": 0.0, "judge": 0.0, "text labels": 0.0}
    for purpose, cost in session.execute(
        select(m.ApiUsage.purpose, func.sum(m.ApiUsage.cost_usd)).where(m.ApiUsage.ts >= since).group_by(m.ApiUsage.purpose)
    ):  # fmt: skip
        parts[_spend_group(purpose)] += float(cost or 0.0)
    month_start = datetime.combine(new_york_date(now).replace(day=1), time(0), tzinfo=NEW_YORK)
    month = float(session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0))
                                 .where(m.ApiUsage.ts >= month_start)) or 0.0)  # fmt: skip
    text = format_weekly(
        end=new_york_date(now), closed=len(closed), wins=sum(1 for t in closed if (t.pnl_usd or 0) > 0),
        pnl=sum(t.pnl_usd or 0.0 for t in closed), excess=sum(excess) / len(excess) if excess else None,
        leaderboard=leaderboard, model_line=model_line, spend_week=sum(parts.values()), spend_parts=parts,
        spend_month=month, budget=config.budget.monthly_usd,
    )  # fmt: skip
    return text + ("\n\n" + "\n".join(extra) if extra else "")


# --------------------------------------------------------------------------- bot answers


def build_status(
    session: Session, config: AppConfig, now: datetime, *, market_open: bool | None
) -> str:
    paused = get_state(session, TRADING_PAUSED_KEY) or {}
    kill = get_state(session, KILL_SWITCH_KEY) or {}
    beat = get_state(session, HEARTBEAT_KEY) or {}
    month_start = datetime.combine(new_york_date(now).replace(day=1), time(0), tzinfo=NEW_YORK)
    spent = float(session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0))
                                 .where(m.ApiUsage.ts >= month_start)) or 0.0)  # fmt: skip
    cycle = session.scalars(
        select(m.DecisionCycle)
        .where(m.DecisionCycle.kind == m.CycleKind.DAILY)
        .order_by(m.DecisionCycle.id.desc())
    ).first()
    model = session.scalars(select(m.ModelVersion).where(m.ModelVersion.is_active)).first()
    open_trades = session.scalar(
        select(func.count()).select_from(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN)
    )
    age = (
        (now - datetime.fromisoformat(beat["at"])).total_seconds() / 60 if beat.get("at") else None
    )
    lines = [
        "Status · PAPER trading",
        "Trading: " + (f"PAUSED ({paused.get('reason', '')})" if paused.get("paused") else "active"),
        "Kill switch: " + ("TRIGGERED" if kill.get("active") else "off"),
        "Market: " + ("open" if market_open else "closed" if market_open is not None else "unknown"),
        f"Open trades: {open_trades or 0}",
        f"API spend this month: {money(spent)} of {money(config.budget.monthly_usd)}",
        "Last research run: " + (f"{cycle.as_of.astimezone(NEW_YORK):%a %d %b %H:%M} ({cycle.status.value})" if cycle else "none"),
        "Pattern model: " + (model.version if model else "none active"),
        "Service heartbeat: " + (f"{age:.0f} min ago" if age is not None else "never"),
    ]  # fmt: skip
    return "\n".join(lines)


def build_pnl(
    session: Session, *, equity: float | None, day_change: float | None, spy_now: float | None
) -> str:
    first = session.scalars(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).first()
    closed = session.scalars(select(m.Trade).where(m.Trade.status == m.TradeStatus.CLOSED,
                                                   m.Trade.is_test.is_(False))).all()  # fmt: skip
    wins = sum(1 for t in closed if (t.pnl_usd or 0) > 0)
    realised = sum(t.pnl_usd or 0.0 for t in closed)
    lines = ["P&L"]
    if equity is not None:
        lines.append(f"Equity {money(equity)} · today {signed_pct(day_change, 2)}")
        if first and first.equity:
            spy = (
                (spy_now / first.benchmark_close - 1) if spy_now and first.benchmark_close else None
            )
            lines.append(f"Since {first.ts.astimezone(NEW_YORK):%d %b}: {signed_pct(equity / first.equity - 1)} "
                         f"vs SPY {signed_pct(spy)}")  # fmt: skip
    else:
        lines.append("Account figures unavailable (Alpaca not reachable or no keys)")
    lines.append(f"Closed trades: {len(closed)} · won {wins} · realised {signed_money(realised)}"
                 + (f" · win rate {wins / len(closed):.0%}" if closed else ""))  # fmt: skip
    return "\n".join(lines)


def build_positions(
    rows: Sequence[tuple[str, float, float, float]], fallback: Sequence[str]
) -> str:
    """rows = (ticker, qty, entry price, current price) from the broker."""
    if rows:
        lines = ["Positions"]
        for ticker, qty, entry, current in rows:
            lines.append(
                f"{ticker} · {qty:g} @ {entry:.2f} · now {current:.2f} ({signed_pct(current / entry - 1 if entry else None)})"
            )
        return "\n".join(lines)
    if fallback:
        return "Positions (from the database; Alpaca not reachable)\n" + "\n".join(fallback)
    return "Positions\nnone"
