"""Structured logging: one JSON object per line on stdout.

Usage:
    configure_logging("INFO", "json")
    log = logging.getLogger(__name__)
    log.info("order placed", extra={"ticker": "NVDA", "qty": 20})
"""

from __future__ import annotations

import json
import logging
import re
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


_SECRET_IN_URL = re.compile(r"(?i)([?&](?:api_?key|token|secret|key)=)[^&\s\"']+")


def redact_url_secrets(text: str) -> str:
    """Mask API keys that some services (FRED) require in the URL itself."""
    return _SECRET_IN_URL.sub(r"\1***", text)


class _UrlSecretFilter(logging.Filter):
    """Third-party HTTP libraries log full URLs; strip keys from them before anything is written."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_url_secrets(str(record.msg))
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_url_secrets(str(arg)) if not isinstance(arg, int | float) else arg
                for arg in record.args
            )
        return True


_URL_LOGGERS = ("httpx", "httpcore", "urllib3")


def install_url_redaction() -> None:
    """Idempotent. Called on import of the HTTP layer, so it works whatever the log setup is."""
    for name in _URL_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, _UrlSecretFilter) for f in logger.filters):
            logger.addFilter(_UrlSecretFilter())


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

    install_url_redaction()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Third-party libraries are chatty at INFO; keep them at WARNING.
    for noisy in ("httpx", "httpcore", "apscheduler", "alembic.runtime.migration"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
