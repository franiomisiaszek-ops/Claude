"""Pause / resume, shared by the trading engine, the CLI and the Telegram bot."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from papertrader.db.state import KILL_SWITCH_KEY, TRADING_PAUSED_KEY, get_state, set_state


def paused(sessions: sessionmaker[Session]) -> dict[str, Any] | None:
    with sessions() as session:
        state = get_state(session, TRADING_PAUSED_KEY)
    return state if state and state.get("paused") else None


def pause(sessions: sessionmaker[Session], reason: str, now: datetime) -> None:
    with sessions() as session:
        set_state(
            session, TRADING_PAUSED_KEY, {"paused": True, "reason": reason, "at": now.isoformat()}
        )
        session.commit()


def resume(sessions: sessionmaker[Session], now: datetime) -> None:
    """Allow new orders again. Also clears the kill switch."""
    with sessions() as session:
        set_state(session, TRADING_PAUSED_KEY, {"paused": False, "at": now.isoformat()})
        set_state(session, KILL_SWITCH_KEY, {"active": False, "at": now.isoformat()})
        session.commit()
