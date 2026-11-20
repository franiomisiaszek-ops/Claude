"""X3: the pre-registered measurement of A vs B vs C vs SPY."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from papertrader.b.models import DecisionLog
from papertrader.cli import app
from papertrader.config import get_settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.experiment import stats
from papertrader.experiment.measure import compare, get_start, report_text, set_start
from papertrader.experiment.switches import review, review_text, switch_for
from papertrader.experiment.weekly import weekly_text
from papertrader.ingest.timeutil import at_new_york

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from ..research.helpers import seed_market

CFG = shipped_config()
START = date(2026, 10, 1)
NOW = datetime(2026, 11, 14, 15, 0, tzinfo=UTC)  # Saturday


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


# --------------------------------------------------------------------------- statistics


def test_the_statistics() -> None:
    assert stats.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert stats.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert (
        stats.ranks([5, 1, 5]) == [2.5, 1.0, 2.5] and stats.spearman([1, 1, 1], [1, 2, 3]) is None
    )
    assert stats.holm([0.01, 0.04, 0.03]) == pytest.approx([0.03, 0.06, 0.06])
    assert stats.holm([0.02, None]) == [0.02, None]
    up = [0.001 + 0.0001 * ((i * 37) % 7 - 3) for i in range(60)]
    low, high = stats.block_bootstrap(up) or (0, 0)
    assert (
        low < sum(up) / len(up) < high and low > 0
    )  # always positive: the interval stays above zero
    assert stats.block_bootstrap(up) == stats.block_bootstrap(up)  # fixed seed: the same every time
    mean, se, p = stats.newey_west(up) or (0, 0, 1)
    assert mean == pytest.approx(0.001, abs=1e-4) and se > 0 and p < 0.001
    noise = [((i * 7919) % 13 - 6) / 1000 for i in range(60)]
    assert (stats.newey_west(noise) or (0, 0, 0))[2] > 0.05
    assert stats.block_bootstrap(up[:5]) is None and stats.newey_west(up[:3]) is None


# --------------------------------------------------------------------------- a whole comparison


def weekdays(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def a_system(engine: Engine, daily: float, cost: float, confidences: dict[str, float]) -> None:
    """End-of-day equity growing `daily`, Claude spend, and three judged ideas a day."""
    with make_session_factory(engine)() as session:
        equity = 25_000.0
        for day in weekdays(date(2026, 9, 30), date(2026, 11, 13)):
            if day >= START:
                equity *= 1 + daily
                session.add(
                    m.ApiUsage(
                        ts=at_new_york(day, time(8, 30)), purpose="judge", model="x", cost_usd=cost
                    )
                )
            session.add(m.EquitySnapshot(ts=at_new_york(day, time(16, 15)), kind="eod", equity=equity, cash=0,
                                         positions_value=equity))  # fmt: skip
            if START <= day <= date(2026, 11, 2):
                cycle = m.DecisionCycle(kind=m.CycleKind.DAILY, as_of=at_new_york(day, time(7, 30)),
                                        status=m.RunStatus.SUCCEEDED)  # fmt: skip
                session.add(cycle)
                session.flush()
                trades = [{"ticker": t, "action": "buy", "final_confidence": c, "status": "proposed",
                           "judge_confidence": c, "agent_agreement": 0.5, "horizon_days": 5}
                          for t, c in confidences.items()]  # fmt: skip
                session.add(m.JudgeDecision(cycle_id=cycle.id, as_of=cycle.as_of, market_summary="", model="x",
                                            payload={"trades": trades}))  # fmt: skip
        session.commit()


def test_a_comparison_of_a_and_b_against_spy(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)  # small stocks beat their sector funds: AAPL most, then MSFT, then PFE
    a_system(engine, 0.001, 0.50, {"AAPL": 0.6, "MSFT": 0.7, "PFE": 0.8})  # ranks them backwards
    a_system(b_engine, 0.002, 0.25, {"AAPL": 0.8, "MSFT": 0.7, "PFE": 0.6})  # ranks them right
    assert get_start(engine) is None
    assert compare({"A": engine}, CFG, None, NOW).phase == "not_started"
    set_start(engine, START, NOW)
    assert get_start(engine) == START
    c = compare({"A": engine, "B": b_engine}, CFG, START, NOW)
    a, b = c.systems["A"], c.systems["B"]
    assert c.phase == "running" and c.trading_days == len(weekdays(START, date(2026, 11, 13)))
    assert b.net_total is not None and a.net_total is not None and b.net_total > a.net_total
    assert a.gross_total == pytest.approx(1.001**c.trading_days - 1, rel=1e-6)
    assert a.net_total < a.gross_total  # the Claude spend is taken off
    assert a.spend_usd == pytest.approx(0.50 * c.trading_days)
    assert a.signal.mean_ic[5] == pytest.approx(-1.0) and b.signal.mean_ic[5] == pytest.approx(1.0)
    tests = {t.name: t for t in c.tests}
    assert tests["H1"].mean == pytest.approx(2.0) and tests["H2"].mean == pytest.approx(
        0.001, rel=0.05
    )
    assert "B beats holding SPY" in tests["H3"].question and tests["H3"].p_holm is not None
    gross, costs, net = b.at_real_capital(1000) or (0, 0, 0)
    assert (
        gross == pytest.approx(b.gross_total * 1000)
        and costs == pytest.approx(b.spend_usd)
        and net == gross - costs
    )
    text = report_text(c, CFG)
    assert text.startswith("COMPARISON · experiment since 01 Oct 2026")
    assert "With your $1,000 instead:" in text and "C not running" in text
    assert any(line.startswith("Trading day ") and "mostly noise" in line for line in c.verdict)
    assert {r["series"] for r in c.curves()} == {"A", "B", "SPY"}


def test_the_decision_after_the_test_follows_the_rule(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    a_system(engine, 0.0001, 3.0, {"AAPL": 0.6, "MSFT": 0.7, "PFE": 0.8})
    a_system(b_engine, 0.0002, 3.0, {"AAPL": 0.8, "MSFT": 0.7, "PFE": 0.6})
    config = CFG.model_copy(
        update={"experiment": CFG.experiment.model_copy(update={"test_days": 21})}
    )
    c = compare({"A": engine, "B": b_engine}, config, START, NOW)
    assert c.phase == "decision"
    assert any(line.startswith("Decision: no system beats SPY after costs") for line in c.verdict)
    assert any("Not for real money" in line for line in c.verdict)  # $3 a day eats a $1,000 account


# --------------------------------------------------------------------------- the switch review


def test_the_switch_review(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    assert switch_for("vetoed by the review: stale") == "3 review" and switch_for(None) == "other"
    with make_session_factory(b_engine)() as session:
        for i, day in enumerate(weekdays(START, date(2026, 10, 30))):
            session.add(DecisionLog(decision_id=i, idea_id=i, cycle_id=i, as_of=at_new_york(day, time(7, 30)),
                                    ticker="AAPL", action="buy", horizon_days=5, judge_confidence=0.7, agreement=0.6,
                                    raw_confidence=0.66, a_status="proposed", b_status="rejected_by_risk",
                                    b_reason="vetoed by the review: the news is old", a_size=0.1, b_size=0.1,
                                    a_stop=1, b_stop=1, a_target=2, b_target=2))  # fmt: skip
        session.add(DecisionLog(decision_id=99, idea_id=99, cycle_id=99, as_of=at_new_york(START, time(7, 30)),
                                ticker="MSFT", action="buy", horizon_days=5, judge_confidence=0.7, agreement=0.6,
                                raw_confidence=0.66, a_status="proposed", b_status="proposed", a_size=0.1, b_size=0.05,
                                a_stop=1, b_stop=1, a_target=2, b_target=2))  # fmt: skip
        session.commit()
    lines = {line.switch: line for line in review(b_engine, CFG, START, NOW)}
    veto = lines["3 review"]
    assert len(veto.removed) == 22 and veto.advice.startswith(
        "recommend OFF: the trades it removed would have made money"
    )
    assert lines["sizing (2 risk, 3 review)"].resized == 1
    text = review_text(list(lines.values()))
    assert "3 review: removed 22" in text and "sizes only" in text


# --------------------------------------------------------------------------- start, report, weekly


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    monkeypatch.setenv("PAPERTRADER_CONFIG", str(tmp_path / "config.yaml"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_start_and_report_from_the_command_line(project: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    before = runner.invoke(app, ["experiment", "report"])
    assert before.exit_code == 0, before.output
    assert "has not started yet" in before.output
    started = runner.invoke(
        app, ["experiment", "start", "--date", "2026-12-07", "--clear-old-equity", "--yes"]
    )
    assert started.exit_code == 0, started.output
    assert (
        "Experiment start recorded: Mon 07 Dec 2026." in started.output
        and "A: 0 older equity" in started.output
    )
    after = runner.invoke(app, ["experiment", "report"])
    assert "COMPARISON · experiment since 07 Dec 2026" in after.output
    assert runner.invoke(app, ["experiment", "start", "--date", "7 Dec"]).exit_code == 1


def test_the_weekly_message(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    assert weekly_text({"A": engine, "B": b_engine}, CFG, NOW).endswith(
        "run `papertrader experiment start`."
    )
    set_start(engine, START, NOW)
    first_week = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    text = weekly_text({"A": engine, "B": b_engine}, CFG, first_week)
    assert text.startswith("COMPARISON") and "SWITCH REVIEW (system B" in text
    assert "SWITCH REVIEW" not in weekly_text(
        {"A": engine, "B": b_engine}, CFG, NOW
    )  # only monthly
