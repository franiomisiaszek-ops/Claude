from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from papertrader.cli import app

from ..conftest import PROJECT_ROOT

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    return tmp_path


def test_jobs_list_shows_the_schedule(project: Path) -> None:
    result = runner.invoke(app, ["jobs", "list"])
    assert result.exit_code == 0, result.output
    assert "market is" in result.output  # open or closed today (no Alpaca: weekdays assumed)
    for line in (
        "ingest            06:30",
        "monitor           every 10 min",
        "weekly            sat 09:00",
    ):
        assert line in result.output


def test_jobs_run_records_the_run_and_history_shows_it(project: Path) -> None:
    result = runner.invoke(app, ["jobs", "run", "research_agents"])
    assert (
        result.exit_code == 1
        and "research_agents: failed - JobFailed: ANTHROPIC_API_KEY is not set" in result.output
    )
    assert "job 'research_agents' failed" in result.output  # the alert that goes to Telegram
    result = runner.invoke(app, ["jobs", "run", "morning_brief"])
    assert result.exit_code == 0 and "morning_brief: succeeded - queued" in result.output
    history = runner.invoke(app, ["jobs", "history"])
    assert (
        "research_agents   failed" in history.output
        and "morning_brief     succeeded" in history.output
    )
    assert runner.invoke(app, ["jobs", "list"]).output.count("succeeded") == 1


def test_jobs_run_refuses_unknown_names(project: Path) -> None:
    result = runner.invoke(app, ["jobs", "run", "buy_everything"])
    assert result.exit_code == 1 and "unknown job buy_everything" in result.output
