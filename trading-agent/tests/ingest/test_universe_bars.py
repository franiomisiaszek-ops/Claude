from __future__ import annotations

from datetime import date, time, timedelta

import httpx
import pytest
from sqlalchemy import Engine, func, select

from papertrader.db.models import PriceBar, UniverseMember
from papertrader.ingest.bars import ingest_bars
from papertrader.ingest.store import get_cursor
from papertrader.ingest.timeutil import at_new_york, daily_bar_available_at
from papertrader.ingest.universe import (
    UniverseError,
    active_members,
    ingest_market_caps,
    ingest_universe,
    parse_constituents,
    pick_spread,
    plain_symbol,
)

from .helpers import NOW, FakeAlpaca, add_members, daily_bars, fetcher, make_ctx, shipped_config

SECTORS = ["Industrials", "Health Care", "Information Technology", "Financials"]


def wiki_page(rows: list[tuple[str, str, str, str, str]]) -> str:
    """A cut-down copy of the real Wikipedia layout (checked against the live page)."""
    body = "".join(
        f'<tr><td><a href="#">{t}</a><sup>[1]</sup></td><td><a href="#">{n}</a></td><td>{s}</td>'
        f"<td>Sub {s}</td><td>City</td><td>{added}</td><td>{cik}</td><td>1900</td></tr>"
        for t, n, s, added, cik in rows
    )
    return (
        "<html><body><table><tr><th>Year</th><th>Level</th></tr><tr><td>2025</td><td>1</td></tr>"
        '</table><table id="constituents"><tbody><tr><th><a>Symbol</a></th><th>Security</th>'
        "<th><a>GICS</a> Sector</th><th>GICS Sub-Industry</th><th>Headquarters Location</th>"
        f"<th>Date added</th><th><a>CIK</a></th><th>Founded</th></tr>{body}</tbody></table>"
        "</body></html>"
    )


def many_rows(count: int, skip: str | None = None) -> list[tuple[str, str, str, str, str]]:
    rows = [
        (f"T{i:03d}", f"Company {i}", SECTORS[i % 4], "2010-01-04", f"{1000 + i:010d}")
        for i in range(count)
    ]
    rows[0] = ("BRK.B", "Berkshire Hathaway", "Financials", "2010-02-16", "0001067983")
    return [row for row in rows if row[0] != skip]


def test_parse_constituents_reads_the_members_table() -> None:
    members = parse_constituents(wiki_page(many_rows(5)))
    assert [m.ticker for m in members] == ["BRK.B", "T001", "T002", "T003", "T004"]
    first = members[0]
    assert (first.name, first.sector, first.cik) == (
        "Berkshire Hathaway",
        "Financials",
        "0001067983",
    )
    assert first.added_on == date(2010, 2, 16)
    assert members[1].sub_industry == "Sub Health Care"


def test_plain_symbol_matches_share_class_spellings() -> None:
    assert plain_symbol("BRK.B") == plain_symbol("BRK-B") == plain_symbol("brkb") == "BRKB"


async def test_universe_sync_adds_removes_and_keeps_history(engine: Engine) -> None:
    pages = [wiki_page(many_rows(500)), wiki_page(many_rows(500, skip="T007"))]
    web = fetcher(lambda _r: httpx.Response(200, text=pages.pop(0)), "web")
    ctx = make_ctx(engine, web=web)

    stats = await ingest_universe(ctx)
    assert stats.rows == 500
    with ctx.session_factory() as session:
        assert len(active_members(session, "sp500")) == 500

    stats = await ingest_universe(ctx)  # T007 left the index
    assert stats.rows == 1
    with ctx.session_factory() as session:
        gone = session.scalars(select(UniverseMember).where(UniverseMember.ticker == "T007")).one()
        assert gone.is_active is False
        assert gone.removed_on == date(2026, 11, 13)
        assert len(active_members(session, "sp500")) == 499


async def test_a_broken_page_never_wipes_the_universe(engine: Engine) -> None:
    pages = [wiki_page(many_rows(500)), "<html><body>layout changed</body></html>"]
    ctx = make_ctx(engine, web=fetcher(lambda _r: httpx.Response(200, text=pages.pop(0)), "web"))
    await ingest_universe(ctx)
    with pytest.raises(UniverseError, match="left untouched"):
        await ingest_universe(ctx)
    with ctx.session_factory() as session:
        assert len(active_members(session, "sp500")) == 500


