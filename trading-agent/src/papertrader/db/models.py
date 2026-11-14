"""Database schema.

Grouped by purpose:

1. Reference + ingested data  - universe, prices, news, filings, earnings, macro
2. Research + decisions       - cycles, agent reports, model scores, judge decisions
3. Trading                    - orders, fills, round-trip trades, equity snapshots
4. Learning                   - outcomes, agent statistics + weights, lessons
5. Operations                 - API usage (cost governor), job runs (heartbeat),
                                notifications, system state (pause / kill switch)

Rules that hold everywhere:

* Every timestamp is timezone-aware UTC (`UTCDateTime` rejects naive values).
* Ingested rows carry `available_at`: the earliest moment this system could have
  known the fact. Anything that makes a decision "as of" time T may only read
  rows with `available_at <= T`. That is what prevents look-ahead leakage.
* Trades link back to the idea, the judge decision and the agent reports that
  caused them, so every outcome can be attributed to agents.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from papertrader.db.base import Base
from papertrader.db.types import JSONType, UTCDateTime, enum_column, utcnow

# --------------------------------------------------------------------------- enums


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class CycleKind(StrEnum):
    DAILY = "daily"
    THESIS_BREAK = "thesis_break"
    DRY_RUN = "dry_run"
    BACKTEST = "backtest"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    MISSED = "missed"


class TradeAction(StrEnum):
    BUY = "buy"
    SELL_SHORT = "sell_short"


class IdeaStatus(StrEnum):
    PROPOSED = "proposed"
    BELOW_THRESHOLD = "below_threshold"
    REJECTED_BY_RISK = "rejected_by_risk"
    SUBMITTED = "submitted"
    EXECUTED = "executed"
    EXPIRED = "expired"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderLeg(StrEnum):
    ENTRY = "entry"
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    EXIT = "exit"


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ExitTrigger(StrEnum):
    TARGET = "target"
    STOP = "stop"
    TIME = "time"
    THESIS_BREAK = "thesis_break"
    KILL_SWITCH = "kill_switch"
    MANUAL = "manual"


class Stance(StrEnum):
    SUPPORT = "support"
    OPPOSE = "oppose"
    NEUTRAL = "neutral"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def _created() -> Mapped[datetime]:
    return mapped_column(UTCDateTime, default=utcnow, nullable=False)


# ===================================================== 1. reference + ingested data


class UniverseMember(Base):
    """An index constituent. Kept point-in-time (added/removed dates) to limit survivorship bias."""

    __tablename__ = "universe_members"
    __table_args__ = (UniqueConstraint("index_name", "ticker"),)

    id: Mapped[int] = _pk()
    index_name: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(255))
    gics_sector: Mapped[str | None] = mapped_column(String(64), index=True)
    gics_sub_industry: Mapped[str | None] = mapped_column(String(128))
    cik: Mapped[str | None] = mapped_column(String(10), index=True)
    market_cap: Mapped[float | None] = mapped_column(Float)
    added_on: Mapped[date | None] = mapped_column(Date)
    removed_on: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class PriceBar(Base):
    """Daily bars (and recent intraday bars). Bulk history also lives in Parquet under data/."""

    __tablename__ = "price_bars"
    __table_args__ = (UniqueConstraint("ticker", "timeframe", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16))
    timeframe: Mapped[str] = mapped_column(String(8))  # "1Day", "10Min", ...
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)  # bar start
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    vwap: Mapped[float | None] = mapped_column(Float)
    trade_count: Mapped[int | None] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime)  # bar close


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (UniqueConstraint("source", "source_item_id"),)

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(String(32))  # "alpaca", "finnhub", ...
    source_item_id: Mapped[str] = mapped_column(String(128))
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    # Hash of the normalised headline: the same story from two sources shares it.
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    # Set when this row repeats an earlier story; agents only see the original.
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("news_items.id"))
    event_type: Mapped[str | None] = mapped_column(String(32))
    sentiment: Mapped[float | None] = mapped_column(Float)
    ingested_at: Mapped[datetime] = _created()

    tickers: Mapped[list[NewsTicker]] = relationship(
        back_populates="news", cascade="all, delete-orphan"
    )


class NewsTicker(Base):
    __tablename__ = "news_tickers"

    news_id: Mapped[int] = mapped_column(
        ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True
    )
    ticker: Mapped[str] = mapped_column(String(16), primary_key=True, index=True)

    news: Mapped[NewsItem] = relationship(back_populates="tickers")


class Filing(Base):
    """An SEC EDGAR filing (8-K, 10-Q, 10-K, 4, 13F-HR)."""

    __tablename__ = "filings"

    id: Mapped[int] = _pk()
    accession_no: Mapped[str] = mapped_column(String(25), unique=True)
    cik: Mapped[str] = mapped_column(String(10), index=True)
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    form_type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    items: Mapped[str | None] = mapped_column(String(255))  # 8-K item numbers, e.g. "2.02,9.01"
    period_of_report: Mapped[date | None] = mapped_column(Date)
    url: Mapped[str | None] = mapped_column(Text)
    text_path: Mapped[str | None] = mapped_column(Text)  # extracted text on disk, if downloaded
    primary_document: Mapped[str | None] = mapped_column(Text)
    text_excerpt: Mapped[str | None] = mapped_column(Text)  # start of the document, plain text
    # For filings whose contents are read in a second step (Form 4, 13F):
    # null = not read yet, "done", "failed" or "skipped".
    detail_status: Mapped[str | None] = mapped_column(String(16), index=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime)  # EDGAR acceptance time
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ingested_at: Mapped[datetime] = _created()


class InsiderTransaction(Base):
    """One transaction line from a Form 4."""

    __tablename__ = "insider_transactions"
    __table_args__ = (UniqueConstraint("filing_id", "line_no"),)

    id: Mapped[int] = _pk()
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    insider_name: Mapped[str] = mapped_column(String(255))
    insider_title: Mapped[str | None] = mapped_column(String(255))
    owner_cik: Mapped[str | None] = mapped_column(String(10))
    is_ten_percent_owner: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    security_title: Mapped[str | None] = mapped_column(String(128))
    direct_ownership: Mapped[bool | None] = mapped_column(Boolean)
    under_10b5_1_plan: Mapped[bool | None] = mapped_column(Boolean)  # pre-scheduled trade
    is_director: Mapped[bool] = mapped_column(Boolean, default=False)
    is_officer: Mapped[bool] = mapped_column(Boolean, default=False)
    transaction_date: Mapped[date] = mapped_column(Date)
    transaction_code: Mapped[str] = mapped_column(String(4))  # P = purchase, S = sale, ...
    acquired: Mapped[bool] = mapped_column(Boolean)
    shares: Mapped[float] = mapped_column(Float)
    price: Mapped[float | None] = mapped_column(Float)
    shares_owned_after: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class InstitutionalHolding(Base):
    """One position from a 13F-HR filing."""

    __tablename__ = "institutional_holdings"
    __table_args__ = (UniqueConstraint("filing_id", "cusip", "line_no"),)

    id: Mapped[int] = _pk()
    filing_id: Mapped[int] = mapped_column(ForeignKey("filings.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    manager_cik: Mapped[str] = mapped_column(String(10), index=True)
    manager_name: Mapped[str] = mapped_column(String(255))
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    cusip: Mapped[str] = mapped_column(String(9))
    title_of_class: Mapped[str | None] = mapped_column(String(64))
    report_period: Mapped[date] = mapped_column(Date)
    shares: Mapped[float] = mapped_column(Float)
    value_usd: Mapped[float] = mapped_column(Float)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class SecurityIdentifier(Base):
    """CUSIP -> ticker, built from the SEC's fails-to-deliver files. Used to read 13F holdings."""

    __tablename__ = "security_identifiers"

    cusip: Mapped[str] = mapped_column(String(9), primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    description: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class EarningsEvent(Base):
    """A scheduled or reported earnings release."""

    __tablename__ = "earnings_events"
    __table_args__ = (UniqueConstraint("source", "ticker", "report_date"),)

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(String(32))
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    report_date: Mapped[date] = mapped_column(Date, index=True)
    time_of_day: Mapped[str | None] = mapped_column(String(8))  # bmo | amc | dmh
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_quarter: Mapped[int | None] = mapped_column(Integer)
    eps_estimate: Mapped[float | None] = mapped_column(Float)
    eps_actual: Mapped[float | None] = mapped_column(Float)
    revenue_estimate: Mapped[float | None] = mapped_column(Float)
    revenue_actual: Mapped[float | None] = mapped_column(Float)
    # When the *actual* figures became known (null while only scheduled).
    actual_available_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime)  # when the calendar entry was known
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class MacroObservation(Base):
    """A FRED data point. `realtime_start` is the vintage: the day this value was published."""

    __tablename__ = "macro_observations"
    __table_args__ = (UniqueConstraint("series_id", "observation_date", "realtime_start"),)

    id: Mapped[int] = _pk()
    series_id: Mapped[str] = mapped_column(String(32), index=True)
    observation_date: Mapped[date] = mapped_column(Date)
    realtime_start: Mapped[date] = mapped_column(Date)
    value: Mapped[float | None] = mapped_column(Float)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)


