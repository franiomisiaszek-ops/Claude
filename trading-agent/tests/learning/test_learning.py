from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.timeutil import at_new_york
from papertrader.judge.judge import agent_weights
from papertrader.learning.lessons import gather, latest_lessons, write_lessons
from papertrader.learning.outcomes import entry_index, resolve_outcomes
from papertrader.learning.stats import compute_stats
from papertrader.learning.weights import update_weights, weight_for

from ..ingest.helpers import shipped_config
from ..research.helpers import FakeClaude, make_claude
from .helpers import (
    DAYS,
    FRI,
    MON,
    MORNING,
    THU,
    THURSDAY_EVENING,
    add_bars,
    add_calls,
    add_members,
)

CFG = shipped_config()
L, S, N = m.Direction.LONG, m.Direction.SHORT, m.Direction.NEUTRAL


def outcomes(engine: Engine) -> dict[int, m.PredictionOutcome]:
    with make_session_factory(engine)() as session:
        return {o.agent_report_id: o for o in session.scalars(select(m.PredictionOutcome))}


def test_the_entry_is_the_first_open_at_or_after_the_decision_time() -> None:
    decide = CFG.schedule.score_and_judge  # 08:20
    assert entry_index(DAYS, MORNING, decide) == 0  # Monday 07:30: Monday's open
    assert entry_index(DAYS, at_new_york(MON, decide), decide) == 0
    assert entry_index(DAYS, at_new_york(MON, decide) + timedelta(minutes=1), decide) == 1
    assert entry_index(DAYS, at_new_york(MON, decide) - timedelta(days=2), decide) == 0  # Saturday


def test_calls_are_scored_like_the_pattern_model_labels(engine: Engine) -> None:
    add_bars(engine, {"SPY": [(100, 100), (101, 101), (101, 102), (102, 103), (103, 104)],
                      "AAPL": [(200, 201), (205, 206), (208, 210), (210, 209), (209, 215)]})  # fmt: skip
    right, wrong, neutral, spy, long_run, no_bars = add_calls(engine, [
        ("earnings", "AAPL", L, 0.8, 3), ("contrarian", "AAPL", S, 0.6, 3), ("technicals", "AAPL", N, 0.5, 3),
        ("macro", "SPY", L, 0.6, 3), ("news_event", "AAPL", L, 0.7, 10), ("news_event", "MSFT", L, 0.7, 3),
    ])  # fmt: skip
    # A thesis check at 11:00 Monday: decided after the open, so it enters Tuesday.
    (late,) = add_calls(engine, [("news_event", "AAPL", L, 0.7, 2)], kind=m.CycleKind.THESIS_BREAK,
                        as_of=at_new_york(MON, MORNING.astimezone().time()) + timedelta(hours=3, minutes=30))  # fmt: skip
    # A dry run "as of" Monday, run ten days later: a replay, never scored.
    (replay,) = add_calls(engine, [("earnings", "AAPL", L, 0.9, 3)], kind=m.CycleKind.DRY_RUN,
                          started_at=MORNING + timedelta(days=10))  # fmt: skip

    with make_session_factory(engine)() as session:
        report = resolve_outcomes(session, CFG, THURSDAY_EVENING + timedelta(days=11))
        session.commit()
    assert (report.resolved, report.waiting, report.no_prices, report.replays) == (4, 1, 1, 1)
    scored = outcomes(engine)
    assert set(scored) == {right, wrong, neutral, late}

    good = scored[right]  # Monday open 200 -> Wednesday close 210 = +5%; SPY 100 -> 102 = +2%
    assert (good.start_price, good.end_price) == (200, 210)
    assert good.return_pct == pytest.approx(0.05) and good.benchmark_return_pct == pytest.approx(
        0.02
    )
    assert good.excess_return_pct == pytest.approx(0.03) and good.hit is True
    assert good.brier == pytest.approx(0.04)  # (0.8 - 1)^2
    assert scored[wrong].hit is False and scored[wrong].brier == pytest.approx(0.36)
    assert scored[neutral].hit is None and scored[neutral].brier is None
    assert scored[late].start_price == 205 and scored[late].excess_return_pct == pytest.approx(
        210 / 205 - 102 / 101
    )
    assert (
        spy not in scored
        and long_run not in scored
        and no_bars not in scored
        and replay not in scored
    )

    with make_session_factory(engine)() as session:  # nothing is scored twice
        assert resolve_outcomes(session, CFG, THURSDAY_EVENING + timedelta(days=11)).resolved == 0


def test_a_horizon_is_only_scored_once_its_last_close_is_published(engine: Engine) -> None:
    add_bars(engine, {"SPY": [(100, 100)] * 5, "AAPL": [(200, 201)] * 5})
    (call,) = add_calls(engine, [("earnings", "AAPL", L, 0.7, 4)])  # Monday..Thursday
    with make_session_factory(engine)() as session:
        assert (
            resolve_outcomes(session, CFG, at_new_york(THU, MORNING.time())).waiting == 1
        )  # Thursday, before the close
        assert resolve_outcomes(session, CFG, THURSDAY_EVENING).resolved == 1
        session.commit()
    assert outcomes(engine)[call].end_price == 201


def _scored_history(engine: Engine) -> None:
    add_members(engine, {"AAPL": "Information Technology", "XOM": "Energy"})
    add_bars(engine, {"SPY": [(100, 100)] * 5, "AAPL": [(100, 110)] * 5, "XOM": [(100, 95)] * 5})
    add_calls(engine, [("earnings", "AAPL", L, 0.7, 1)] * 6 + [("earnings", "XOM", L, 0.7, 1)] * 2
              + [("contrarian", "XOM", S, 0.6, 1)] * 3 + [("contrarian", "AAPL", N, 0.5, 1)], event_type="earnings")  # fmt: skip
    with make_session_factory(engine)() as session:
        resolve_outcomes(session, CFG, THURSDAY_EVENING)
        session.commit()


