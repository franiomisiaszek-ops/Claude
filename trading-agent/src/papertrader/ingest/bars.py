"""Price bars from Alpaca: idempotent, resumable, and self-correcting after splits/dividends."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select

from papertrader.db.models import PriceBar
from papertrader.ingest.alpaca_data import RawBar, timeframe_minutes
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.store import get_cursors, set_cursor, upsert
from papertrader.ingest.timeutil import (
    at_new_york,
    daily_bar_available_at,
    new_york_date,
    parse_utc,
)

log = logging.getLogger(__name__)

SOURCE = "alpaca.bars"
CHUNK = 50  # tickers per request
OVERLAP_DAYS = 7  # re-read this many days on every run to catch corrections
# If a stored close differs from the freshly downloaded one by more than this, the
# adjusted history changed (split or dividend) and the whole history is re-read.
DRIFT_TOLERANCE = 0.0005
_UPDATE = ("open", "high", "low", "close", "volume", "vwap", "trade_count", "available_at")


def _rows(raw: dict[str, list[RawBar]], timeframe: str, now: datetime) -> list[dict[str, Any]]:
    daily = timeframe.endswith("Day")
    span = timedelta(minutes=timeframe_minutes(timeframe))
    rows: list[dict[str, Any]] = []
    for ticker, bars in raw.items():
        for bar in bars:
            ts = parse_utc(str(bar["t"]))
            available = daily_bar_available_at(new_york_date(ts)) if daily else ts + span
            if available > now:
                continue  # the bar is not finished yet; storing it would be look-ahead
            rows.append(
                {
                    "ticker": ticker, "timeframe": timeframe, "ts": ts,
                    "open": float(bar["o"]), "high": float(bar["h"]), "low": float(bar["l"]),
                    "close": float(bar["c"]), "volume": float(bar["v"]),
                    "vwap": float(bar["vw"]) if bar.get("vw") is not None else None,
                    "trade_count": int(bar["n"]) if bar.get("n") is not None else None,
                    "available_at": available,
                }
            )  # fmt: skip
    return rows


def _drifted(ctx: IngestContext, rows: list[dict[str, Any]], timeframe: str) -> set[str]:
    """Tickers whose stored prices no longer match what Alpaca returns for the same bars."""
    if not rows:
        return set()
    fresh = {(row["ticker"], row["ts"]): row["close"] for row in rows}
    tickers = sorted({row["ticker"] for row in rows})
    earliest = min(row["ts"] for row in rows)
    drifted: set[str] = set()
    with ctx.session_factory() as session:
        stored = session.execute(
            select(PriceBar.ticker, PriceBar.ts, PriceBar.close).where(
                PriceBar.timeframe == timeframe,
                PriceBar.ticker.in_(tickers),
                PriceBar.ts >= earliest,
            )
        )
        for ticker, ts, close in stored:
            new = fresh.get((ticker, ts))
            if new is not None and close and abs(new / close - 1) > DRIFT_TOLERANCE:
                drifted.add(ticker)
    return drifted


async def ingest_bars(
    ctx: IngestContext, tickers: Sequence[str], *, start: date, timeframe: str = "1Day"
) -> StepStats:
    """Fetch bars for `tickers` from `start` (first run) or from where the last run stopped."""
    assert ctx.alpaca is not None
    stats = StepStats()
    now = ctx.now()
    # The free Alpaca plan only serves data that is at least 15 minutes old.
    end = now - timedelta(minutes=20)
    source = f"{SOURCE}.{timeframe}"
    daily = timeframe.endswith("Day")

    with ctx.session_factory() as session:
        cursors = get_cursors(session, source, tickers)

    groups: dict[date, list[str]] = {}
    for ticker in dict.fromkeys(tickers):
        cursor = cursors.get(ticker, {})
        covered_from = cursor.get("from")
        through = cursor.get("through")
        if through is None or covered_from is None or start < date.fromisoformat(covered_from):
            fetch_from = start  # never fetched, or the backfill now reaches further back
        else:
            fetch_from = max(start, date.fromisoformat(through) - timedelta(days=OVERLAP_DAYS))
        groups.setdefault(fetch_from, []).append(ticker)

    for fetch_from, group in sorted(groups.items()):
        for index in range(0, len(group), CHUNK):
            chunk = group[index : index + CHUNK]
            raw = await asyncio.to_thread(
                ctx.alpaca.bars, chunk, timeframe, at_new_york(fetch_from, time(0, 0)), end
            )
            stats.requests += 1
            rows = _rows(raw, timeframe, now)

            if daily and fetch_from > start:
                changed = sorted(_drifted(ctx, rows, timeframe))
                if changed:
                    stats.note(f"adjusted history re-read for {', '.join(changed)}")
                    full = await asyncio.to_thread(
                        ctx.alpaca.bars, changed, timeframe, at_new_york(start, time(0, 0)), end
                    )
                    stats.requests += 1
                    rows = [r for r in rows if r["ticker"] not in changed]
                    rows += _rows(full, timeframe, now)

            latest: dict[str, date] = {}
            for row in rows:
                day = new_york_date(row["ts"])
                if day > latest.get(row["ticker"], date.min):
                    latest[row["ticker"]] = day

            with ctx.session_factory() as session:
                stats.rows += upsert(
                    session, PriceBar, rows, conflict=("ticker", "timeframe", "ts"), update=_UPDATE
                )
                for ticker in chunk:
                    previous = cursors.get(ticker, {})
                    through = max(
                        latest.get(ticker, date.min),
                        date.fromisoformat(previous["through"])
                        if previous.get("through")
                        else date.min,
                    )
                    if through == date.min:
                        continue  # no data at all (unknown or brand-new symbol)
                    covered_from = min(
                        start,
                        date.fromisoformat(previous["from"]) if previous.get("from") else start,
                    )
                    set_cursor(
                        session, source, ticker,
                        {"from": covered_from.isoformat(), "through": through.isoformat()},
                    )  # fmt: skip
                session.commit()
            log.info(
                "bars stored",
                extra={"timeframe": timeframe, "tickers": len(chunk), "rows": len(rows)},
            )

    empty = [t for t in dict.fromkeys(tickers) if t not in cursors]
    with ctx.session_factory() as session:
        still_empty = [t for t in empty if t not in get_cursors(session, source, empty)]
    if still_empty:
        stats.note(f"no bars returned for: {', '.join(still_empty[:20])}")
    return stats
