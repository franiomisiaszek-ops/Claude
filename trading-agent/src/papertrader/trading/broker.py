"""The connection to Alpaca's PAPER trading account.

Everything the trading engine needs from the broker goes through the small
`Broker` interface below, so tests can use a fake and the real adapter stays thin.

Paper only: `AlpacaPaperBroker` runs the paper-only guard before it creates the
client, asks the SDK for the paper endpoint, and then checks the URL the client
actually ended up with. There is no way to construct it for a live account.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from typing import Any, Protocol, cast

from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from papertrader.ingest.timeutil import NEW_YORK, parse_utc
from papertrader.safety import ALPACA_PAPER_BASE_URL, LiveTradingForbiddenError, assert_paper_only

log = logging.getLogger(__name__)

FINAL_STATUSES = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped"}
)


class BrokerError(RuntimeError):
    """The broker refused or failed a request. The message is safe to show."""


@dataclass(frozen=True)
class Account:
    equity: float
    last_equity: float  # equity at the previous close
    cash: float
    status: str
    trading_blocked: bool
    daytrade_count: int | None = None  # day trades in the last 5 trading days (US day-trading rule)

    @property
    def day_change_pct(self) -> float:
        return self.equity / self.last_equity - 1 if self.last_equity > 0 else 0.0


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float  # negative for a short
    avg_entry_price: float
    market_value: float
    current_price: float


@dataclass
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    order_class: str
    time_in_force: str
    qty: float
    status: str
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    legs: list[BrokerOrder] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        return self.status in FINAL_STATUSES


@dataclass(frozen=True)
class Clock:
    timestamp: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


@dataclass(frozen=True)
class TradingDay:
    day: date
    open: datetime  # UTC
    close: datetime  # UTC (early on half days)


class Broker(Protocol):
    def account(self) -> Account: ...
    def clock(self) -> Clock: ...
    def calendar(self, start: date, end: date) -> list[TradingDay]: ...
    def positions(self) -> list[Position]: ...
    def open_orders(self) -> list[BrokerOrder]: ...
    def order(self, order_id: str) -> BrokerOrder: ...
    def submit_bracket(
        self, *, symbol: str, qty: int, side: str, client_order_id: str, limit_price: float | None,
        take_profit: float, stop_loss: float, time_in_force: str,
    ) -> BrokerOrder: ...  # fmt: skip
    def submit_market(
        self, *, symbol: str, qty: int, side: str, client_order_id: str
    ) -> BrokerOrder: ...
    def cancel(self, order_id: str) -> None: ...
    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]: ...


# --------------------------------------------------------------------------- parsing


def _float(value: Any) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _when(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return parse_utc(str(value))


def parse_order(raw: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        id=str(raw["id"]),
        client_order_id=str(raw.get("client_order_id") or ""),
        symbol=str(raw.get("symbol") or ""),
        side=str(raw.get("side") or ""),
        order_type=str(raw.get("type") or raw.get("order_type") or ""),
        order_class=str(raw.get("order_class") or "simple"),
        time_in_force=str(raw.get("time_in_force") or ""),
        qty=_float(raw.get("qty")) or 0.0,
        status=str(raw.get("status") or ""),
        filled_qty=_float(raw.get("filled_qty")) or 0.0,
        filled_avg_price=_float(raw.get("filled_avg_price")),
        limit_price=_float(raw.get("limit_price")),
        stop_price=_float(raw.get("stop_price")),
        submitted_at=_when(raw.get("submitted_at")),
        filled_at=_when(raw.get("filled_at")),
        legs=[parse_order(leg) for leg in raw.get("legs") or []],
        raw={k: v for k, v in raw.items() if k != "legs"},
    )


def _ny(value: datetime) -> datetime:
    """Calendar times come back without a zone; they are New York times."""
    return (value.replace(tzinfo=NEW_YORK) if value.tzinfo is None else value).astimezone(UTC)


def round_price(price: float) -> float:
    """US equities above $1 trade in whole cents; below $1 in hundredths of a cent."""
    return round(price, 2) if price >= 1 else round(price, 4)


# --------------------------------------------------------------------------- the real adapter


class AlpacaPaperBroker:
    def __init__(self, api_key: str, secret_key: str, *, live_feed: str = "iex") -> None:
        assert_paper_only(ALPACA_PAPER_BASE_URL, api_key=api_key)
        self._client = TradingClient(api_key, secret_key, paper=True, raw_data=True)
        base = getattr(self._client, "_base_url", "")
        actual = str(getattr(base, "value", base))  # the SDK stores an enum, not a plain string
        if actual.rstrip("/") != ALPACA_PAPER_BASE_URL:
            raise LiveTradingForbiddenError(
                f"trading client points at {actual!r}, not the paper endpoint"
            )
        assert_paper_only(actual, api_key=api_key)
        # Real-time prices: the free data plan allows the IEX feed only.
        self._data = StockHistoricalDataClient(api_key, secret_key, raw_data=True)
        self._feed = DataFeed(live_feed)

    def _call(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except APIError as exc:
            raise BrokerError(f"Alpaca refused to {what}: {exc}") from exc

    def account(self) -> Account:
        raw = cast(dict[str, Any], self._call("read the account", self._client.get_account))
        return Account(
            equity=_float(raw.get("equity")) or 0.0, last_equity=_float(raw.get("last_equity")) or 0.0,
            cash=_float(raw.get("cash")) or 0.0, status=str(raw.get("status")),
            trading_blocked=bool(raw.get("trading_blocked") or raw.get("account_blocked")),
            daytrade_count=int(raw["daytrade_count"]) if raw.get("daytrade_count") is not None else None,
        )  # fmt: skip

    def clock(self) -> Clock:
        raw = cast(dict[str, Any], self._call("read the market clock", self._client.get_clock))
        return Clock(_when(raw["timestamp"]), bool(raw["is_open"]), _when(raw["next_open"]),  # type: ignore[arg-type]
                     _when(raw["next_close"]))  # type: ignore[arg-type]  # fmt: skip

    def calendar(self, start: date, end: date) -> list[TradingDay]:
        rows = self._call("read the market calendar", self._client.get_calendar,
                          GetCalendarRequest(start=start, end=end))  # fmt: skip
        days = []
        for row in rows:
            day = date.fromisoformat(str(row["date"]))
            opens = datetime.combine(day, time.fromisoformat(str(row["open"])))
            closes = datetime.combine(day, time.fromisoformat(str(row["close"])))
            days.append(TradingDay(day, _ny(opens), _ny(closes)))
        return days

    def positions(self) -> list[Position]:
        rows = self._call("read positions", self._client.get_all_positions)
        return [
            Position(str(r["symbol"]), _float(r.get("qty")) or 0.0, _float(r.get("avg_entry_price")) or 0.0,
                     _float(r.get("market_value")) or 0.0, _float(r.get("current_price")) or 0.0)
            for r in rows
        ]  # fmt: skip

    def open_orders(self) -> list[BrokerOrder]:
        rows = self._call("list orders", self._client.get_orders,
                          GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500))  # fmt: skip
        return [parse_order(r) for r in rows]

    def order(self, order_id: str) -> BrokerOrder:
        from alpaca.trading.requests import GetOrderByIdRequest

        raw = self._call(
            "read an order",
            self._client.get_order_by_id,
            order_id,
            GetOrderByIdRequest(nested=True),
        )
        return parse_order(cast(dict[str, Any], raw))

    def submit_bracket(
        self, *, symbol: str, qty: int, side: str, client_order_id: str, limit_price: float | None,
        take_profit: float, stop_loss: float, time_in_force: str,
    ) -> BrokerOrder:  # fmt: skip
        common: dict[str, Any] = {
            "symbol": symbol, "qty": qty, "side": OrderSide(side), "client_order_id": client_order_id,
            "time_in_force": TimeInForce(time_in_force), "order_class": OrderClass.BRACKET,
            "take_profit": TakeProfitRequest(limit_price=round_price(take_profit)),
            "stop_loss": StopLossRequest(stop_price=round_price(stop_loss)),
        }  # fmt: skip
        request = (
            LimitOrderRequest(limit_price=round_price(limit_price), **common)
            if limit_price is not None
            else MarketOrderRequest(**common)
        )
        return parse_order(
            cast(
                dict[str, Any],
                self._call(
                    f"place a bracket order for {symbol}", self._client.submit_order, request
                ),
            )
        )

    def submit_market(
        self, *, symbol: str, qty: int, side: str, client_order_id: str
    ) -> BrokerOrder:
        request = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide(side), client_order_id=client_order_id,
                                     time_in_force=TimeInForce.DAY)  # fmt: skip
        return parse_order(
            cast(
                dict[str, Any],
                self._call(
                    f"place a market order for {symbol}", self._client.submit_order, request
                ),
            )
        )

    def cancel(self, order_id: str) -> None:
        self._call("cancel an order", self._client.cancel_order_by_id, order_id)

    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        if not symbols:
            return {}
        try:
            raw = self._data.get_stock_latest_trade(
                StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=self._feed)
            )
        except APIError as exc:
            raise BrokerError(f"Alpaca refused the latest prices: {exc}") from exc
        prices = {}
        for symbol, trade in cast(dict[str, Any], raw).items():
            price = _float((trade or {}).get("p"))
            if price:
                prices[symbol] = price
        return prices
