from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.timeutil import new_york_date
from papertrader.llm.governor import BudgetStatus
from papertrader.notify.alerts import budget_alerts, job_failed, job_missed
from papertrader.notify.reports import build_brief, build_weekly

from ..ingest.helpers import shipped_config
from ..trading.helpers import OPEN_TIME, idea, seed_decision


def test_the_morning_brief_lists_todays_ideas_and_open_positions(engine: Engine) -> None:
    config = shipped_config()
    seed_decision(engine, [
        idea("NVDA", 140.0, final_confidence=0.82, rationale="Data-centre demand is strong. Second sentence."),
        idea("JPM", 150.0, final_confidence=0.61, status=m.IdeaStatus.BELOW_THRESHOLD),
    ])  # fmt: skip
    seed_decision(
        engine, [idea("XOM", 100.0)], kind=m.CycleKind.THESIS_BREAK
    )  # not the morning run
    with make_session_factory(engine)() as session:
        session.add(m.Trade(ticker="AAPL", direction=m.Direction.LONG, qty=10, entry_price=200.0,
                            entered_at=OPEN_TIME - timedelta(days=2), status=m.TradeStatus.OPEN))  # fmt: skip
        session.commit()
        brief = build_brief(session, config, OPEN_TIME, {"AAPL": 204.0})
    assert brief is not None
    lines = brief.split("\n")
    assert lines[0] == "Morning brief · Mon 16 Nov" and lines[1] == "Market: s"
    assert "1. BUY NVDA · 82% · entry 139.30-140.70 · target 148.40 · stop 135.80" in lines
    assert "   Data-centre demand is strong" in lines
    assert any(line.startswith("2. BUY JPM · 61%") for line in lines)
    assert "   strong reasons (below the bar)" in lines
    assert "XOM" not in brief and lines[-1] == "Open positions: AAPL +2.0%"


def test_no_brief_without_a_daily_run_today(engine: Engine) -> None:
    seed_decision(engine, [idea("NVDA", 140.0)], as_of=OPEN_TIME - timedelta(days=1))
    with make_session_factory(engine)() as session:
        assert build_brief(session, shipped_config(), OPEN_TIME) is None


def test_the_weekly_summary(engine: Engine) -> None:
    now = OPEN_TIME
    with make_session_factory(engine)() as session:
        for pnl, excess, test in ((120.0, 0.02, False), (-40.0, -0.01, False), (999.0, 0.5, True)):
            session.add(m.Trade(ticker="NVDA", direction=m.Direction.LONG, qty=1, entry_price=1.0, is_test=test,
                                entered_at=now - timedelta(days=4), exited_at=now - timedelta(days=1),
                                status=m.TradeStatus.CLOSED, pnl_usd=pnl, excess_return_pct=excess))  # fmt: skip
        day = new_york_date(now)
        for agent, window, n, hit in (("earnings", 30, 10, 0.6), ("earnings", 90, 25, 0.56),
                                      ("contrarian", 90, 8, 0.4), ("macro", 90, 0, None)):  # fmt: skip
            session.add(
                m.AgentStat(as_of_date=day, agent_name=agent, window_days=window, n=n, hit_rate=hit)
            )
        session.add(
            m.AgentStat(
                as_of_date=day,
                agent_name="earnings",
                sector="Energy",
                window_days=90,
                n=3,
                hit_rate=1.0,
            )
        )
        session.add(
            m.ModelVersion(version="pm-20261114", artifact_path="x", metrics={"ic_daily": -0.0131})
        )
        for purpose, cost, ago in (("agent:earnings", 3.0, 1), ("judge", 2.0, 2), ("text_features", 0.5, 3),
                                   ("agent:macro", 10.0, 9)):  # fmt: skip
            session.add(
                m.ApiUsage(
                    purpose=purpose,
                    model="claude-sonnet-5-5",
                    cost_usd=cost,
                    ts=now - timedelta(days=ago),
                )
            )
        session.commit()
        text = build_weekly(session, shipped_config(), now)
    lines = text.split("\n")
    assert lines[0] == "Weekly summary · week to Mon 16 Nov"
    assert lines[1] == "Trades closed: 2 · won 1 · P&L +$80.00 · avg vs SPY +0.5%"
    assert lines[4:7] == [
        "1. earnings · 56% · 25 calls",
        "2. contrarian · 40% · 8 calls",
        "3. macro · n/a · 0 calls",
    ]
    assert "Model: pm-20261114" in text and "not promoted, daily IC -0.013" in text
    assert "this week $5.50 (agents $3.00, judge $2.00, text labels $0.50)" in text
    assert "month $15.50 of $100.00" in text


def _status(mode: str, spent: float, warn: bool) -> BudgetStatus:
    return BudgetStatus(mode, "over the daily pace", spent, 100.0, 1.0, 4.0, warn)  # type: ignore[arg-type]


def test_budget_alerts_come_once(engine: Engine) -> None:
    with make_session_factory(engine)() as session:
        for _ in range(3):
            budget_alerts(session, _status("normal", 50.0, False), OPEN_TIME)
        assert session.scalars(select(m.Notification)).all() == []
        for _ in range(3):
            budget_alerts(session, _status("degraded", 86.0, True), OPEN_TIME)
            budget_alerts(session, _status("stopped", 100.0, True), OPEN_TIME + timedelta(hours=1))
        budget_alerts(session, _status("stopped", 100.0, True), OPEN_TIME + timedelta(days=1))
        texts = [
            n.text for n in session.scalars(select(m.Notification).order_by(m.Notification.id))
        ]
    assert len(texts) == 4  # degraded today, one monthly warning, stopped today, stopped tomorrow
    assert "budget-saving mode: over the daily pace" in texts[0]
    assert "86% of the monthly budget ($86.00 of $100.00)" in texts[1]
    assert "used up ($100.00 of $100.00)" in texts[2]


def test_job_alerts(engine: Engine) -> None:
    with make_session_factory(engine)() as session:
        job_failed(session, "research", "x" * 1000, OPEN_TIME)
        job_failed(
            session, "research", "again", OPEN_TIME + timedelta(minutes=5)
        )  # same hour: one alert
        job_missed(session, "morning_brief", OPEN_TIME)
        texts = [
            n.text for n in session.scalars(select(m.Notification).order_by(m.Notification.id))
        ]
    assert len(texts) == 2 and len(texts[0]) < 400
    assert "job 'morning_brief' did not run (due Mon 10:00 New York)" in texts[1]
