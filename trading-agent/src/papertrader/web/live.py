"""Live account numbers for the web app, written by the hub, so the web app needs no keys.

Every 2 minutes while the market is open, the hub (which already holds each system's
paper keys) asks each paper account for its equity and positions and stores them in
that system's own state table under `web.live`. The web app only reads them, and shows
how old they are. For system A this is data written next to A's tables by the hub, like
the hub's Telegram offset; A's frozen code never reads it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from papertrader.db.state import get_state, set_state

log = logging.getLogger(__name__)

LIVE_KEY = "web.live"
STALE_MINUTES = 15  # older numbers are shown as "not live"


@dataclass(frozen=True)
class LivePosition:
    symbol: str
    qty: float
    avg_entry: float
    price: float

    @property
    def pnl_usd(self) -> float:
        return (self.price - self.avg_entry) * self.qty

    @property
    def pnl_pct(self) -> float | None:
        if not self.avg_entry:
            return None
        change = self.price / self.avg_entry - 1
        return change if self.qty >= 0 else -change


@dataclass
class Live:
    at: datetime | None = None
    equity: float | None = None
    last_equity: float | None = None
    cash: float | None = None
    spy: float | None = None
    positions: dict[str, LivePosition] = field(default_factory=dict)
    error: str | None = None

    @property
    def day_change_pct(self) -> float | None:
        if self.equity is None or not self.last_equity:
            return None
        return self.equity / self.last_equity - 1

    def age_minutes(self, now: datetime) -> float | None:
        return (now - self.at).total_seconds() / 60 if self.at else None

    def fresh(self, now: datetime) -> bool:
        age = self.age_minutes(now)
        return age is not None and age <= STALE_MINUTES


def capture(broker: Any, benchmark: str, now: datetime) -> dict[str, Any]:
    """One snapshot of a paper account, as stored."""
    account = broker.account()
    positions = [p for p in broker.positions() if p.qty]
    spy = broker.latest_prices([benchmark]).get(benchmark)
    return {
        "at": now.isoformat(),
        "equity": account.equity,
        "last_equity": account.last_equity,
        "cash": account.cash,
        "spy": float(spy) if spy else None,
        "positions": [
            {
                "symbol": p.symbol,
                "qty": p.qty,
                "avg_entry": p.avg_entry_price,
                "price": p.current_price,
            }
            for p in positions
        ],
    }


def write_all(
    views: Mapping[Any, Any],
    brokers: Mapping[Any, Callable[[], Any]],
    now: datetime,
) -> dict[str, str]:
    """Store a snapshot for every system that has a broker. Returns problems by system."""
    problems: dict[str, str] = {}
    for name, view in views.items():
        factory = brokers.get(name)
        broker = factory() if factory else None
        if broker is None:
            continue
        try:
            value = capture(broker, view.config.universe.benchmark, now)
        except Exception as exc:  # one account failing must not stop the others
            problems[name] = type(exc).__name__
            log.warning("live snapshot failed", extra={"system": name, "error": type(exc).__name__})
            continue
        with view.sessions() as session:
            set_state(session, LIVE_KEY, value)
            session.commit()
    return problems


def read(sessions: sessionmaker[Session]) -> Live:
    with sessions() as session:
        raw = get_state(session, LIVE_KEY)
    if not raw:
        return Live()
    try:
        return Live(
            at=datetime.fromisoformat(raw["at"]),
            equity=raw.get("equity"),
            last_equity=raw.get("last_equity"),
            cash=raw.get("cash"),
            spy=raw.get("spy"),
            positions={
                p["symbol"]: LivePosition(
                    p["symbol"], float(p["qty"]), float(p["avg_entry"]), float(p["price"])
                )
                for p in raw.get("positions", [])
            },
        )
    except (KeyError, TypeError, ValueError) as exc:
        return Live(error=f"unreadable live numbers ({exc})")
