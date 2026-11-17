"""Daily: what actually happened after each agent call whose horizon has ended.

A call is scored exactly like the pattern model's labels, so both learn from the same
yardstick:

    entry  = the open of the first trading day whose decision time (08:20 New York)
             is at or after the call
    exit   = the close `horizon_days` trading days later, counting the entry day
    excess = the stock's return minus SPY's over the same open-to-close span

A long call is right when the excess return is above zero, a short call when it is
below. Neutral calls are stored but have no right or wrong. The Brier score treats the
agent's confidence as its probability of being right: (confidence - 1 or 0) squared.

Only live calls count. A dry run "as of" a date long past is a replay: Claude may
already know what happened next from its training data, so it would flatter the agent.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import at_new_york, new_york_date

LIVE_KINDS = (m.CycleKind.DAILY, m.CycleKind.THESIS_BREAK, m.CycleKind.DRY_RUN)
REPLAY_LAG = timedelta(hours=36)  # a run started this long after its "as of" is a replay


@dataclass
class ResolveReport:
    resolved: int = 0
    waiting: int = 0  # horizon not over yet (or today's prices not loaded yet)
    no_prices: int = 0  # the stock has no price on the entry or exit day
    replays: int = 0  # dry runs of the past: never scored


@dataclass(frozen=True)
class _Bar:
    open: float
    close: float


def _daily_bars(
    session: Session, tickers: set[str], start: date, now: datetime
) -> dict[str, dict[date, _Bar]]:
    rows = session.execute(
        select(m.PriceBar.ticker, m.PriceBar.ts, m.PriceBar.open, m.PriceBar.close).where(
            m.PriceBar.timeframe == "1Day", m.PriceBar.ticker.in_(sorted(tickers)),
            m.PriceBar.ts >= at_new_york(start, time(0)), m.PriceBar.available_at <= now,
        )
    )  # fmt: skip
    out: dict[str, dict[date, _Bar]] = defaultdict(dict)
    for ticker, ts, open_, close in rows:
        out[ticker][new_york_date(ts)] = _Bar(float(open_), float(close))
    return out


def entry_index(days: list[date], as_of: datetime, decision_time: time) -> int:
    """Index of the first trading day whose decision time is at or after `as_of`."""
    day = new_york_date(as_of)
    index = bisect.bisect_left(days, day)
    if index < len(days) and days[index] == day and at_new_york(day, decision_time) < as_of:
        index += 1  # decided after that day's decision time: the next day's open
    return index


def resolve_outcomes(session: Session, config: AppConfig, now: datetime) -> ResolveReport:
    report = ResolveReport()
    benchmark = config.universe.benchmark
    since = now - timedelta(days=config.learning.resolve_max_age_days)
    pending = session.execute(
        select(m.AgentReport, m.DecisionCycle.started_at)
        .join(m.DecisionCycle, m.DecisionCycle.id == m.AgentReport.cycle_id)
        .outerjoin(m.PredictionOutcome, m.PredictionOutcome.agent_report_id == m.AgentReport.id)
        .where(m.PredictionOutcome.id.is_(None), m.AgentReport.as_of >= since,
               m.AgentReport.as_of <= now, m.DecisionCycle.kind.in_(LIVE_KINDS),
               m.AgentReport.ticker != benchmark)
        .order_by(m.AgentReport.id)
    ).all()  # fmt: skip
    live = []
    for row, started_at in pending:
        if started_at - row.as_of > REPLAY_LAG:
            report.replays += 1
        else:
            live.append(row)
    if not live:
        return report

    start = min(new_york_date(r.as_of) for r in live) - timedelta(days=5)
    bars = _daily_bars(session, {r.ticker for r in live} | {benchmark}, start, now)
    spy = bars.get(benchmark, {})
    days = sorted(spy)  # SPY trades on every trading day: it is the calendar
    decision_time = config.schedule.score_and_judge
    for call in live:
        first = entry_index(days, call.as_of, decision_time)
        last = first + call.horizon_days - 1
        if last >= len(days):
            report.waiting += 1
            continue
        entry_day, exit_day = days[first], days[last]
        stock = bars.get(call.ticker, {})
        if entry_day not in stock or exit_day not in stock or stock[entry_day].open <= 0:
            report.no_prices += 1
            continue
        ret = stock[exit_day].close / stock[entry_day].open - 1
        bench = spy[exit_day].close / spy[entry_day].open - 1
        excess = ret - bench
        hit: bool | None = None
        brier: float | None = None
        if call.direction != m.Direction.NEUTRAL:
            hit = excess > 0 if call.direction == m.Direction.LONG else excess < 0
            brier = (call.confidence - (1.0 if hit else 0.0)) ** 2
        session.add(m.PredictionOutcome(
            agent_report_id=call.id, resolved_at=now, horizon_days=call.horizon_days,
            start_price=stock[entry_day].open, end_price=stock[exit_day].close, return_pct=ret,
            benchmark_return_pct=bench, excess_return_pct=excess, hit=hit, brier=brier,
        ))  # fmt: skip
        report.resolved += 1
    session.flush()
    return report
