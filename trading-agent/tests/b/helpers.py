"""Shared set-up for system B's tests: A has run its agents, B judges a copy."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import Engine, select

from papertrader.b.models import DecisionLog
from papertrader.config import AppConfig, DecisionConfig
from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.research.cycle import run_cycle

from ..ingest.helpers import shipped_config
from ..research.helpers import AS_OF, FakeClaude, make_claude, seed_market
from ..research.test_cycle import busy_market
from ..systems.test_b_and_hub import AGENTS_AT, b_jobs

CFG = shipped_config()


def b_config(**switches: Any) -> AppConfig:
    """The shipped config with B's switches set, e.g. b_config(veto={"enabled": True})."""
    return CFG.model_copy(update={"decision": DecisionConfig.model_validate(switches)})


async def a_has_researched(engine: Engine, fake: FakeClaude) -> None:
    seed_market(engine)
    busy_market(fake)
    await run_cycle(config=CFG, sessions=make_session_factory(engine), claude=make_claude(engine, fake),
                    as_of=AGENTS_AT, kind=m.CycleKind.DAILY, stage="agents")  # fmt: skip


async def b_judges(b_engine: Engine, fake: FakeClaude, config: AppConfig) -> Any:
    jobs = b_jobs(b_engine, fake, [AS_OF], config)
    outcome = await asyncio.to_thread(jobs.run, "judge")
    assert outcome.status == "succeeded", outcome.detail
    return outcome


def ideas(engine: Engine) -> dict[str, m.TradeIdea]:
    with make_session_factory(engine)() as session:
        rows = session.scalars(select(m.TradeIdea)).all()
        session.expunge_all()
    return {r.ticker: r for r in rows}


def decision_log(engine: Engine) -> dict[str, DecisionLog]:
    with make_session_factory(engine)() as session:
        rows = session.scalars(select(DecisionLog)).all()
        session.expunge_all()
    return {r.ticker: r for r in rows}
