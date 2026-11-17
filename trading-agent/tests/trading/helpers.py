"""A fake Alpaca paper account that behaves like the real one where it matters:
bracket orders with two linked exit legs, fills, cancels, positions, the clock."""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import Engine

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.timeutil import at_new_york
from papertrader.trading.broker import (
    Account,
    BrokerError,
    BrokerOrder,
    Clock,
    Position,
    TradingDay,
)
from papertrader.trading.engine import TradingEngine

from ..ingest.helpers import shipped_config

# Monday 17 November 2026, 10:00 New York.
MONDAY = date(2026, 11, 16)
OPEN_TIME = at_new_york(MONDAY, time(10, 0))
HOLIDAYS = {date(2026, 11, 26)}  # Thanksgiving
HALF_DAYS = {date(2026, 11, 27)}  # closes at 13:00


@dataclass
class Holding:
    qty: float
    avg: float


class FakeBroker:
    def __init__(self, clock: list[datetime], equity: float = 100_000.0) -> None:
        self._clock = clock  # shared, mutable "now"
        self.equity = equity
        self.last_equity = equity
        self.cash = equity
        self.prices: dict[str, float] = {}
        self.orders: dict[str, BrokerOrder] = {}
        self.holdings: dict[str, Holding] = {}
        self.is_open = True
        self.refuse_market: int = 0  # refuse this many market orders (shares still "held")
        self.cancelled: list[str] = []
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- reading
    def account(self) -> Account:
        return Account(self.equity, self.last_equity, self.cash, "ACTIVE", False)

    def clock(self) -> Clock:
        now = self._clock[0]
        return Clock(now, self.is_open, now + timedelta(hours=20), now + timedelta(hours=6))

    def calendar(self, start: date, end: date) -> list[TradingDay]:
        days, day = [], start
        while day <= end:
            if day.weekday() < 5 and day not in HOLIDAYS:
                close = time(13, 0) if day in HALF_DAYS else time(16, 0)
                days.append(TradingDay(day, at_new_york(day, time(9, 30)), at_new_york(day, close)))
            day += timedelta(days=1)
        return days

    def positions(self) -> list[Position]:
        return [
            Position(s, h.qty, h.avg, h.qty * self.prices.get(s, h.avg), self.prices.get(s, h.avg))
            for s, h in self.holdings.items()
        ]

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self.orders.values() if not o.is_final and o.raw.get("parent") is None]

    def order(self, order_id: str) -> BrokerOrder:
        found = self.orders[order_id]
        return replace(found, legs=[replace(self.orders[leg.id]) for leg in found.legs])

    def latest_prices(self, symbols: Sequence[str]) -> dict[str, float]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    # ---------------------------------------------------------------- writing
    def _new(self, **fields: object) -> BrokerOrder:
        order = BrokerOrder(id=f"alp-{next(self._ids)}", submitted_at=self._clock[0], **fields)  # type: ignore[arg-type]
        self.orders[order.id] = order
        return order

    def submit_bracket(self, *, symbol: str, qty: int, side: str, client_order_id: str, limit_price: float | None,
                       take_profit: float, stop_loss: float, time_in_force: str) -> BrokerOrder:  # fmt: skip
        if any(o.client_order_id == client_order_id for o in self.orders.values()):
            raise BrokerError("client_order_id must be unique")
        exit_side = "sell" if side == "buy" else "buy"
        parent = self._new(client_order_id=client_order_id, symbol=symbol, side=side,
                           order_type="limit" if limit_price else "market", order_class="bracket",
                           time_in_force=time_in_force, qty=qty, status="new", limit_price=limit_price)  # fmt: skip
        tp = self._new(client_order_id=f"{parent.id}-tp", symbol=symbol, side=exit_side, order_type="limit",
                       order_class="bracket", time_in_force=time_in_force, qty=qty, status="held",
                       limit_price=take_profit, raw={"parent": parent.id})  # fmt: skip
        sl = self._new(client_order_id=f"{parent.id}-sl", symbol=symbol, side=exit_side, order_type="stop",
                       order_class="bracket", time_in_force=time_in_force, qty=qty, status="held",
                       stop_price=stop_loss, raw={"parent": parent.id})  # fmt: skip
        parent.legs = [tp, sl]
        return self.order(parent.id)

    def submit_market(
        self, *, symbol: str, qty: int, side: str, client_order_id: str
    ) -> BrokerOrder:
        if self.refuse_market > 0:
            self.refuse_market -= 1
            raise BrokerError("insufficient qty available for order (requested: 1, available: 0)")
        return replace(self._new(client_order_id=client_order_id, symbol=symbol, side=side, order_type="market",
                                 order_class="simple", time_in_force="day", qty=qty, status="accepted"))  # fmt: skip

    def cancel(self, order_id: str) -> None:
        order = self.orders[order_id]
        if order.is_final:
            raise BrokerError(f"order is already {order.status}")
        order.status = "canceled"
        self.cancelled.append(order_id)
        for leg in order.legs:
            self.orders[leg.id].status = "canceled"
        parent = order.raw.get("parent")
        if parent:  # one-cancels-other: the sibling goes too
            for leg in self.orders[str(parent)].legs:
                self.orders[leg.id].status = "canceled"

    # ---------------------------------------------------------------- market simulation
    def _fill(self, order: BrokerOrder, price: float) -> None:
        order.status, order.filled_qty, order.filled_avg_price, order.filled_at = (
            "filled",
            order.qty,
            price,
            self._clock[0],
        )
        sign = 1 if order.side == "buy" else -1
        held = self.holdings.get(order.symbol)
        new_qty = (held.qty if held else 0) + sign * order.qty
        self.cash -= sign * order.qty * price
        if new_qty == 0:
            self.holdings.pop(order.symbol, None)
        else:
            self.holdings[order.symbol] = Holding(new_qty, price if held is None else held.avg)

    def fill_entry(self, symbol: str, price: float, qty: float | None = None) -> None:
        parent = next(
            o for o in self.orders.values() if o.symbol == symbol and o.legs and not o.is_final
        )
        if qty is not None and qty < parent.qty:
            parent.status, parent.filled_qty, parent.filled_avg_price = (
                "partially_filled",
                qty,
                price,
            )
            return
        self._fill(parent, price)
        for leg in parent.legs:
            self.orders[leg.id].status = "new"

    def hit(self, symbol: str, which: str, price: float) -> None:
        kind = "limit" if which == "target" else "stop"
        leg = next(o for o in self.orders.values()
                   if o.symbol == symbol and o.raw.get("parent") and o.order_type == kind and o.status == "new")  # fmt: skip
        self._fill(leg, price)
        for sibling in self.orders[str(leg.raw["parent"])].legs:
            if sibling.id != leg.id:
                self.orders[sibling.id].status = "canceled"

    def fill_market(self, symbol: str, price: float) -> None:
        order = next(o for o in self.orders.values()
                     if o.symbol == symbol and o.order_type == "market" and not o.is_final)  # fmt: skip
        self._fill(order, price)


