"""Tables only system B has, in its own schema.

They use their own `MetaData`, not the shared `Base`, so the freeze rule holds: the
shared tables (and system A's frozen code) never see them. `db provision` creates them
next to B's copies of the ordinary system tables.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, Float, Integer, MetaData, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from papertrader.db.base import NAMING_CONVENTION
from papertrader.db.types import JSONType, UTCDateTime, utcnow


class BBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


class DecisionLog(BBase):
    """One row per trade idea of B's judge: what A's rules would have done with it and
    what B did instead, step by step. The comparison of A and B is built on this."""

    __tablename__ = "b_decision_log"

    id: Mapped[int] = _pk()
    decision_id: Mapped[int] = mapped_column(Integer, index=True)
    idea_id: Mapped[int] = mapped_column(Integer, index=True)
    cycle_id: Mapped[int] = mapped_column(Integer, index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    action: Mapped[str] = mapped_column(String(16))
    horizon_days: Mapped[int] = mapped_column(Integer)
    judge_confidence: Mapped[float] = mapped_column(Float)
    agreement: Mapped[float] = mapped_column(Float)
    raw_confidence: Mapped[float] = mapped_column(Float)  # judge + agreement, no pattern model
    calibrated: Mapped[float | None] = mapped_column(Float)
    conformal: Mapped[str] = mapped_column(
        String(16), default="off"
    )  # trade | abstain | waiting | off
    veto: Mapped[str] = mapped_column(
        String(16), default="off"
    )  # keep | shrink | veto | failed | off
    veto_reason: Mapped[str | None] = mapped_column(Text)
    meta_probability: Mapped[float | None] = mapped_column(Float)
    a_status: Mapped[str] = mapped_column(String(32))
    a_reason: Mapped[str | None] = mapped_column(Text)
    b_status: Mapped[str] = mapped_column(String(32))
    b_reason: Mapped[str | None] = mapped_column(Text)
    a_size: Mapped[float] = mapped_column(Float)
    b_size: Mapped[float] = mapped_column(Float)
    a_stop: Mapped[float] = mapped_column(Float)
    b_stop: Mapped[float] = mapped_column(Float)
    a_target: Mapped[float] = mapped_column(Float)
    b_target: Mapped[float] = mapped_column(Float)
    entry_reference: Mapped[float | None] = mapped_column(Float)
    atr_pct: Mapped[float | None] = mapped_column(Float)
    market_trend: Mapped[str | None] = mapped_column(
        String(16)
    )  # above | below the 200-day average
    signals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    switches: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    steps: Mapped[list[str]] = mapped_column(
        JSONType, default=list
    )  # what each switch did, in words
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Signal(BBase):
    """B's extra signals of one day (insider clusters, options, analysts), from 08:00."""

    __tablename__ = "b_signals"
    __table_args__ = (UniqueConstraint("day", "ticker", "kind"),)

    id: Mapped[int] = _pk()
    day: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # insider_cluster | options | analysts
    value: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)  # one line, shown to the judge
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    available_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Calibration(BBase):
    """Each fit of the confidence calibration (switch 1), with how much data it had."""

    __tablename__ = "b_calibrations"

    id: Mapped[int] = _pk()
    fitted_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    n_total: Mapped[int] = mapped_column(Integer)
    n_fit: Mapped[int] = mapped_column(Integer)
    n_conformal: Mapped[int] = mapped_column(Integer)
    base_rate: Mapped[float | None] = mapped_column(Float)  # share of ideas that beat the market
    quantile: Mapped[float | None] = mapped_column(Float)  # conformal threshold
    xs: Mapped[list[float]] = mapped_column(JSONType, default=list)
    ys: Mapped[list[float]] = mapped_column(JSONType, default=list)
    brier_raw: Mapped[float | None] = mapped_column(Float)
    brier_calibrated: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")


class ShadowReport(BBase):
    """The Fable test: an agent's report written by Claude Fable on the same inputs as the
    real (Sonnet) report. Never traded; scored the same way, to compare the two."""

    __tablename__ = "b_shadow_reports"

    id: Mapped[int] = _pk()
    day: Mapped[date] = mapped_column(Date, index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    agent_name: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    ticker: Mapped[str] = mapped_column(String(16))
    direction: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    horizon_days: Mapped[int] = mapped_column(Integer)
    thesis: Mapped[str] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    excess_return_pct: Mapped[float | None] = mapped_column(Float)
    hit: Mapped[bool | None] = mapped_column(Boolean)
    brier: Mapped[float | None] = mapped_column(Float)


class Backtest(BBase):
    """Results of B's historical tests (for example insider clusters), kept for the record."""

    __tablename__ = "b_backtests"

    id: Mapped[int] = _pk()
    kind: Mapped[str] = mapped_column(String(32), index=True)
    ran_at: Mapped[datetime] = mapped_column(UTCDateTime)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    verdict: Mapped[str] = mapped_column(Text)


class IntradayPlan(BBase):
    """One trade the intraday scanner decided on (X2b), from the alert to the result.

    status: shadow (scored but never placed) | planned (waiting out the /skip window) |
    skipped | placed | refused (a risk check or the price said no at placing time) |
    expired | done. Real trades are tagged by `idea_id` / `trade_id`.
    """

    __tablename__ = "b_intraday_plans"

    id: Mapped[int] = _pk()
    day: Mapped[date] = mapped_column(Date, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    mode: Mapped[str] = mapped_column(String(8))  # shadow | live
    status: Mapped[str] = mapped_column(String(16), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    rule: Mapped[str] = mapped_column(String(32))  # which cheap rule found it
    facts: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )  # the numbers behind the rule
    price_at_decision: Mapped[float] = mapped_column(Float)  # the live (IEX) price then
    entry_low: Mapped[float] = mapped_column(Float)
    entry_high: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float] = mapped_column(Float)
    size_pct: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    expected_hold_minutes: Mapped[int] = mapped_column(Integer)
    time_stop_at: Mapped[datetime] = mapped_column(UTCDateTime)
    place_after: Mapped[datetime] = mapped_column(UTCDateTime)
    why: Mapped[str] = mapped_column(Text)
    invalidation: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")  # why skipped / refused / expired
    idea_id: Mapped[int | None] = mapped_column(Integer, index=True)
    trade_id: Mapped[int | None] = mapped_column(Integer)
    qty: Mapped[int | None] = mapped_column(Integer)
    # The result, for live trades from the account and for shadow plans from minute bars.
    entry_price: Mapped[float | None] = mapped_column(Float)
    entered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exited_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    exit_reason: Mapped[str | None] = mapped_column(String(16))  # target | stop | time | no_fill
    net_return_pct: Mapped[float | None] = mapped_column(Float)  # after costs
    feed_gap_bps: Mapped[float | None] = mapped_column(
        Float
    )  # live (IEX) price vs the full market's
