"""Alpaca Market Data (prices and news) through the official `alpaca-py` SDK.

This talks to Alpaca's *data* service only. It cannot place orders: trading lives
in `papertrader.trading` (Phase 5) and is locked to the paper endpoint.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

RawBar = dict[str, Any]  # keys: t, o, h, l, c, v, n, vw
RawNews = dict[
    str, Any
]  # keys: id, headline, summary, author, url, symbols, created_at, updated_at


class AlpacaData(Protocol):
    """The two calls ingestion needs. Tests supply a fake with the same shape."""

    def bars(
        self, symbols: Sequence[str], timeframe: str, start: datetime, end: datetime
    ) -> dict[str, list[RawBar]]: ...

    def news(self, symbols: Sequence[str], start: datetime, end: datetime) -> list[RawNews]: ...


_TIMEFRAME = re.compile(r"^(\d+)(Min|Hour|Day)$")
_UNITS = {"Min": TimeFrameUnit.Minute, "Hour": TimeFrameUnit.Hour, "Day": TimeFrameUnit.Day}
_UNIT_MINUTES = {"Min": 1, "Hour": 60, "Day": 24 * 60}


def split_timeframe(timeframe: str) -> tuple[int, str]:
    match = _TIMEFRAME.match(timeframe)
    if not match:
        raise ValueError(f"unknown timeframe {timeframe!r}; use e.g. 1Day, 10Min, 1Hour")
    return int(match.group(1)), match.group(2)


def timeframe_minutes(timeframe: str) -> int:
    amount, unit = split_timeframe(timeframe)
    return amount * _UNIT_MINUTES[unit]


class _Throttle:
    """Blocking rate limit for the SDK's worker thread (free plan: 200 requests/minute)."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1.0 / per_second
        self._next = 0.0
        self._lock = threading.Lock()
        self.calls = 0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
            self.calls += 1
        if delay > 0:
            time.sleep(delay)


# The SDK pages through results by itself, one HTTP request per page. Overriding the
# single-request hook is the only place to slow that loop down to the rate limit.
class _ThrottledBarsClient(StockHistoricalDataClient):
    throttle: _Throttle

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:  # type: ignore[type-arg]
        self.throttle.wait()
        return super()._one_request(method, url, opts, retry)


class _ThrottledNewsClient(NewsClient):
    throttle: _Throttle

    def _one_request(self, method: str, url: str, opts: dict, retry: int) -> dict:  # type: ignore[type-arg]
        self.throttle.wait()
        return super()._one_request(method, url, opts, retry)


class AlpacaSdkData:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        feed: str = "sip",
        adjustment: str = "all",
        requests_per_second: float = 2.8,
    ) -> None:
        self.throttle = _Throttle(requests_per_second)
        self._bars = _ThrottledBarsClient(api_key, secret_key, raw_data=True)
        self._news = _ThrottledNewsClient(api_key, secret_key, raw_data=True)
        self._bars.throttle = self.throttle
        self._news.throttle = self.throttle
        self._feed = DataFeed(feed)
        self._adjustment = Adjustment(adjustment)

    def bars(
        self, symbols: Sequence[str], timeframe: str, start: datetime, end: datetime
    ) -> dict[str, list[RawBar]]:
        amount, unit = split_timeframe(timeframe)
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame(amount, _UNITS[unit]),
            start=start,
            end=end,
            adjustment=self._adjustment,
            feed=self._feed,
        )
        raw = self._bars.get_stock_bars(request)
        assert isinstance(raw, dict)
        return raw

    def news(self, symbols: Sequence[str], start: datetime, end: datetime) -> list[RawNews]:
        request = NewsRequest(
            symbols=",".join(symbols),
            start=start,
            end=end,
            sort="asc",
            include_content=False,
            exclude_contentless=False,
        )
        raw = self._news.get_news(request)
        assert isinstance(raw, dict)
        return list(raw.get("news", []))