def test_stats_per_agent_sector_and_event_type(engine: Engine) -> None:
    _scored_history(engine)
    with make_session_factory(engine)() as session:
        written = compute_stats(session, CFG, FRI)
        session.commit()
        assert compute_stats(session, CFG, FRI) == written  # recomputing replaces, never duplicates
        session.commit()
        rows = {
            (s.window_days, s.agent_name, s.sector, s.event_type): s
            for s in session.scalars(select(m.AgentStat))
        }
    assert len(rows) == written
    overall = rows[(90, "earnings", "", "")]
    assert (overall.n, overall.hit_rate) == (8, pytest.approx(6 / 8))
    assert overall.avg_excess_return_pct == pytest.approx((6 * 0.10 + 2 * -0.05) / 8)
    assert rows[(90, "earnings", "Energy", "")].hit_rate == 0.0
    assert rows[(90, "earnings", "", "earnings")].n == 8
    short = rows[(30, "contrarian", "", "")]  # the neutral call is not counted
    assert (short.n, short.hit_rate) == (3, 1.0)
    assert short.avg_excess_return_pct == pytest.approx(0.05)  # a falling stock counts for a short
    assert short.brier == pytest.approx(0.16)


def test_weights_shrink_small_samples_toward_one() -> None:
    assert weight_for(9, 0.9, CFG) == 1.0  # below the minimum number of calls
    assert weight_for(30, 0.6, CFG) == pytest.approx(1 + 4 * 0.1 * 0.5)
    assert weight_for(300, 0.6, CFG) == pytest.approx(round(1 + 4 * 0.1 * 300 / 330, 3))
    assert weight_for(1000, 0.95, CFG) == CFG.learning.weight_max
    assert weight_for(1000, 0.1, CFG) == CFG.learning.weight_min
    assert weight_for(50, None, CFG) == 1.0


def test_weekly_weights_reach_the_judge(engine: Engine) -> None:
    _scored_history(engine)
    base = shipped_config()
    config = base.model_copy(
        update={"learning": base.learning.model_copy(update={"weight_min_calls": 3})}
    )
    now = at_new_york(FRI + timedelta(days=1), MORNING.time())
    with make_session_factory(engine)() as session:
        compute_stats(session, config, FRI)
        changes = update_weights(session, config, now)
        session.commit()
        weights = agent_weights(session, now + timedelta(minutes=1))
        assert agent_weights(session, now - timedelta(minutes=1)) == {}  # not before it was made
        basis = (
            session.scalars(select(m.AgentWeight).where(m.AgentWeight.agent_name == "earnings"))
            .one()
            .basis
        )
    by_agent = {c.agent: c for c in changes}
    assert by_agent["earnings"].old == 1.0 and by_agent["earnings"].new == weight_for(
        8, 0.75, config
    )
    assert weights == {
        "earnings": weight_for(8, 0.75, config),
        "contrarian": weight_for(3, 1.0, config),
    }
    assert basis["n"] == 8 and basis["window_days"] == 90


def test_lessons_material_quotes_the_numbers(engine: Engine) -> None:
    _scored_history(engine)
    with make_session_factory(engine)() as session:
        text, n = gather(session, CFG, MON - timedelta(days=7), FRI)
    assert n == 11
    assert "## All directional calls: 9 of 11 right (82%)" in text
    assert "- earnings: 6 of 8 right (75%)" in text and "- Energy: 3 of 5 right (60%)" in text
    assert "LONG XOM (Energy) | earnings | confidence 0.70 | 1d | vs SPY -5.0% | WRONG" in text
    assert "## Last week's lessons\nnone yet" in text


async def test_weekly_lessons_are_written_and_given_to_the_judge(engine: Engine) -> None:
    _scored_history(engine)
    fake = FakeClaude()
    fake.responses["lessons"] = {"summary": "Earnings longs worked (6 of 8).", "worked": ["earnings longs: 6 of 8"],
                                 "failed": ["energy longs: 0 of 2"], "advice": ["discount energy longs"]}  # fmt: skip
    sessions = make_session_factory(engine)
    now = at_new_york(FRI + timedelta(days=1), MORNING.time())
    result = await write_lessons(sessions, CFG, make_claude(engine, fake, now=lambda: now), now)
    assert result.status == "written" and result.cost_usd > 0
    call = fake.call("lessons")
    assert (
        call.body["model"] == CFG.judge.model and call.body["output_config"]["effort"] == "medium"
    )
    assert "9 of 11 right" in call.user
    with sessions() as session:
        text = latest_lessons(session, CFG, now + timedelta(days=1))
        assert (
            latest_lessons(
                session, CFG, now + timedelta(days=CFG.learning.lessons_max_age_days + 1)
            )
            is None
        )
        purposes = [u.purpose for u in session.scalars(select(m.ApiUsage))]
    assert text is not None and text.startswith(
        "Lessons from 11 resolved calls (24 Oct to 21 Nov):"
    )
    assert "Failed:\n- energy longs: 0 of 2" in text and "Advice:\n- discount energy longs" in text
    assert purposes == ["lessons"]


async def test_no_lessons_from_too_few_calls(engine: Engine) -> None:
    fake = FakeClaude()
    result = await write_lessons(
        make_session_factory(engine), CFG, make_claude(engine, fake), MORNING
    )
    assert (
        result.status == "skipped" and "only 0 resolved calls" in result.detail and fake.calls == []
    )
