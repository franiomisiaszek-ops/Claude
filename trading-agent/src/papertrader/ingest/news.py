"""News from Alpaca (and optionally Finnhub), mapped to tickers and de-duplicated."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from papertrader.db.models import NewsItem, NewsTicker
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.http import FetchError
from papertrader.ingest.store import get_cursors, set_cursor
from papertrader.ingest.timeutil import new_york_date, parse_utc

log = logging.getLogger(__name__)

ALPACA_SOURCE = "alpaca.news"
FINNHUB_SOURCE = "finnhub.news"
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
GROUP_SIZE = 30  # tickers per Alpaca request
WINDOW = timedelta(days=31)  # one request series per month keeps a backfill resumable
OVERLAP = timedelta(hours=6)

_NON_WORD = re.compile(r"[^a-z0-9]+")
_MIN_HASHABLE = 15  # shorter headlines ("Update", "") are too generic to compare


@dataclass
class NewsRecord:
    source: str
    source_item_id: str
    headline: str
    published_at: datetime
    available_at: datetime
    summary: str | None = None
    url: str | None = None
    author: str | None = None
    tickers: set[str] = field(default_factory=set)


def normalise_headline(headline: str) -> str:
    text = unicodedata.normalize("NFKC", headline).lower()
    return _NON_WORD.sub(" ", text).strip()


def headline_hash(record: NewsRecord) -> str:
    """Same story, same hash - whatever the source, punctuation or capitalisation."""
    normalised = normalise_headline(record.headline)
    if len(normalised) < _MIN_HASHABLE:
        normalised = f"{record.source}:{record.source_item_id}"  # never matches anything else
    return hashlib.sha256(normalised.encode()).hexdigest()


def _clean_ticker(symbol: str) -> str | None:
    cleaned = symbol.strip().upper()
    return cleaned if 0 < len(cleaned) <= 16 else None


def store_news(session: Session, records: Iterable[NewsRecord]) -> int:
    """Insert new items, refresh edited ones. Returns how many rows were added or changed."""
    by_key: dict[tuple[str, str], NewsRecord] = {}
    for record in records:
        key = (record.source, record.source_item_id)
        if key in by_key:  # the same article can come back for several tickers
            by_key[key].tickers |= record.tickers
            if record.available_at > by_key[key].available_at:
                record.tickers = by_key[key].tickers
                by_key[key] = record
        else:
            by_key[key] = record

    changed = 0
    by_source: dict[str, list[NewsRecord]] = defaultdict(list)
    for record in by_key.values():
        by_source[record.source].append(record)

    for source, group in by_source.items():
        for start in range(0, len(group), 500):
            part = group[start : start + 500]
            existing = {
                item.source_item_id: item
                for item in session.scalars(
                    select(NewsItem)
                    .where(
                        NewsItem.source == source,
                        NewsItem.source_item_id.in_([r.source_item_id for r in part]),
                    )
                    .options(selectinload(NewsItem.tickers))
                )
            }
            for record in part:
                tickers = {t for t in map(_clean_ticker, record.tickers) if t}
                item = existing.get(record.source_item_id)
                if item is None:
                    session.add(
                        NewsItem(
                            source=source, source_item_id=record.source_item_id,
                            headline=record.headline, summary=record.summary, url=record.url,
                            author=record.author, published_at=record.published_at,
                            available_at=record.available_at, content_hash=headline_hash(record),
                            tickers=[NewsTicker(ticker=t) for t in sorted(tickers)],
                        )
                    )  # fmt: skip
                    changed += 1
                    continue
                touched = False
                if record.available_at > item.available_at:  # the article was edited
                    item.headline = record.headline
                    item.summary = record.summary
                    item.available_at = record.available_at
                    item.content_hash = headline_hash(record)
                    touched = True
                missing = tickers - {link.ticker for link in item.tickers}
                if missing:
                    item.tickers.extend(NewsTicker(ticker=t) for t in sorted(missing))
                    touched = True
                changed += touched
        session.flush()
    return changed


def _alpaca_record(raw: dict[str, object]) -> NewsRecord | None:
    headline = str(raw.get("headline") or "").strip()
    if not headline or raw.get("id") is None or not raw.get("created_at"):
        return None
    created = parse_utc(str(raw["created_at"]))
    updated = parse_utc(str(raw.get("updated_at") or raw["created_at"]))
    symbols = raw.get("symbols") or []
    return NewsRecord(
        source="alpaca",
        source_item_id=str(raw["id"]),
        headline=headline,
        summary=str(raw.get("summary") or "").strip() or None,
        url=str(raw.get("url") or "") or None,
        author=str(raw.get("author") or "")[:255] or None,
        published_at=created,
        # An edited article may contain later information, so it only counts as known
        # from its last edit.
        available_at=max(created, updated),
        tickers={str(s) for s in symbols} if isinstance(symbols, list) else set(),
    )


async def ingest_alpaca_news(
    ctx: IngestContext, tickers: Sequence[str], *, start: datetime
) -> StepStats:
    """Fetch news month by month. Each finished month is committed, so a restart resumes."""
    assert ctx.alpaca is not None
    stats = StepStats()
    end = ctx.now()
    unique = list(dict.fromkeys(tickers))

    with ctx.session_factory() as session:
        cursors = get_cursors(session, ALPACA_SOURCE, unique)
    progress: dict[str, datetime] = {}
    first_run: set[str] = set()
    for ticker in unique:
        cursor = cursors.get(ticker, {})
        covered_from = cursor.get("from")
        if cursor.get("through") and covered_from and parse_utc(covered_from) <= start:
            progress[ticker] = parse_utc(cursor["through"])
        else:
            progress[ticker] = start
            first_run.add(ticker)

    while True:
        todo = {t: at for t, at in progress.items() if end - at > timedelta(minutes=1)}
        if not todo:
            break
        earliest = min(todo.values())
        group = [t for t, at in todo.items() if at == earliest][:GROUP_SIZE]
        window_end = min(earliest + WINDOW, end)
        fetch_from = earliest if group[0] in first_run else earliest - OVERLAP

        raw = await asyncio.to_thread(ctx.alpaca.news, group, fetch_from, window_end)
        stats.requests += 1
        records = [r for r in map(_alpaca_record, raw) if r is not None]

        with ctx.session_factory() as session:
            stats.rows += store_news(session, records)
            for ticker in group:
                set_cursor(
                    session, ALPACA_SOURCE, ticker,
                    {"from": start.isoformat(), "through": window_end.isoformat()},
                )  # fmt: skip
            session.commit()
        for ticker in group:
            progress[ticker] = window_end
            if window_end >= end:
                first_run.discard(ticker)
        log.info(
            "news stored",
            extra={"tickers": len(group), "items": len(records),
                   "window_start": new_york_date(earliest).isoformat()},
        )  # fmt: skip
    return stats


async def ingest_finnhub_news(
    ctx: IngestContext, tickers: Sequence[str], *, days: int = 3
) -> StepStats:
    """Recent company news from Finnhub: a second source next to Alpaca."""
    assert ctx.finnhub is not None
    stats = StepStats()
    today = ctx.now().date()
    params_base = {"from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()}

    for ticker in dict.fromkeys(tickers):
        try:
            raw = await ctx.finnhub.get_json(
                FINNHUB_NEWS_URL, params={"symbol": ticker, **params_base}
            )
        except FetchError as exc:
            stats.note(f"{ticker}: {exc}")
            continue
        stats.requests += 1
        records = []
        for item in raw if isinstance(raw, list) else []:
            headline = str(item.get("headline") or "").strip()
            if not headline or not item.get("id") or not item.get("datetime"):
                continue
            published = datetime.fromtimestamp(int(item["datetime"]), tz=UTC)
            records.append(
                NewsRecord(
                    source="finnhub", source_item_id=str(item["id"]), headline=headline,
                    summary=str(item.get("summary") or "").strip() or None,
                    url=str(item.get("url") or "") or None,
                    author=str(item.get("source") or "")[:255] or None,
                    published_at=published, available_at=published, tickers={ticker},
                )
            )  # fmt: skip
        with ctx.session_factory() as session:
            stats.rows += store_news(session, records)
            session.commit()
    return stats


def dedupe_news(session: Session, *, since: datetime, window_hours: int) -> int:
    """Mark repeats of the same story. The earliest copy stays the original.

    Two items are the same story when their normalised headlines match and they were
    published within `window_hours` of the original. Safe to run again and again.
    """
    window = timedelta(hours=window_hours)
    floor = since - window
    repeated = (
        select(NewsItem.content_hash)
        .where(NewsItem.published_at >= floor)
        .group_by(NewsItem.content_hash)
        .having(func.count() > 1)
    )
    items = session.scalars(
        select(NewsItem)
        .where(NewsItem.content_hash.in_(repeated), NewsItem.published_at >= floor)
        .order_by(NewsItem.content_hash, NewsItem.published_at, NewsItem.id)
    ).all()

    changed = 0
    original: NewsItem | None = None
    for item in items:
        if original is None or item.content_hash != original.content_hash:
            original = None
            # The first item we can see may itself repeat an older story outside the range.
            if item.duplicate_of_id is not None:
                older = session.get(NewsItem, item.duplicate_of_id)
                if older is not None and older.content_hash == item.content_hash:
                    original = older
        if original is None or item.published_at - original.published_at > window:
            original = item
            target = None
        else:
            target = original.id
        if item.duplicate_of_id != target:
            item.duplicate_of_id = target
            changed += 1
    session.flush()
    return changed