class IngestState(Base):
    """Bookmark per source so ingestion can stop and resume without gaps or duplicates."""

    __tablename__ = "ingest_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


# ===================================================== 2. research + decisions


class DecisionCycle(Base):
    """One run of agents -> pattern model -> judge, as of a fixed timestamp."""

    __tablename__ = "decision_cycles"

    id: Mapped[int] = _pk()
    kind: Mapped[CycleKind] = mapped_column(enum_column(CycleKind))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus), default=RunStatus.PENDING)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class AgentReport(Base):
    __tablename__ = "agent_reports"
    __table_args__ = (
        Index("ix_agent_reports_ticker_as_of", "ticker", "as_of"),
        Index("ix_agent_reports_agent_as_of", "agent_name", "as_of"),
    )

    id: Mapped[int] = _pk()
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="CASCADE"), index=True
    )
    agent_name: Mapped[str] = mapped_column(String(64))
    sector: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str | None] = mapped_column(String(32))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    ticker: Mapped[str] = mapped_column(String(16))
    direction: Mapped[Direction] = mapped_column(enum_column(Direction))
    confidence: Mapped[float] = mapped_column(Float)
    horizon_days: Mapped[int] = mapped_column(Integer)
    thesis: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    risks: Mapped[list[str]] = mapped_column(JSONType, default=list)
    catalysts: Mapped[list[str]] = mapped_column(JSONType, default=list)
    dropped_claims: Mapped[int] = mapped_column(
        Integer, default=0
    )  # claims removed for lacking a citation
    model: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()


