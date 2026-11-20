"""The live-data adapter builds real Alpaca requests (checked against the installed SDK,
with the network replaced): the fakes in the scanner tests cannot catch a wrong name."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from papertrader.b.live import AlpacaLive

START, END = datetime(2026, 11, 16, 14, 30, tzinfo=UTC), datetime(2026, 11, 16, 16, 0, tzinfo=UTC)


def test_requests_are_built_with_the_sdks_own_types() -> None:
    live = AlpacaLive("PKTESTKEY000000000001", "secret", feed="iex")
    seen: dict[str, Any] = {}

    def bars(request: Any) -> dict[str, Any]:
        seen.setdefault("bars", []).append(request)
        return {
            "AAPL": [{"t": "2026-11-16T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]
        }

    def snapshots(request: Any) -> dict[str, Any]:
        seen["snapshot"] = request
        return {"AAPL": {"latestTrade": {"t": "2026-11-16T16:00:00Z", "p": 101.0},
                         "dailyBar": {"t": "2026-11-16T05:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": 5},
                         "prevDailyBar": {"t": "2026-11-13T05:00:00Z", "o": 99, "h": 100, "l": 98, "c": 100, "v": 9}}}  # fmt: skip

    def news(request: Any) -> dict[str, Any]:
        seen["news"] = request
        return {
            "news": [
                {
                    "created_at": "2026-11-16T13:05:00Z",
                    "headline": "Apple news",
                    "symbols": ["AAPL"],
                }
            ]
        }

    live._data.get_stock_bars = bars  # type: ignore[method-assign]
    live._data.get_stock_snapshot = snapshots  # type: ignore[method-assign]
    live._news.get_news = news  # type: ignore[method-assign]

    five = live.candles(["AAPL"], "5Min", START, END)
    one = live.candles(["AAPL"], "1Min", START, END, full_market=True)
    assert five["AAPL"][0].close == 1.5 and len(one["AAPL"]) == 1
    first, second = seen["bars"]
    assert (first.timeframe.amount_value, first.timeframe.unit_value.name) == (5, "Minute")
    assert first.feed.value == "iex" and second.feed.value == "sip"
    assert live.quotes(["AAPL"])["AAPL"].price == 101.0 and seen["snapshot"].feed.value == "iex"
    assert live.headlines(["AAPL"], START, END) == {"AAPL": ["2026-11-16 13:05 UTC: Apple news"]}
