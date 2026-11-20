"""Startup: load config, enforce the paper-only guard, connect to the database."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig, ConfigError, Settings, get_config, get_settings
from papertrader.db.migrate import current_revision, head_revision, upgrade_to_head
from papertrader.db.provision import provision, verify
from papertrader.db.session import make_engine, make_session_factory, make_system_engine
from papertrader.logging_setup import configure_logging
from papertrader.safety import ALPACA_PAPER_BASE_URL, assert_distinct_paper_keys, assert_paper_only
from papertrader.systems import SYSTEMS, SystemName, check_system

log = logging.getLogger(__name__)

# How long B, C and the hub wait at start for system A to set up the shared tables.
SHARED_WAIT_SECONDS = 300.0


class SharedSchemaNotReadyError(ConfigError):
    pass


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    config: AppConfig
    engine: Engine
    session_factory: sessionmaker[Session]
    system: SystemName = "A"


def paper_guard(settings: Settings, system: SystemName) -> None:
    """Paper endpoint only, no live keys, and a different paper account per system."""
    key = settings.alpaca_keys(system)[0]
    assert_paper_only(ALPACA_PAPER_BASE_URL, api_key=key.get_secret_value() if key else None)
    assert_distinct_paper_keys({
        name: (k.get_secret_value() if (k := settings.alpaca_keys(name)[0]) else None) for name in SYSTEMS
    })  # fmt: skip


def wait_for_shared_schema(
    database_url: str,
    seconds: float = SHARED_WAIT_SECONDS,
    *,
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until system A has set up the shared tables (prices, news, ... and A's own).

    Only A creates them: if A, B and the hub all did it at the same moment on a new
    database, they would trip over each other.
    """
    head = head_revision(database_url)
    engine = make_engine(database_url)
    deadline = clock() + seconds
    try:
        while True:
            try:
                current = current_revision(engine)
            except OperationalError:
                current = None
            if current == head:
                return
            if clock() >= deadline:
                raise SharedSchemaNotReadyError(
                    f"System A has not set up the shared database yet (it is at {current}, "
                    f"needs {head}). Start system A first: docker compose up -d app"
                )
            log.info("waiting for system A to set up the shared database",
                     extra={"at": current, "needs": head})  # fmt: skip
            sleep(poll)
    finally:
        engine.dispose()


def bootstrap(
    *,
    migrate: bool = False,
    log_format: Literal["json", "console"] | None = None,
    system: str | None = None,
    owns_shared: bool | None = None,
    wait_seconds: float = SHARED_WAIT_SECONDS,
) -> Runtime:
    """Everything that must be true before any other code runs.

    The paper-only guard comes first: if it fails, nothing else is touched.
    `log_format` overrides LOG_FORMAT for commands a person runs at a terminal.
    `owns_shared`: whether this program may create the shared tables. By default only
    system A does; B, C and the hub wait for it instead.
    """
    settings = get_settings()
    configure_logging(settings.log_level, log_format or settings.log_format)

    name = check_system(system or settings.system)
    paper_guard(settings, name)

    config = get_config()
    if migrate:
        # The shared schema (A's tables and the market data). A no-op once it is at the
        # frozen revision, which it never moves past (see papertrader.systems).
        if name == "A" if owns_shared is None else owns_shared:
            upgrade_to_head(settings.database_url)
        else:
            wait_for_shared_schema(settings.database_url, wait_seconds)
    engine = make_system_engine(settings.database_url, name)
    if name != "A":
        if migrate:
            provision(engine, name)
        verify(engine, name)  # never let B or C fall through to A's tables
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    log.info(
        "startup checks passed",
        extra={
            "trading_endpoint": ALPACA_PAPER_BASE_URL,
            "mode": "PAPER",
            "system": name,
            "database": engine.url.get_backend_name(),
            "universe": config.universe.index,
            "monthly_budget_usd": config.budget.monthly_usd,
        },
    )
    return Runtime(settings, config, engine, make_session_factory(engine), name)


def migration_status(runtime: Runtime) -> tuple[str | None, str | None]:
    """(revision the database is at, newest revision in the code)."""
    return current_revision(runtime.engine), head_revision(runtime.settings.database_url)
