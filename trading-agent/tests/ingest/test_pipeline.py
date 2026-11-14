"""The whole backfill, the daily run, the CLI and the real Alpaca SDK adapter."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alpaca.common.rest import RESTClient
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from papertrader.cli import app
from papertrader.db import models as m
from papertrader.ingest import pipeline
from papertrader.ingest.alpaca_data import AlpacaSdkData, split_timeframe, timeframe_minutes
from papertrader.ingest.context import IngestContext
from papertrader.ingest.pipeline import SelectionError, coverage, run_backfill, run_daily

from ..conftest import PROJECT_ROOT
from .helpers import NOW, FakeAlpaca, article, daily_bars, fetcher, make_ctx, shipped_config
from .test_earnings_macro import calendar_item
from .test_sec import FORM4_XML, FTD, INFO_TABLE_XML, filing, submissions
from .test_universe_bars import many_rows, wiki_page

TICKERS = [f"T{i:03d}" for i in range(1, 21)]  # twenty tickers, as in the acceptance test


def full_world(engine: Engine) -> tuple[IngestContext, FakeAlpaca, list[str]]:
    """A context where every source answers with small but realistic data."""
    hits: list[str] = []
    alpaca = FakeAlpaca()
    first, last = date(2026, 9, 1), date(2026, 11, 13)
    for ticker in [*TICKERS, "SPY", "XLK", "XLF", "BRK.B", "ACME"]:
        alpaca.bar_data[ticker] = daily_bars(first, last, 100)
    alpaca.news_data = [
        article(i, f"{t} announces quarterly results above expectations", [t], NOW - timedelta(days=i))
        for i, t in enumerate(TICKERS, start=1)
    ] + [article(500, "T001 announces quarterly results above expectations!", ["T001"],
                 NOW - timedelta(days=1) + timedelta(minutes=5))]  # fmt: skip

    company = submissions([
        filing("0000000-26-000001", "8-K", "2026-11-02T21:30:23.000Z", items="2.02", primaryDocument="e.htm"),
        filing("0000000-26-000002", "4", "2026-11-05T23:00:00.000Z", primaryDocument="xslF345X06/f4.xml"),
    ])  # fmt: skip
    manager = submissions([filing("0009999-26-000002", "13F-HR", "2026-08-14T20:05:04.000Z",
                                  reportDate="2026-06-30", primaryDocument="primary_doc.xml")])  # fmt: skip

    def sec(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        hits.append(url)
        if url.endswith("company_tickers.json"):
            return httpx.Response(200, json={})
        if "/submissions/" in url:
            cik = url.rsplit("CIK", 1)[1][:10]
            body = dict(company)
            # Each company gets its own accession numbers.
            recent = {k: list(v) for k, v in company["filings"]["recent"].items()}
            recent["accessionNumber"] = [f"{cik}-26-{n:06d}" for n in (1, 2)]
            body["filings"] = {"recent": recent, "files": []}
            if cik in MANAGER_CIKS:
                own = {k: list(v) for k, v in manager["filings"]["recent"].items()}
                own["accessionNumber"] = [f"{cik}-26-000009"]
                return httpx.Response(200, json={"filings": {"recent": own, "files": []}})
            return httpx.Response(200, json=body)
        if url.endswith("f4.xml"):
            return httpx.Response(200, text=FORM4_XML)
        if url.endswith("index.json"):
            return httpx.Response(
                200, json={"directory": {"item": [{"name": "info.xml", "size": "9"}]}}
            )
        if url.endswith("info.xml"):
            return httpx.Response(200, text=INFO_TABLE_XML.format(v1=300, v2=200))
        if url.endswith("cnsfails202610a.zip"):
            return httpx.Response(200, content=FTD)
        return httpx.Response(404)

    def finnhub(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        path, symbol = request.url.path, request.url.params.get("symbol", "T001")
        if path.endswith("/calendar/earnings"):
            return httpx.Response(
                200, json={"earningsCalendar": [calendar_item(symbol, "2026-07-30", "amc", 1.6)]}
            )
        if path.endswith("/company-news"):
            # The same story as Alpaca's, half an hour later.
            stamp = int((NOW - timedelta(days=int(symbol[1:])) + timedelta(minutes=30)).timestamp())
            return httpx.Response(200, json=[{"id": 9000 + int(symbol[1:]), "datetime": stamp,
                                              "headline": f"{symbol} announces quarterly results above expectations",
                                              "source": "Reuters", "url": "https://example.com/x"}])  # fmt: skip
        return httpx.Response(200, json={"marketCapitalization": 1000.0})

    def fred(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        obs = [{"date": "2026-11-10", "realtime_start": "2026-11-12", "value": "4.2"}]
        return httpx.Response(200, json={"count": 1, "observations": obs})

    rows = many_rows(500)
    rows[21] = ("ACME", "Acme Corp", "Industrials", "2010-01-04", "0000001111")
    web = fetcher(lambda _r: httpx.Response(200, text=wiki_page(rows)), "web")
    ctx = make_ctx(
        engine, config=shipped_config(sector_etfs=("XLK", "XLF")), web=web, alpaca=alpaca,
        sec=fetcher(sec, "sec"), finnhub=fetcher(finnhub, "finnhub"),
        fred=fetcher(fred, "fred"), fred_api_key="k",
    )  # fmt: skip
    return ctx, alpaca, hits


MANAGER_CIKS = {manager.cik for manager in shipped_config().ingest.managers_13f}


def totals(ctx: IngestContext) -> dict[str, int]:
    return {name: rows for name, rows, _span in coverage(ctx)}


async def test_backfill_for_twenty_tickers_works_and_is_idempotent(engine: Engine) -> None:
    """The Phase 2 acceptance test, against fakes of every source."""
    ctx, alpaca, hits = full_world(engine)

    first = await run_backfill(ctx, years=5, tickers=TICKERS)
    assert [r.name for r in first] == list(pipeline.STEPS)
    assert {r.status for r in first} == {"ok"}, [
        (r.name, r.detail) for r in first if r.status != "ok"
    ]

    stored = totals(ctx)
    assert stored["universe_members"] == 500
    assert (
        stored["price_bars"] == 23 * 53
    )  # 20 tickers + SPY + 2 sector funds, 53 finished sessions
    assert stored["news_items"] == 41  # 21 from Alpaca + 20 from Finnhub
    assert stored["filings"] == 20 * 2 + 15  # two per company, one 13F per tracked manager
    assert stored["insider_transactions"] == 20 * 2
    assert stored["institutional_holdings"] == 15
    assert stored["earnings_events"] == 20 * 2  # one from Finnhub and one from the 8-K, per ticker
    assert stored["macro_observations"] == 13
    assert stored["security_identifiers"] == 1

    with ctx.session_factory() as session:
        duplicates = session.scalar(
            select(func.count())
            .select_from(m.NewsItem)
            .where(m.NewsItem.duplicate_of_id.is_not(None))
        )
        caps = session.scalar(
            select(func.count())
            .select_from(m.UniverseMember)
            .where(m.UniverseMember.market_cap.is_not(None))
        )
        # Nothing stored claims to have been known after "now".
        for model in (m.PriceBar, m.NewsItem, m.Filing, m.InsiderTransaction, m.MacroObservation):
            assert session.scalar(select(func.max(model.available_at))) <= NOW, model.__name__
    assert duplicates == 21  # same story from both sources, plus one repost
    assert caps == 20

    hits.clear()
    calls_before = len(alpaca.bar_calls) + len(alpaca.news_calls)
    second = await run_backfill(ctx, years=5, tickers=TICKERS)
    assert {r.status for r in second} == {"ok"}
    assert {r.name: r.rows for r in second if r.rows} == {}  # nothing new, nothing changed
    assert totals(ctx) == stored
    assert not any(
        url.endswith(("f4.xml", "info.xml")) for url in hits
    )  # details not re-downloaded
    assert len(alpaca.bar_calls) + len(alpaca.news_calls) - calls_before <= 2


async def test_missing_keys_skip_their_steps_and_say_why(engine: Engine) -> None:
    ctx, _alpaca, _hits = full_world(engine)
    ctx.alpaca = ctx.sec = ctx.finnhub = ctx.fred = None
    results = {r.name: r for r in await run_backfill(ctx, years=1, limit=20)}
    assert results["universe"].status == "ok"
    assert results["dedupe"].status == "ok"
    assert results["bars"].status == "skipped" and "ALPACA_API_KEY" in results["bars"].detail
    assert results["filings"].status == "skipped" and "SEC_USER_AGENT" in results["filings"].detail
    assert "optional" in results["macro"].detail and "optional" in results["earnings"].detail


def test_without_a_stock_list_per_stock_steps_are_skipped_not_reported_ok(engine: Engine) -> None:
    ctx, _alpaca, _hits = full_world(engine)
    reasons = {
        name: missing for name, missing, _ in pipeline._steps(ctx, [], years=5, backfill=True)
    }
    per_stock = ("cusip_map", "news", "finnhub_news", "filings", "insiders", "holdings_13f",
                 "earnings", "market_caps")  # fmt: skip
    assert all(reasons[name] == pipeline.NO_STOCKS for name in per_stock)
    # Prices for the benchmark and macro data do not depend on the stock list.
    assert reasons["bars"] is None and reasons["macro"] is None and reasons["dedupe"] is None


async def test_one_failing_step_does_not_stop_the_rest(engine: Engine) -> None:
    ctx, alpaca, _hits = full_world(engine)
    alpaca.fail_when = lambda _symbols: True
    results = {r.name: r for r in await run_backfill(ctx, years=1, tickers=TICKERS[:3])}
    assert results["bars"].status == "failed" and "alpaca is down" in results["bars"].detail
    assert results["news"].status == "ok" and results["filings"].status == "ok"


async def test_only_runs_the_chosen_steps_and_unknown_tickers_are_refused(engine: Engine) -> None:
    ctx, _alpaca, _hits = full_world(engine)
    results = await run_backfill(ctx, years=1, tickers=["T001"], only=["universe", "macro"])
    assert [r.name for r in results] == ["universe", "macro"]
    with pytest.raises(SelectionError, match="NOPE"):
        await run_backfill(ctx, years=1, tickers=["T001", "NOPE"], only=["bars"])


async def test_limit_picks_tickers_from_different_sectors(engine: Engine) -> None:
    ctx, alpaca, _hits = full_world(engine)
    for ticker in [r[0] for r in many_rows(500)]:
        alpaca.bar_data.setdefault(ticker, daily_bars(date(2026, 11, 2), date(2026, 11, 13)))
    await run_backfill(ctx, years=1, limit=20, only=["universe", "bars"])
    with ctx.session_factory() as session:
        tickers = set(session.scalars(select(m.PriceBar.ticker).distinct()))
        sectors = set(session.scalars(
            select(m.UniverseMember.gics_sector).where(m.UniverseMember.ticker.in_(tickers))
        ))  # fmt: skip
    assert len(tickers - {"SPY", "XLK", "XLF"}) == 20
    assert len(sectors) == 4  # every sector in the fake universe is represented


async def test_daily_run_covers_the_whole_universe_incrementally(engine: Engine) -> None:
    ctx, alpaca, _hits = full_world(engine)
    await run_backfill(ctx, years=1, tickers=TICKERS, only=["universe"])
    results = {r.name: r for r in await run_daily(ctx, only=["bars", "earnings"])}
    assert results["bars"].status == "ok"
    asked = {symbol for call in alpaca.bar_calls for symbol in call[0]}
    assert len(asked) == 500 + 3  # all members plus SPY and the sector funds
    assert results["earnings"].requests == 1  # one calendar request, not one per ticker


async def test_daily_run_never_starts_a_multi_year_download_by_itself(engine: Engine) -> None:
    """If the full backfill was never run, the morning job must still finish quickly."""
    ctx, alpaca, _hits = full_world(engine)
    await run_daily(ctx, only=["universe", "news", "filings", "insiders"])
    earliest_news_request = min(start for _symbols, start, _end in alpaca.news_calls)
    assert NOW - earliest_news_request <= timedelta(days=7, hours=6)
    with ctx.session_factory() as session:
        oldest_filing = session.scalar(select(func.min(m.Filing.published_at)))
        read = session.scalar(
            select(func.count()).select_from(m.Filing).where(m.Filing.detail_status == "done")
        )
    assert oldest_filing is not None and NOW - oldest_filing <= timedelta(days=31)
    # 500 members share 499 SEC identifiers here, and one (Berkshire) only has fund filings.
    assert read == 498  # recent insider filings were read for every company


# ----------------------------------------------------------------------------- real SDK adapter


def test_timeframes() -> None:
    assert split_timeframe("1Day") == (1, "Day")
    assert timeframe_minutes("10Min") == 10 and timeframe_minutes("1Hour") == 60
    with pytest.raises(ValueError, match="unknown timeframe"):
        split_timeframe("daily")


def test_alpaca_sdk_adapter_pages_through_results_with_the_right_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drives the real alpaca-py clients with only the HTTP call replaced."""
    requests: list[tuple[str, dict[str, Any]]] = []
    bar = {
        "t": "2026-11-12T05:00:00Z",
        "o": 1.0,
        "h": 2.0,
        "l": 0.5,
        "c": 1.5,
        "v": 100,
        "n": 9,
        "vw": 1.2,
    }
    pages = {
        "/stocks/bars": [{"bars": {"AAPL": [bar]}, "next_page_token": "p2"},
                         {"bars": {"AAPL": [bar], "MSFT": [bar]}, "next_page_token": None}],
        "/news": [{"news": [{"id": 1, "headline": "a"}], "next_page_token": "n2"},
                  {"news": [{"id": 2, "headline": "b"}], "next_page_token": None}],
    }  # fmt: skip

    def fake_request(
        self: RESTClient, method: str, url: str, opts: dict[str, Any], retry: int
    ) -> Any:
        key = next(k for k in pages if url.endswith(k))
        requests.append((url, dict(opts["params"])))
        assert opts["headers"]["APCA-API-KEY-ID"] == "PKTEST"
        return pages[key].pop(0)

    monkeypatch.setattr(RESTClient, "_one_request", fake_request)
    data = AlpacaSdkData(
        "PKTEST", "secret", feed="sip", adjustment="all", requests_per_second=10_000
    )

    start, end = datetime(2026, 11, 1, tzinfo=NOW.tzinfo), NOW
    bars = data.bars(["AAPL", "MSFT"], "1Day", start, end)
    assert {symbol: len(rows) for symbol, rows in bars.items()} == {"AAPL": 2, "MSFT": 1}
    url, params = requests[0]
    assert (
        url == "https://data.alpaca.markets/v2/stocks/bars"
    )  # the data service, not a trading endpoint
    assert params["symbols"] == "AAPL,MSFT"
    assert (str(params["timeframe"]), params["feed"].value, params["adjustment"].value) == (
        "1Day", "sip", "all")  # fmt: skip
    assert requests[1][1]["page_token"] == "p2"

    news = data.news(["AAPL", "MSFT"], start, end)
    assert [item["id"] for item in news] == [1, 2]
    url, params = requests[2]
    assert url == "https://data.alpaca.markets/v1beta1/news"
    assert (params["symbols"], params["sort"], params["limit"]) == ("AAPL,MSFT", "asc", 50)
    assert data.throttle.calls == 4  # every HTTP request went through the rate limit


