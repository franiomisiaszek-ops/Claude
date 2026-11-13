"""Tiny key/value store on top of the `system_state` table."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from papertrader.db.models import SystemState
from papertrader.db.types import utcnow

HEARTBEAT_KEY = "heartbeat.app"
TRADING_PAUSED_KEY = "trading.paused"
KILL_SWITCH_KEY = "trading.kill_switch"


def get_state(session: Session, key: str) -> dict[str, Any] | None:
    row = session.get(SystemState, key)
    return None if row is None else dict(row.value)


def set_state(session: Session, key: str, value: dict[str, Any]) -> None:
    row = session.get(SystemState, key)
    if row is None:
        session.add(SystemState(key=key, value=value))
    else:
        row.value = value
        row.updated_at = utcnow()
