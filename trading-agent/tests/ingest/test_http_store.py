from __future__ import annotations

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papertrader.db.models import MacroObservation, SystemState
from papertrader.ingest.http import FetchError, NotFoundError, RateLimiter
from papertrader.ingest.store import get_cursor, get_cursors, set_cursor, upsert

from .helpers import NOW, fetcher


async def test_rate_limiter_spaces_calls() -> None:
    clock = {"now": 0.0}
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    limiter = RateLimiter(per_second=10, clock=lambda: clock["now"], sleep=sleep)
    for _ in range(4):
        await limiter.acquire()
    assert waits == pytest.approx([0.1, 0.1, 0.1])  # the first call goes straight through


async def test_retries_on_429_and_honours_retry_after() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    async def sleep(seconds: float) -> None:
        waits.append(seconds)

    f = fetcher(handler)
    f._sleep = sleep
    assert await f.get_json("https://api.example.com/x") == {"ok": True}
    assert len(calls) == 3
    assert waits == [7.0, 7.0]


async def test_gives_up_after_max_retries_on_server_errors() -> None:
    f = fetcher(lambda _r: httpx.Response(503), max_retries=2)
    with pytest.raises(FetchError, match="after 3 attempts"):
        await f.get("https://api.example.com/x")
    assert f.requests_made == 3


async def test_network_errors_are_retried() -> None:
    attempts: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, text="fine")

    assert await fetcher(handler).get_text("https://api.example.com/x") == "fine"


async def test_404_and_403_are_not_retried() -> None:
    f = fetcher(lambda _r: httpx.Response(404))
    with pytest.raises(NotFoundError):
        await f.get("https://api.example.com/x")
    assert f.requests_made == 1

    f = fetcher(lambda _r: httpx.Response(403))
    with pytest.raises(FetchError, match="HTTP 403") as caught:
        await f.get("https://api.example.com/x")
    assert f.requests_made == 1
    assert caught.value.status == 403


async def test_api_keys_in_the_query_never_reach_error_messages() -> None:
    f = fetcher(lambda _r: httpx.Response(500), max_retries=0)
    with pytest.raises(FetchError) as caught:
        await f.get("https://api.example.com/x", params={"api_key": "SECRET-KEY-123"})
    assert "SECRET-KEY-123" not in str(caught.value)
    assert "api.example.com/x" in str(caught.value)


def _obs(value: float | None) -> dict[str, object]:
    return {"series_id": "DGS10", "observation_date": NOW.date(), "realtime_start": NOW.date(),
            "value": value, "available_at": NOW}  # fmt: skip


def test_upsert_inserts_then_ignores_duplicates(session: Session) -> None:
    key = ("series_id", "observation_date", "realtime_start")
    assert upsert(session, MacroObservation, [_obs(4.1)], conflict=key) == 1
    assert upsert(session, MacroObservation, [_obs(9.9)], conflict=key) == 0
    session.commit()
    assert session.scalars(select(MacroObservation.value)).all() == [4.1]


def test_upsert_updates_only_when_a_value_really_changed(session: Session) -> None:
    key = ("series_id", "observation_date", "realtime_start")
    upsert(session, MacroObservation, [_obs(4.1)], conflict=key, update=("value",))
    assert upsert(session, MacroObservation, [_obs(4.1)], conflict=key, update=("value",)) == 0
    assert upsert(session, MacroObservation, [_obs(4.2)], conflict=key, update=("value",)) == 1
    assert upsert(session, MacroObservation, [_obs(None)], conflict=key, update=("value",)) == 1
    session.commit()
    assert session.scalars(select(MacroObservation.value)).all() == [None]
    assert session.scalar(select(func.count()).select_from(MacroObservation)) == 1


def test_upsert_handles_large_batches(session: Session) -> None:
    rows = [{"key": f"k{i}", "value": {"i": i}, "updated_at": NOW} for i in range(1200)]
    assert upsert(session, SystemState, rows, conflict=("key",)) == 1200
    assert upsert(session, SystemState, rows, conflict=("key",)) == 0


def test_cursors_round_trip(session: Session) -> None:
    assert get_cursor(session, "alpaca.news", "AAPL") == {}
    set_cursor(session, "alpaca.news", "AAPL", {"through": "2026-01-01"})
    set_cursor(session, "alpaca.news", "MSFT", {"through": "2026-02-01"})
    set_cursor(session, "alpaca.news", "AAPL", {"through": "2026-03-01"})
    session.commit()
    assert get_cursor(session, "alpaca.news", "AAPL") == {"through": "2026-03-01"}
    assert get_cursors(session, "alpaca.news", ["AAPL", "MSFT", "NVDA"]) == {
        "AAPL": {"through": "2026-03-01"},
        "MSFT": {"through": "2026-02-01"},
    }
