"""Polite HTTP: a per-source rate limit plus retries with backoff.

Every external data source gets its own `Fetcher`, so one slow or rate-limited
source never affects another. Query strings are never put in error messages or
logs because some APIs (FRED) carry the API key there.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

from papertrader.logging_setup import install_url_redaction

log = logging.getLogger(__name__)
install_url_redaction()

RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class FetchError(RuntimeError):
    """A request failed for good (after retries, or with a non-retryable status)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFoundError(FetchError):
    """HTTP 404. Callers often treat this as "no data" rather than a failure."""


class RateLimiter:
    """Spaces calls so that at most `per_second` start each second."""

    def __init__(
        self,
        per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if per_second <= 0:
            raise ValueError("per_second must be positive")
        self._interval = 1.0 / per_second
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = self._clock()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            await self._sleep(wait)


def _safe_url(url: httpx.URL | str) -> str:
    parsed = httpx.URL(str(url))
    return str(parsed.copy_with(query=None))


class Fetcher:
    """GET with rate limiting, retries on 429/5xx/network errors, and Retry-After support."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        per_second: float,
        max_retries: int = 5,
        backoff_base: float = 1.0,
        backoff_cap: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._client = client
        self.name = name
        self._limiter = limiter or RateLimiter(per_second, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep or asyncio.sleep
        self.requests_made = 0

    def _delay(self, attempt: int, response: httpx.Response | None) -> float:
        if response is not None:
            retry_after = response.headers.get("Retry-After", "")
            if retry_after.isdigit():
                return min(float(retry_after), self._backoff_cap)
        base = min(self._backoff_base * (2**attempt), self._backoff_cap)
        return float(base * (0.5 + random.random() / 2))  # noqa: S311 - jitter, not security

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        last_problem = "no attempt made"
        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            self.requests_made += 1
            response: httpx.Response | None = None
            try:
                response = await self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                last_problem = f"{type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return response
                if response.status_code == 404:
                    raise NotFoundError(f"{self.name}: 404 for {_safe_url(url)}", status=404)
                if response.status_code not in RETRY_STATUS:
                    raise FetchError(
                        f"{self.name}: HTTP {response.status_code} for {_safe_url(url)}",
                        status=response.status_code,
                    )
                last_problem = f"HTTP {response.status_code}"

            if attempt < self._max_retries:
                delay = self._delay(attempt, response)
                log.warning(
                    "request failed, retrying",
                    extra={"source": self.name, "url": _safe_url(url), "problem": last_problem,
                           "attempt": attempt + 1, "retry_in_s": round(delay, 1)},
                )  # fmt: skip
                await self._sleep(delay)

        raise FetchError(
            f"{self.name}: gave up on {_safe_url(url)} after {self._max_retries + 1} attempts "
            f"({last_problem})"
        )

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return (await self.get(url, **kwargs)).json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        return (await self.get(url, **kwargs)).text

    async def get_bytes(self, url: str, **kwargs: Any) -> bytes:
        return (await self.get(url, **kwargs)).content
