"""Switch 4: leave out agents whose track record is worse than a coin flip.

Each agent's hit rate is shrunk toward 50% before it is judged:

    shrunk = (hits + 0.5 x prior_calls) / (calls + prior_calls)

so 12 unlucky calls barely move an agent, while 200 bad ones do. An agent is left out of
B's judge (its reports are not shown and do not count in the agreement score) only when
it has at least `min_calls` scored calls and its shrunk hit rate is below `drop_below`.
The agents still run (in system A); B simply stops listening to them, and starts again
if their record recovers.

The record comes from system A's scored calls (read-only): B's reports are copies of
A's, and A's history goes back further.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Engine, case, func, select
from sqlalchemy.orm import Session

from papertrader.config import PruningSwitch
from papertrader.db import models as m
from papertrader.db.provision import a_tables


@dataclass(frozen=True)
class AgentRecord:
    agent: str
    calls: int
    hits: int
    shrunk: float
    keep: bool


def records(
    engine: Engine, cfg: PruningSwitch, now: datetime, window_days: int = 365
) -> dict[str, AgentRecord]:
    since = now - timedelta(days=window_days)
    with engine.connect() as conn:
        view = conn.execution_options(schema_translate_map=a_tables(engine))
        with Session(bind=view) as session:
            rows = session.execute(
                select(m.AgentReport.agent_name, func.count(m.PredictionOutcome.id),
                       func.sum(case((m.PredictionOutcome.hit.is_(True), 1), else_=0)))
                .join(m.PredictionOutcome, m.PredictionOutcome.agent_report_id == m.AgentReport.id)
                .where(m.PredictionOutcome.hit.is_not(None), m.PredictionOutcome.resolved_at >= since,
                       m.PredictionOutcome.resolved_at <= now)
                .group_by(m.AgentReport.agent_name)
            ).all()  # fmt: skip
    out = {}
    for agent, calls, hits in rows:
        calls, hits = int(calls), int(hits or 0)
        shrunk = (hits + 0.5 * cfg.prior_calls) / (calls + cfg.prior_calls)
        keep = calls < cfg.min_calls or shrunk >= cfg.drop_below
        out[agent] = AgentRecord(agent, calls, hits, round(shrunk, 4), keep)
    return out
