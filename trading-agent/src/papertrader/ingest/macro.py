"""Macro data from FRED: rates, inflation, unemployment, yield curve.

Many macro numbers are revised after first publication. For series marked
`revised: true` every published version (FRED calls them vintages) is stored, so a
decision made "as of" a past date sees the number as it was known that day.
"""

from __future__ import annotations

from datetime import date, time, timedelta
from typing import Any

from papertrader.config import FredSeries
from papertrader.db.models import MacroObservation
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.http import FetchError
from papertrader.ingest.store import upsert
from papertrader.ingest.timeutil import at_new_york

OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
ALL_VINTAGES = {"realtime_start": "1776-07-04", "realtime_end": "9999-12-31"}
PAGE = 100_000


def observation_rows(
    series: FredSeries, observations: list[dict[str, Any]], *, with_vintages: bool
) -> list[dict[str, Any]]:
    rows = []
    for item in observations:
        observed = date.fromisoformat(str(item["date"]))
        raw = str(item.get("value", "."))
        try:
            value: float | None = float(raw)
        except ValueError:
            value = None  # FRED writes "." for a missing value
        if with_vintages:
            published = date.fromisoformat(str(item["realtime_start"]))
            # Releases come out at 08:30 New York time; 10:00 leaves a margin.
            available = at_new_york(published, time(10, 0))
        else:
            # Market series (yields, VIX) are never revised and appear on FRED the next day.
            published = observed
            available = at_new_york(observed + timedelta(days=1), time(18, 0))
        rows.append(
            {"series_id": series.id, "observation_date": observed, "realtime_start": published,
             "value": value, "available_at": available}
        )  # fmt: skip
    return rows


async def _fetch(ctx: IngestContext, series: FredSeries, start: date, vintages: bool) -> list[Any]:
    assert ctx.fred is not None and ctx.fred_api_key is not None
    collected: list[Any] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "series_id": series.id, "api_key": ctx.fred_api_key, "file_type": "json",
            "observation_start": start.isoformat(), "limit": PAGE, "offset": offset,
        }  # fmt: skip
        if vintages:
            params.update(ALL_VINTAGES)
        payload = await ctx.fred.get_json(OBSERVATIONS_URL, params=params)
        batch = payload.get("observations", [])
        collected.extend(batch)
        offset += len(batch)
        if len(batch) < PAGE or offset >= int(payload.get("count", 0)):
            return collected


async def ingest_macro(ctx: IngestContext, *, start: date) -> StepStats:
    stats = StepStats()
    for series in ctx.config.ingest.fred_series:
        vintages = series.revised
        try:
            try:
                observations = await _fetch(ctx, series, start, vintages)
            except FetchError as exc:
                if not (vintages and exc.status == 400):
                    raise
                # FRED refuses the full revision history for some series; fall back.
                stats.note(f"{series.id}: revision history unavailable, using current values")
                vintages = False
                observations = await _fetch(ctx, series, start, vintages)
        except FetchError as exc:
            stats.note(f"{series.id}: {exc}")
            continue
        stats.requests += 1
        rows = observation_rows(series, observations, with_vintages=vintages)
        with ctx.session_factory() as session:
            stats.rows += upsert(
                session, MacroObservation, rows,
                conflict=("series_id", "observation_date", "realtime_start"),
                update=() if vintages else ("value",),
            )  # fmt: skip
            session.commit()
    return stats
