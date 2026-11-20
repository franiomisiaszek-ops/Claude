"""Live market data for the intraday scanner, straight from Alpaca (nothing is written to the
shared market tables: system B never loads market data for the others).

On the free data plan the live feed is IEX: real-time, but only the trades on one
exchange, a few percent of all volume. Prices are usually within a cent or two of the
full market for large stocks; volume is not comparable with the full market's, so the
scanner compares today's IEX volume with yesterday's IEX volume, never with the daily
history (which is the full market's). After the close, the scanner checks its prices
against the full market's minute bars (SIP, free once 15 minutes old) and records the
gap, which tells whether the $99/month real-time SIP feed would be worth it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, cast

TIMEFRAMES = {"1Min": (1, "Minute"), "5Min": (5, "Minute")}
CHUNK = 200


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: float  # the latest trade
    at: datetime | None
    day: str  # date of today's bar, "YYYY-MM-DD"
    open: float
    high: float
    low: float
    volume: float  # today so far, on this feed
    prev_close: float
    prev_volume: float  # yesterday, on this feed


@dataclass(frozen=True)
class Candle:
    at: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class LiveData(Protocol):
    def quotes(self, tickers: Sequence[str]) -> dict[str, Quote]: ...
    def candles(self, tickers: Sequence[str], timeframe: str, start: datetime, end: datetime,
                *, full_market: bool = False) -> dict[str, list[Candle]]: ...  # fmt: skip
    def headlines(
        self, tickers: Sequence[str], since: datetime, until: datetime
    ) -> dict[str, list[str]]: ...


def _time(text: Any) -> datetime | None:
    if not text:
        return None
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def parse_snapshot(ticker: str, raw: dict[str, Any]) -> Quote | None:
    trade, today, before = (
        raw.get("latestTrade") or {},
        raw.get("dailyBar") or {},
        raw.get("prevDailyBar") or {},
    )
    try:
        return Quote(
            ticker=ticker, price=float(trade["p"]), at=_time(trade.get("t")), day=str(today["t"])[:10],
            open=float(today["o"]), high=float(today["h"]), low=float(today["l"]), volume=float(today["v"]),
            prev_close=float(before["c"]), prev_volume=float(before["v"]),
        )  # fmt: skip
    except (KeyError, TypeError, ValueError):
        return None


def parse_candles(rows: Sequence[dict[str, Any]]) -> list[Candle]:
    out = []
    for row in rows:
        at = _time(row.get("t"))
        if at is None:
            continue
        out.append(Candle(at, float(row["o"]), float(row["h"]), float(row["l"]), float(row["c"]),
                          float(row.get("v", 0))))  # fmt: skip
    return out


class AlpacaLive:
    def __init__(self, api_key: str, secret_key: str, *, feed: str = "iex") -> None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import NewsClient, StockHistoricalDataClient

        self._data = StockHistoricalDataClient(api_key, secret_key, raw_data=True)
        self._news = NewsClient(api_key, secret_key, raw_data=True)
        self._feed = DataFeed(feed)
        self._full = DataFeed("sip")

    def quotes(self, tickers: Sequence[str]) -> dict[str, Quote]:
        from alpaca.data.requests import StockSnapshotRequest

        out: dict[str, Quote] = {}
        symbols = list(dict.fromkeys(tickers))
        for start in range(0, len(symbols), CHUNK):
            chunk = symbols[start : start + CHUNK]
            raw = cast(dict[str, Any], self._data.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=chunk, feed=self._feed)))  # fmt: skip
            for ticker, body in raw.items():
                if isinstance(body, dict) and (quote := parse_snapshot(ticker, body)) is not None:
                    out[ticker] = quote
        return out

    def candles(self, tickers: Sequence[str], timeframe: str, start: datetime, end: datetime,
                *, full_market: bool = False) -> dict[str, list[Candle]]:  # fmt: skip
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        amount, unit = TIMEFRAMES[timeframe]
        raw = cast(dict[str, Any], self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=list(tickers), timeframe=TimeFrame(amount, TimeFrameUnit[unit]),
            start=start, end=end, feed=self._full if full_market else self._feed)))  # fmt: skip
        bars = raw.get("bars", raw) if isinstance(raw, dict) else {}
        return {t: parse_candles(rows or []) for t, rows in bars.items() if isinstance(rows, list)}

    def headlines(
        self, tickers: Sequence[str], since: datetime, until: datetime
    ) -> dict[str, list[str]]:
        from alpaca.data.requests import NewsRequest

        if not tickers:
            return {}
        raw = cast(dict[str, Any], self._news.get_news(NewsRequest(
            symbols=",".join(tickers), start=since, end=until, sort="desc", limit=50,
            include_content=False, exclude_contentless=False)))  # fmt: skip
        out: dict[str, list[str]] = {}
        for item in raw.get("news", []):
            when = str(item.get("created_at", ""))[:16].replace("T", " ")
            for symbol in item.get("symbols", []):
                if symbol in tickers and len(out.setdefault(symbol, [])) < 5:
                    out[symbol].append(f"{when} UTC: {item.get('headline', '')}")
        return out
