"""Alembic environment. The database URL comes from DATABASE_URL (see .env)."""

from __future__ import annotations

from typing import Any, Literal

from alembic import context
from sqlalchemy import JSON, engine_from_config, pool

from papertrader.config import get_settings
from papertrader.db import models  # noqa: F401  (registers all tables on Base.metadata)
from papertrader.db.base import Base
from papertrader.db.session import normalise_database_url
from papertrader.db.types import UTCDateTime

config = context.config
target_metadata = Base.metadata

if not config.get_main_option("sqlalchemy.url"):
    url = normalise_database_url(get_settings().database_url)
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    # Keep migration files free of imports from the application package.
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if type_ == "type" and isinstance(obj, JSON) and "postgresql" in obj._variant_mapping:
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "sa.JSON().with_variant(postgresql.JSONB(), 'postgresql')"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        render_item=render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # lets SQLite handle ALTER TABLE
            render_item=render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
