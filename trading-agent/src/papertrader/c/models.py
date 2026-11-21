"""Tables only system C has, in its own schema (own MetaData: the freeze rule holds)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, Float, Integer, MetaData, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from papertrader.db.base import NAMING_CONVENTION
from papertrader.db.types import JSONType, UTCDateTime, utcnow


class CBase(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _pk() -> Mapped[int]:
    return mapped_column(Integer, primary_key=True, autoincrement=True)


class Price(CBase):
    """Daily prices adjusted for dividends and splits (so returns are total returns).

    source "alpaca": the live window, refreshed every day (adjusted prices change when a
    fund pays out). source "tiingo": the long history for the backtest. "fred" holds the
    3-month T-bill rate (in percent a year) as ticker DTB3.
    """

    __tablename__ = "c_prices"
    __table_args__ = (UniqueConstraint("source", "ticker", "day"),)

    id: Mapped[int] = _pk()
    source: Mapped[str] = mapped_column(String(16))
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float | None] = mapped_column(Float)
    high: Mapped[float | None] = mapped_column(Float)
    low: Mapped[float | None] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)


class Plan(CBase):
    """One weekly decision: the target portfolio, what the review changed, the orders."""

    __tablename__ = "c_plans"

    id: Mapped[int] = _pk()
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    signal_day: Mapped[date] = mapped_column(Date, unique=True)  # the close the signals are from
    trade_day: Mapped[date] = mapped_column(Date)  # when the orders go in
    status: Mapped[str] = mapped_column(String(16))  # planned | trading | done | cancelled
    state: Mapped[str] = mapped_column(String(16))  # normal | halved | flat (drawdown control)
    equity: Mapped[float | None] = mapped_column(Float)
    drawdown: Mapped[float | None] = mapped_column(Float)
    targets: Mapped[dict[str, float]] = mapped_column(JSONType, default=dict)  # after the review
    shadow_targets: Mapped[dict[str, float]] = mapped_column(JSONType, default=dict)  # without it
    reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )  # per fund: why in / out
    review: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # Claude's verdicts
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")


class Order(CBase):
    """C's orders at the broker: rebalancing limit orders and the resting emergency stops."""

    __tablename__ = "c_orders"

    id: Mapped[int] = _pk()
    plan_id: Mapped[int | None] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # rebalance | stop | flatten
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    alpaca_order_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), index=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_avg_price: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SleevePlan(CBase):
    """C's daily chart trades (shadow first): one row per signal, from alert to result."""

    __tablename__ = "c_sleeve_plans"
    __table_args__ = (UniqueConstraint("signal_day", "ticker"),)

    id: Mapped[int] = _pk()
    signal_day: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str] = mapped_column(String(16))
    rule: Mapped[str] = mapped_column(String(16))  # breakout | dip
    mode: Mapped[str] = mapped_column(String(8))
    reference: Mapped[float] = mapped_column(Float)  # the signal day's close
    stop_price: Mapped[float] = mapped_column(Float)
    target_price: Mapped[float] = mapped_column(Float)
    hold_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    exit_day: Mapped[date | None] = mapped_column(Date)
    exit_reason: Mapped[str | None] = mapped_column(String(16))
    net_return_pct: Mapped[float | None] = mapped_column(Float)


class BacktestRun(CBase):
    """Each run of the 20-year backtest, with its full result and recommendations."""

    __tablename__ = "c_backtests"

    id: Mapped[int] = _pk()
    ran_at: Mapped[datetime] = mapped_column(UTCDateTime)
    data_from: Mapped[date | None] = mapped_column(Date)
    data_to: Mapped[date | None] = mapped_column(Date)
    chosen: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )  # settings picked in-sample
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    verdict: Mapped[str] = mapped_column(Text)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
