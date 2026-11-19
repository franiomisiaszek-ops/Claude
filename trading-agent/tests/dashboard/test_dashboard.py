from __future__ import annotations

import shutil
from datetime import time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from papertrader.dashboard import data
from papertrader.db import models as m
from papertrader.db.session import make_engine, make_session_factory
from papertrader.db.state import HEARTBEAT_KEY, TRADING_PAUSED_KEY, set_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import at_new_york
from papertrader.notify.outbox import queue
from papertrader.runtime import bootstrap

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from ..trading.helpers import MONDAY, idea, seed_decision

CFG = shipped_config()
APP = str(PROJECT_ROOT / "src/papertrader/dashboard/app.py")


def seed(engine: Engine) -> None:
    """A week of history: snapshots, trades, a decision with reports, scores, spend, jobs."""
    sessions = make_session_factory(engine)
    days = [MONDAY + timedelta(days=i) for i in range(5)]
    with sessions() as session:
        for i, day in enumerate(days):  # equity rises then falls; SPY rises steadily
            equity = [100_000, 101_000, 102_000, 100_980, 101_500][i]
            session.add(m.EquitySnapshot(ts=at_new_york(day, time(16, 15)), kind="eod", equity=equity, cash=50_000,
                                         positions_value=equity - 50_000, benchmark_close=600 + 3 * i,
                                         day_pnl_pct=0.001 * i))  # fmt: skip
        session.add(m.EquitySnapshot(ts=at_new_york(days[-1], time(11, 0)), kind="intraday", equity=99_000, cash=1,
                                     positions_value=1, benchmark_close=611))  # earlier than the close: ignored  # fmt: skip
        session.add(m.Trade(ticker="AAPL", sector="Information Technology", direction=m.Direction.LONG, qty=10,
                            entry_price=200, entered_at=at_new_york(days[1], time(9, 40)), target_price=212,
                            stop_price=194, horizon_ends_at=at_new_york(days[4], time(15, 45)),
                            status=m.TradeStatus.OPEN))  # fmt: skip
        session.add(m.Trade(ticker="XOM", direction=m.Direction.LONG, qty=5, entry_price=100,
                            entered_at=at_new_york(days[0], time(9, 40)), exit_price=106,
                            exited_at=at_new_york(days[2], time(10, 0)), exit_trigger=m.ExitTrigger.TARGET,
                            status=m.TradeStatus.CLOSED, pnl_usd=30, pnl_pct=0.06, excess_return_pct=0.05))  # fmt: skip
        session.add(m.Trade(ticker="SPY", direction=m.Direction.LONG, qty=1, entry_price=600, is_test=True,
                            entered_at=at_new_york(days[0], time(9, 40)), exit_price=601,
                            exited_at=at_new_york(days[0], time(10, 0)), status=m.TradeStatus.CLOSED, pnl_usd=1))  # fmt: skip
        for agent, n, hit in (("earnings", 20, 0.6), ("technicals", 15, 0.4)):
            session.add(m.AgentStat(as_of_date=days[-1], agent_name=agent, window_days=90, n=n, hit_rate=hit,
                                    brier=0.24, avg_excess_return_pct=0.004))  # fmt: skip
            session.add(m.AgentStat(as_of_date=days[-1], agent_name=agent, sector="Energy", window_days=90, n=5,
                                    hit_rate=0.8))  # fmt: skip
            session.add(m.AgentWeight(agent_name=agent, weight=1.1 if hit > 0.5 else 0.9,
                                      effective_from=at_new_york(days[-1], time(9, 0))))  # fmt: skip
        session.add(m.Lesson(period_start=days[0], period_end=days[-1], text="Lessons from 35 calls:\n- earnings worked",
                             model="claude-opus-5-5"))  # fmt: skip
        session.add(m.ModelVersion(version="pm-1", artifact_path="x", is_active=False, notes="no skill",
                                   metrics={"rows": 5000, "hit_rate": 0.51, "base_rate": 0.5, "ic_daily": -0.013,
                                            "brier": 0.25, "backtest": {"strategy": {"total_return": 0.1},
                                                                        "benchmark": {"total_return": 0.2}}}))  # fmt: skip
        for purpose, cost in (
            ("agent:earnings", 0.05),
            ("judge", 0.2),
            ("text_features", 0.01),
            ("lessons", 0.12),
        ):
            session.add(
                m.ApiUsage(purpose=purpose, model="claude-sonnet-5-5", cost_usd=cost, ts=utcnow())
            )
        session.add(m.JobRun(job_name="judge", scheduled_for=utcnow() - timedelta(hours=1), started_at=utcnow(),
                             finished_at=utcnow(), status=m.RunStatus.SUCCEEDED, details={"summary": "2 ideas"}))  # fmt: skip
        queue(session, "alert", "ALERT: hello")
        set_state(session, HEARTBEAT_KEY, {"at": utcnow().isoformat()})
        set_state(session, TRADING_PAUSED_KEY, {"paused": True, "reason": "testing"})
        session.commit()
    seed_decision(engine, [idea("AAPL", 200.0), idea("MSFT", 400.0, status=m.IdeaStatus.BELOW_THRESHOLD)],
                  reports=[("AAPL", "earnings", m.Stance.SUPPORT), ("AAPL", "contrarian", m.Stance.OPPOSE)])  # fmt: skip


