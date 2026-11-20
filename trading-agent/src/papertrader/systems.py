"""The three systems of the experiment, and where each one keeps its data.

    A  the original system, frozen. Its tables live in the default schema ("public"),
       next to the shared market data, exactly as before the experiment.
    B  A's research plus a better decision layer.
    C  a rules-based weekly ETF trend portfolio.

Market data (prices, news, filings, ...) is loaded once, by A, and read by everyone.
B and C each have their own copy of every *system* table (decisions, orders, trades,
outcomes, messages, budget) in a schema of their own: "sys_b" and "sys_c". A database
connection for B looks in sys_b first and falls back to the shared market tables, so
the same code serves all three without ever mixing their decisions.

On SQLite (tests, local runs) B and C get their own database file, with A's file
attached: unqualified names find B's own tables first and A's market tables otherwise.

The freeze rule: A runs from an image built at the start of the experiment, so the
shared schema must stay as it was. No new Alembic migrations, and no new columns on
existing tables. New tables for B or C are created in their own schemas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast, get_args

from sqlalchemy import Table
from sqlalchemy.engine import make_url

SystemName = Literal["A", "B", "C"]
SYSTEMS: tuple[SystemName, ...] = cast(tuple[SystemName, ...], get_args(SystemName))

# Shared, read-only for B and C. Loaded by A's ingest job.
MARKET_TABLES = frozenset({
    "universe_members", "price_bars", "news_items", "news_features", "news_tickers", "filings",
    "insider_transactions", "institutional_holdings", "security_identifiers", "earnings_events",
    "macro_observations", "ingest_state",
})  # fmt: skip

# The Alembic revision A was frozen at. The shared schema must never move past it.
FROZEN_REVISION = "0006"

# Where A's own tables are, seen from a B or C connection.
A_SCHEMA = {"postgresql": "public", "sqlite": "a"}


class UnknownSystemError(ValueError):
    pass


def check_system(name: str) -> SystemName:
    upper = name.strip().upper()
    if upper not in SYSTEMS:
        raise UnknownSystemError(f"unknown system {name!r}; the systems are A, B and C")
    return upper


def schema_name(system: SystemName) -> str | None:
    """PostgreSQL schema of a system's own tables (None: the default schema, for A)."""
    return None if system == "A" else f"sys_{system.lower()}"


def system_tables() -> list[Table]:
    """Every table each system has its own copy of (all but the shared market data)."""
    from papertrader.db import models  # noqa: F401  (registers the tables)
    from papertrader.db.base import Base

    return [t for name, t in Base.metadata.tables.items() if name not in MARKET_TABLES]


def sqlite_file(database_url: str, system: SystemName) -> str:
    """The SQLite file of a system: data/papertrader.db -> data/papertrader_b.db for B."""
    path = Path(make_url(database_url).database or "")
    if system == "A":
        return str(path)
    return str(path.with_name(f"{path.stem}_{system.lower()}{path.suffix or '.db'}"))


def tag(system: SystemName) -> str:
    """Prefix for messages and logs, e.g. "[B]"."""
    return f"[{system}]"
