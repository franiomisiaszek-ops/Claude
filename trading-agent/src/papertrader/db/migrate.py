"""Run Alembic migrations from Python (used by the CLI and at container start)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from papertrader.db.session import ensure_sqlite_directory, normalise_database_url

# Migrations ship inside the package so they are found wherever it is installed.
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # '%' must be doubled because Alembic's config uses %-interpolation.
    cfg.set_main_option("sqlalchemy.url", normalise_database_url(database_url).replace("%", "%%"))
    return cfg


def upgrade_to_head(database_url: str) -> None:
    ensure_sqlite_directory(database_url)
    command.upgrade(alembic_config(database_url), "head")


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def head_revision(database_url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