async def test_sec_identifiers_fill_gaps_for_custom_universe(engine: Engine) -> None:
    cfg = shipped_config()
    cfg = cfg.model_copy(
        update={"universe": cfg.universe.model_copy(
            update={"index": "custom", "custom_tickers": ("AAPL", "BRK.B")})}
    )  # fmt: skip
    sec_json = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
                "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"}}  # fmt: skip
    ctx = make_ctx(engine, config=cfg, sec=fetcher(lambda _r: httpx.Response(200, json=sec_json)))
    await ingest_universe(ctx)
    with ctx.session_factory() as session:
        found = {m.ticker: (m.cik, m.name) for m in active_members(session, "custom")}
    assert found == {"AAPL": ("0000320193", "Apple Inc."),
                     "BRK.B": ("0001067983", "BERKSHIRE HATHAWAY INC")}  # fmt: skip


def test_pick_spread_takes_tickers_from_every_sector(engine: Engine) -> None:
    ctx = make_ctx(engine)
    add_members(ctx, *[(f"T{i:02d}", str(i), SECTORS[i % 4]) for i in range(40)])
    with ctx.session_factory() as session:
        picked = pick_spread(active_members(session, "sp500"), 8)
    assert len(picked) == 8
    assert {m.gics_sector for m in picked} == set(SECTORS)


async def test_market_caps_are_refreshed_at_most_weekly(engine: Engine) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Finnhub-Token"] == "key"
        calls.append(request.url.params["symbol"])
        return httpx.Response(200, json={"marketCapitalization": 3_500_000.5, "ticker": "AAPL"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"X-Finnhub-Token": "key"}
    )
    from papertrader.ingest.http import Fetcher

    from .helpers import no_sleep

    ctx = make_ctx(engine, finnhub=Fetcher(client, name="finnhub", per_second=1000, sleep=no_sleep))
    add_members(ctx, ("AAPL", "320193", "Information Technology"))

    assert (await ingest_market_caps(ctx, ["AAPL"])).rows == 1
    assert (await ingest_market_caps(ctx, ["AAPL"])).rows == 0  # fresh enough, no request
    assert calls == ["AAPL"]
    with ctx.session_factory() as session:
        assert session.scalars(select(UniverseMember.market_cap)).one() == pytest.approx(
            3.5000005e12
        )


# ----------------------------------------------------------------------------- bars

START = date(2026, 9, 1)
THURSDAY, FRIDAY = date(2026, 11, 12), date(2026, 11, 13)


def _count(ctx, ticker: str | None = None) -> int:  # type: ignore[no-untyped-def]
    with ctx.session_factory() as session:
        query = select(func.count()).select_from(PriceBar)
        if ticker:
            query = query.where(PriceBar.ticker == ticker)
        return session.scalar(query) or 0