# ----------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    return tmp_path


def test_cli_lists_steps() -> None:
    result = runner.invoke(app, ["ingest", "steps"])
    assert result.exit_code == 0
    assert all(step in result.output for step in pipeline.STEPS)


def test_cli_rejects_unknown_steps(project: Path) -> None:
    result = runner.invoke(app, ["ingest", "backfill", "--only", "bars,weather"])
    assert result.exit_code == 1
    assert "weather" in result.output


def test_cli_status_on_an_empty_database(project: Path) -> None:
    result = runner.invoke(app, ["ingest", "status"])
    assert result.exit_code == 0, result.output
    assert "price_bars" in result.output and "news_items" in result.output


def test_cli_backfill_without_keys_explains_what_was_skipped(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def offline(self: Any, url: str, **_kwargs: Any) -> Any:
        raise httpx.ConnectError("offline in tests")

    monkeypatch.setattr(httpx.AsyncClient, "get", offline)
    monkeypatch.setattr("papertrader.ingest.http.asyncio.sleep", lambda _s: _noop())
    result = runner.invoke(app, ["ingest", "backfill", "--limit", "20", "--years", "1"])
    assert result.exit_code == 0, result.output
    assert "universe      ok" in result.output
    assert "built-in list from 2026-11-13" in result.output  # no network: the shipped list is used
    assert "ALPACA_API_KEY / ALPACA_SECRET_KEY not set" in result.output
    assert '{"ts"' not in result.output  # readable lines, not JSON, for a person at a terminal

    status = runner.invoke(app, ["ingest", "status"])
    assert "universe_members" in status.output and "503" in status.output


async def _noop() -> None:
    return None
