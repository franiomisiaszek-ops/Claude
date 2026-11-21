"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def normalise_database_url(url: str) -> str:
    """Make sure PostgreSQL URLs use the psycopg 3 driver that is installed."""
    parsed = make_url(url)
    if parsed.drivername in ("postgres", "postgresql", "postgresql+psycopg2"):
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed.render_as_string(hide_password=False)


def ensure_sqlite_directory(database_url: str) -> None:
    """SQLite cannot create missing folders itself; make the parent folder if needed."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str, *, read_only: bool = False) -> Engine:
    """Create an engine. `read_only=True` makes the database reject all writes (the web app's pages)."""
    url = make_url(normalise_database_url(database_url))
    kwargs: dict[str, Any] = {"pool_pre_ping": True}

    if url.get_backend_name() == "sqlite":
        ensure_sqlite_directory(database_url)
        engine = create_engine(url, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
            cursor.close()

        return engine

    if read_only:
        kwargs["connect_args"] = {"options": "-c default_transaction_read_only=on"}
    return create_engine(url, **kwargs)


def make_system_engine(database_url: str, system: str, *, read_only: bool = False) -> Engine:
    """An engine for one system of the experiment (see papertrader.systems).

    A uses the database as it is. B and C see their own schema (PostgreSQL) or their
    own file (SQLite) first, and the shared market data behind it.
    """
    from papertrader.systems import A_SCHEMA, check_system, schema_name, sqlite_file

    name = check_system(system)
    if name == "A":
        return make_engine(database_url, read_only=read_only)
    url = make_url(normalise_database_url(database_url))
    kwargs: dict[str, Any] = {"pool_pre_ping": True}

    if url.get_backend_name() == "sqlite":
        a_file = sqlite_file(database_url, "A")
        own = url.set(database=sqlite_file(database_url, name))
        ensure_sqlite_directory(own.render_as_string(hide_password=False))
        engine = create_engine(own, **kwargs)

        @event.listens_for(engine, "connect")
        def _attach_shared(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"ATTACH DATABASE ? AS {A_SCHEMA['sqlite']}", (a_file,))
            if read_only:
                cursor.execute("PRAGMA query_only=ON")
            cursor.close()

        return engine

    options = f"-c search_path={schema_name(name)},public"
    if read_only:
        options += " -c default_transaction_read_only=on"
    kwargs["connect_args"] = {"options": options}
    return create_engine(url, **kwargs)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