async def test_backfill_stores_bars_and_a_second_run_changes_nothing(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": daily_bars(START, FRIDAY), "SPY": daily_bars(START, FRIDAY, 500)}
    ctx = make_ctx(engine, alpaca=alpaca)

    first = await ingest_bars(ctx, ["AAPL", "SPY"], start=START)
    stored = _count(ctx)
    assert first.rows == stored > 100

    second = await ingest_bars(ctx, ["AAPL", "SPY"], start=START)
    assert second.rows == 0
    assert _count(ctx) == stored
    # The second run only asked for the last few days, not the whole history again.
    assert alpaca.bar_calls[-1][2] == at_new_york(THURSDAY - timedelta(days=7), time(0, 0))


async def test_an_unfinished_trading_day_is_never_stored(engine: Engine) -> None:
    """NOW is Friday 07:00 New York: Friday's bar does not exist yet and must not be saved."""
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": daily_bars(START, FRIDAY)}
    ctx = make_ctx(engine, alpaca=alpaca)
    # The 20-minute safety margin on `end` would hide Friday's bar; remove it to prove the
    # availability check alone is enough.
    alpaca.bars = lambda symbols, timeframe, start, end: {"AAPL": alpaca.bar_data["AAPL"]}  # type: ignore[method-assign]
    await ingest_bars(ctx, ["AAPL"], start=START)

    with ctx.session_factory() as session:
        latest = session.scalars(select(PriceBar).order_by(PriceBar.ts.desc())).first()
        assert latest is not None
        assert latest.ts == at_new_york(THURSDAY, time(0, 0))
        assert latest.available_at == daily_bar_available_at(THURSDAY) <= NOW
        assert get_cursor(session, "alpaca.bars.1Day", "AAPL")["through"] == "2026-11-12"


async def test_a_split_or_dividend_triggers_a_full_re_read(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": daily_bars(START, FRIDAY), "MSFT": daily_bars(START, FRIDAY, 300)}
    ctx = make_ctx(engine, alpaca=alpaca)
    await ingest_bars(ctx, ["AAPL", "MSFT"], start=START)

    # A 2-for-1 split: Alpaca now reports every past AAPL price at half its old value.
    alpaca.bar_data["AAPL"] = daily_bars(START, FRIDAY, factor=0.5)
    stats = await ingest_bars(ctx, ["AAPL", "MSFT"], start=START)

    assert "AAPL" in stats.notes[0] and "MSFT" not in stats.notes[0]
    with ctx.session_factory() as session:
        closes = session.scalars(
            select(PriceBar.close).where(PriceBar.ticker == "AAPL").order_by(PriceBar.ts)
        ).all()
        msft_first = session.scalars(
            select(PriceBar.close).where(PriceBar.ticker == "MSFT").order_by(PriceBar.ts)
        ).first()
    assert closes[0] == 50.0  # the oldest bar was corrected too, not just the recent ones
    assert closes == sorted(closes)
    assert msft_first == 300.0
    assert stats.rows == len(closes)


async def test_a_crash_keeps_finished_chunks_and_resumes(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    tickers = [f"T{i:03d}" for i in range(60)]  # two chunks of 50 and 10
    alpaca.bar_data = {t: daily_bars(date(2026, 11, 2), FRIDAY) for t in tickers}
    alpaca.fail_when = lambda symbols: "T055" in symbols
    ctx = make_ctx(engine, alpaca=alpaca)

    with pytest.raises(RuntimeError, match="alpaca is down"):
        await ingest_bars(ctx, tickers, start=date(2026, 11, 2))
    assert _count(ctx, "T000") == 9  # first chunk was committed
    assert _count(ctx, "T055") == 0

    alpaca.fail_when = lambda _symbols: False
    await ingest_bars(ctx, tickers, start=date(2026, 11, 2))
    assert _count(ctx, "T055") == 9
    assert _count(ctx) == 60 * 9


async def test_extending_the_backfill_further_back_fetches_the_older_part(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": daily_bars(date(2026, 1, 1), FRIDAY)}
    ctx = make_ctx(engine, alpaca=alpaca)
    await ingest_bars(ctx, ["AAPL"], start=START)
    before = _count(ctx)
    await ingest_bars(ctx, ["AAPL"], start=date(2026, 1, 1))
    assert _count(ctx) > before
    with ctx.session_factory() as session:
        assert get_cursor(session, "alpaca.bars.1Day", "AAPL")["from"] == "2026-01-01"


async def test_unknown_symbols_are_reported_not_fatal(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": daily_bars(START, FRIDAY)}
    ctx = make_ctx(engine, alpaca=alpaca)
    stats = await ingest_bars(ctx, ["AAPL", "NOPE"], start=START)
    assert any("NOPE" in note for note in stats.notes)


async def test_intraday_bars_become_available_when_the_bar_ends(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.bar_data = {"AAPL": [
        {"t": "2026-11-12T15:30:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10, "n": 1, "vw": 1.5},
        {"t": "2026-11-13T11:55:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10, "n": 1, "vw": 1.5},
    ]}  # fmt: skip
    alpaca.bars = lambda symbols, timeframe, start, end: dict(alpaca.bar_data)  # type: ignore[method-assign]
    ctx = make_ctx(engine, alpaca=alpaca)
    await ingest_bars(ctx, ["AAPL"], start=date(2026, 11, 10), timeframe="10Min")
    with ctx.session_factory() as session:
        bars = session.scalars(select(PriceBar)).all()
    # The 11:55 bar ends at 12:05, after NOW (12:00), so only the first one is stored.
    assert [(b.timeframe, b.available_at.isoformat()) for b in bars] == [
        ("10Min", "2026-11-12T15:40:00+00:00")
    ]
