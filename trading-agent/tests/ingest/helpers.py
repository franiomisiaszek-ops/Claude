"""Fakes for ingestion tests: no network, no keys, no waiting."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import Engine

from papertrader.config import AppConfig, load_config
from papertrader.db.models import UniverseMember
from papertrader.db.session import make_session_factory
from papertrader.ingest.context import IngestContext
from papertrader.ingest.http import Fetcher
from papertrader.ingest.timeutil import at_new_york, parse_utc

from ..conftest import PROJECT_ROOT

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)  # Friday 07:00 in New York
Handler = Callable[[httpx.Request], httpx.Response]


async def no_sleep(_seconds: float) -> None:
    return None


def fetcher(handler: Handler, name: str = "test", **kwargs: Any) -> Fetcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Fetcher(client, name=name, per_second=10_000, sleep=no_sleep, **kwargs)


def not_found(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def shipped_config(**ingest_overrides: Any) -> AppConfig:
    cfg = load_config(PROJECT_ROOT / "config.yaml")
    if ingest_overrides:
        cfg = cfg.model_copy(update={"ingest": cfg.ingest.model_copy(update=ingest_overrides)})
    return cfg


def make_ctx(engine: Engine, *, config: AppConfig | None = None, **parts: Any) -> IngestContext:
    return IngestContext(
        config=config or shipped_config(),
        session_factory=make_session_factory(engine),
        web=parts.pop("web", fetcher(not_found, "web")),
        now=parts.pop("now", lambda: NOW),
        **parts,
    )


def add_members(ctx: IngestContext, *members: tuple[str, str, str]) -> None:
    """(ticker, cik, sector) rows in the sp500 universe."""
    with ctx.session_factory() as session:
        for ticker, cik, sector in members:
            session.add(
                UniverseMember(index_name="sp500", ticker=ticker, name=f"{ticker} Inc",
                               gics_sector=sector, cik=cik.zfill(10), is_active=True)
            )  # fmt: skip
        session.commit()


def daily_bars(first: date, last: date, price: float = 100.0, factor: float = 1.0) -> list[dict]:
    """One bar per weekday, stamped at New York midnight like Alpaca does."""
    bars, day, index = [], first, 0
    while day <= last:
        if day.weekday() < 5:
            close = round((price + index) * factor, 4)
            bars.append({"t": at_new_york(day, time(0, 0)).isoformat().replace("+00:00", "Z"),
                         "o": close - 1, "h": close + 1, "l": close - 2, "c": close,
                         "v": 1_000_000 + index, "n": 5000, "vw": close - 0.5})  # fmt: skip
            index += 1
        day += timedelta(days=1)
    return bars


def article(item_id: int, headline: str, symbols: Sequence[str], created: datetime,
            updated: datetime | None = None, summary: str = "summary") -> dict[str, Any]:  # fmt: skip
    return {"id": item_id, "headline": headline, "summary": summary, "author": "Benzinga Newsdesk",
            "url": f"https://example.com/{item_id}", "symbols": list(symbols), "source": "benzinga",
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "updated_at": (updated or created).isoformat().replace("+00:00", "Z"), "content": ""}  # fmt: skip


class FakeAlpaca:
    """Stands in for the Alpaca SDK adapter; records every call."""

    def __init__(self) -> None:
        self.bar_data: dict[str, list[dict]] = {}
        self.news_data: list[dict[str, Any]] = []
        self.bar_calls: list[tuple[tuple[str, ...], str, datetime, datetime]] = []
        self.news_calls: list[tuple[tuple[str, ...], datetime, datetime]] = []
        self.fail_when: Callable[[tuple[str, ...]], bool] = lambda _symbols: False
        self.fail_news_call: int | None = None

    def bars(self, symbols: Sequence[str], timeframe: str, start: datetime, end: datetime):  # type: ignore[no-untyped-def]
        self.bar_calls.append((tuple(symbols), timeframe, start, end))
        if self.fail_when(tuple(symbols)):
            raise RuntimeError("alpaca is down")
        return {
            s: [b for b in self.bar_data[s] if start <= parse_utc(b["t"]) <= end]
            for s in symbols
            if s in self.bar_data
        }

    def news(self, symbols: Sequence[str], start: datetime, end: datetime):  # type: ignore[no-untyped-def]
        self.news_calls.append((tuple(symbols), start, end))
        if self.fail_news_call == len(self.news_calls):
            raise RuntimeError("alpaca is down")
        return [
            a for a in self.news_data
            if set(a["symbols"]) & set(symbols) and start <= parse_utc(a["updated_at"]) <= end
        ]  # fmt: skip