class ModelVersion(Base):
    """A trained pattern model. Only one is active; a new one must beat it out-of-sample."""

    __tablename__ = "model_versions"

    id: Mapped[int] = _pk()
    version: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = _created()
    trained_from: Mapped[date | None] = mapped_column(Date)
    trained_to: Mapped[date | None] = mapped_column(Date)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    artifact_path: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    promoted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)


class ModelScore(Base):
    __tablename__ = "model_scores"
    __table_args__ = (UniqueConstraint("cycle_id", "ticker", "horizon_days"),)

    id: Mapped[int] = _pk()
    cycle_id: Mapped[int] = mapped_column(ForeignKey("decision_cycles.id", ondelete="CASCADE"))
    model_version_id: Mapped[int] = mapped_column(ForeignKey("model_versions.id"))
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    horizon_days: Mapped[int] = mapped_column(Integer)
    probability_up: Mapped[float] = mapped_column(Float)  # calibrated P(excess return > 0)
    raw_score: Mapped[float | None] = mapped_column(Float)
    features: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Lesson(Base):
    """Weekly "what worked / what failed" summary written by the judge from resolved outcomes."""

    __tablename__ = "lessons"

    id: Mapped[int] = _pk()
    created_at: Mapped[datetime] = _created()
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class JudgeDecision(Base):
    __tablename__ = "judge_decisions"

    id: Mapped[int] = _pk()
    cycle_id: Mapped[int] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="CASCADE"), unique=True
    )
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    market_summary: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(64))
    lesson_id: Mapped[int | None] = mapped_column(ForeignKey("lessons.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )  # full validated JudgeDecision
    created_at: Mapped[datetime] = _created()

    ideas: Mapped[list[TradeIdea]] = relationship(
        back_populates="decision", cascade="all, delete-orphan"
    )


class TradeIdea(Base):
    """One ranked trade from a judge decision, with every component of its final confidence."""

    __tablename__ = "trade_ideas"
    __table_args__ = (UniqueConstraint("decision_id", "ticker"),)

    id: Mapped[int] = _pk()
    decision_id: Mapped[int] = mapped_column(
        ForeignKey("judge_decisions.id", ondelete="CASCADE"), index=True
    )
    rank: Mapped[int] = mapped_column(Integer)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[TradeAction] = mapped_column(enum_column(TradeAction))
    judge_confidence: Mapped[float] = mapped_column(Float)
    model_probability: Mapped[float | None] = mapped_column(Float)
    agent_agreement: Mapped[float | None] = mapped_column(Float)
    final_confidence: Mapped[float] = mapped_column(Float)
    entry_low: Mapped[float | None] = mapped_column(Float)
    entry_high: Mapped[float | None] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    size_pct: Mapped[float] = mapped_column(Float)  # suggested share of equity, 0-1
    horizon_days: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text)
    dissent: Mapped[str | None] = mapped_column(Text)
    status: Mapped[IdeaStatus] = mapped_column(enum_column(IdeaStatus), default=IdeaStatus.PROPOSED)
    status_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()

    decision: Mapped[JudgeDecision] = relationship(back_populates="ideas")
    report_links: Mapped[list[TradeIdeaReport]] = relationship(
        back_populates="idea", cascade="all, delete-orphan"
    )


class TradeIdeaReport(Base):
    """Which agent reports stood behind (or against) a trade idea - the attribution link."""

    __tablename__ = "trade_idea_reports"

    trade_idea_id: Mapped[int] = mapped_column(
        ForeignKey("trade_ideas.id", ondelete="CASCADE"), primary_key=True
    )
    agent_report_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reports.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    stance: Mapped[Stance] = mapped_column(enum_column(Stance))

    idea: Mapped[TradeIdea] = relationship(back_populates="report_links")
    report: Mapped[AgentReport] = relationship()


# ===================================================== 3. trading (paper only)


class Trade(Base):
    """A round trip: one entry and (eventually) one exit of a position."""

    __tablename__ = "trades"

    id: Mapped[int] = _pk()
    trade_idea_id: Mapped[int | None] = mapped_column(ForeignKey("trade_ideas.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    sector: Mapped[str | None] = mapped_column(String(64))
    direction: Mapped[Direction] = mapped_column(enum_column(Direction))
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entered_at: Mapped[datetime] = mapped_column(UTCDateTime)
    target_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    horizon_ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[TradeStatus] = mapped_column(
        enum_column(TradeStatus), default=TradeStatus.OPEN, index=True
    )
    exit_price: Mapped[float | None] = mapped_column(Float)
    exited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_trigger: Mapped[ExitTrigger | None] = mapped_column(enum_column(ExitTrigger))
    pnl_usd: Mapped[float | None] = mapped_column(Float)
    pnl_pct: Mapped[float | None] = mapped_column(Float)
    benchmark_return_pct: Mapped[float | None] = mapped_column(Float)  # SPY over the same period
    excess_return_pct: Mapped[float | None] = mapped_column(Float)

    orders: Mapped[list[Order]] = relationship(back_populates="trade")


class Order(Base):
    """An order sent to the Alpaca PAPER account (bracket parents and their legs)."""

    __tablename__ = "orders"

    id: Mapped[int] = _pk()
    client_order_id: Mapped[str] = mapped_column(
        String(64), unique=True
    )  # ours; makes submits idempotent
    alpaca_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    parent_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"))
    trade_idea_id: Mapped[int | None] = mapped_column(ForeignKey("trade_ideas.id"), index=True)
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[OrderSide] = mapped_column(enum_column(OrderSide))
    leg: Mapped[OrderLeg] = mapped_column(enum_column(OrderLeg))
    order_type: Mapped[str] = mapped_column(String(16))  # market | limit | stop | stop_limit
    order_class: Mapped[str | None] = mapped_column(String(16))  # bracket | oco | simple
    time_in_force: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), index=True)  # Alpaca's order status, verbatim
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    trade: Mapped[Trade | None] = relationship(back_populates="orders")
    fills: Mapped[list[Fill]] = relationship(back_populates="order", cascade="all, delete-orphan")


class Fill(Base):
    __tablename__ = "fills"

    id: Mapped[int] = _pk()
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    ticker: Mapped[str] = mapped_column(String(16))
    side: Mapped[OrderSide] = mapped_column(enum_column(OrderSide))
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    filled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)

    order: Mapped[Order] = relationship(back_populates="fills")


