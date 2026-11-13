from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import selectinload

from papertrader.db.models import NewsItem, NewsTicker
from papertrader.ingest.context import IngestContext
from papertrader.ingest.news import (
    NewsRecord,
    dedupe_news,
    headline_hash,
    ingest_alpaca_news,
    ingest_finnhub_news,
    normalise_headline,
    store_news,
)
from papertrader.ingest.store import get_cursor

from .helpers import NOW, FakeAlpaca, article, fetcher, make_ctx

START = NOW - timedelta(days=75)


def _items(ctx: IngestContext) -> list[NewsItem]:
    with ctx.session_factory() as session:
        return list(
            session.scalars(
                select(NewsItem).options(selectinload(NewsItem.tickers)).order_by(NewsItem.id)
            )
        )


def _record(source: str, item_id: str, headline: str, at: datetime, *tickers: str) -> NewsRecord:
    return NewsRecord(source=source, source_item_id=item_id, headline=headline, published_at=at,
                      available_at=at, tickers=set(tickers))  # fmt: skip


def test_headline_normalisation_ignores_case_and_punctuation() -> None:
    a = normalise_headline("Apple's Q4 Results: Revenue UP 8%!")
    b = normalise_headline("apple s q4 results - revenue up 8")
    assert a == b


def test_very_short_headlines_never_match_each_other() -> None:
    one = _record("alpaca", "1", "Update", NOW)
    two = _record("alpaca", "2", "Update", NOW)
    assert headline_hash(one) != headline_hash(two)


