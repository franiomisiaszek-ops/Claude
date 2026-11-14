"""Startup: load config, enforce the paper-only guard, connect to the database."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig, Settings, get_config, get_settings
from papertrader.db.migrate import current_revision, head_revision, upgrade_to_head
from papertrader.db.session import make_engine, make_session_factory
from papertrader.logging_setup import configure_logging
from papertrader.safety import ALPACA_PAPER_BASE_URL, assert_paper_only

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    config: AppConfig
    engine: Engine
    session_factory: sessionmaker[Session]


def bootstrap(
    *, migrate: bool = False, log_format: Literal["json", "console"] | None = None
) -> Runtime:
    """Everything that must be true before any other code runs.

    The paper-only guard comes first: if it fails, nothing else is touched.
    `log_format` overrides LOG_FORMAT for commands a person runs at a terminal.
    """
    settings = get_settings()
    configure_logging(settings.log_level, log_format or settings.log_format)

    api_key = settings.alpaca_api_key.get_secret_value() if settings.alpaca_api_key else None
    assert_paper_only(ALPACA_PAPER_BASE_URL, api_key=api_key)

    config = get_config()
    if migrate:
        upgrade_to_head(settings.database_url)
    engine = make_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    log.info(
        "startup checks passed",
        extra={
            "trading_endpoint": ALPACA_PAPER_BASE_URL,
            "mode": "PAPER",
            "database": engine.url.get_backend_name(),
            "universe": config.universe.index,
            "monthly_budget_usd": config.budget.monthly_usd,
        },
    )
    return Runtime(settings, config, engine, make_session_factory(engine))


def migration_status(runtime: Runtime) -> tuple[str | None, str | None]:
    """(revision the database is at, newest revision in the code)."""
    return current_revision(runtime.engine), head_revision(runtime.settings.database_url)
