"""Notifications are written to an outbox table first and sent afterwards.

The trading engine only ever calls `queue()`. A sender (Telegram, Phase 6) picks up
pending rows and marks them sent. This way a Telegram outage can never block or
break trading, and every message is kept even if it could not be delivered.
The dedup key makes sure one event never produces two messages.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.db.models import Notification, NotificationStatus


def queue(session: Session, kind: str, text: str, dedup_key: str | None = None) -> bool:
    """Add a message unless one with the same dedup key exists. True if added."""
    if dedup_key is not None:
        exists = session.scalar(select(Notification.id).where(Notification.dedup_key == dedup_key))
        if exists is not None:
            return False
    session.add(Notification(kind=kind, text=text, dedup_key=dedup_key))
    session.flush()
    return True


def pending(session: Session, limit: int = 50) -> list[Notification]:
    return list(
        session.scalars(
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.id)
            .limit(limit)
        )
    )
