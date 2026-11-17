"""Per-agent accuracy from resolved calls: overall, per sector and per event type.

For each window (e.g. the last 30, 90 and 365 days of calls) and each agent:
  n          directional (long or short) calls that have been resolved
  hit_rate   share of them that were right
  brier      mean Brier score (0 is perfect, 0.25 is a coin flip at 50% confidence)
  avg excess average excess return in the direction of the call (short calls count
             a falling stock as a gain)
Neutral calls are not counted: they make no claim that can be right or wrong.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, time, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import at_new_york, new_york_date


def compute_stats(session: Session, config: AppConfig, today: date) -> int:
    """Replace today's rows in agent_stats. Returns how many were written."""
    windows = sorted(set(config.learning.stats_windows_days))
    start = at_new_york(today - timedelta(days=max(windows)), time(0))
    sectors = dict(
        session.execute(select(m.UniverseMember.ticker, m.UniverseMember.gics_sector)).all()
    )
    rows = session.execute(
        select(m.AgentReport.agent_name, m.AgentReport.ticker, m.AgentReport.sector,
               m.AgentReport.event_type, m.AgentReport.as_of, m.AgentReport.direction,
               m.PredictionOutcome.hit, m.PredictionOutcome.brier, m.PredictionOutcome.excess_return_pct)
        .join(m.PredictionOutcome, m.PredictionOutcome.agent_report_id == m.AgentReport.id)
        .where(m.AgentReport.as_of >= start, m.PredictionOutcome.hit.is_not(None))
    ).all()  # fmt: skip

    groups: dict[tuple[int, str, str, str], list[tuple[bool, float, float]]] = defaultdict(list)
    for agent, ticker, own_sector, event, as_of, direction, hit, brier, excess in rows:
        signed = excess if direction == m.Direction.LONG else -excess
        sector = sectors.get(ticker) or own_sector or ""
        age = (today - new_york_date(as_of)).days
        for window in windows:
            if age <= window:
                value = (bool(hit), float(brier or 0.0), float(signed))
                groups[(window, agent, "", "")].append(value)
                if sector:
                    groups[(window, agent, sector, "")].append(value)
                if event:
                    groups[(window, agent, "", event)].append(value)

    session.execute(delete(m.AgentStat).where(m.AgentStat.as_of_date == today))
    for (window, agent, sector, event), values in sorted(groups.items()):
        n = len(values)
        session.add(m.AgentStat(
            as_of_date=today, agent_name=agent, sector=sector, event_type=event[:32], window_days=window,
            n=n, hit_rate=sum(v[0] for v in values) / n, brier=sum(v[1] for v in values) / n,
            avg_excess_return_pct=sum(v[2] for v in values) / n,
        ))  # fmt: skip
    session.flush()
    return len(groups)
