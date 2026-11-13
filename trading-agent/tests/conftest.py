"""Shared fixtures. No test touches the network or needs real API keys."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from papertrader import config as config_module
from papertrader.db.migrate import upgrade_to_head
from papertrader.db.session import make_engine, make_session_factory, normalise_database_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Variables that would otherwise leak from the developer's shell / .env into tests.
_ENV_VARS = (
    "ANTHROPIC_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID", "FINNHUB_API_KEY", "FRED_API_KEY", "SEC_USER_AGENT", "DATABASE_URL",
    "PAPERTRADER_CONFIG", "DATA_DIR", "LOG_LEVEL", "LOG_FORMAT",
    "APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL",
)  # fmt: skip


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Each test starts with no secrets, no .env file and fresh config caches."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # so a real .env in the project folder is never read
    config_module.get_settings.cache_clear()
    config_module.get_config.cache_clear()
    yield
    config_module.get_settings.cache_clear()
    config_module.get_config.cache_clear()


def _database_urls() -> list[str]:
    """SQLite always; PostgreSQL too when TEST_DATABASE_URL is set (CI sets it)."""
    urls = ["sqlite"]
    if os.environ.get("TEST_DATABASE_URL"):
        urls.append("postgresql")
    return urls


@pytest.fixture(params=_database_urls())
def database_url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'test.db'}"
        return

    url = normalise_database_url(os.environ["TEST_DATABASE_URL"])
    engine = make_engine(url)

    def _reset() -> None:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))

    _reset()
    yield url
    _reset()
    engine.dispose()


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    """A database migrated to the latest schema (via the real Alembic migrations)."""
    upgrade_to_head(database_url)
    engine = make_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as session:
        yield session
