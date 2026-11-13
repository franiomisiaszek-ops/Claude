from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

from papertrader.logging_setup import configure_logging


def _last_line(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    line = capsys.readouterr().out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert isinstance(parsed, dict)
    return parsed


def test_json_log_line_has_utc_timestamp_and_extras(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger("papertrader.test").info("order placed", extra={"ticker": "NVDA", "qty": 20})

    record = _last_line(capsys)
    assert record["msg"] == "order placed"
    assert record["level"] == "INFO"
    assert record["logger"] == "papertrader.test"
    assert record["ticker"] == "NVDA"
    assert record["qty"] == 20
    ts = datetime.fromisoformat(str(record["ts"]))
    assert ts.utcoffset() is not None and ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_secret_looking_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    logging.getLogger("papertrader.test").info(
        "calling api",
        extra={"api_key": "sk-ant-real", "telegram_bot_token": "123:abc", "model": "m"},
    )
    out = capsys.readouterr().out
    assert "sk-ant-real" not in out
    assert "123:abc" not in out
    assert json.loads(out.strip().splitlines()[-1])["model"] == "m"


def test_exceptions_are_included(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO", "json")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("papertrader.test").exception("job failed")
    record = _last_line(capsys)
    assert "RuntimeError: boom" in str(record["exc"])


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING", "json")
    logging.getLogger("papertrader.test").info("quiet")
    assert capsys.readouterr().out == ""
