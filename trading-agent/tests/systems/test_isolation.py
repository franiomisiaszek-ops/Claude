"""Systems B and C keep their own decisions and trades, and share A's market data."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text

from papertrader.db import models as m
from papertrader.db.provision import SchemaNotReadyError, a_tables, provision, verify
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.safety import LiveTradingForbiddenError, assert_distinct_paper_keys
from papertrader.systems import (
    MARKET_TABLES,
    UnknownSystemError,
    check_system,
    sqlite_file,
    system_tables,
)

from ..conftest import PROJECT_ROOT

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    """System B next to an already migrated system A."""
    b = make_system_engine(database_url, "B")
    assert sorted(provision(b, "B")) == sorted(t.name for t in system_tables())
    yield b
    b.dispose()


def count(engine: Engine, model: type[m.Base], **options: object) -> int:
    with engine.connect() as conn:
        if options:
            conn = conn.execution_options(**options)
        return int(conn.execute(select(func.count()).select_from(model)).scalar_one())


def add_trade(engine: Engine, ticker: str) -> None:
    with make_session_factory(engine)() as session:
        session.add(
            m.Trade(
                ticker=ticker, direction=m.Direction.LONG, qty=1, entry_price=100.0, entered_at=NOW
            )
        )
        session.commit()


def test_the_split_between_shared_and_own_tables() -> None:
    names = {t.name for t in system_tables()}
    assert (
        names.isdisjoint(MARKET_TABLES)
        and {"trades", "orders", "notifications", "api_usage"} <= names
    )
    assert check_system("b") == "B"
    with pytest.raises(UnknownSystemError):
        check_system("D")
    assert sqlite_file("sqlite:///data/papertrader.db", "C") == "data/papertrader_c.db"


def test_b_writes_only_its_own_tables(engine: Engine, b_engine: Engine) -> None:
    add_trade(engine, "AAPL")
    add_trade(b_engine, "NVDA")
    add_trade(b_engine, "MSFT")
    assert count(engine, m.Trade) == 1 and count(b_engine, m.Trade) == 2
    with make_session_factory(engine)() as session:
        assert session.scalars(select(m.Trade.ticker)).all() == ["AAPL"]


def test_b_reads_the_shared_market_data(engine: Engine, b_engine: Engine) -> None:
    with make_session_factory(engine)() as session:
        session.add(
            m.UniverseMember(
                index_name="sp500", ticker="AAPL", name="Apple", gics_sector="IT", is_active=True
            )
        )
        session.commit()
    with make_session_factory(b_engine)() as session:
        assert session.scalars(select(m.UniverseMember.ticker)).all() == ["AAPL"]


def test_b_can_look_at_as_reports_on_purpose(engine: Engine, b_engine: Engine) -> None:
    add_trade(engine, "AAPL")
    assert count(b_engine, m.Trade) == 0
    assert count(b_engine, m.Trade, schema_translate_map=a_tables(b_engine)) == 1


def test_a_missing_own_table_stops_b_instead_of_falling_through(
    engine: Engine, b_engine: Engine
) -> None:
    verify(b_engine, "B")
    with b_engine.begin() as conn:
        name = (
            "main.notifications"
            if b_engine.url.get_backend_name() == "sqlite"
            else "sys_b.notifications"
        )
        conn.execute(text(f"DROP TABLE {name}"))
    with pytest.raises(SchemaNotReadyError, match="table notifications is missing"):
        verify(b_engine, "B")
    assert provision(b_engine, "B") == ["notifications"]
    verify(b_engine, "B")


def test_each_system_needs_its_own_paper_account() -> None:
    assert_distinct_paper_keys({"A": "PKAAA", "B": "PKBBB", "C": None})
    with pytest.raises(LiveTradingForbiddenError, match="systems A and B use the same Alpaca key"):
        assert_distinct_paper_keys({"A": "PKAAA", "B": "PKAAA"})
    with pytest.raises(LiveTradingForbiddenError, match="LIVE"):
        assert_distinct_paper_keys({"A": "PKAAA", "C": "AKLIVE"})


def test_bootstrap_for_b_creates_and_checks_its_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from papertrader.runtime import bootstrap

    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("ALPACA_API_KEY", "PKAAA")
    monkeypatch.setenv("ALPACA_B_API_KEY", "PKAAA")
    with pytest.raises(LiveTradingForbiddenError, match="same Alpaca key"):
        bootstrap(system="B")
    monkeypatch.setenv("ALPACA_B_API_KEY", "PKBBB")
    from papertrader import config as config_module

    config_module.get_settings.cache_clear()
    runtime = bootstrap(system="B", migrate=True)
    assert (
        runtime.system == "B"
        and (tmp_path / "app_b.db").exists()
        and (tmp_path / "app.db").exists()
    )
    verify(runtime.engine, "B")
    assert runtime.settings.alpaca_keys("B")[0] is not None
    runtime.engine.dispose()
