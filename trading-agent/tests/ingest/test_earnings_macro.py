from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from papertrader.config import FredSeries
from papertrader.db.models import EarningsEvent, MacroObservation
from papertrader.ingest.earnings import earnings_row, ingest_earnings, results_known_at
from papertrader.ingest.macro import ingest_macro, observation_rows

from .helpers import NOW, fetcher, make_ctx, shipped_config


def calendar_item(
    symbol: str, day: str, hour: str, eps: float | None, **extra: Any
) -> dict[str, Any]:
    return {"symbol": symbol, "date": day, "hour": hour, "epsActual": eps, "epsEstimate": 1.5,
            "revenueActual": None if eps is None else 9.5e9, "revenueEstimate": 9.0e9,
            "quarter": 3, "year": 2026, **extra}  # fmt: skip


def test_results_are_assumed_public_late_never_early() -> None:
    day = date(2026, 7, 30)
    assert results_known_at(day, "bmo") == datetime(
        2026, 7, 30, 13, 30, tzinfo=UTC
    )  # 09:30 New York
    assert results_known_at(day, "amc") == datetime(2026, 7, 31, 0, 0, tzinfo=UTC)  # 20:00 New York
    assert results_known_at(day, None) == results_known_at(day, "amc")  # unknown: assume late


def test_past_report_carries_actuals_and_their_availability_time() -> None:
    row = earnings_row(calendar_item("aapl", "2026-07-30", "amc", 1.62), NOW)
    assert row is not None
    assert (row["ticker"], row["eps_actual"], row["eps_estimate"]) == ("AAPL", 1.62, 1.5)
    assert (
        row["actual_available_at"] == row["available_at"] == datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
    )
    assert (row["fiscal_year"], row["fiscal_quarter"]) == (2026, 3)


def test_upcoming_report_has_no_actuals_and_is_known_from_now() -> None:
    row = earnings_row(calendar_item("AAPL", "2026-11-20", "amc", None), NOW)
    assert row is not None
    assert row["eps_actual"] is None and row["actual_available_at"] is None
    assert row["available_at"] == NOW


def test_results_dated_today_after_close_are_not_trusted_before_the_close() -> None:
    """NOW is 07:00 New York. A number for 'today, after close' cannot be real yet."""
    row = earnings_row(calendar_item("AAPL", "2026-11-13", "amc", 9.99), NOW)
    assert row is not None
    assert row["eps_actual"] is None and row["revenue_actual"] is None


async def test_backfill_is_per_symbol_and_updates_when_results_arrive(engine: Engine) -> None:
    requests: list[dict[str, str]] = []
    reported: dict[str, float | None] = {"eps": None}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(dict(request.url.params))
        symbol = request.url.params.get("symbol", "AAPL")
        return httpx.Response(200, json={"earningsCalendar": [
            calendar_item(symbol, "2026-07-30", "amc", 1.62),
            calendar_item(symbol, "2026-11-12", "amc", reported["eps"]),
            calendar_item("OTHER", "2026-07-30", "amc", 1.0),
        ]})  # fmt: skip

    ctx = make_ctx(engine, finnhub=fetcher(handler, "finnhub"))
    stats = await ingest_earnings(ctx, ["AAPL", "MSFT"], start=date(2021, 11, 13), per_symbol=True)
    assert [r["symbol"] for r in requests] == ["AAPL", "MSFT"]
    assert requests[0]["from"] == "2021-11-13" and requests[0]["to"] == "2027-02-11"
    assert stats.rows == 4  # two events for each of two tickers; OTHER is not in our list

    assert (
        await ingest_earnings(ctx, ["AAPL", "MSFT"], start=date(2021, 11, 13), per_symbol=True)
    ).rows == 0

    # Daily refresh, one request for everyone; yesterday's results have now appeared.
    requests.clear()
    reported["eps"] = 2.05
    stats = await ingest_earnings(ctx, ["AAPL", "MSFT"], start=date(2021, 11, 13), per_symbol=False)
    assert len(requests) == 1 and "symbol" not in requests[0]
    assert stats.rows == 1  # only AAPL comes back from the general calendar in this fake
    with ctx.session_factory() as session:
        event = session.scalars(
            select(EarningsEvent).where(
                EarningsEvent.ticker == "AAPL", EarningsEvent.report_date == date(2026, 11, 12)
            )
        ).one()
    assert event.eps_actual == 2.05
    assert event.actual_available_at == datetime(
        2026, 11, 13, 1, 0, tzinfo=UTC
    )  # 20:00 New York, EST


