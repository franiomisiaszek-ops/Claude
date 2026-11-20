"""X2c: "why did it move?" post-mortems, evidence levels, and careful, capped lessons."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from papertrader.b import postmortem as pm
from papertrader.b.models import DecisionLog, LessonUse, PostMortem
from papertrader.config import PostMortemSwitch, get_settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.hub import HubBot, SystemView

from ..notify.helpers import CHAT, FakeTelegram
from ..research.helpers import AS_OF, FRIDAY, FakeClaude, make_claude, seed_market
from ..systems.test_b_and_hub import b_jobs
from .helpers import CFG, a_has_researched, b_config, b_judges, decision_log

MONDAY_7AM = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


def an_idea(session: Any, ticker: str, as_of: datetime) -> m.TradeIdea:
    cycle = m.DecisionCycle(kind=m.CycleKind.DAILY, as_of=as_of, status=m.RunStatus.SUCCEEDED)
    session.add(cycle)
    session.flush()
    decision = m.JudgeDecision(
        cycle_id=cycle.id, as_of=as_of, market_summary="", model="x", payload={}
    )
    idea = m.TradeIdea(rank=1, ticker=ticker, action=m.TradeAction.BUY, judge_confidence=0.7, final_confidence=0.66,
                       entry_low=99, entry_high=100, target_price=110, stop_price=95, size_pct=0.05, horizon_days=5,
                       rationale=f"buy {ticker} on the product news", status=m.IdeaStatus.BELOW_THRESHOLD)  # fmt: skip
    decision.ideas.append(idea)
    session.add(decision)
    session.flush()
    return idea


def b_history(b_engine: Engine) -> None:
    """A trade that closed on Friday, and an idea B passed on that then went its way."""
    with make_session_factory(b_engine)() as session:
        traded = an_idea(session, "AAPL", AS_OF - timedelta(days=3))
        session.add(m.Trade(trade_idea_id=traded.id, ticker="AAPL", direction=m.Direction.LONG, qty=10,
                            entry_price=340.0, entered_at=AS_OF - timedelta(days=3), status=m.TradeStatus.CLOSED,
                            exit_price=347.0, exited_at=AS_OF + timedelta(hours=1), exit_trigger=m.ExitTrigger.TARGET,
                            pnl_usd=70.0, pnl_pct=0.0206))  # fmt: skip
        passed = an_idea(session, "PFE", AS_OF - timedelta(days=4))
        session.add(DecisionLog(decision_id=1, idea_id=passed.id, cycle_id=1, as_of=AS_OF - timedelta(days=4),
                                ticker="PFE", action="buy", horizon_days=5, judge_confidence=0.7, agreement=0.6,
                                raw_confidence=0.66, a_status="below_threshold", b_status="below_threshold",
                                b_reason="confidence 0.66 is below 0.70", a_size=0.05, b_size=0.05, a_stop=95,
                                b_stop=95, a_target=110, b_target=110, market_trend="above", atr_pct=0.01))  # fmt: skip
        session.commit()


def case_answer(call: Any) -> dict[str, Any]:
    assert "known then" in call.user and "B's decision record:" in call.user
    return {"causes": [{"cause": "a new product line", "likelihood": 0.6, "sources": ["news:1", "news:999"]},
                       {"cause": "a calm, rising tape", "likelihood": 0.3, "sources": []}],
            "why_b_acted": "the agents agreed", "decision_quality": "good", "what_would_change": "nothing",
            "lesson": "when product news is fresh and the sector is rising, small names follow through",
            "adjust": "more_confident"}  # fmt: skip


def test_picking_and_writing_the_days_cases(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    b_history(b_engine)
    fake = FakeClaude()
    fake.responses["postmortem_case"] = case_answer
    fake.responses["postmortem_digest"] = {
        "text": "- AAPL +2.1%: new products (60%); B bought, a sound call."
    }
    config = b_config(postmortems={"enabled": True, "big_move_ranges": 0.1})
    sessions = make_session_factory(b_engine)
    claude = make_claude(b_engine, fake, config=config, now=lambda: MONDAY_7AM)
    out = asyncio.run(pm.run_day(sessions, config, claude, FRIDAY, MONDAY_7AM))
    assert out["picked"] == ["AAPL (closed_trade)", "PFE (missed)"] and out["cases"] == 2
    with sessions() as session:
        cases = {c.kind: c for c in session.scalars(select(PostMortem))}
        digest = session.scalars(select(m.Notification)).one().text
    closed = cases["closed_trade"]
    assert (
        closed.outcome == "good"
        and closed.decision_quality == "good"
        and closed.evidence == "anecdote"
    )
    assert closed.causes[0]["sources"] == ["news:1"]  # a made-up source is dropped
    assert closed.causes[-1]["cause"].startswith(
        "the market, the sector or noise"
    )  # always considered
    assert "bought 10 at 340.00" in closed.what_b_did and closed.move_vs_sector is not None
    assert (
        cases["missed"].outcome == "bad"
        and "did not trade the judge's buy idea" in cases["missed"].what_b_did
    )
    assert digest.startswith("WHY IT MOVED · Fri 13 Nov") and digest.endswith(
        "Details: /why TICKER"
    )
    again = asyncio.run(pm.run_day(sessions, config, claude, FRIDAY, MONDAY_7AM))
    assert again["cases"] == 0  # the same events are not written up twice


def case(
    cid: int, adjust: str = "more_cautious", ticker: str = "AAPL", **conditions: Any
) -> PostMortem:
    base = {
        "event_types": ["product"],
        "sector": "Information Technology",
        "market_trend": "above",
        "vol": "low",
    }
    return PostMortem(id=cid, day=date(2026, 10, 1), created_at=AS_OF - timedelta(days=40), kind="closed_trade",
                      ticker=ticker, period_start=date(2026, 10, 1), period_end=date(2026, 10, 2), move_pct=-0.03,
                      what_b_did="bought", decision_quality="unclear", outcome="bad", adjust=adjust,
                      lesson="when the product news is a week old, it is already in the price",
                      conditions=base | conditions)  # fmt: skip


def test_evidence_levels_are_counted_not_claimed(b_engine: Engine) -> None:
    with make_session_factory(b_engine)() as session:
        session.add(case(1))
        session.flush()
        assert pm.evidence(session, session.get(PostMortem, 1)) == "anecdote"
        for i in range(2, 4):
            session.add(case(i, ticker="MSFT"))
        session.flush()
        assert pm.evidence(session, session.get(PostMortem, 1)) == "emerging"  # 3 alike, all agree
        for i in range(4, 9):
            session.add(case(i, ticker="MSFT"))
        session.add(case(9, adjust="more_confident", ticker="MSFT"))
        session.flush()
        assert pm.evidence(session, session.get(PostMortem, 1)) == "supported"  # 8 of 9 agree
        score, differences = pm.similarity(case(10).conditions, {"event_types": ["product"], "market_trend": "below",
                                                                  "vol": "low", "sector": "Health Care"}, "AAPL", "PFE")  # fmt: skip
        assert score == 2.5 and differences == ["SPY vs its 200-day average: above then, below now"]


def test_nudges_are_capped_and_follow_the_lesson() -> None:
    cfg = PostMortemSwitch(enabled=True)
    cautious, confident, neutral = (
        case(1),
        case(2, adjust="more_confident"),
        case(3, adjust="no_change"),
    )
    assert pm.adjustment(cfg, cautious, -1.0) == -0.03
    assert pm.adjustment(cfg, cautious, +0.8) == 0.0  # against its own lesson: ignored
    assert pm.adjustment(cfg, confident, +5.0) == 0.03
    assert pm.adjustment(cfg, neutral, -1.0) == 0.0
    cautious.evidence = "supported"
    assert pm.adjustment(cfg, cautious, -1.0) == -0.07


async def test_lessons_at_the_judge_only_make_it_more_careful(
    engine: Engine, b_engine: Engine
) -> None:
    fake = FakeClaude()
    await a_has_researched(engine, fake)
    with make_session_factory(b_engine)() as session:
        session.add(case(1, event_types=["product"]))  # comparable: lowers AAPL
        session.add(
            case(2, adjust="more_confident", event_types=["product"], market_trend="below")
        )  # not comparable
        session.commit()

    def compare(call: Any) -> dict[str, Any]:
        assert (
            "differences from today: SPY vs its 200-day average: below then, above now" in call.user
        )
        return {"verdicts": [{"ticker": "AAPL", "case_id": 1, "comparable": True, "change": -1, "why": "same stale news"},
                             {"ticker": "AAPL", "case_id": 2, "comparable": False, "change": 1, "why": "other market"}]}  # fmt: skip

    fake.responses["lesson_compare"] = compare
    config = b_config(postmortems={"enabled": True, "apply_to_judge": True})
    await b_judges(b_engine, fake, config)
    assert "lesson_compare" in fake.labels()
    with make_session_factory(b_engine)() as session:
        use = session.scalars(select(LessonUse)).one()
        shown = {
            c.id: (c.times_shown, c.times_comparable) for c in session.scalars(select(PostMortem))
        }
    assert use.ticker == "AAPL" and use.comparable_ids == [1] and use.adjustment == -0.03
    assert use.confidence_with == pytest.approx(use.confidence_without - 0.03)
    assert shown == {1: (1, 1), 2: (1, 0)}
    assert any(s.startswith("X2c lessons: -0.03") for s in decision_log(b_engine)["AAPL"].steps)


def test_the_monthly_check_and_the_three_month_rule(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    sessions = make_session_factory(b_engine)
    with sessions() as session:  # a lesson stopped an AAPL buy that then beat the market
        session.add(LessonUse(as_of=AS_OF - timedelta(days=40), ticker="AAPL", action="buy", horizon_days=5,
                              case_ids=[1], comparable_ids=[1], adjustment=-0.03, confidence_without=0.7,
                              confidence_with=0.67, status_without="proposed", status_with="below_threshold"))  # fmt: skip
        session.commit()
        row = pm.evaluate(session, CFG, AS_OF)
        assert row is not None and row.result["stopped_winners"] == 1
        assert (
            row.verdict.startswith("no help yet") and pm.evaluate(session, CFG, AS_OF) is None
        )  # monthly
        later = pm.evaluate(session, CFG, AS_OF + timedelta(days=80), force=True)
        assert later is not None and "recommend switching" in later.verdict
        session.commit()
        texts = [n.text for n in session.scalars(select(m.Notification))]
    assert any(t.startswith("LESSONS CHECK (system B, monthly): after three months") for t in texts)


def test_why_and_the_daily_job(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    b_history(b_engine)
    fake = FakeClaude()
    fake.responses["postmortem_case"] = case_answer
    fake.responses["postmortem_digest"] = {"text": "- AAPL: new products."}
    config = b_config(postmortems={"enabled": True})
    clock = [MONDAY_7AM]
    jobs = b_jobs(b_engine, fake, clock, config)
    outcome = jobs.run("postmortems")
    assert outcome.status == "succeeded" and outcome.detail == "1 post-mortem(s) for Fri 13 Nov", (
        outcome.detail
    )
    sessions = make_session_factory(b_engine)
    bot = HubBot({"B": SystemView("B", config, b_engine, sessions)}, FakeTelegram().client(), CHAT, get_settings(),  # type: ignore[arg-type]
                 telegram=CFG.telegram, brokers={"B": lambda: None})  # fmt: skip
    answer = bot.handle("/why aapl")
    assert answer.startswith("[B] WHY IT MOVED · AAPL (Fri 13 Nov, closed trade)")
    assert "1. a new product line (60%) [news:1]" in answer and "Lesson (anecdote):" in answer
    assert bot.handle("/why C") == "[B] No post-mortem for C yet."
    assert bot.handle("/why") == "Say which stock: /why NVDA"
