"""CLI, startup, heartbeat and dashboard-status tests."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from papertrader import __version__
from papertrader.cli import app
from papertrader.dashboard.data import load_status
from papertrader.db.session import make_engine, make_session_factory, session_scope
from papertrader.db.state import HEARTBEAT_KEY, get_state, set_state
from papertrader.db.types import utcnow
from papertrader.runtime import bootstrap, migration_status
from papertrader.scheduler.service import beat, build_scheduler

from .conftest import PROJECT_ROOT

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working folder with the shipped config.yaml and a SQLite database."""
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    return tmp_path


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_check_passes_and_reports_missing_keys_without_values(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret")
    result = runner.invoke(app, ["check"])

    assert result.exit_code == 0, result.output
    assert "https://paper-api.alpaca.markets" in result.output
    assert "PAPER only" in result.output
    assert "sk-ant-super-secret" not in result.output
    assert "set      ANTHROPIC_API_KEY" in result.output
    assert "MISSING  ALPACA_API_KEY" in result.output
    assert "not created yet" in result.output


def test_db_upgrade_then_check_reports_schema_up_to_date(project: Path) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_check_refuses_a_live_trading_url(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "Refusing to run" in result.output


def test_run_refuses_a_live_api_key_before_doing_anything(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "AKLIVEKEY123")
    result = runner.invoke(app, ["run"])
    assert result.exit_code == 1
    assert "LIVE" in result.output
    assert not (project / "data" / "app.db").exists()  # guard ran before the database was touched


def test_check_fails_clearly_on_a_broken_config(project: Path) -> None:
    (project / "config.yaml").write_text("risk:\n  max_open_positions: 0\n")
    result = runner.invoke(app, ["check"])
    assert result.exit_code == 1
    assert "config.yaml is invalid" in result.output


def test_bootstrap_with_migrate_creates_the_schema(project: Path) -> None:
    runtime = bootstrap(migrate=True)
    current, head = migration_status(runtime)
    assert current == head is not None
    runtime.engine.dispose()


def test_heartbeat_is_recorded_and_scheduled(project: Path) -> None:
    runtime = bootstrap(migrate=True)
    beat(runtime.session_factory)
    with session_scope(runtime.session_factory) as session:
        state = get_state(session, HEARTBEAT_KEY)
    assert state is not None and state["version"] == __version__

    scheduler = build_scheduler(runtime)
    assert [job.id for job in scheduler.get_jobs()] == ["heartbeat"]
    assert str(scheduler.timezone) == "America/New_York"
    runtime.engine.dispose()


def test_dashboard_status_before_and_after_migration(tmp_path: Path, engine: Engine) -> None:
    empty = make_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert load_status(empty).schema_ready is False
    empty.dispose()

    status = load_status(engine)
    assert status.schema_ready is True
    assert status.app_alive is False
    assert status.table_rows["trades"] == 0

    factory = make_session_factory(engine)
    with session_scope(factory) as session:
        set_state(session, HEARTBEAT_KEY, {"at": utcnow().isoformat()})
    assert load_status(engine).app_alive is True

    with session_scope(factory) as session:
        set_state(session, HEARTBEAT_KEY, {"at": (utcnow() - timedelta(minutes=10)).isoformat()})
    stale = load_status(engine)
    assert stale.app_alive is False
    assert stale.table_rows["system_state"] == 1


def test_dashboard_page_renders_without_errors(project: Path) -> None:
    from streamlit.testing.v1 import AppTest

    bootstrap(migrate=True).engine.dispose()
    page = AppTest.from_file(str(PROJECT_ROOT / "src/papertrader/dashboard/app.py")).run(timeout=30)
    assert not page.exception
    assert not page.error
    assert page.title[0].value == "papertrader"
    assert "PAPER TRADING ONLY" in page.caption[0].value


def test_healthcheck_follows_the_heartbeat(project: Path) -> None:
    from papertrader.healthcheck import main

    runtime = bootstrap(migrate=True)
    assert main() == 1  # no heartbeat yet
    beat(runtime.session_factory)
    assert main() == 0
    runtime.engine.dispose()
