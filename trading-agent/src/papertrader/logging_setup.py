"""Structured logging: one JSON object per line on stdout.

Usage:
    configure_logging("INFO", "json")
    log = logging.getLogger(__name__)
    log.info("order placed", extra={"ticker": "NVDA", "qty": 20})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Literal

# Attributes every LogRecord has; anything else was passed via `extra=`.
_STANDARD_ATTRS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

# Field names that must never be written to logs, whatever their value.
_SECRET_KEY_HINTS = ("api_key", "apikey", "secret", "token", "password", "authorization")


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_KEY_HINTS):
        return "***"
    return value


class JsonFormatter(logging.Formatter):
    """Formats each record as a single-line JSON object with a UTC timestamp."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: Literal["json", "console"] = "json") -> None:
    """Replace the root logger's handlers with a single stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party libraries are chatty at INFO; keep them at WARNING.
    for noisy in ("httpx", "httpcore", "apscheduler", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
