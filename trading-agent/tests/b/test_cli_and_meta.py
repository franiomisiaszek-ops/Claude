"""System B's commands, and meta-labelling (switch 10)."""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from papertrader.b.meta import train
from papertrader.b.outcomes import IdeaSample, Resolved
from papertrader.cli import app
from papertrader.config import MetaLabelSwitch, get_settings

from ..conftest import PROJECT_ROOT
from ..research.helpers import AS_OF

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(PROJECT_ROOT / "config" / "b.yaml", tmp_path / "b.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    monkeypatch.setenv("PAPERTRADER_CONFIG", str(tmp_path / "b.yaml"))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_switches_and_the_insider_backtest_from_the_command_line(project: Path) -> None:
    assert (
        runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    )  # system A sets up the shared tables
    shown = runner.invoke(app, ["--system", "B", "switches"])
    assert shown.exit_code == 0, shown.output
    assert "ON   3  review that may only remove or shrink trades" in shown.output
    assert "off  5  insider cluster buying" in shown.output
    assert "Left-out agents  : none" in shown.output
    tested = runner.invoke(app, ["--system", "B", "backtest", "insiders"])
    assert tested.exit_code == 0, tested.output
    assert (
        "Clusters found: 0" in tested.output
        and "Verdict: keep OFF: only 0 clusters" in tested.output
    )
    checked = runner.invoke(app, ["--system", "B", "paid-data-check"])
    assert "nothing to check (and nothing is paid for)" in checked.output
    scanned = runner.invoke(app, ["--system", "B", "scanner"])
    assert scanned.exit_code == 0, scanned.output
    assert "Scanner: ON, mode shadow, every 5 minutes 09:45-15:30 New York" in scanned.output
    assert "Today: 0 plan(s)" in scanned.output


def ideas(n: int, rule: str) -> list[Resolved]:
    out = []
    for i in range(n):
        buy = i % 2 == 0
        judge = 0.8 if i % 3 else 0.6
        sample = IdeaSample(AS_OF - timedelta(days=n - i), "AAPL", "buy" if buy else "sell_short", 5, 0.7,
                            "A", {"judge": judge, "agreement": 0.6})  # fmt: skip
        worked = buy if rule == "buys work" else (i * 7919) % 11 < 5
        out.append(Resolved(sample, 0.01 if worked else -0.01, worked))
    return out


def test_meta_labelling_waits_learns_and_refuses_to_add_noise() -> None:
    cfg = MetaLabelSwitch(enabled=True, min_ideas=150)
    assert not train(ideas(100, "buys work"), cfg).active
    model = train(ideas(300, "buys work"), cfg)
    assert model.active, model.note
    buy = IdeaSample(AS_OF, "AAPL", "buy", 5, 0.7, "B", {"judge": 0.8, "agreement": 0.6})
    short = IdeaSample(AS_OF, "AAPL", "sell_short", 5, 0.7, "B", {"judge": 0.8, "agreement": 0.6})
    assert (model.probability(buy) or 0) > 0.7 > 0.3 > (model.probability(short) or 1)
    noise = train(ideas(300, "noise"), cfg)
    assert not noise.active and noise.note.startswith(
        "not used: no better than the blend or the plain hit rate"
    )
