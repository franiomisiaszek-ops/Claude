"""Schema tests. They run on SQLite always and on PostgreSQL when TEST_DATABASE_URL is set."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from papertrader.db import models as m
from papertrader.db.base import Base
from papertrader.db.migrate import current_revision, head_revision, upgrade_to_head
from papertrader.db.session import make_engine, make_session_factory, session_scope
from papertrader.db.state import get_state, set_state

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def test_migrations_create_every_table(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    assert "alembic_version" in tables


def test_migrations_match_the_models_exactly(engine: Engine) -> None:
    """If someone edits models.py without writing a migration, this fails."""
    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"models and migrations have drifted apart: {diff}"


def test_upgrade_is_idempotent(engine: Engine, database_url: str) -> None:
    upgrade_to_head(database_url)  # second run must be a no-op
    assert current_revision(engine) == head_revision(database_url)


def test_timestamps_round_trip_as_aware_utc(session: Session) -> None:
    new_york_morning = datetime(2026, 11, 13, 8, 45, tzinfo=timezone(timedelta(hours=-5)))
    session.add(
        m.NewsItem(
            source="alpaca",
            source_item_id="1",
            headline="Example headline",
            published_at=new_york_morning,
            available_at=new_york_morning,
            content_hash="a" * 64,
        )
    )
    session.commit()
    session.expire_all()

    row = session.scalars(select(m.NewsItem)).one()
    assert row.published_at.tzinfo is not None
    assert row.published_at.utcoffset() == timedelta(0)
    assert row.published_at == datetime(2026, 11, 13, 13, 45, tzinfo=UTC)
    assert row.ingested_at.tzinfo is not None


def test_naive_timestamps_are_rejected(session: Session) -> None:
    session.add(
        m.NewsItem(
            source="alpaca",
            source_item_id="1",
            headline="x",
            published_at=datetime(2026, 11, 13, 8, 45),  # noqa: DTZ001 - the point of the test
            available_at=NOW,
            content_hash="a" * 64,
        )
    )
    with pytest.raises(StatementError, match="naive datetime"):
        session.commit()


def test_the_same_news_item_cannot_be_stored_twice(session: Session) -> None:
    def item() -> m.NewsItem:
        return m.NewsItem(
            source="alpaca",
            source_item_id="42",
            headline="x",
            published_at=NOW,
            available_at=NOW,
            content_hash="b" * 64,
        )

    session.add(item())
    session.commit()
    session.add(item())
    with pytest.raises(IntegrityError):
        session.commit()


def test_news_maps_to_tickers(session: Session) -> None:
    news = m.NewsItem(
        source="alpaca",
        source_item_id="7",
        headline="NVDA and AMD rally",
        published_at=NOW,
        available_at=NOW,
        content_hash="c" * 64,
    )
    news.tickers = [m.NewsTicker(ticker="NVDA"), m.NewsTicker(ticker="AMD")]
    session.add(news)
    session.commit()

    found = session.scalars(
        select(m.NewsItem).join(m.NewsTicker).where(m.NewsTicker.ticker == "AMD")
    ).one()
    assert found.id == news.id


def test_invalid_enum_value_is_rejected(session: Session) -> None:
    cycle = m.DecisionCycle(kind=m.CycleKind.DRY_RUN, as_of=NOW)
    session.add(cycle)
    session.flush()
    session.add(
        m.AgentReport(
            cycle_id=cycle.id,
            agent_name="macro",
            as_of=NOW,
            ticker="XLF",
            direction="sideways",
            confidence=0.5,
            horizon_days=5,
            thesis="x",
            model="claude-sonnet-5-5",
        )
    )
    with pytest.raises((StatementError, LookupError)):
        session.commit()


def test_foreign_keys_are_enforced(session: Session) -> None:
    session.add(
        m.AgentReport(
            cycle_id=999_999,
            agent_name="macro",
            as_of=NOW,
            ticker="XLF",
            direction=m.Direction.LONG,
            confidence=0.5,
            horizon_days=5,
            thesis="x",
            model="claude-sonnet-5-5",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_a_closed_trade_can_be_traced_back_to_the_agents_behind_it(session: Session) -> None:
    """Spec: every trade links to the reports and decision that caused it."""
    cycle = m.DecisionCycle(kind=m.CycleKind.DAILY, as_of=NOW)
    session.add(cycle)
    session.flush()

    def report(agent: str, direction: m.Direction) -> m.AgentReport:
        return m.AgentReport(
            cycle_id=cycle.id,
            agent_name=agent,
            as_of=NOW,
            ticker="NVDA",
            direction=direction,
            confidence=0.8,
            horizon_days=5,
            thesis="t",
            model="claude-sonnet-5-5",
            evidence=[{"claim": "c", "source_id": "news:1", "url": "https://example.com"}],
        )

    bull, bear = report("earnings", m.Direction.LONG), report("contrarian", m.Direction.SHORT)
    session.add_all([bull, bear])
    session.flush()

    decision = m.JudgeDecision(
        cycle_id=cycle.id, as_of=NOW, market_summary="s", model="claude-opus-5-5"
    )
    idea = m.TradeIdea(
        rank=1,
        ticker="NVDA",
        action=m.TradeAction.BUY,
        judge_confidence=0.8,
        model_probability=0.7,
        agent_agreement=0.5,
        final_confidence=0.71,
        target_price=150,
        stop_price=135,
        size_pct=0.1,
        horizon_days=5,
        rationale="r",
    )
    idea.report_links = [
        m.TradeIdeaReport(agent_report_id=bull.id, stance=m.Stance.SUPPORT),
        m.TradeIdeaReport(agent_report_id=bear.id, stance=m.Stance.OPPOSE),
    ]
    decision.ideas = [idea]
    session.add(decision)
    session.flush()

    trade = m.Trade(
        trade_idea_id=idea.id,
        ticker="NVDA",
        direction=m.Direction.LONG,
        qty=20,
        entry_price=142.10,
        entered_at=NOW,
    )
    session.add(trade)
    session.flush()
    order = m.Order(
        client_order_id="pt-1",
        trade_idea_id=idea.id,
        trade_id=trade.id,
        ticker="NVDA",
        side=m.OrderSide.BUY,
        leg=m.OrderLeg.ENTRY,
        order_type="limit",
        order_class="bracket",
        time_in_force="day",
        qty=20,
        status="filled",
    )
    order.fills = [m.Fill(ticker="NVDA", side=m.OrderSide.BUY, qty=20, price=142.10, filled_at=NOW)]
    session.add(order)
    session.commit()
    session.expire_all()

    rows = session.execute(
        select(m.AgentReport.agent_name, m.TradeIdeaReport.stance)
        .join(m.TradeIdeaReport, m.TradeIdeaReport.agent_report_id == m.AgentReport.id)
        .join(m.TradeIdea, m.TradeIdea.id == m.TradeIdeaReport.trade_idea_id)
        .join(m.Trade, m.Trade.trade_idea_id == m.TradeIdea.id)
        .where(m.Trade.id == trade.id)
        .order_by(m.AgentReport.agent_name)
    ).all()
    assert [(name, stance) for name, stance in rows] == [
        ("contrarian", m.Stance.OPPOSE),
        ("earnings", m.Stance.SUPPORT),
    ]
    stored = session.get(m.AgentReport, bull.id)
    assert stored is not None and stored.evidence[0]["source_id"] == "news:1"
    assert session.get(m.Trade, trade.id).orders[0].fills[0].price == 142.10  # type: ignore[union-attr]


def test_api_usage_rows_can_be_summed_for_the_cost_governor(session: Session) -> None:
    session.add_all(
        [
            m.ApiUsage(
                ts=NOW,
                purpose="macro",
                model="claude-sonnet-5-5",
                input_tokens=1000,
                output_tokens=500,
                cost_usd=0.007,
            ),
            m.ApiUsage(
                ts=NOW,
                purpose="judge",
                model="claude-opus-5-5",
                input_tokens=2000,
                output_tokens=1000,
                cost_usd=0.028,
            ),
        ]
    )
    session.commit()
    total = session.execute(text("SELECT SUM(cost_usd) FROM api_usage")).scalar_one()
    assert total == pytest.approx(0.035)


def test_system_state_set_and_get(session: Session) -> None:
    assert get_state(session, "trading.paused") is None
    set_state(session, "trading.paused", {"paused": True, "reason": "manual"})
    session.commit()
    set_state(session, "trading.paused", {"paused": False})
    session.commit()
    session.expire_all()
    assert get_state(session, "trading.paused") == {"paused": False}


def test_session_scope_rolls_back_on_error(engine: Engine) -> None:
    factory = make_session_factory(engine)
    with pytest.raises(RuntimeError), session_scope(factory) as session:
        set_state(session, "k", {"v": 1})
        raise RuntimeError("fail mid-transaction")
    with session_scope(factory) as session:
        assert get_state(session, "k") is None


def test_read_only_engine_cannot_write(engine: Engine, database_url: str) -> None:
    """The dashboard uses this mode: reads work, every write is refused by the database."""
    read_only = make_engine(database_url, read_only=True)
    try:
        with Session(read_only) as session:
            assert session.scalars(select(m.SystemState)).all() == []
            session.add(m.SystemState(key="x", value={}))
            with pytest.raises(Exception, match=r"(?i)read.?only|readonly"):
                session.commit()
    finally:
        read_only.dispose()
