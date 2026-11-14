"""Everything an ingestion step needs, bundled so steps are easy to test with fakes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy.orm import Session, sessionmaker

from papertrader import __version__
from papertrader.config import AppConfig, Settings
from papertrader.db.types import utcnow
from papertrader.ingest.alpaca_data import AlpacaData, AlpacaSdkData
from papertrader.ingest.http import Fetcher


@dataclass
class StepStats:
    """What one ingestion step did."""

    rows: int = 0
    requests: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)


@dataclass
class IngestContext:
    config: AppConfig
    session_factory: sessionmaker[Session]
    web: Fetcher  # public pages (Wikipedia)
    sec: Fetcher | None = None  # needs SEC_USER_AGENT
    fred: Fetcher | None = None
    fred_api_key: str | None = None
    finnhub: Fetcher | None = None  # API key is sent as a header by its client
    alpaca: AlpacaData | None = None
    now: Callable[[], datetime] = utcnow


def web_user_agent(contact: str | None) -> str:
    """Wikipedia asks automated tools for: name/version (contact) library/version.

    The contact is only sent if the user set WIKIPEDIA_CONTACT; nothing personal is
    sent by default.
    """
    who = contact.strip() if contact else "personal paper-trading research tool"
    return f"papertrader/{__version__} ({who}) python-httpx/{httpx.__version__}"


@asynccontextmanager
async def open_context(
    config: AppConfig, settings: Settings, session_factory: sessionmaker[Session]
) -> AsyncIterator[IngestContext]:
    """Create the real HTTP clients for every source whose key is present."""
    timeout = httpx.Timeout(60.0, connect=20.0)
    clients: list[httpx.AsyncClient] = []

    def client(headers: dict[str, str]) -> httpx.AsyncClient:
        made = httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True)
        clients.append(made)
        return made

    try:
        ctx = IngestContext(
            config=config,
            session_factory=session_factory,
            web=Fetcher(
                client({"User-Agent": web_user_agent(settings.wikipedia_contact)}),
                name="web",
                per_second=2,
            ),
        )
        if settings.sec_user_agent:
            # The SEC requires a contact string on every request and at most 10 requests/second.
            ctx.sec = Fetcher(
                client({"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"}),
                name="sec",
                per_second=config.ingest.sec_requests_per_second,
            )
        if settings.fred_api_key:
            ctx.fred = Fetcher(client({}), name="fred", per_second=1.5)
            ctx.fred_api_key = settings.fred_api_key.get_secret_value()
        if settings.finnhub_api_key:
            token = settings.finnhub_api_key.get_secret_value()
            # Free tier: 60 calls/minute.
            ctx.finnhub = Fetcher(
                client({"X-Finnhub-Token": token}), name="finnhub", per_second=0.9
            )
        if settings.alpaca_api_key and settings.alpaca_secret_key:
            ctx.alpaca = AlpacaSdkData(
                settings.alpaca_api_key.get_secret_value(),
                settings.alpaca_secret_key.get_secret_value(),
                feed=config.ingest.alpaca_feed,
                adjustment=config.ingest.bar_adjustment,
            )
        yield ctx
    finally:
        for made in clients:
            await made.aclose()
