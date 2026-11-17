"""Weekly: turn each agent's track record into the reliability weight the judge uses.

    weight = 1 + scale x (hit rate - 50%) x n / (n + prior_calls)

kept between weight_min and weight_max. The n / (n + prior) factor shrinks small
samples toward 1.0: 12 lucky calls barely move an agent, 200 good ones do. With fewer
than weight_min_calls resolved calls the weight stays 1.0 ("no track record yet").
The weight changes how much an agent counts in the agreement score and is shown to
the judge; it never stops an agent from running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.judge.judge import agent_weights


@dataclass(frozen=True)
class WeightChange:
    agent: str
    old: float
    new: float
    n: int
    hit_rate: float | None


def weight_for(n: int, hit_rate: float | None, config: AppConfig) -> float:
    cfg = config.learning
    if hit_rate is None or n < cfg.weight_min_calls:
        return 1.0
    shrink = n / (n + cfg.weight_prior_calls)
    raw = 1 + cfg.weight_scale * (hit_rate - 0.5) * shrink
    return round(min(cfg.weight_max, max(cfg.weight_min, raw)), 3)


def update_weights(session: Session, config: AppConfig, now: datetime) -> list[WeightChange]:
    latest = session.scalar(select(func.max(m.AgentStat.as_of_date)))
    if latest is None:
        return []
    window = config.learning.weight_window_days
    stats = session.scalars(
        select(m.AgentStat).where(m.AgentStat.as_of_date == latest, m.AgentStat.window_days == window,
                                  m.AgentStat.sector == "", m.AgentStat.event_type == "")
        .order_by(m.AgentStat.agent_name)
    ).all()  # fmt: skip
    before = agent_weights(session, now)
    changes = []
    for stat in stats:
        weight = weight_for(stat.n, stat.hit_rate, config)
        session.add(m.AgentWeight(
            agent_name=stat.agent_name, weight=weight, effective_from=now,
            basis={"stats_date": latest.isoformat(), "window_days": window, "n": stat.n,
                   "hit_rate": stat.hit_rate, "brier": stat.brier,
                   "avg_excess_return_pct": stat.avg_excess_return_pct},
        ))  # fmt: skip
        changes.append(
            WeightChange(
                stat.agent_name, before.get(stat.agent_name, 1.0), weight, stat.n, stat.hit_rate
            )
        )
    session.flush()
    return changes