def test_the_queries(engine: Engine) -> None:
    seed(engine)
    view = data.overview(engine, CFG)
    assert (view.equity, view.open_trades, view.closed_trades, view.win_rate) == (
        101_500,
        1,
        1,
        100,
    )
    assert view.total_pct == 1.5 and view.spy_total_pct == 2.0 and view.paused == "testing"
    assert view.realised_usd == 30 and view.month_spend == pytest.approx(0.38)

    curve = data.equity_curve(engine)
    assert len(curve) == 5 and list(curve["papertrader_pct"]) == [0.0, 1.0, 2.0, 0.98, 1.5]
    assert list(curve["spy_pct"])[-1] == 2.0 and min(curve["drawdown_pct"]) == pytest.approx(-1.0)

    open_trades = data.trades(engine, open_only=True)
    assert list(open_trades["ticker"]) == ["AAPL"] and "time limit" in open_trades
    closed = data.trades(engine, open_only=False)
    assert (
        list(closed["ticker"]) == ["XOM"]
        and closed["P&L %"].iloc[0] == 6.0
        and closed["days held"].iloc[0] == 2.0
    )
    assert len(data.trades(engine, open_only=False, include_tests=True)) == 2

    ((decision_id, label),) = data.decision_choices(engine)
    assert "daily · 2 idea(s)" in label
    decision = data.decision_view(engine, decision_id)
    assert decision is not None and list(decision.ideas["ticker"]) == ["AAPL", "MSFT"]
    assert decision.ideas["final %"].iloc[0] == 80 and {r["agent"] for r in decision.reports} == {
        "earnings",
        "contrarian",
    }

    board = data.leaderboard(engine, 90)
    assert (
        list(board.overall["agent"]) == ["earnings", "technicals"]
        and board.overall["hit rate %"].iloc[0] == 60.0
    )
    assert list(board.by_sector["sector"]) == ["Energy", "Energy"] and board.by_event.empty
    assert data.leaderboard(engine, 30).overall.empty
    assert set(data.weights(engine)["agent"]) == {"earnings", "technicals"}
    lesson = data.latest_lesson(engine)
    assert lesson is not None and lesson[1].startswith("Lessons from 35 calls")
    versions = data.model_versions(engine)
    assert versions["daily IC"].iloc[0] == -0.013 and versions["backtest %"].iloc[0] == 10.0

    spend = data.api_spend(engine)
    assert set(spend.daily.columns) == {"research agents", "judge", "headline labels"}
    assert spend.month_total == pytest.approx(0.38) and spend.today_total == pytest.approx(0.38)
    assert spend.by_purpose["purpose"].iloc[0] == "judge"
    assert data.job_runs(engine)["summary"].iloc[0] == "2 ideas"
    assert data.notifications(engine)["text"].iloc[0] == "ALERT: hello"


def test_empty_database_gives_empty_frames(engine: Engine) -> None:
    assert data.equity_curve(engine).empty and data.trades(engine, open_only=True).empty
    assert data.decision_choices(engine) == [] and data.leaderboard(engine, 90).as_of is None
    assert data.model_versions(engine).empty and data.api_spend(engine).daily.empty
    assert data.weights(engine).empty and data.latest_lesson(engine) is None


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return tmp_path


@pytest.mark.parametrize("filled", [True, False])
def test_every_page_renders(project: Path, filled: bool) -> None:
    from streamlit.testing.v1 import AppTest

    bootstrap(migrate=True).engine.dispose()
    if filled:
        engine = make_engine(f"sqlite:///{project / 'app.db'}")
        seed(engine)
        engine.dispose()
    page = AppTest.from_file(APP).run(timeout=30)
    assert not page.exception, page.exception
    pages = page.sidebar.radio[0].options
    assert len(pages) == 9
    for name in pages:
        page.sidebar.radio[0].set_value(name).run(timeout=30)
        assert not page.exception, (name, page.exception)
        assert not page.error, (name, [e.value for e in page.error])
    page.sidebar.radio[0].set_value("Overview").run(timeout=30)
    if filled:
        assert any(w.value.startswith("Trading is paused: testing") for w in page.warning)
        page.sidebar.radio[0].set_value("Decisions and reports").run(timeout=30)
        assert any("AAPL · LONG · earnings" in e.label for e in page.expander)
    else:
        page.sidebar.radio[0].set_value("Equity vs SPY").run(timeout=30)
        assert any("No equity snapshots yet" in i.value for i in page.info)
