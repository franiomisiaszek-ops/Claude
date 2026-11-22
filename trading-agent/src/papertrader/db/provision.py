"""Create and check the own tables of systems B and C.

A's tables are created by the Alembic migrations, as always. B's and C's copies of the
system tables are created here from the same table definitions, in their own schema
(PostgreSQL) or file (SQLite). The freeze rule (see papertrader.systems) means these
definitions no longer change for existing tables, so creating missing tables is all
that is ever needed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, Engine, Table, inspect, text

from papertrader.config import ConfigError
from papertrader.systems import A_SCHEMA, SystemName, schema_name, system_tables


class SchemaNotReadyError(ConfigError):
    """A system's own tables are missing or different: run `papertrader db provision`."""


def extra_tables(system: SystemName) -> list[Table]:
    """Tables only this system has (outside the shared definitions, see b/models.py)."""
    from papertrader.notify.warnings import WarnBase  # X6: the order warnings, B and C

    if system == "B":
        from papertrader.b.models import BBase

        return [*BBase.metadata.sorted_tables, *WarnBase.metadata.sorted_tables]
    if system == "C":
        from papertrader.c.models import CBase

        return [*CBase.metadata.sorted_tables, *WarnBase.metadata.sorted_tables]
    return []


def own_tables(system: SystemName) -> list[Table]:
    return [*system_tables(), *extra_tables(system)]


def own_schema(engine: Engine, system: SystemName) -> str | None:
    """Where a system's own tables are, as seen from its engine."""
    if engine.url.get_backend_name() == "sqlite":
        return "main"
    return schema_name(system)


def a_tables(engine: Engine) -> dict[str | None, str]:
    """A schema_translate_map that points unqualified table names at A's own tables."""
    return {None: A_SCHEMA[engine.url.get_backend_name()]}


def provision(engine: Engine, system: SystemName) -> list[str]:
    """Create the system's own tables where missing. Returns the names it created."""
    if system == "A":
        raise ValueError("A's tables are created by the Alembic migrations, not here")
    schema = own_schema(engine, system)
    missing = _missing_tables(engine, schema, system)
    with engine.begin() as conn:
        if engine.url.get_backend_name() != "sqlite":
            conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        translated: Connection = conn.execution_options(schema_translate_map={None: schema})
        tables = [t for t in own_tables(system) if t.name in missing]
        for metadata in {id(t.metadata): t.metadata for t in tables}.values():
            metadata.create_all(translated, tables=[t for t in tables if t.metadata is metadata],
                                checkfirst=True)  # fmt: skip
    return sorted(missing)


def _missing_tables(engine: Engine, schema: str | None, system: SystemName) -> set[str]:
    existing = set(inspect(engine).get_table_names(schema=schema))
    return {t.name for t in own_tables(system)} - existing


def verify(engine: Engine, system: SystemName) -> None:
    """Refuse to run unless every own table exists with the expected columns.

    Without this a missing table would silently fall through to A's table of the same
    name, and B would write into A's data.
    """
    if system == "A":
        return
    schema = own_schema(engine, system)
    inspector = inspect(engine)
    existing = set(inspector.get_table_names(schema=schema))
    problems: list[str] = []
    for table in own_tables(system):
        if table.name not in existing:
            problems.append(f"table {table.name} is missing")
            continue
        columns = {c["name"] for c in inspector.get_columns(table.name, schema=schema)}
        expected = {c.name for c in table.columns}
        if columns != expected:
            problems.append(f"table {table.name}: columns {sorted(columns ^ expected)} differ")
    if problems:
        raise SchemaNotReadyError(
            f"System {system}'s own tables are not ready ({'; '.join(problems[:5])}). "
            f"Run: papertrader --system {system} db provision"
        )


def describe(engine: Engine, system: SystemName) -> dict[str, Any]:
    schema = own_schema(engine, system) if system != "A" else None
    missing = _missing_tables(engine, schema, system) if system != "A" else set()
    return {"system": system, "schema": schema, "missing": sorted(missing)}
