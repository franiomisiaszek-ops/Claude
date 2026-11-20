"""B starts from A's research; the hub sends and answers for every system."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from sqlalchemy import Engine, func, select

from papertrader.config import get_settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.hub import HubBot, SystemView, flush_all
from papertrader.notify.outbox import queue
from papertrader.research.cycle import run_cycle
from papertrader.research.reuse import (
    FROZEN_A_FINGERPRINT,
    ReuseRefused,
    agents_fingerprint,
    copy_a_research,
)
from papertrader.runtime import Runtime
from papertrader.scheduler.jobs import Jobs, specs_for
from papertrader.trading import control

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from ..notify.helpers import CHAT, FakeTelegram
from ..research.helpers import AS_OF, FRIDAY, FakeClaude, make_claude, seed_market
from ..research.test_cycle import busy_market
from ..trading.helpers import FakeBroker

CFG = shipped_config()
AGENTS_AT = AS_OF - timedelta(minutes=50)


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


def count(engine: Engine, model: type[m.Base]) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(model)).scalar_one())


def b_jobs(b_engine: Engine, fake: FakeClaude, clock: list[Any], config: Any = CFG) -> Jobs:
    runtime = Runtime(settings=get_settings(), config=config, engine=b_engine,
                      session_factory=make_session_factory(b_engine), system="B")  # fmt: skip

    def anthropic() -> AsyncAnthropic:
        return AsyncAnthropic(api_key="k", max_retries=0,
                              http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handler)))  # fmt: skip

    broker = FakeBroker(clock)
    return Jobs(
        runtime,
        broker=lambda: broker,
        anthropic=anthropic,
        now=lambda: clock[0],
        sleep=lambda _s: None,
    )


def test_each_system_runs_its_own_jobs() -> None:
    assert [s.name for s in specs_for("B", CFG)] == ["b_signals", "judge", "morning_brief", "place_orders",
                                                    "monitor", "reconcile_eod", "resolve_outcomes", "weekly"]  # fmt: skip
    fable_on = CFG.model_copy(update={"decision": CFG.decision.model_copy(update={
        "fable_shadow": CFG.decision.fable_shadow.model_copy(update={"enabled": True})})})  # fmt: skip
    assert "fable_shadow" in [s.name for s in specs_for("B", fable_on)]
    a_jobs = [s.name for s in specs_for("A")]
    assert "ingest" in a_jobs and not {"b_signals", "fable_shadow"} & set(a_jobs)
    assert specs_for("C") == ()


def test_the_agent_settings_still_match_the_frozen_a() -> None:
    assert agents_fingerprint(CFG) == FROZEN_A_FINGERPRINT
    from papertrader.config import load_config

    assert (
        agents_fingerprint(load_config(PROJECT_ROOT / "config" / "a.yaml")) == FROZEN_A_FINGERPRINT
    )
    assert (
        agents_fingerprint(load_config(PROJECT_ROOT / "config" / "b.yaml")) == FROZEN_A_FINGERPRINT
    )


async def test_b_judges_a_copy_of_as_research(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    await run_cycle(config=CFG, sessions=make_session_factory(engine), claude=make_claude(engine, fake),
                    as_of=AGENTS_AT, kind=m.CycleKind.DAILY, stage="agents")  # fmt: skip
    a_reports, a_calls = count(engine, m.AgentReport), len(fake.calls)

    clock = [AS_OF]
    jobs = b_jobs(b_engine, fake, clock)
    outcome = await __import__("asyncio").to_thread(jobs.run, "judge")
    assert outcome.status == "succeeded", outcome.detail
    assert outcome.details["reports_from_a"] == a_reports == 7
    assert fake.labels()[a_calls:] == ["judge"]  # B paid for its judge only, no agent calls
    assert count(b_engine, m.AgentReport) == 7 and count(b_engine, m.JudgeDecision) == 1
    assert count(engine, m.JudgeDecision) == 0  # A's own judge has not run: B never touched A
    with make_session_factory(b_engine)() as session:
        cycle = session.scalars(select(m.DecisionCycle)).one()
        assert cycle.notes["copied_from_a"] and cycle.status == m.RunStatus.SUCCEEDED
        assert {u.purpose for u in session.scalars(select(m.ApiUsage))} == {"judge"}
    assert copy_a_research(b_engine, make_session_factory(b_engine), CFG, FRIDAY)[1] == 7  # type: ignore[index]
    assert count(b_engine, m.AgentReport) == 7  # a second copy is not made


def test_b_waits_for_a_and_refuses_changed_agents(engine: Engine, b_engine: Engine) -> None:
    clock = [AS_OF]
    outcome = b_jobs(b_engine, FakeClaude(), clock).run("judge")
    assert outcome.status == "skipped" and "has not finished today's research" in outcome.detail
    changed = CFG.model_copy(
        update={"agents": CFG.agents.model_copy(update={"max_news_items": 10})}
    )
    with pytest.raises(ReuseRefused, match="differ from A's frozen ones"):
        copy_a_research(b_engine, make_session_factory(b_engine), changed, FRIDAY)


def views(engine: Engine, b_engine: Engine) -> dict[str, SystemView]:
    return {"A": SystemView("A", CFG, engine, make_session_factory(engine)),
            "B": SystemView("B", CFG, b_engine, make_session_factory(b_engine))}  # fmt: skip


def test_the_hub_sends_every_outbox_with_its_tag(engine: Engine, b_engine: Engine) -> None:
    for eng, text in ((engine, "ALERT · from A"), (b_engine, "ALERT · from B")):
        with make_session_factory(eng)() as session:
            queue(session, "alert", text)
            session.commit()
    fake = FakeTelegram()
    reports = flush_all(views(engine, b_engine), fake.client(), CHAT, CFG.telegram)  # type: ignore[arg-type]
    assert reports["A"].sent == reports["B"].sent == 1  # type: ignore[index]
    assert fake.texts() == ["<b>[A] ALERT · from A</b>", "<b>[B] ALERT · from B</b>"]


def test_the_hub_answers_for_one_or_all_systems(engine: Engine, b_engine: Engine) -> None:
    fake = FakeTelegram()
    bot = HubBot(views(engine, b_engine), fake.client(), CHAT, get_settings(), telegram=CFG.telegram,  # type: ignore[arg-type]
                 brokers={"A": lambda: None, "B": lambda: None})  # fmt: skip
    both = bot.handle("/pnl")
    assert both.startswith("[A] P&L") and "\n\n[B] P&L" in both
    assert bot.handle("/pnl B").startswith("[B] P&L") and "[A]" not in bot.handle("/pnl b")
    assert bot.handle("/pause") == "Say which system: /pause A, /pause B or /pause C."
    assert bot.handle("/pause B earnings week").startswith("[B] Trading paused")
    assert (
        control.paused(make_session_factory(b_engine))
        and control.paused(make_session_factory(engine)) is None
    )
    assert bot.handle("/status C") == "System C is not set up yet."
    assert "add A, B or C" in bot.handle("/help")


def test_the_hub_finds_the_systems_that_are_set_up(engine: Engine, database_url: str) -> None:
    from papertrader.config import Settings
    from papertrader.hub import open_views

    settings = Settings(database_url=database_url)
    folder = PROJECT_ROOT / "config"
    first = open_views(settings, folder)
    assert sorted(first) == ["A"]  # B and C are not provisioned yet
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    later = open_views(settings, folder, skip=set(first))  # the hub's 10-minute refresh
    assert sorted(later) == ["B"] and later["B"].config.budget.monthly_usd == 30
    for view in (*first.values(), *later.values()):
        view.engine.dispose()
    b.dispose()