class ExitSignal(Base):
    __tablename__ = "exit_signals"

    id: Mapped[int] = _pk()
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trades.id"), index=True)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("judge_decisions.id"))
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    trigger: Mapped[ExitTrigger] = mapped_column(enum_column(ExitTrigger))
    reason: Mapped[str] = mapped_column(Text)
    acted_on: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = _created()


class EquitySnapshot(Base):
    """Account equity over time, next to the benchmark price, for the equity curve."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = mapped_column(UTCDateTime, unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="intraday")  # intraday | eod
    equity: Mapped[float] = mapped_column(Float)
    cash: Mapped[float] = mapped_column(Float)
    positions_value: Mapped[float] = mapped_column(Float)
    benchmark_close: Mapped[float | None] = mapped_column(Float)
    day_pnl_usd: Mapped[float | None] = mapped_column(Float)
    day_pnl_pct: Mapped[float | None] = mapped_column(Float)


# ===================================================== 4. learning


class PredictionOutcome(Base):
    """What actually happened after an agent report, once its horizon has passed."""

    __tablename__ = "prediction_outcomes"

    id: Mapped[int] = _pk()
    agent_report_id: Mapped[int] = mapped_column(
        ForeignKey("agent_reports.id", ondelete="CASCADE"), unique=True
    )
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    horizon_days: Mapped[int] = mapped_column(Integer)
    start_price: Mapped[float] = mapped_column(Float)
    end_price: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)
    benchmark_return_pct: Mapped[float] = mapped_column(Float)
    excess_return_pct: Mapped[float] = mapped_column(Float)
    hit: Mapped[bool | None] = mapped_column(Boolean)  # null for neutral calls
    brier: Mapped[float | None] = mapped_column(Float)


class AgentStat(Base):
    """Per-agent accuracy, overall ('' sector / event_type) and broken down."""

    __tablename__ = "agent_stats"
    __table_args__ = (
        UniqueConstraint("as_of_date", "agent_name", "sector", "event_type", "window_days"),
    )

    id: Mapped[int] = _pk()
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    agent_name: Mapped[str] = mapped_column(String(64), index=True)
    sector: Mapped[str] = mapped_column(String(64), default="")
    event_type: Mapped[str] = mapped_column(String(32), default="")
    window_days: Mapped[int] = mapped_column(Integer)
    n: Mapped[int] = mapped_column(Integer)
    hit_rate: Mapped[float | None] = mapped_column(Float)
    brier: Mapped[float | None] = mapped_column(Float)
    avg_excess_return_pct: Mapped[float | None] = mapped_column(Float)


class AgentWeight(Base):
    """Reliability weight the judge applies to an agent, updated weekly."""

    __tablename__ = "agent_weights"
    __table_args__ = (UniqueConstraint("agent_name", "effective_from"),)

    id: Mapped[int] = _pk()
    agent_name: Mapped[str] = mapped_column(String(64), index=True)
    weight: Mapped[float] = mapped_column(Float)
    effective_from: Mapped[datetime] = mapped_column(UTCDateTime)
    basis: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )  # the numbers behind the weight


# ===================================================== 5. operations


class ApiUsage(Base):
    """One row per Claude API call. The cost governor sums `cost_usd` per day and month."""

    __tablename__ = "api_usage"

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    purpose: Mapped[str] = mapped_column(
        String(64), index=True
    )  # agent name, "judge", "lessons", ...
    model: Mapped[str] = mapped_column(String(64))
    effort: Mapped[str | None] = mapped_column(String(8))
    cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("decision_cycles.id", ondelete="SET NULL")
    )
    request_id: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[str | None] = mapped_column(String(64))
    is_batch: Mapped[bool] = mapped_column(Boolean, default=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    stop_reason: Mapped[str | None] = mapped_column(String(32))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class JobRun(Base):
    """Every scheduled job execution. The heartbeat looks here for jobs that did not run."""

    __tablename__ = "job_runs"
    __table_args__ = (UniqueConstraint("job_name", "scheduled_for"),)

    id: Mapped[int] = _pk()
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    scheduled_for: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[RunStatus] = mapped_column(enum_column(RunStatus), default=RunStatus.PENDING)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(
        String(32), index=True
    )  # morning_brief, trade_executed, alert, ...
    dedup_key: Mapped[str | None] = mapped_column(
        String(128), unique=True
    )  # never send the same event twice
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus), default=NotificationStatus.PENDING
    )
    telegram_message_id: Mapped[str | None] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SystemState(Base):
    """Small key/value store: trading paused flag, kill-switch state, last heartbeat."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
