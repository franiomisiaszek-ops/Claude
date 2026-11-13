"""Earnings calendar with EPS / revenue actuals and estimates, from Finnhub.

Without a Finnhub key the system still knows *when* results came out, from the
8-K filings handled in `sec.py`; it just has no estimates to compare against.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from papertrader.db.models import EarningsEvent
from papertrader.db.types import utcnow
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.http import FetchError
from papertrader.ingest.store import upsert
from papertrader.ingest.timeutil import at_new_york, new_york_date

CALENDAR_URL = "https://finnhub.io/api/v1/calendar/earnings"
_UPDATE = ("time_of_day", "fiscal_year", "fiscal_quarter", "eps_estimate", "eps_actual",
           "revenue_estimate", "revenue_actual", "actual_available_at")  # fmt: skip


def results_known_at(report_date: date, hour: str | None) -> datetime:
    """A deliberately late estimate of when the numbers were public.

    Finnhub only says "before open" or "after close", not the minute. Assuming a
    late time keeps a backtest from using results before they existed.
    """
    if hour == "bmo":
        return at_new_york(report_date, time(9, 30))
    return at_new_york(report_date, time(20, 0))


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def earnings_row(item: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    ticker = str(item.get("symbol") or "").upper()
    raw_date = str(item.get("date") or "")
    if not ticker or not raw_date:
        return None
    report_date = date.fromisoformat(raw_date[:10])
    hour = str(item.get("hour") or "").lower() or None
    eps_actual = _float(item.get("epsActual"))
    revenue_actual = _float(item.get("revenueActual"))
    known_at = results_known_at(report_date, hour)
    reported = (eps_actual is not None or revenue_actual is not None) and known_at <= now
    upcoming = report_date >= new_york_date(now)
    return {
        "source": "finnhub", "ticker": ticker, "report_date": report_date,
        "time_of_day": hour, "fiscal_year": item.get("year"), "fiscal_quarter": item.get("quarter"),
        "eps_estimate": _float(item.get("epsEstimate")),
        "eps_actual": eps_actual if reported else None,
        "revenue_estimate": _float(item.get("revenueEstimate")),
        "revenue_actual": revenue_actual if reported else None,
        "actual_available_at": known_at if reported else None,
        # For past events we cannot know when the date was first announced, so we only
        # claim the entry was known once the results were out.
        "available_at": now if upcoming else known_at,
        "updated_at": utcnow(),
    }  # fmt: skip


async def ingest_earnings(
    ctx: IngestContext, tickers: Sequence[str], *, start: date, per_symbol: bool
) -> StepStats:
    """`per_symbol=True` for a backfill (one request per ticker); False for the daily refresh."""
    assert ctx.finnhub is not None
    stats = StepStats()
    now = ctx.now()
    today = new_york_date(now)
    wanted = set(tickers)
    window = {"from": start.isoformat(), "to": (today + timedelta(days=90)).isoformat()}
    queries: list[dict[str, str]] = (
        [{"symbol": t, **window} for t in dict.fromkeys(tickers)]
        if per_symbol
        else [{"from": (today - timedelta(days=7)).isoformat(),
               "to": (today + timedelta(days=30)).isoformat()}]
    )  # fmt: skip

    for params in queries:
        try:
            payload = await ctx.finnhub.get_json(CALENDAR_URL, params=params)
        except FetchError as exc:
            stats.note(f"{params.get('symbol', 'calendar')}: {exc}")
            continue
        stats.requests += 1
        items = payload.get("earningsCalendar") if isinstance(payload, dict) else None
        rows = [
            row for row in (earnings_row(item, now) for item in items or [])
            if row is not None and row["ticker"] in wanted
        ]  # fmt: skip
        with ctx.session_factory() as session:
            stats.rows += upsert(
                session, EarningsEvent, rows,
                conflict=("source", "ticker", "report_date"), update=_UPDATE,
                also_set=("updated_at",),
            )  # fmt: skip
            session.commit()
    return stats
