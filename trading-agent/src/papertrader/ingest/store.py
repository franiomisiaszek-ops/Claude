"""Database helpers shared by all ingestion code: upserts and resume bookmarks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from papertrader.db.base import Base
from papertrader.db.models import IngestState
from papertrader.db.types import utcnow

_CHUNK = 500


def _table(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


def upsert(
    session: Session,
    model: type[Base],
    rows: Sequence[Mapping[str, Any]],
    *,
    conflict: Sequence[str],
    update: Iterable[str] = (),
    also_set: Iterable[str] = (),
) -> int:
    """Insert rows; on a duplicate key either update the given columns or leave the row alone.

    This is what makes re-running any ingestion step safe. Returns the number of rows
    inserted or changed (duplicates left alone are not counted).

    `update` columns decide whether an existing row counts as changed; `also_set` columns
    (timestamps and the like) are written along with them but never trigger a change alone.
    """
    if not rows:
        return 0
    # PostgreSQL rejects a statement that touches the same row twice, and real feeds do
    # send repeats. Keep one row per key: the last one given.
    unique = {tuple(row[col] for col in conflict): row for row in rows}
    rows = list(unique.values())
    table = _table(model)
    update_cols = list(update)
    extra_cols = list(also_set)
    dialect = session.get_bind().dialect.name
    insert = postgresql.insert if dialect == "postgresql" else sqlite.insert
    affected = 0

    # Python-side column defaults (created_at etc.) are applied by SQLAlchemy for Core inserts.
    for start in range(0, len(rows), _CHUNK):
        chunk = [dict(row) for row in rows[start : start + _CHUNK]]
        stmt = insert(table).values(chunk)
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(conflict),
                set_={col: stmt.excluded[col] for col in [*update_cols, *extra_cols]},
                # Only touch the row when something really changed.
                where=_any_different(table, stmt, update_cols),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict))
        # RETURNING lists exactly the rows that were inserted or changed. (The driver's
        # row count is not reliable for multi-row inserts on PostgreSQL.)
        returned = session.execute(stmt.returning(*table.primary_key.columns))
        affected += len(returned.all())
    return affected


def _any_different(table: Table, stmt: Any, columns: Sequence[str]) -> Any:
    condition = None
    for col in columns:
        differs = table.c[col].is_distinct_from(stmt.excluded[col])
        condition = differs if condition is None else (condition | differs)
    return condition


def get_cursor(session: Session, source: str, key: str) -> dict[str, Any]:
    row = session.get(IngestState, (source, key))
    return {} if row is None else dict(row.cursor)


def set_cursor(session: Session, source: str, key: str, cursor: Mapping[str, Any]) -> None:
    row = session.get(IngestState, (source, key))
    if row is None:
        session.add(IngestState(source=source, key=key, cursor=dict(cursor)))
    else:
        row.cursor = dict(cursor)
        row.updated_at = utcnow()


def get_cursors(session: Session, source: str, keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = list(keys)
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), _CHUNK):
        part = wanted[start : start + _CHUNK]
        rows = (
            session.query(IngestState)
            .filter(IngestState.source == source, IngestState.key.in_(part))
            .all()
        )
        found.update({row.key: dict(row.cursor) for row in rows})
    return found