async def test_finnhub_listing_one_release_twice_keeps_the_entry_with_results(
    engine: Engine,
) -> None:
    """The real data that broke the first full run (Amcor, 4 November 2026)."""
    payload = {"earningsCalendar": [
        calendar_item("AMCR", "2027-02-01", "", None, year=2027, quarter=2),
        calendar_item("AMCR", "2026-11-04", "amc", None, year=2027, quarter=1, epsEstimate=0.9852),
        calendar_item("AMCR", "2026-11-04", "amc", 0.95, year=2026, quarter=3, epsEstimate=0.986),
    ]}  # fmt: skip
    ctx = make_ctx(engine, finnhub=fetcher(lambda _r: httpx.Response(200, json=payload), "finnhub"))

    stats = await ingest_earnings(ctx, ["AMCR"], start=date(2021, 11, 13), per_symbol=True)
    assert stats.rows == 2
    with ctx.session_factory() as session:
        events = session.scalars(select(EarningsEvent).order_by(EarningsEvent.report_date)).all()
    reported, upcoming = events
    assert (reported.report_date, reported.eps_actual, reported.eps_estimate) == (
        date(2026, 11, 4),
        0.95,
        0.986,
    )
    assert (upcoming.report_date, upcoming.eps_actual, upcoming.time_of_day) == (
        date(2027, 2, 1),
        None,
        None,
    )
    assert (
        await ingest_earnings(ctx, ["AMCR"], start=date(2021, 11, 13), per_symbol=True)
    ).rows == 0


# ----------------------------------------------------------------------------- FRED


def test_revised_series_keep_every_published_version() -> None:
    rows = observation_rows(
        FredSeries(id="PAYEMS", revised=True),
        [{"date": "2026-08-01", "realtime_start": "2026-09-04", "realtime_end": "2026-10-01", "value": "159000"},
         {"date": "2026-08-01", "realtime_start": "2026-10-02", "realtime_end": "9999-12-31", "value": "159120"}],
        with_vintages=True,
    )  # fmt: skip
    assert [(r["realtime_start"], r["value"]) for r in rows] == [
        (date(2026, 9, 4), 159000.0), (date(2026, 10, 2), 159120.0)]  # fmt: skip
    assert rows[0]["available_at"] == datetime(2026, 9, 4, 14, 0, tzinfo=UTC)  # 10:00 New York, EDT


def test_market_series_use_a_stable_key_and_next_day_availability() -> None:
    rows = observation_rows(
        FredSeries(id="DGS10"),
        [{"date": "2026-11-10", "realtime_start": "2026-11-13", "realtime_end": "2026-11-13", "value": "4.21"},
         {"date": "2026-11-11", "realtime_start": "2026-11-13", "realtime_end": "2026-11-13", "value": "."}],
        with_vintages=False,
    )  # fmt: skip
    # FRED stamps today's date on every row; storing that would create a new copy every day.
    assert rows[0]["realtime_start"] == date(2026, 11, 10)
    assert rows[0]["available_at"] == datetime(2026, 11, 11, 23, 0, tzinfo=UTC)
    assert rows[1]["value"] is None  # holiday


async def test_fred_ingest_is_idempotent_and_never_leaks_the_api_key(
    engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen.append(params)
        assert params["api_key"] == "FRED-SECRET" and params["file_type"] == "json"
        series = params["series_id"]
        if series == "BROKEN":
            return httpx.Response(500)
        if series == "ICSA" and "realtime_start" in params:
            return httpx.Response(400)  # FRED refuses the revision history for this one
        if "realtime_start" in params:
            obs = [{"date": "2026-09-01", "realtime_start": "2026-10-10", "value": "3.0"},
                   {"date": "2026-09-01", "realtime_start": "2026-11-12", "value": "3.1"}]  # fmt: skip
        else:
            obs = [{"date": "2026-11-10", "realtime_start": NOW.date().isoformat(), "value": "4.2"}]
        return httpx.Response(200, json={"count": len(obs), "observations": obs})

    cfg = shipped_config(fred_series=(
        FredSeries(id="DGS10"), FredSeries(id="CPIAUCSL", revised=True),
        FredSeries(id="ICSA", revised=True), FredSeries(id="BROKEN"),
    ))  # fmt: skip
    ctx = make_ctx(
        engine, config=cfg, fred=fetcher(handler, "fred", max_retries=1), fred_api_key="FRED-SECRET"
    )

    with caplog.at_level(logging.DEBUG):
        stats = await ingest_macro(ctx, start=date(2021, 11, 13))
    assert stats.rows == 4  # DGS10 x1, CPI x2 versions, ICSA x1 via the fallback
    assert any("ICSA" in note and "revision history" in note for note in stats.notes)
    assert any("BROKEN" in note for note in stats.notes)
    everything_logged = caplog.text + " ".join(stats.notes)
    assert "FRED-SECRET" not in everything_logged
    assert seen[0]["observation_start"] == "2021-11-13"

    with ctx.session_factory() as session:
        cpi = session.scalars(
            select(MacroObservation).where(MacroObservation.series_id == "CPIAUCSL")
            .order_by(MacroObservation.realtime_start)
        ).all()  # fmt: skip
    assert [(o.realtime_start, o.value) for o in cpi] == [
        (date(2026, 10, 10), 3.0),
        (date(2026, 11, 12), 3.1),
    ]
    # A decision made on 1 November must see 3.0, the value known then - not the later revision.
    known_then = [o for o in cpi if o.available_at <= datetime(2026, 11, 1, tzinfo=UTC)]
    assert [o.value for o in known_then] == [3.0]

    ctx.now = lambda: NOW + timedelta(days=1)
    assert (await ingest_macro(ctx, start=date(2021, 11, 13))).rows == 0
