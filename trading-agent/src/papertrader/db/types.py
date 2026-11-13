"""Column types shared by all tables."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

# JSONB on PostgreSQL (indexable, compact), plain JSON on SQLite.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC.

    Writing a naive datetime is an error: every record in this system must say
    exactly when it happened, otherwise "data available before the decision
    time" cannot be checked. SQLite drops timezone info, so it is re-attached
    on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"naive datetime {value!r} not allowed; use a timezone-aware UTC datetime"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: Any | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        assert isinstance(value, datetime)
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def enum_column(enum_cls: type[StrEnum], length: int = 32) -> Enum:
    """Store a StrEnum as its string value in a VARCHAR (portable, easy to extend)."""
    return Enum(
        enum_cls,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


def utcnow() -> datetime:
    return datetime.now(UTC)