async def test_backfill_goes_month_by_month_and_is_idempotent(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.news_data = [
        article(1, "Apple launches a new product line today", ["AAPL"], START + timedelta(days=2)),
        article(
            2,
            "Apple and Microsoft sign cloud agreement",
            ["AAPL", "MSFT"],
            START + timedelta(days=40),
        ),
        article(3, "Microsoft raises its quarterly dividend", ["MSFT"], NOW - timedelta(hours=3)),
        article(4, "Bitcoin rallies over the weekend again", ["BTCUSD"], NOW - timedelta(days=1)),
    ]
    ctx = make_ctx(engine, alpaca=alpaca)

    stats = await ingest_alpaca_news(ctx, ["AAPL", "MSFT"], start=START)
    assert stats.rows == 3
    assert len(alpaca.news_calls) == 3  # 75 days = three windows of at most 31 days
    assert all(end - start <= timedelta(days=31) for _, start, end in alpaca.news_calls)

    items = _items(ctx)
    assert [i.source_item_id for i in items] == ["1", "2", "3"]
    assert sorted(t.ticker for t in items[1].tickers) == ["AAPL", "MSFT"]  # mapped to both
    assert items[0].published_at == START + timedelta(days=2)

    again = await ingest_alpaca_news(ctx, ["AAPL", "MSFT"], start=START)
    assert again.rows == 0
    assert len(_items(ctx)) == 3


async def test_an_interrupted_backfill_resumes_from_the_last_finished_month(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.news_data = [
        article(
            1, "First month story about Apple supply chain", ["AAPL"], START + timedelta(days=2)
        ),
        article(2, "Third month story about Apple services", ["AAPL"], NOW - timedelta(days=2)),
    ]
    alpaca.fail_news_call = 2
    ctx = make_ctx(engine, alpaca=alpaca)

    with pytest.raises(RuntimeError):
        await ingest_alpaca_news(ctx, ["AAPL"], start=START)
    assert [i.source_item_id for i in _items(ctx)] == ["1"]  # month one was saved
    with ctx.session_factory() as session:
        saved = get_cursor(session, "alpaca.news", "AAPL")["through"]
    assert saved == (START + timedelta(days=31)).isoformat()

    alpaca.fail_news_call = None
    calls_before = len(alpaca.news_calls)
    await ingest_alpaca_news(ctx, ["AAPL"], start=START)
    assert [i.source_item_id for i in _items(ctx)] == ["1", "2"]
    resumed_from = alpaca.news_calls[calls_before][1]
    assert resumed_from >= START + timedelta(days=31) - timedelta(hours=6)  # not from the start


async def test_an_edited_article_is_updated_and_only_known_from_the_edit(engine: Engine) -> None:
    created = NOW - timedelta(days=3)
    alpaca = FakeAlpaca()
    alpaca.news_data = [article(7, "Acme guides revenue higher for the year", ["ACME"], created)]
    ctx = make_ctx(engine, alpaca=alpaca)
    await ingest_alpaca_news(ctx, ["ACME"], start=START)

    edited = NOW - timedelta(hours=2)
    alpaca.news_data = [
        article(
            7,
            "Acme guides revenue LOWER for the year (corrected)",
            ["ACME", "PEER"],
            created,
            edited,
        )
    ]
    ctx.now = lambda: NOW + timedelta(hours=1)  # the next run, an hour later
    stats = await ingest_alpaca_news(ctx, ["ACME"], start=START)

    assert stats.rows == 1
    (item,) = _items(ctx)
    assert "LOWER" in item.headline
    assert item.published_at == created
    assert item.available_at == edited  # a past decision must not see the corrected text
    assert sorted(t.ticker for t in item.tickers) == ["ACME", "PEER"]


async def test_new_tickers_added_later_get_their_own_backfill(engine: Engine) -> None:
    alpaca = FakeAlpaca()
    alpaca.news_data = [
        article(1, "Apple story from two months ago here", ["AAPL"], START + timedelta(days=5)),
        article(2, "Nvidia story from two months ago here", ["NVDA"], START + timedelta(days=6)),
    ]
    ctx = make_ctx(engine, alpaca=alpaca)
    await ingest_alpaca_news(ctx, ["AAPL"], start=START)
    await ingest_alpaca_news(ctx, ["AAPL", "NVDA"], start=START)
    assert [i.source_item_id for i in _items(ctx)] == ["1", "2"]


def test_the_same_story_from_two_sources_is_stored_once_as_original(engine: Engine) -> None:
    ctx = make_ctx(engine)
    t0 = NOW - timedelta(days=2)
    with ctx.session_factory() as session:
        store_news(session, [
            _record("finnhub", "f1", "Acme Corp. to acquire Widget Inc for $2 billion", t0 + timedelta(minutes=9), "ACME"),
            _record("alpaca", "a1", "ACME Corp to Acquire Widget Inc. for $2 Billion!", t0, "ACME", "WIDG"),
            _record("alpaca", "a2", "Acme Corp to acquire Widget Inc for $2 billion", t0 + timedelta(hours=30), "ACME"),
            _record("alpaca", "a3", "Acme Corp to acquire Widget Inc for $2 billion", t0 + timedelta(days=30), "ACME"),
            _record("alpaca", "a4", "A completely different headline about Acme", t0, "ACME"),
        ])  # fmt: skip
        session.commit()
        assert dedupe_news(session, since=NOW - timedelta(days=60), window_hours=72) == 2
        session.commit()
        assert dedupe_news(session, since=NOW - timedelta(days=60), window_hours=72) == 0  # stable

        by_id = {i.source_item_id: i for i in session.scalars(select(NewsItem))}
        original = by_id["a1"]  # the earliest copy
        assert original.duplicate_of_id is None
        assert by_id["f1"].duplicate_of_id == original.id  # other source, 9 minutes later
        assert by_id["a2"].duplicate_of_id == original.id  # repost within 72 hours
        assert by_id["a3"].duplicate_of_id is None  # a month later: a new event
        assert by_id["a4"].duplicate_of_id is None

        originals = session.scalar(
            select(func.count()).select_from(NewsItem).where(NewsItem.duplicate_of_id.is_(None))
        )
        assert originals == 3


def test_dedupe_respects_an_original_just_outside_the_scanned_range(engine: Engine) -> None:
    ctx = make_ctx(engine)
    headline = "Acme Corp announces a large share buyback programme"
    with ctx.session_factory() as session:
        store_news(session, [
            _record("alpaca", "old", headline, NOW - timedelta(hours=100), "ACME"),
            _record("alpaca", "mid", headline, NOW - timedelta(hours=60), "ACME"),
        ])  # fmt: skip
        session.commit()
        dedupe_news(session, since=NOW - timedelta(days=30), window_hours=72)
        store_news(
            session, [_record("finnhub", "new", headline, NOW - timedelta(hours=40), "ACME")]
        )
        session.commit()
        # A narrow daily pass: "old" is outside it, but "mid" still points at it.
        dedupe_news(session, since=NOW - timedelta(hours=1), window_hours=72)
        by_id = {i.source_item_id: i for i in session.scalars(select(NewsItem))}
        assert by_id["mid"].duplicate_of_id == by_id["old"].id
        assert by_id["new"].duplicate_of_id == by_id["old"].id


def test_one_article_returned_for_several_tickers_is_stored_once(engine: Engine) -> None:
    ctx = make_ctx(engine)
    with ctx.session_factory() as session:
        changed = store_news(session, [
            _record("alpaca", "1", "Chip makers rally on strong demand outlook", NOW, "NVDA"),
            _record("alpaca", "1", "Chip makers rally on strong demand outlook", NOW, "AMD", " "),
        ])  # fmt: skip
        session.commit()
        assert changed == 1
        assert session.scalars(select(NewsTicker.ticker).order_by(NewsTicker.ticker)).all() == [
            "AMD",
            "NVDA",
        ]


async def test_finnhub_news_is_a_second_source(engine: Engine) -> None:
    stamp = int((NOW - timedelta(hours=5)).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/company-news"
        symbol = request.url.params["symbol"]
        if symbol == "BAD":
            return httpx.Response(403)
        return httpx.Response(200, json=[
            {"id": 900, "datetime": stamp, "headline": f"{symbol} wins a major new contract",
             "summary": "s", "url": "https://example.com/900", "source": "Reuters", "related": symbol},
            {"id": 0, "datetime": stamp, "headline": "no id, skipped"},
        ])  # fmt: skip

    ctx = make_ctx(engine, finnhub=fetcher(handler, "finnhub"))
    stats = await ingest_finnhub_news(ctx, ["AAPL", "BAD"])
    assert stats.rows == 1
    assert any("BAD" in note for note in stats.notes)  # one bad ticker does not stop the rest
    (item,) = _items(ctx)
    assert (item.source, item.author) == ("finnhub", "Reuters")
    assert item.published_at == datetime.fromtimestamp(stamp, tz=UTC)
    assert [t.ticker for t in item.tickers] == ["AAPL"]
