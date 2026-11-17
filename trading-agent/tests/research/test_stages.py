"""The daily run in two steps: agents at 07:30, pattern model + judge at 08:20."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Engine, func, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.research.cycle import AGENTS_DONE, run_cycle, run_judge_stage

from ..ingest.helpers import shipped_config
from .helpers import AS_OF, FakeClaude, make_claude, seed_market
from .test_cycle import busy_market

CFG = shipped_config()
AGENTS_AT = AS_OF - timedelta(minutes=50)  # 07:30 New York


async def test_agents_then_judge(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    claude = make_claude(engine, fake)
    sessions = make_session_factory(engine)

    agents = await run_cycle(config=CFG, sessions=sessions, claude=claude, as_of=AGENTS_AT,
                             kind=m.CycleKind.DAILY, stage="agents")  # fmt: skip
    assert agents.status == "succeeded" and agents.decision is None
    assert "judge" not in fake.labels() and len(agents.reports) == 7
    with sessions() as session:
        cycle = session.get(m.DecisionCycle, agents.cycle_id)
        assert cycle is not None and cycle.status == m.RunStatus.RUNNING
        assert cycle.notes["stage"] == AGENTS_DONE and cycle.finished_at is None
        assert session.scalar(select(func.count()).select_from(m.JudgeDecision)) == 0

    judged = await run_judge_stage(config=CFG, sessions=sessions, claude=claude, now=AS_OF,
                                   positions=["NVDA: long 5 shares @ 140.00 since 10 Nov"],
                                   lessons="Lessons from 40 resolved calls: discount technicals.")  # fmt: skip
    assert judged.status == "succeeded" and judged.cycle_id == agents.cycle_id
    assert judged.decision is not None and [t.ticker for t in judged.decision.trades] == [
        "AAPL",
        "XOM",
    ]
    prompt = fake.call("judge").user
    assert "## Open positions\n- NVDA: long 5 shares" in prompt
    assert "## Lessons from past outcomes\nLessons from 40 resolved calls" in prompt
    assert "[report:" in prompt and "contrarian | SHORT XOM" in prompt
    with sessions() as session:
        cycle = session.get(m.DecisionCycle, agents.cycle_id)
        assert cycle is not None and cycle.status == m.RunStatus.SUCCEEDED and cycle.finished_at
        assert cycle.notes["stage"] == "done"
        assert [a["agent"] for a in cycle.notes["agents"]][-2:] == ["contrarian", "judge"]
        assert cycle.notes["cost_usd"] > 0
        decision = session.scalars(select(m.JudgeDecision)).one()
        assert decision.cycle_id == agents.cycle_id and decision.as_of == AGENTS_AT

    again = await run_judge_stage(config=CFG, sessions=sessions, claude=claude, now=AS_OF)
    assert again.status == "skipped" and "No research run is waiting" in again.notes[0]


async def test_a_quiet_morning_still_leaves_a_decision_for_the_brief(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()  # every analyst answers with no reports
    claude = make_claude(engine, fake)
    sessions = make_session_factory(engine)
    await run_cycle(config=CFG, sessions=sessions, claude=claude, as_of=AGENTS_AT,
                    kind=m.CycleKind.DAILY, stage="agents")  # fmt: skip
    judged = await run_judge_stage(config=CFG, sessions=sessions, claude=claude, now=AS_OF)
    assert judged.status == "succeeded" and "judge" not in fake.labels()
    with sessions() as session:
        decision = session.scalars(select(m.JudgeDecision)).one()
        assert decision.model == "none" and decision.ideas == []