def make_engine(engine: Engine, *, config: AppConfig | None = None, start: datetime = OPEN_TIME,
                equity: float = 100_000.0) -> tuple[TradingEngine, FakeBroker, list[datetime]]:  # fmt: skip
    clock = [start]
    broker = FakeBroker(clock, equity)
    broker.prices["SPY"] = 600.0
    trading = TradingEngine(make_session_factory(engine), broker, config or shipped_config(),
                            now=lambda: clock[0], sleep=lambda _s: None)  # fmt: skip
    return trading, broker, clock


def seed_decision(
    engine: Engine,
    ideas: Sequence[dict[str, object]],
    *,
    kind: m.CycleKind = m.CycleKind.DAILY,
    as_of: datetime = OPEN_TIME - timedelta(hours=2),
    reports: Sequence[tuple[str, str, m.Stance]] = (),
) -> list[int]:
    """A judge decision with the given ideas; `reports` = (ticker, agent, stance) links."""
    sessions = make_session_factory(engine)
    with sessions() as session:
        for ticker, sector in (("AAPL", "Information Technology"), ("MSFT", "Information Technology"),
                               ("NVDA", "Information Technology"), ("JPM", "Financials"),
                               ("XOM", "Energy"), ("PFE", "Health Care")):  # fmt: skip
            if not session.query(m.UniverseMember).filter_by(ticker=ticker).first():
                session.add(m.UniverseMember(index_name="sp500", ticker=ticker, name=f"{ticker} Inc",
                                             gics_sector=sector, is_active=True))  # fmt: skip
        cycle = m.DecisionCycle(kind=kind, as_of=as_of, status=m.RunStatus.SUCCEEDED)
        session.add(cycle)
        session.flush()
        decision = m.JudgeDecision(
            cycle_id=cycle.id, as_of=as_of, market_summary="s", model="claude-opus-5-5"
        )
        for rank, fields in enumerate(ideas, start=1):
            idea = m.TradeIdea(**({
                "rank": rank, "action": m.TradeAction.BUY, "judge_confidence": 0.8, "final_confidence": 0.8,
                "size_pct": 0.10, "horizon_days": 5, "rationale": "strong reasons", "status": m.IdeaStatus.PROPOSED,
            } | fields))  # fmt: skip
            decision.ideas.append(idea)
        session.add(decision)
        session.flush()
        for ticker, agent, stance in reports:
            row = m.AgentReport(cycle_id=cycle.id, agent_name=agent, as_of=as_of, ticker=ticker,
                                direction=m.Direction.LONG if stance == m.Stance.SUPPORT else m.Direction.SHORT,
                                confidence=0.7, horizon_days=5, thesis="t", model="claude-sonnet-5-5")  # fmt: skip
            session.add(row)
            session.flush()
            idea = next(i for i in decision.ideas if i.ticker == ticker)
            session.add(
                m.TradeIdeaReport(trade_idea_id=idea.id, agent_report_id=row.id, stance=stance)
            )
        session.commit()
        return [i.id for i in decision.ideas]


def idea(ticker: str, price: float, **extra: object) -> dict[str, object]:
    """An idea around `price`: entry +-0.5%, target +6%, stop -3%."""
    return {"ticker": ticker, "entry_low": round(price * 0.995, 2), "entry_high": round(price * 1.005, 2),
            "target_price": round(price * 1.06, 2), "stop_price": round(price * 0.97, 2)} | extra  # fmt: skip
