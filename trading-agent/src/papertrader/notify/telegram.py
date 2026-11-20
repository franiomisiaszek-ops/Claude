"""Telegram Bot API: sending messages and reading commands.

Plain HTTPS calls to https://api.telegram.org. The bot token is part of every URL,
so URLs never appear in errors or logs here (and the log filter masks them too).
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import TelegramConfig
from papertrader.db.models import Notification, NotificationStatus
from papertrader.db.types import utcnow
from papertrader.logging_setup import install_url_redaction

log = logging.getLogger(__name__)
install_url_redaction()

API = "https://api.telegram.org"
MAX_LENGTH = 4000  # Telegram's limit is 4096 characters per message


class TelegramError(RuntimeError):
    def __init__(
        self, message: str, *, retry_after: float | None = None, permanent: bool = False
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.permanent = permanent


def to_html(text: str) -> str:
    """Escape everything; make the first line bold, as a title."""
    first, _, rest = text.partition("\n")
    escaped = f"<b>{html.escape(first)}</b>"
    return escaped + ("\n" + html.escape(rest) if rest else "")


def split_message(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Split on line breaks so no part exceeds the limit."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts or [""]


class TelegramClient:
    def __init__(
        self, token: str, *, http: httpx.Client | None = None, timeout: float = 40.0
    ) -> None:
        self._token = token
        self._http = http or httpx.Client(timeout=timeout)

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        try:
            response = self._http.post(f"{API}/bot{self._token}/{method}", json=payload,
                                       timeout=timeout or self._http.timeout)  # fmt: skip
        except httpx.TransportError as exc:
            raise TelegramError(f"could not reach Telegram ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"Telegram answered HTTP {response.status_code}") from None
        if body.get("ok"):
            return body.get("result")
        description = str(body.get("description") or f"HTTP {response.status_code}")
        retry_after = (body.get("parameters") or {}).get("retry_after")
        status = int(body.get("error_code") or response.status_code)
        # 400 (bad chat id, bad text), 401 (bad token) and 403 (bot blocked) will not fix themselves.
        raise TelegramError(f"Telegram refused: {description}", retry_after=retry_after,
                            permanent=status in (400, 401, 403, 404))  # fmt: skip

    def send(self, chat_id: str, text: str) -> list[int]:
        ids = []
        for part in split_message(text):
            result = self._call("sendMessage", {
                "chat_id": chat_id, "text": to_html(part), "parse_mode": "HTML",
                "disable_web_page_preview": True,
            })  # fmt: skip
            ids.append(int(result["message_id"]))
        return ids

    def updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return list(self._call("getUpdates", payload, timeout=timeout + 15))

    def me(self) -> dict[str, Any]:
        return dict(self._call("getMe", {}))

    def close(self) -> None:
        self._http.close()


@dataclass
class FlushReport:
    sent: int = 0
    failed: int = 0
    dropped: int = 0
    waiting: bool = False  # stopped early (rate limit or network); the rest goes next time
    setup_problem: str | None = None  # wrong token or chat id: nothing can be sent until fixed


def _setup_problem(exc: TelegramError) -> bool:
    text = str(exc).lower()
    return exc.permanent and any(
        hint in text for hint in ("unauthorized", "chat not found", "blocked by the user",
                                  "bot was kicked", "not enough rights")
    )  # fmt: skip


def flush_outbox(
    sessions: sessionmaker[Session], client: TelegramClient, chat_id: str, cfg: TelegramConfig,
    *, now: datetime | None = None, limit: int = 30, prefix: str = "",
) -> FlushReport:  # fmt: skip
    """Send pending messages, oldest first. A temporary problem stops the run (so the
    order is kept); a permanent one marks that message failed and moves on."""
    report = FlushReport()
    now = now or utcnow()
    oldest = now - timedelta(hours=cfg.max_age_hours)
    with sessions() as session:
        rows = session.scalars(
            select(Notification).where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.id).limit(limit)
        ).all()  # fmt: skip
        for row in rows:
            if row.created_at < oldest:
                row.status, row.error = NotificationStatus.FAILED, "too old to send"
                report.dropped += 1
                continue
            try:
                ids = client.send(chat_id, prefix + row.text)
            except TelegramError as exc:
                if _setup_problem(exc):
                    # Not this message's fault: keep everything queued and stop.
                    report.setup_problem, report.waiting = str(exc), True
                    break
                row.attempts = (row.attempts or 0) + 1
                row.error = str(exc)[:500]
                if exc.permanent or row.attempts >= cfg.max_attempts:
                    row.status = NotificationStatus.FAILED
                    report.failed += 1
                    continue
                report.waiting = True
                break
            row.status, row.sent_at = NotificationStatus.SENT, utcnow()
            row.telegram_message_id = ",".join(map(str, ids))[:32]
            row.error = None
            report.sent += 1
        session.commit()
    return report


def pending_count(session: Session) -> int:
    return len(
        session.scalars(
            select(Notification.id).where(Notification.status == NotificationStatus.PENDING)
        ).all()
    )
