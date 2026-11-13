"""Time helpers. Everything stored is UTC; market rules are in New York time."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def at_new_york(day: date, clock: time) -> datetime:
    """`clock` on `day` in New York, returned as UTC (handles daylight saving)."""
    return datetime.combine(day, clock, tzinfo=NEW_YORK).astimezone(UTC)


def new_york_date(moment: datetime) -> date:
    return moment.astimezone(NEW_YORK).date()


def parse_utc(text: str) -> datetime:
    """Parse an ISO-8601 timestamp; a trailing Z or missing offset means UTC."""
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def daily_bar_available_at(session_day: date) -> datetime:
    """When a daily bar is final and visible: the close plus a 15-minute data delay.

    Half days close at 13:00, so 16:15 is late for those. Late is safe; early would
    let a decision see a price before it existed.
    """
    return at_new_york(session_day, MARKET_CLOSE) + timedelta(minutes=15)


def session_part(moment: datetime) -> str:
    """bmo = before market open, amc = after market close, dmh = during market hours."""
    local = moment.astimezone(NEW_YORK).time()
    if local < MARKET_OPEN:
        return "bmo"
    if local >= MARKET_CLOSE:
        return "amc"
    return "dmh"
