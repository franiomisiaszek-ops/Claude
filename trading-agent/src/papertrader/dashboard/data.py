"""Queries behind the dashboard. Plain functions so they can be tested without Streamlit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.orm import Session

from papertrader.db.base import Base
from papertrader.db.state import HEARTBEAT_KEY, get_state
from papertrader.db.types import utcnow


@dataclass(frozen=True)
class SystemStatus:
    schema_ready: bool
    heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    table_rows: dict[str, int]

    @property
    def app_alive(self) -> bool:
        return self.heartbeat_age_seconds is not None and self.heartbeat_age_seconds < 180


def load_status(engine: Engine) -> SystemStatus:
    existing = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables)
    if not expected <= existing:
        return SystemStatus(False, None, None, {})

    with Session(engine) as session:
        counts = {
            name: session.scalar(select(func.count()).select_from(table)) or 0
            for name, table in sorted(Base.metadata.tables.items())
        }
        heartbeat = get_state(session, HEARTBEAT_KEY)

    at = datetime.fromisoformat(heartbeat["at"]) if heartbeat else None
    age = (utcnow() - at).total_seconds() if at else None
    return SystemStatus(True, at, age, counts)
