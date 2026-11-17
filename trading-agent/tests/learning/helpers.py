"""A few days of prices and some agent calls to score."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.timeutil import at_new_york, daily_bar_available_at

MON, TUE, WED, THU, FRI = (date(2026, 11, 16) + timedelta(days=i) for i in range(5))
DAYS = [MON, TUE, WED, THU, FRI]
MORNING = at_new_york(MON, time(7, 30))  # the agents' time on Monday
THURSDAY_EVENING = at_new_york(THU, time(18, 0))


def add_bars(engine: Engine, prices: Mapping[str, Sequence[tuple[float, float]]]) -> None:
    """prices[ticker] = [(open, close) for MON..FRI]."""
    with make_session_factory(engine)() as session:
        for ticker, rows in prices.items():
            for day, (open_, close) in zip(DAYS, rows, strict=False):
                session.add(m.PriceBar(ticker=ticker, timeframe="1Day", ts=at_new_york(day, time(0)), open=open_,
                                       high=max(open_, close), low=min(open_, close), close=close, volume=1e6,
                                       available_at=daily_bar_available_at(day)))  # fmt: skip
        session.commit()


def add_members(engine: Engine, members: Mapping[str, str]) -> None:
    with make_session_factory(engine)() as session:
        for ticker, sector in members.items():
            session.add(m.UniverseMember(index_name="sp500", ticker=ticker, name=ticker, gics_sector=sector,
                                         is_active=True))  # fmt: skip
        session.commit()


def add_calls(
    engine: Engine,
    calls: Sequence[tuple[str, str, m.Direction, float, int]],
    *,
    as_of: datetime = MORNING,
    kind: m.CycleKind = m.CycleKind.DAILY,
    started_at: datetime | None = None,
    event_type: str | None = None,
) -> list[int]:
    """calls = (agent, ticker, direction, confidence, horizon)."""
    with make_session_factory(engine)() as session:
        cycle = m.DecisionCycle(kind=kind, as_of=as_of, status=m.RunStatus.SUCCEEDED,
                                started_at=started_at or as_of)  # fmt: skip
        session.add(cycle)
        session.flush()
        rows = [m.AgentReport(cycle_id=cycle.id, agent_name=agent, as_of=as_of, ticker=ticker, direction=direction,
                              confidence=confidence, horizon_days=horizon, thesis=f"{agent} on {ticker}",
                              model="claude-sonnet-5-5", event_type=event_type)
                for agent, ticker, direction, confidence, horizon in calls]  # fmt: skip
        session.add_all(rows)
        session.commit()
        return [row.id for row in rows]
