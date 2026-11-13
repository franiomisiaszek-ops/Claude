from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from papertrader.config import (
    GICS_SECTORS,
    AppConfig,
    ConfigError,
    Settings,
    get_config,
    load_config,
)

from .conftest import PROJECT_ROOT

SHIPPED_CONFIG = PROJECT_ROOT / "config.yaml"


def _shipped() -> dict[str, Any]:
    data = yaml.safe_load(SHIPPED_CONFIG.read_text())
    assert isinstance(data, dict)
    return data


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_shipped_config_loads_and_matches_the_agreed_decisions() -> None:
    cfg = load_config(SHIPPED_CONFIG)
    assert cfg.universe.index == "sp500"
    assert cfg.budget.monthly_usd == 100
    assert cfg.judge.model == "claude-opus-5-5"
    assert cfg.judge.effort == "high"
    assert cfg.judge.confidence_threshold == 0.65
    assert cfg.agents.defaults.model == "claude-sonnet-5-5"
    assert cfg.agents.defaults.effort == "medium"
    assert cfg.schedule.monitor_interval_minutes == 10
    assert cfg.schedule.place_orders == time(9, 35)
    assert cfg.backfill.years == 5


def test_shipped_risk_limits_match_the_spec() -> None:
    risk = load_config(SHIPPED_CONFIG).risk
    assert risk.max_position_pct == 0.10
    assert risk.max_open_positions == 10
    assert risk.max_sector_pct == 0.30
    assert risk.allow_shorts is False
    assert risk.daily_loss_kill_switch_pct == 0.03


def test_there_are_eleven_gics_sectors() -> None:
    assert len(GICS_SECTORS) == 11
    assert len(set(GICS_SECTORS)) == 11


def test_defaults_alone_are_not_enough_because_models_need_prices() -> None:
    with pytest.raises(ValueError, match=r"budget\.prices has no entry"):
        AppConfig()


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["risk"]["max_positon_pct"] = 0.2  # typo
    with pytest.raises(ConfigError, match="max_positon_pct"):
        load_config(_write(tmp_path, data))


def test_missing_file_gives_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_invalid_yaml_gives_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("risk: [unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_orders_before_0935_are_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["schedule"]["place_orders"] = "09:30"
    with pytest.raises(ConfigError, match="09:35"):
        load_config(_write(tmp_path, data))


def test_limits_that_would_need_leverage_are_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["risk"]["max_position_pct"] = 0.25
    data["risk"]["max_open_positions"] = 10  # 250% of equity
    with pytest.raises(ConfigError, match="leverage"):
        load_config(_write(tmp_path, data))


def test_blend_weights_must_sum_to_one(tmp_path: Path) -> None:
    data = _shipped()
    data["judge"]["blend"] = {"judge": 0.5, "model": 0.5, "agreement": 0.5}
    with pytest.raises(ConfigError, match="sum to 1"):
        load_config(_write(tmp_path, data))


def test_model_without_a_price_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["judge"]["model"] = "claude-some-new-model"
    with pytest.raises(ConfigError, match="claude-some-new-model"):
        load_config(_write(tmp_path, data))


def test_unknown_effort_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["agents"]["defaults"]["effort"] = "extreme"
    with pytest.raises(ConfigError, match="effort"):
        load_config(_write(tmp_path, data))


def test_per_agent_override_merges_with_defaults(tmp_path: Path) -> None:
    data = _shipped()
    data["agents"]["overrides"] = {
        "sector_analyst": {"model": "claude-haiku-5-5", "effort": "low"},
        "contrarian": {"enabled": False},
    }
    cfg = load_config(_write(tmp_path, data))

    sector = cfg.agents.for_agent("sector_analyst")
    assert (sector.model, sector.effort, sector.max_tokens) == ("claude-haiku-5-5", "low", 16000)
    assert cfg.agents.for_agent("contrarian").enabled is False
    assert cfg.agents.for_agent("macro") == cfg.agents.defaults
    assert cfg.models_in_use() == {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"}


def test_override_for_unknown_agent_is_rejected(tmp_path: Path) -> None:
    data = _shipped()
    data["agents"]["overrides"] = {"astrology": {"effort": "max"}}
    with pytest.raises(ConfigError, match="astrology"):
        load_config(_write(tmp_path, data))


def test_config_is_immutable() -> None:
    cfg = load_config(SHIPPED_CONFIG)
    with pytest.raises(ValueError, match="frozen"):
        cfg.risk.allow_shorts = True  # type: ignore[misc]


def test_get_config_reads_the_path_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _shipped()
    data["budget"]["monthly_usd"] = 42
    monkeypatch.setenv("PAPERTRADER_CONFIG", str(_write(tmp_path, data)))
    assert get_config().budget.monthly_usd == 42


# ----------------------------------------------------------------------------- settings / .env


def test_settings_read_dotenv_and_hide_secrets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-test-123\nALPACA_API_KEY=PKTEST\nFINNHUB_API_KEY=\nLOG_LEVEL=\n"
    )
    settings = Settings()

    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-ant-test-123"
    assert "sk-ant-test-123" not in repr(settings)
    assert "sk-ant-test-123" not in settings.model_dump_json()
    assert settings.finnhub_api_key is None  # blank line means "not provided"
    assert settings.log_level == "INFO"  # blank falls back to the default

    status = settings.secret_status()
    assert status["ANTHROPIC_API_KEY"] is True
    assert status["FINNHUB_API_KEY"] is False
    assert all(isinstance(value, bool) for value in status.values())


def test_env_example_lists_every_setting() -> None:
    example = (PROJECT_ROOT / ".env.example").read_text()
    keys = {
        line.split("=", 1)[0] for line in example.splitlines() if "=" in line and line[0] != "#"
    }
    for field in Settings.model_fields:
        assert field.upper() in keys, f"{field.upper()} is missing from .env.example"


def test_env_example_contains_no_real_values() -> None:
    for line in (PROJECT_ROOT / ".env.example").read_text().splitlines():
        if line.startswith(("ANTHROPIC_", "ALPACA_", "TELEGRAM_", "FINNHUB_", "FRED_")):
            assert line.split("=", 1)[1] == "", (
                f"{line.split('=')[0]} must be blank in .env.example"
            )
