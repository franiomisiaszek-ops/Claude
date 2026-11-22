"""X6. A heads-up on Telegram before every order a system sends itself, with Skip and Go now.

How it works, for systems B and C (A's code is frozen; the hub writes A's warnings, as
information only, see papertrader.notify.ahead):

1. Before an order, the system records a warning (`order_warnings`, one row per order,
   in the system's own tables) and queues ONE Telegram message for the group of orders
   it is about to send, with a [Skip] button per order and one [Go now].
2. The hub sends it with the buttons. A tap writes "skipped" or "go" into the row.
3. When the order's time comes (or after Go now), the system looks at the row: skipped
   orders are not sent; everything else goes ahead. No answer means it goes ahead.

Every warning has a stable reference, so a second look at the same order never warns
twice: `idea:12` (a buy at the open), `exit:7:time:2026-11-23` (a sell; a skip lasts for
the rest of that day), `intraday:5`, `c:3:GLD:buy`. Nothing here sleeps: orders that are
still waiting are sent by the next run once their time has come (a one-minute check).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, Integer, MetaData, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from papertrader.config import WarningsConfig
from papertrader.db.base import NAMING_CONVENTION
from papertrader.db.types import JSONType, UTCDateTime
from papertrader.ingest.timeutil import NEW_YORK
from papertrader.notify.messages import money
from papertrader.notify.outbox import queue

WARN_KIND = "order_warning"  # a message with buttons (systems B and C)
INFO_KIND = "order_info"  # system A's warnings: information only, no buttons
WAITING, SKIPPED, GO, SENT = "waiting", "skipped", "go", "sent"


class WarnBase(DeclarativeBase):
    """Tables of systems B and C only, in their own schema (the freeze rule holds)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class OrderWarning(WarnBase):
    __tablename__ = "order_warnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ref: Mapped[str] = mapped_column(String(96), unique=True)
    group_key: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    side: Mapped[str] = mapped_column(String(8))  # BUY | SELL | SHORT | COVER
    ticker: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(
        String(32)
    )  # open | intraday | time | thesis | judge | rebalance
    status: Mapped[str] = mapped_column(String(12), index=True, default=WAITING)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    decided_by: Mapped[str | None] = mapped_column(String(16))  # you | timeout
    message_id: Mapped[str | None] = mapped_column(String(32))
    buttons_open: Mapped[bool] = mapped_column(Boolean, default=False)
    can_go_early: Mapped[bool] = mapped_column(Boolean, default=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")


@dataclass(frozen=True)
class Pending:
    """One order about to be sent, with what the message shows."""

    ref: str
    side: str  # BUY | SELL | SHORT | COVER
    ticker: str
    source: str
    shares: float | None = None
    price: str = ""  # "limit $101.20", "zone $99.50-$100.50", "at the market"
    target: float | None = None
    stop: float | None = None
    hold: str = ""  # expected holding time
    confidence: float | None = None
    reasons: tuple[str, ...] = ()
    against: str = ""
    details: dict[str, Any] = field(default_factory=dict)  # what the system needs to send it later


SOURCE_WORDS = {
    "open": "today's buys at the open",
    "intraday": "intraday scanner",
    "time": "holding period over",
    "thesis": "the news broke the reason for the trade",
    "judge": "the judge dropped it",
    "rebalance": "weekly rebalance",
}


def render(items: Sequence[Pending], due: datetime, *, buttons: bool, early: bool = True) -> str:
    """The message: one block per order, then what happens without an answer."""
    when = due.astimezone(NEW_YORK)
    head = f"HEADS-UP · {len(items)} order(s) at {when:%H:%M} New York" if len(items) > 1 else (
        f"HEADS-UP · {items[0].side} {items[0].ticker} at {when:%H:%M} New York")  # fmt: skip
    lines = [head]
    for item in items:
        lines.append("")
        size = f"{item.shares:g} shares" if item.shares else "shares as the risk rules allow"
        lines.append(
            f"{item.side} {item.ticker} · {size}" + (f" · {item.price}" if item.price else "")
        )
        levels = []
        if item.target:
            levels.append(f"target {money(item.target)}")
        if item.stop:
            levels.append(f"stop {money(item.stop)}")
        if item.hold:
            levels.append(f"hold {item.hold}")
        if item.confidence is not None:
            levels.append(f"confidence {item.confidence:.0%}")
        if levels:
            lines.append(" · ".join(levels))
        lines.append(f"Why ({SOURCE_WORDS.get(item.source, item.source)}):")
        lines += [f"• {reason}" for reason in item.reasons if reason] or ["• (no reason recorded)"]
        if item.against:
            lines.append(f"Strongest argument against: {item.against}")
    lines.append("")
    if buttons:
        lines.append("No answer = it goes ahead. Skip = not sent." + (
            " Go now = sent right away." if early else " Go now = confirms it (it can't go earlier)."))  # fmt: skip
    else:
        lines.append("Information only: system A is the frozen comparison, so it can't be skipped.")
    return "\n".join(lines)


def group_key(refs: Iterable[str], now: datetime) -> str:
    digest = hashlib.sha256(("|".join(sorted(refs)) + now.isoformat()).encode()).hexdigest()
    return digest[:16]


class Gate:
    """Warn before orders and say, when their time comes, which may be sent."""

    def __init__(self, sessions: sessionmaker[Session], cfg: WarningsConfig, *, now: Any) -> None:
        self.sessions = sessions
        self.cfg = cfg
        self.now = now  # a callable returning the current UTC time

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    def rows(self, refs: Iterable[str]) -> dict[str, OrderWarning]:
        wanted = list(refs)
        if not wanted:
            return {}
        with self.sessions() as session:
            found = session.scalars(select(OrderWarning).where(OrderWarning.ref.in_(wanted))).all()
            for row in found:
                session.expunge(row)
        return {r.ref: r for r in found}

    def warn(
        self, items: Sequence[Pending], *, due: datetime | None = None, early: bool = True
    ) -> list[str]:
        """Record and announce the orders not warned yet. Returns their references."""
        if not self.enabled or not items:
            return []
        now = self.now()
        known = self.rows(i.ref for i in items)
        fresh = [i for i in items if i.ref not in known]
        if not fresh:
            return []
        due = due or now + timedelta(minutes=self.cfg.minutes)
        key = group_key((i.ref for i in fresh), now)
        with self.sessions() as session:
            for item in fresh:
                session.add(OrderWarning(ref=item.ref, group_key=key, created_at=now, due_at=due, side=item.side,
                                         ticker=item.ticker, source=item.source, status=WAITING, can_go_early=early,
                                         details=item.details))  # fmt: skip
            queue(
                session,
                WARN_KIND,
                render(fresh, due, buttons=True, early=early),
                dedup_key=f"warn:{key}",
            )
            session.commit()
        return [i.ref for i in fresh]

    def decide(
        self, items: Sequence[Pending], *, early: bool = True
    ) -> tuple[set[str], set[str], set[str]]:
        """(may go now, skipped, still waiting). Orders never warned are warned now and wait."""
        if not self.enabled:
            return {i.ref for i in items}, set(), set()
        self.warn(items, early=early)
        now = self.now()
        rows = self.rows(i.ref for i in items)
        ready, skipped, waiting = set(), set(), set()
        for item in items:
            row = rows.get(item.ref)
            if row is None:
                ready.add(item.ref)  # cannot happen; never block an order on a missing row
            elif row.status == SKIPPED:
                skipped.add(item.ref)
            elif row.status == SENT or row.due_at <= now or (row.status == GO and row.can_go_early):
                ready.add(item.ref)
            else:
                waiting.add(item.ref)
        return ready, skipped, waiting

    def mark_sent(self, refs: Iterable[str]) -> None:
        refs = list(refs)
        if not refs:
            return
        now = self.now()
        with self.sessions() as session:
            for row in session.scalars(select(OrderWarning).where(OrderWarning.ref.in_(refs))):
                if row.status in (WAITING, GO):
                    if row.status == WAITING:
                        row.decided_at, row.decided_by = now, "timeout"
                    row.status = SENT
            session.commit()

    def due(self, prefix: str = "") -> list[OrderWarning]:
        """Waiting orders whose time has come, or that you sent on with Go now."""
        now = self.now()
        with self.sessions() as session:
            query = select(OrderWarning).where(
                OrderWarning.status.in_((WAITING, GO)),
                (OrderWarning.due_at <= now)
                | ((OrderWarning.status == GO) & OrderWarning.can_go_early.is_(True)),
            )
            if prefix:
                query = query.where(OrderWarning.ref.startswith(prefix))
            found = list(session.scalars(query.order_by(OrderWarning.id)).all())
            for row in found:
                session.expunge(row)
        return found


# --------------------------------------------------------------------------- the hub's side


def keyboard(rows: Sequence[OrderWarning], system: str) -> list[list[dict[str, str]]] | None:
    """[Skip X] for each order of a group not sent yet, and one [Go now]. None when all is
    decided. The button data names the system: "B|ow:12:skip", "C|og:<group>:go"."""
    open_rows = [r for r in rows if r.status in (WAITING, GO)]
    waiting = [r for r in open_rows if r.status == WAITING]
    if not open_rows:
        return None
    buttons = [[{"text": f"Skip {r.side.lower()} {r.ticker}" if len(rows) > 1 else "Skip",
                 "callback_data": f"{system}|ow:{r.id}:skip"}] for r in open_rows]  # fmt: skip
    if waiting:
        buttons.append([{"text": "Go now", "callback_data": f"{system}|og:{rows[0].group_key}:go"}])
    return buttons


def group_rows(session: Session, key: str) -> list[OrderWarning]:
    return list(
        session.scalars(
            select(OrderWarning).where(OrderWarning.group_key == key).order_by(OrderWarning.id)
        )
    )


def tap(session: Session, data: str, now: datetime) -> tuple[str, str | None]:
    """Apply a button tap. Returns (short answer for you, group key to refresh)."""
    parts = data.split(":")
    if len(parts) != 3:
        return "That button is not understood.", None
    kind, ident, action = parts
    if kind == "ow" and action == "skip" and ident.isdigit():
        row = session.get(OrderWarning, int(ident))
        if row is None:
            return "That order is no longer known.", None
        if row.status == SENT:
            return (
                f"Too late: the {row.side.lower()} of {row.ticker} was already sent.",
                row.group_key,
            )
        if row.status == SKIPPED:
            return f"{row.ticker} is already skipped.", row.group_key
        row.status, row.decided_at, row.decided_by = SKIPPED, now, "you"
        return f"Skipped: {row.side.lower()} {row.ticker} will not be sent.", row.group_key
    if kind == "og" and action == "go":
        rows = group_rows(session, ident)
        waiting = [r for r in rows if r.status == WAITING]
        if not waiting:
            return "Nothing is waiting any more.", ident if rows else None
        for row in waiting:
            row.status, row.decided_at, row.decided_by = GO, now, "you"
        early = all(r.can_go_early for r in waiting)
        names = ", ".join(f"{r.side.lower()} {r.ticker}" for r in waiting)
        return (f"Go: {names} within a minute." if early else
                f"Confirmed: {names} at the planned time (it can't go earlier)."), ident  # fmt: skip
    return "That button is not understood.", None
