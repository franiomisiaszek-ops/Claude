"""The trading engine: from approved trade ideas to bracket orders, fills, trades and exits.

Alpaca does the moment-to-moment work: each position is opened with a bracket order,
so its take-profit and stop-loss sit at the broker and trigger even if this program
is not running. This engine places those orders, records every order and fill,
turns fills into trades, adds the exits Alpaca cannot know about (time stop, thesis
break, kill switch, manual), and keeps the database in step with the account.

Every order carries a client order id derived from the idea or trade it belongs to,
so running a step twice never places a second order.
"""

from __future__ import annotations

import contextlib
import logging
import time as clock_time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db import types as db_types
from papertrader.db.state import KILL_SWITCH_KEY, TRADING_PAUSED_KEY, get_state, set_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import NEW_YORK, new_york_date
from papertrader.notify import messages
from papertrader.notify.outbox import queue
from papertrader.trading import control
from papertrader.trading.broker import Broker, BrokerError, BrokerOrder, TradingDay
from papertrader.trading.risk import Book, Proposal, check_entry

if TYPE_CHECKING:
    from papertrader.notify.warnings import Gate

log = logging.getLogger(__name__)

TRADABLE_KINDS = (m.CycleKind.DAILY, m.CycleKind.MANUAL)  # never dry runs
EXIT_RETRIES = 5
# X6: sells the system decides itself are announced first. Kill-switch and manual exits are not.
GATED_TRIGGERS = (m.ExitTrigger.TIME, m.ExitTrigger.THESIS_BREAK)


def _source(trigger: m.ExitTrigger, reason: str) -> str:
    if trigger == m.ExitTrigger.TIME:
        return "time"
    return "judge" if reason.startswith("judge:") else "thesis"


class TradingRefused(RuntimeError):
    """Nothing was done, for a reason the user should see (market closed, paused...)."""


@dataclass
class Placement:
    ticker: str
    idea_id: int
    status: str  # submitted | rejected | failed | skipped
    detail: str
    qty: int = 0
    limit_price: float | None = None


@dataclass
class SyncReport:
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    expired: list[str] = field(default_factory=list)
    updated: int = 0


@dataclass
class MonitorReport:
    equity: float = 0.0
    day_change_pct: float = 0.0
    kill_switch: bool = False
    sync: SyncReport = field(default_factory=SyncReport)
    time_stops: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def entry_client_id(idea_id: int) -> str:
    return f"pt-i{idea_id}-entry"


def exit_client_id(trade_id: int, attempt: int = 0) -> str:
    return f"pt-t{trade_id}-exit" + (f"-{attempt}" if attempt else "")


def horizon_end(
    days: Sequence[TradingDay], entered_at: datetime, horizon: int, minutes_before: int
) -> datetime:
    """When the time stop fires: shortly before the close of the horizon's last trading
    day, counting the entry day as day one. Half days close early, and that is respected."""
    entry_day = new_york_date(entered_at)
    trading = [d for d in sorted(days, key=lambda d: d.day) if d.day >= entry_day]
    if trading and entered_at >= trading[0].close:
        trading = trading[1:]  # filled after the close: day one is the next session
    if not trading:
        return entered_at + timedelta(days=horizon)
    last = trading[min(horizon - 1, len(trading) - 1)]
    return last.close - timedelta(minutes=minutes_before)


class TradingEngine:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        broker: Broker,
        config: AppConfig,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = clock_time.sleep,
        gate: Gate | None = None,
    ) -> None:
        self.sessions = sessions
        self.broker = broker
        self.config = config
        self.now: Callable[[], datetime] = now or (lambda: db_types.utcnow())
        self.sleep = sleep
        self.benchmark = config.universe.benchmark
        # X6 (systems B and C): a Telegram heads-up before each order, with Skip / Go now.
        self.gate = gate

    # ------------------------------------------------------------------ pause / resume

    def paused(self) -> dict[str, Any] | None:
        return control.paused(self.sessions)

    def pause(self, reason: str) -> None:
        control.pause(self.sessions, reason, self.now())

    def resume(self) -> None:
        control.resume(self.sessions, self.now())

    # ------------------------------------------------------------------ helpers

    def _sectors(self, session: Session) -> dict[str, str | None]:
        rows = session.execute(
            select(m.UniverseMember.ticker, m.UniverseMember.gics_sector).where(
                m.UniverseMember.index_name == self.config.universe.index
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    def _benchmark_price(self) -> float | None:
        try:
            return self.broker.latest_prices([self.benchmark]).get(self.benchmark)
        except BrokerError:
            return None

    def _our_open_entries(self, session: Session) -> list[m.Order]:
        return list(
            session.scalars(
                select(m.Order).where(
                    m.Order.leg == m.OrderLeg.ENTRY,
                    m.Order.status.not_in(sorted(_FINAL)),
                )
            )
        )

    def _book(self, session: Session, prices: Mapping[str, float]) -> Book:
        account = self.broker.account()
        held = {p.symbol: abs(p.market_value) for p in self.broker.positions() if p.qty}
        pending: dict[str, float] = {}
        for order in self._our_open_entries(session):
            remaining = max(order.qty - (order.filled_qty or 0.0), 0.0)
            price = order.limit_price or prices.get(order.ticker) or 0.0
            if remaining and order.ticker not in held:
                pending[order.ticker] = pending.get(order.ticker, 0.0) + remaining * price
        return Book(equity=account.equity, cash=account.cash - sum(pending.values()), held=held,
                    pending=pending, sectors=self._sectors(session))  # fmt: skip

    def _record_order(self, session: Session, placed: BrokerOrder, *, leg: m.OrderLeg,
                      idea_id: int | None, trade_id: int | None, parent_id: int | None) -> m.Order:  # fmt: skip
        row = session.scalar(select(m.Order).where(m.Order.alpaca_order_id == placed.id))
        if row is None:
            client_id = placed.client_order_id or f"{placed.id}"
            row = m.Order(
                client_order_id=client_id, alpaca_order_id=placed.id, parent_order_id=parent_id,
                trade_idea_id=idea_id, trade_id=trade_id, ticker=placed.symbol,
                side=m.OrderSide.BUY if placed.side == "buy" else m.OrderSide.SELL, leg=leg,
                order_type=placed.order_type, order_class=placed.order_class,
                time_in_force=placed.time_in_force, qty=placed.qty, status=placed.status,
            )  # fmt: skip
            session.add(row)
        self._update_order(row, placed)
        session.flush()
        return row

    @staticmethod
    def _update_order(row: m.Order, placed: BrokerOrder) -> None:
        row.status = placed.status
        row.filled_qty = placed.filled_qty
        row.filled_avg_price = placed.filled_avg_price
        row.limit_price = placed.limit_price
        row.stop_price = placed.stop_price
        row.submitted_at = placed.submitted_at
        row.filled_at = placed.filled_at
        row.raw = {
            k: v for k, v in placed.raw.items() if isinstance(v, str | int | float | bool | None)
        }
        row.updated_at = utcnow()

    @staticmethod
    def _leg_kind(order: BrokerOrder) -> m.OrderLeg:
        return m.OrderLeg.TAKE_PROFIT if order.order_type == "limit" else m.OrderLeg.STOP_LOSS

    # ------------------------------------------------------------------ placing orders

    def _check_market(self) -> None:
        clock = self.broker.clock()
        if not clock.is_open:
            opens = clock.next_open.astimezone(NEW_YORK)
            raise TradingRefused(
                f"the market is closed; it next opens {opens:%a %d %b %H:%M} New York time"
            )
        local = self.now().astimezone(NEW_YORK).time()
        if local < self.config.schedule.place_orders:
            raise TradingRefused(
                f"orders are only placed from {self.config.schedule.place_orders:%H:%M} New York time"
            )

    def tradable_ideas(self, session: Session, decision_id: int | None) -> list[m.TradeIdea]:
        query = (
            select(m.TradeIdea)
            .join(m.JudgeDecision, m.JudgeDecision.id == m.TradeIdea.decision_id)
            .join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
            .where(
                m.TradeIdea.status == m.IdeaStatus.PROPOSED,
                m.DecisionCycle.kind.in_(TRADABLE_KINDS),
            )
            .order_by(m.JudgeDecision.id.desc(), m.TradeIdea.rank)
        )
        if decision_id is not None:
            query = query.where(m.JudgeDecision.id == decision_id)
        else:  # only today's decisions: yesterday's ideas are stale
            today = new_york_date(self.now())
            query = query.where(m.JudgeDecision.as_of >= _day_start(today))
        return list(session.scalars(query))

    def place_orders(
        self, decision_id: int | None = None, *, qty_overrides: Mapping[int, int] | None = None,
        gated: bool = True,
    ) -> list[Placement]:  # fmt: skip
        """Turn approved ideas into bracket orders, within every risk limit.

        With a gate (X6), ideas you skipped on Telegram are dropped, and ideas whose
        heads-up time has not come yet wait for the next run."""
        if (state := self.paused()) is not None:
            raise TradingRefused(f"trading is paused ({state.get('reason', 'no reason given')})")
        self._check_market()
        account = self.broker.account()
        if account.trading_blocked or account.status.upper() != "ACTIVE":
            raise TradingRefused(f"the paper account cannot trade (status {account.status})")

        trading, risk = self.config.trading, self.config.risk
        results: list[Placement] = []
        skipped_ids, waiting_ids = (
            self._gate_ideas(decision_id, account.equity) if gated else (set(), set())
        )
        sent: list[str] = []
        with self.sessions() as session:
            ideas = self.tradable_ideas(session, decision_id)
            prices = self.broker.latest_prices(sorted({i.ticker for i in ideas})) if ideas else {}
            book = self._book(session, prices)
            for idea in ideas:
                if idea.id in skipped_ids:
                    idea.status, idea.status_reason = (
                        m.IdeaStatus.EXPIRED,
                        "skipped by you on Telegram",
                    )
                    results.append(
                        Placement(idea.ticker, idea.id, "skipped", "skipped by you on Telegram")
                    )
                    continue
                if idea.id in waiting_ids:
                    results.append(
                        Placement(idea.ticker, idea.id, "waiting", "waiting for its heads-up time")
                    )
                    continue
                client_id = entry_client_id(idea.id)
                if session.scalar(select(m.Order.id).where(m.Order.client_order_id == client_id)):
                    idea.status = m.IdeaStatus.SUBMITTED
                    results.append(Placement(idea.ticker, idea.id, "skipped", "already ordered"))
                    continue
                proposal = Proposal(
                    ticker=idea.ticker, action=idea.action.value, entry_low=idea.entry_low or 0.0,
                    entry_high=idea.entry_high or 0.0, target_price=idea.target_price,
                    stop_price=idea.stop_price, size_pct=idea.size_pct,
                    qty_override=(qty_overrides or {}).get(idea.id),
                )  # fmt: skip
                verdict = check_entry(proposal, prices.get(idea.ticker), book, risk, trading)
                if not verdict.ok:
                    idea.status, idea.status_reason = m.IdeaStatus.REJECTED_BY_RISK, verdict.reason
                    results.append(Placement(idea.ticker, idea.id, "rejected", verdict.reason))
                    continue
                try:
                    placed = self.broker.submit_bracket(
                        symbol=idea.ticker, qty=verdict.qty, side=verdict.side, client_order_id=client_id,
                        limit_price=verdict.limit_price, take_profit=verdict.take_profit,
                        stop_loss=verdict.stop_loss, time_in_force=trading.time_in_force,
                    )  # fmt: skip
                except BrokerError as exc:
                    idea.status, idea.status_reason = m.IdeaStatus.REJECTED_BY_RISK, str(exc)[:500]
                    results.append(Placement(idea.ticker, idea.id, "failed", str(exc)))
                    continue
                parent = self._record_order(session, placed, leg=m.OrderLeg.ENTRY, idea_id=idea.id,
                                            trade_id=None, parent_id=None)  # fmt: skip
                for leg in placed.legs:
                    self._record_order(session, leg, leg=self._leg_kind(leg), idea_id=idea.id,
                                       trade_id=None, parent_id=parent.id)  # fmt: skip
                idea.status, idea.status_reason = m.IdeaStatus.SUBMITTED, None
                sent.append(f"idea:{idea.id}")
                book.add(idea.ticker, verdict.notional)
                results.append(Placement(idea.ticker, idea.id, "submitted", "bracket order placed",
                                         verdict.qty, verdict.limit_price))  # fmt: skip
                log.info("order placed", extra={"ticker": idea.ticker, "qty": verdict.qty,
                                                "limit": verdict.limit_price})  # fmt: skip
            session.commit()
        if self.gate is not None:
            self.gate.mark_sent(sent)
        return results

    def _gate_ideas(self, decision_id: int | None, equity: float) -> tuple[set[int], set[int]]:
        """(skipped idea ids, waiting idea ids). Ideas never announced are announced now."""
        if self.gate is None or not self.gate.enabled:
            return set(), set()
        from papertrader.notify.ahead import idea_pending

        with self.sessions() as session:
            items = [idea_pending(i, equity) for i in self.tradable_ideas(session, decision_id)]
        if not items:
            return set(), set()
        _ready, skipped, waiting = self.gate.decide(items, early=False)
        return {int(r.split(":")[1]) for r in skipped}, {int(r.split(":")[1]) for r in waiting}

    # ------------------------------------------------------------------ syncing fills

    def sync(self) -> SyncReport:
        """Bring every unfinished order up to date and turn fills into trades."""
        report = SyncReport()
        with self.sessions() as session:
            unfinished = session.scalars(
                select(m.Order).where(m.Order.status.not_in(sorted(_FINAL)))
            ).all()
            tops = {o.parent_order_id or o.id for o in unfinished}
            for top_id in sorted(tops):
                top = session.get(m.Order, top_id)
                if top is None or top.alpaca_order_id is None:
                    continue
                try:
                    fresh = self.broker.order(top.alpaca_order_id)
                except BrokerError as exc:
                    log.warning(
                        "could not refresh an order",
                        extra={"order": top.client_order_id, "error": str(exc)},
                    )
                    continue
                self._apply(session, top, fresh, report)
            self._catch_up(session, report)
            session.commit()
        return report

    def _catch_up(self, session: Session, report: SyncReport) -> None:
        """Fills that were already final when first recorded (for example a market order
        that filled before the submit call returned) never pass through `_apply`."""
        entries = session.scalars(
            select(m.Order).where(m.Order.leg == m.OrderLeg.ENTRY, m.Order.status.in_(sorted(_FINAL)),
                                  m.Order.filled_qty > 0, m.Order.trade_id.is_(None))
        ).all()  # fmt: skip
        for entry in entries:
            self._record_fill(session, entry)
            trade = self._open_trade(session, entry)
            for leg in session.scalars(select(m.Order).where(m.Order.parent_order_id == entry.id)):
                leg.trade_id = trade.id
            report.opened.append(trade.ticker)
        exits = session.scalars(
            select(m.Order)
            .join(m.Trade, m.Trade.id == m.Order.trade_id)
            .where(m.Order.leg.in_((m.OrderLeg.EXIT, m.OrderLeg.TAKE_PROFIT, m.OrderLeg.STOP_LOSS)),
                   m.Order.status == "filled", m.Trade.status == m.TradeStatus.OPEN)
        ).all()  # fmt: skip
        for order in exits:
            self._record_fill(session, order)
            trigger = {
                m.OrderLeg.TAKE_PROFIT: m.ExitTrigger.TARGET,
                m.OrderLeg.STOP_LOSS: m.ExitTrigger.STOP,
            }.get(order.leg)
            closing = session.get(m.Trade, order.trade_id)
            trigger = trigger or (
                closing.exit_trigger if closing and closing.exit_trigger else m.ExitTrigger.MANUAL
            )
            if order.trade_id is not None and self._close_trade(
                session, order.trade_id, order, trigger
            ):
                report.closed.append(order.ticker)

    def _apply(
        self, session: Session, top: m.Order, fresh: BrokerOrder, report: SyncReport
    ) -> None:
        was_final = top.status in _FINAL
        self._update_order(top, fresh)
        report.updated += 1
        legs: list[m.Order] = []
        for leg in fresh.legs:
            row = self._record_order(session, leg, leg=self._leg_kind(leg), idea_id=top.trade_idea_id,
                                     trade_id=top.trade_id, parent_id=top.id)  # fmt: skip
            legs.append(row)

        if top.leg == m.OrderLeg.ENTRY:
            if fresh.is_final and not was_final:
                self._record_fill(session, top)
                if (top.filled_qty or 0) > 0 and top.trade_id is None:
                    trade = self._open_trade(session, top)
                    for row in legs:
                        row.trade_id = trade.id
                    report.opened.append(trade.ticker)
                elif (top.filled_qty or 0) == 0 and top.trade_idea_id is not None:
                    idea = session.get(m.TradeIdea, top.trade_idea_id)
                    if idea is not None and idea.status == m.IdeaStatus.SUBMITTED:
                        idea.status = m.IdeaStatus.EXPIRED
                        idea.status_reason = f"entry order {fresh.status} without a fill"
                        report.expired.append(idea.ticker)
            for row in legs:
                if row.status == "filled" and row.trade_id is not None:
                    self._record_fill(session, row)
                    trigger = (
                        m.ExitTrigger.TARGET
                        if row.leg == m.OrderLeg.TAKE_PROFIT
                        else m.ExitTrigger.STOP
                    )
                    if self._close_trade(session, row.trade_id, row, trigger):
                        report.closed.append(row.ticker)
        elif top.leg == m.OrderLeg.EXIT and fresh.status == "filled" and not was_final:
            self._record_fill(session, top)
            if top.trade_id is not None:
                closing = session.get(m.Trade, top.trade_id)
                trigger = (
                    closing.exit_trigger
                    if closing and closing.exit_trigger
                    else m.ExitTrigger.MANUAL
                )
                if self._close_trade(session, top.trade_id, top, trigger):
                    report.closed.append(top.ticker)

    def _record_fill(self, session: Session, order: m.Order) -> None:
        if not order.filled_qty or order.filled_avg_price is None or order.alpaca_order_id is None:
            return
        if session.scalar(select(m.Fill.id).where(m.Fill.execution_id == order.alpaca_order_id)):
            return
        session.add(m.Fill(order_id=order.id, execution_id=order.alpaca_order_id, ticker=order.ticker,
                           side=order.side, qty=order.filled_qty, price=order.filled_avg_price,
                           filled_at=order.filled_at or self.now()))  # fmt: skip

    def _open_trade(self, session: Session, entry: m.Order) -> m.Trade:
        idea = session.get(m.TradeIdea, entry.trade_idea_id) if entry.trade_idea_id else None
        cycle_kind = None
        if idea is not None:
            cycle_kind = session.scalar(
                select(m.DecisionCycle.kind)
                .join(m.JudgeDecision, m.JudgeDecision.cycle_id == m.DecisionCycle.id)
                .where(m.JudgeDecision.id == idea.decision_id)
            )
        entered = entry.filled_at or self.now()
        horizon = idea.horizon_days if idea else 5
        start = new_york_date(entered)
        try:
            days = self.broker.calendar(start, start + timedelta(days=horizon * 2 + 10))
        except BrokerError:
            days = []
        trade = m.Trade(
            trade_idea_id=entry.trade_idea_id, ticker=entry.ticker,
            sector=self._sectors(session).get(entry.ticker),
            direction=m.Direction.LONG if entry.side == m.OrderSide.BUY else m.Direction.SHORT,
            qty=entry.filled_qty, entry_price=entry.filled_avg_price or 0.0, entered_at=entered,
            target_price=idea.target_price if idea else None, stop_price=idea.stop_price if idea else None,
            horizon_ends_at=horizon_end(days, entered, horizon, self.config.trading.time_stop_minutes_before_close),
            benchmark_entry_price=self._benchmark_price(), is_test=cycle_kind == m.CycleKind.MANUAL,
        )  # fmt: skip
        session.add(trade)
        session.flush()
        entry.trade_id = trade.id
        if idea is not None:
            idea.status, idea.status_reason = m.IdeaStatus.EXECUTED, None
        queue(
            session, "trade_executed",
            messages.trade_executed(side=entry.side.value, qty=trade.qty, ticker=trade.ticker,
                                    price=trade.entry_price, confidence=idea.final_confidence if idea else None,
                                    reason=idea.rationale if idea else "", test=trade.is_test),
            dedup_key=f"executed:{entry.client_order_id}",
        )  # fmt: skip
        return trade

    def _close_trade(
        self, session: Session, trade_id: int, exit_order: m.Order, trigger: m.ExitTrigger
    ) -> bool:
        trade = session.get(m.Trade, trade_id)
        if (
            trade is None
            or trade.status == m.TradeStatus.CLOSED
            or exit_order.filled_avg_price is None
        ):
            return False
        long = trade.direction == m.Direction.LONG
        price = exit_order.filled_avg_price
        trade.exit_price = price
        trade.exited_at = exit_order.filled_at or self.now()
        trade.exit_trigger = trigger
        trade.status = m.TradeStatus.CLOSED
        trade.pnl_usd = (price - trade.entry_price) * trade.qty * (1 if long else -1)
        trade.pnl_pct = (
            trade.pnl_usd / (trade.entry_price * trade.qty) if trade.entry_price else 0.0
        )
        spy_now = self._benchmark_price()
        if trade.benchmark_entry_price and spy_now:
            spy = spy_now / trade.benchmark_entry_price - 1
            trade.benchmark_return_pct = spy
            trade.excess_return_pct = trade.pnl_pct - (spy if long else -spy)
        # Cancel whatever is left of the bracket (the other leg is usually cancelled by Alpaca).
        for leg in session.scalars(
            select(m.Order).where(m.Order.trade_id == trade.id, m.Order.status.not_in(sorted(_FINAL)),
                                  m.Order.id != exit_order.id)
        ):  # fmt: skip
            if leg.alpaca_order_id and leg.leg in (m.OrderLeg.TAKE_PROFIT, m.OrderLeg.STOP_LOSS):
                with contextlib.suppress(BrokerError):
                    self.broker.cancel(leg.alpaca_order_id)
        right, wrong = self._agents_verdict(session, trade)
        queue(
            session, "trade_closed",
            messages.trade_closed(ticker=trade.ticker, exit_price=price, pnl_usd=trade.pnl_usd,
                                  pnl_pct=trade.pnl_pct, excess_pct=trade.excess_return_pct,
                                  opened=trade.entered_at, closed=trade.exited_at, trigger=trigger.value,
                                  right=right, wrong=wrong, test=trade.is_test),
            dedup_key=f"closed:{trade.id}",
        )  # fmt: skip
        return True

    @staticmethod
    def _agents_verdict(session: Session, trade: m.Trade) -> tuple[list[str], list[str]]:
        return agents_verdict(session, trade)

    # ------------------------------------------------------------------ exits

    def exit_trade(
        self, trade_id: int, trigger: m.ExitTrigger, reason: str, *, gated: bool = True
    ) -> str:
        """Close a position with a market order, after removing its bracket legs.

        With a gate (X6), time-limit and thesis sells are announced first and sent once
        their heads-up time has come (or after Go now); a skip holds for the rest of the day."""
        if gated and self.gate is not None and self.gate.enabled and trigger in GATED_TRIGGERS:
            from papertrader.notify.ahead import exit_pending

            with self.sessions() as session:
                trade = session.get(m.Trade, trade_id)
                if trade is None or trade.status == m.TradeStatus.CLOSED:
                    return "already closed"
                item = exit_pending(
                    session, trade, _source(trigger, reason), reason, new_york_date(self.now())
                )
            _ready, skipped, waiting = self.gate.decide([item])
            if item.ref in skipped:
                self._note_skip(trade_id, trigger, reason)
                return "skipped by you on Telegram (until tomorrow)"
            if item.ref in waiting:
                return "announced; waiting for its heads-up time"
            outcome = self._exit_now(trade_id, trigger, reason)
            if outcome == "exit order placed":
                self.gate.mark_sent([item.ref])
            return outcome
        return self._exit_now(trade_id, trigger, reason)

    def _note_skip(self, trade_id: int, trigger: m.ExitTrigger, reason: str) -> None:
        """Record the skip once a day per position and reason type."""
        with self.sessions() as session:
            note = f"skipped by you on Telegram: {reason}"[:2000]
            if not session.scalar(select(m.ExitSignal.id).where(
                    m.ExitSignal.trade_id == trade_id, m.ExitSignal.trigger == trigger,
                    m.ExitSignal.acted_on.is_(False), m.ExitSignal.reason.startswith("skipped by you on Telegram"),
                    m.ExitSignal.created_at >= _day_start(new_york_date(self.now()))).limit(1)):  # fmt: skip
                trade = session.get(m.Trade, trade_id)
                session.add(m.ExitSignal(trade_id=trade_id, ticker=trade.ticker if trade else "?", trigger=trigger,
                                         reason=note, acted_on=False))  # fmt: skip
                session.commit()

    def _exit_now(self, trade_id: int, trigger: m.ExitTrigger, reason: str) -> str:
        with self.sessions() as session:
            trade = session.get(m.Trade, trade_id)
            if trade is None or trade.status == m.TradeStatus.CLOSED:
                return "already closed"
            pending_exit = session.scalar(
                select(m.Order.id).where(m.Order.trade_id == trade.id, m.Order.leg == m.OrderLeg.EXIT,
                                         m.Order.status.not_in(sorted(_FINAL)))
            )  # fmt: skip
            if pending_exit:
                return "an exit order is already working"
            for leg in session.scalars(
                select(m.Order).where(m.Order.trade_id == trade.id, m.Order.status.not_in(sorted(_FINAL)),
                                      m.Order.leg.in_((m.OrderLeg.TAKE_PROFIT, m.OrderLeg.STOP_LOSS)))
            ):  # fmt: skip
                try:
                    if leg.alpaca_order_id:
                        self.broker.cancel(leg.alpaca_order_id)
                except BrokerError as exc:
                    log.info(
                        "leg cancel refused (probably cancelled with its partner)",
                        extra={"error": str(exc)},
                    )
            side = "sell" if trade.direction == m.Direction.LONG else "buy"
            placed: BrokerOrder | None = None
            attempts = session.scalar(
                select(func.count()).select_from(m.Order).where(m.Order.trade_id == trade.id,
                                                                m.Order.leg == m.OrderLeg.EXIT)
            ) or 0  # fmt: skip
            for attempt in range(EXIT_RETRIES):
                try:
                    placed = self.broker.submit_market(symbol=trade.ticker, qty=int(trade.qty), side=side,
                                                       client_order_id=exit_client_id(trade.id, attempts + attempt))  # fmt: skip
                    break
                except BrokerError as exc:
                    # Right after a cancel the shares can still be "held" for a moment.
                    if attempt == EXIT_RETRIES - 1:
                        queue(session, "alert", messages.alert(f"could not close {trade.ticker}: {exc}"),
                              dedup_key=f"exitfail:{trade.id}:{new_york_date(self.now())}")  # fmt: skip
                        session.commit()
                        return f"failed: {exc}"
                    self.sleep(2.0)
            assert placed is not None
            self._record_order(session, placed, leg=m.OrderLeg.EXIT, idea_id=trade.trade_idea_id,
                               trade_id=trade.id, parent_id=None)  # fmt: skip
            trade.exit_trigger, trade.exit_reason = trigger, reason[:1000]
            session.add(m.ExitSignal(trade_id=trade.id, ticker=trade.ticker, trigger=trigger,
                                     reason=reason[:2000], acted_on=True))  # fmt: skip
            queue(session, "sell_signal", messages.sell_signal(ticker=trade.ticker, trigger=trigger.value, reason=reason),
                  dedup_key=f"sell:{trade.id}:{trigger.value}")  # fmt: skip
            session.commit()
        return "exit order placed"

    def exit_ticker(self, ticker: str, trigger: m.ExitTrigger, reason: str) -> str:
        with self.sessions() as session:
            trade_id = session.scalar(
                select(m.Trade.id).where(
                    m.Trade.ticker == ticker.upper(), m.Trade.status == m.TradeStatus.OPEN
                )
            )
        if trade_id is None:
            return f"no open trade in {ticker.upper()}"
        return self.exit_trade(trade_id, trigger, reason)

    def time_stops(self) -> list[str]:
        now = self.now()
        with self.sessions() as session:
            due = session.execute(
                select(m.Trade.id, m.Trade.ticker, m.Trade.horizon_ends_at).where(
                    m.Trade.status == m.TradeStatus.OPEN, m.Trade.horizon_ends_at.is_not(None),
                    m.Trade.horizon_ends_at <= now,
                )
            ).all()  # fmt: skip
        stopped = []
        reason = "the planned holding period is over"
        if due and self.gate is not None and self.gate.enabled:  # one heads-up for all of them
            from papertrader.notify.ahead import exit_pending

            with self.sessions() as session:
                items = [exit_pending(session, t, "time", reason, new_york_date(now))
                         for t in (session.get(m.Trade, trade_id) for trade_id, _t, _e in due) if t is not None]  # fmt: skip
            self.gate.warn(items)
        for trade_id, ticker, _ends in due:
            outcome = self.exit_trade(trade_id, m.ExitTrigger.TIME, reason)
            if outcome == "exit order placed":
                stopped.append(ticker)
        return stopped

    # ------------------------------------------------------------------ monitoring

    def kill_switch(self, day_change_pct: float) -> bool:
        """Pause trading and cancel unfilled entries once the day's loss passes the limit.
        Open positions keep their stop-losses. Stays paused until resumed by hand."""
        limit = self.config.risk.daily_loss_kill_switch_pct
        if day_change_pct > -limit:
            return False
        today = new_york_date(self.now())
        with self.sessions() as session:
            state = get_state(session, KILL_SWITCH_KEY) or {}
            if state.get("active") and state.get("day") == today.isoformat():
                return True
            set_state(session, KILL_SWITCH_KEY, {"active": True, "day": today.isoformat(),
                                                 "day_change_pct": day_change_pct, "at": self.now().isoformat()})  # fmt: skip
            set_state(session, TRADING_PAUSED_KEY, {
                "paused": True, "at": self.now().isoformat(),
                "reason": f"kill switch: the account is down {day_change_pct:.1%} today",
            })  # fmt: skip
            for order in self._our_open_entries(session):
                if order.alpaca_order_id and not order.filled_qty:
                    with contextlib.suppress(BrokerError):
                        self.broker.cancel(order.alpaca_order_id)
            queue(session, "alert",
                  messages.alert(f"KILL SWITCH: account down {day_change_pct:.1%} today. Trading paused, unfilled "
                                 "orders cancelled, open positions keep their stops. Resume by hand."),
                  dedup_key=f"kill:{today}")  # fmt: skip
            session.commit()
        return True

    def snapshot(self, kind: str = "intraday") -> m.EquitySnapshot:
        account = self.broker.account()
        positions = self.broker.positions()
        now = self.now()
        with self.sessions() as session:
            row = session.scalar(select(m.EquitySnapshot).where(m.EquitySnapshot.ts == now))
            if row is None:
                row = m.EquitySnapshot(ts=now, equity=0.0, cash=0.0, positions_value=0.0)
                session.add(row)
            row.kind = kind
            row.equity, row.cash = account.equity, account.cash
            row.positions_value = sum(p.market_value for p in positions)
            row.benchmark_close = self._benchmark_price()
            row.day_pnl_usd = account.equity - account.last_equity
            row.day_pnl_pct = account.day_change_pct
            session.commit()
        return row

    def reconcile_positions(self) -> list[str]:
        """Warn about positions the database does not know, and trades the account lacks."""
        warnings = []
        held = {p.symbol: p for p in self.broker.positions() if p.qty}
        today = new_york_date(self.now())
        with self.sessions() as session:
            open_trades = {
                t.ticker: t
                for t in session.scalars(
                    select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN)
                )
            }
            for symbol in sorted(set(held) - set(open_trades)):
                text = f"{symbol} is held in the paper account but not managed by this system"
                warnings.append(text)
                queue(
                    session, "alert", messages.alert(text), dedup_key=f"unmanaged:{symbol}:{today}"
                )
            for ticker in sorted(set(open_trades) - set(held)):
                text = f"open trade in {ticker} has no position at Alpaca (closed outside this system?)"
                warnings.append(text)
                queue(session, "alert", messages.alert(text), dedup_key=f"missing:{ticker}:{today}")
            session.commit()
        return warnings

    def monitor(self) -> MonitorReport:
        """One pass: account check, kill switch, fills, time stops, reconciliation."""
        report = MonitorReport()
        account = self.broker.account()
        report.equity, report.day_change_pct = account.equity, account.day_change_pct
        report.kill_switch = self.kill_switch(account.day_change_pct)
        report.sync = self.sync()
        if self.broker.clock().is_open:
            report.time_stops = self.time_stops()
            if report.time_stops:
                self.sleep(3.0)
                extra = self.sync()
                report.sync.closed += extra.closed
        report.warnings = self.reconcile_positions()
        self.snapshot("intraday")
        return report

    def end_of_day(self) -> dict[str, Any]:
        """Cancel entries that did not fill, record the closing equity, write the summary."""
        self.sync()
        with self.sessions() as session:
            for order in self._our_open_entries(session):
                if order.alpaca_order_id:
                    try:
                        self.broker.cancel(order.alpaca_order_id)
                    except BrokerError as exc:
                        log.info(
                            "cancel refused",
                            extra={"order": order.client_order_id, "error": str(exc)},
                        )
        self.sleep(2.0)
        report = self.sync()
        snap = self.snapshot("eod")
        with self.sessions() as session:
            first = session.scalars(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).first()
            closed = session.scalars(
                select(m.Trade).where(
                    m.Trade.status == m.TradeStatus.CLOSED, m.Trade.is_test.is_(False)
                )
            ).all()
            wins = [t for t in closed if (t.pnl_usd or 0) > 0]
            win_rate = len(wins) / len(closed) if closed else None
            total = snap.equity / first.equity - 1 if first and first.equity else None
            spy = (snap.benchmark_close / first.benchmark_close - 1
                   if first and first.benchmark_close and snap.benchmark_close else None)  # fmt: skip
            positions = [(p.symbol, p.market_value, p.current_price / p.avg_entry_price - 1 if p.avg_entry_price else 0.0)
                         for p in self.broker.positions() if p.qty]  # fmt: skip
            summary = {
                "equity": snap.equity, "day_pnl": snap.day_pnl_usd or 0.0, "day_pnl_pct": snap.day_pnl_pct or 0.0,
                "total_pct": total, "spy_total_pct": spy, "win_rate": win_rate,
                "closed_trades": len(closed), "positions": positions, "expired": report.expired,
            }  # fmt: skip
            text = messages.eod_summary(
                day=self.now(), equity=snap.equity, day_pnl=snap.day_pnl_usd or 0.0,
                day_pnl_pct=snap.day_pnl_pct or 0.0, total_pct=total, spy_total_pct=spy,
                win_rate=win_rate, closed_trades=len(closed), positions=positions,
            )  # fmt: skip
            queue(session, "eod", text, dedup_key=f"eod:{new_york_date(self.now())}")
            session.commit()
        return summary

    # ------------------------------------------------------------------ test orders

    def create_test_idea(self, ticker: str) -> tuple[int, int]:
        """A small, clearly marked test trade: +2% target, -2% stop, one-day horizon."""
        ticker = ticker.upper()
        price = self.broker.latest_prices([ticker]).get(ticker)
        if not price:
            raise TradingRefused(f"no current price for {ticker}")
        now = self.now()
        with self.sessions() as session:
            cycle = m.DecisionCycle(kind=m.CycleKind.MANUAL, as_of=now, status=m.RunStatus.SUCCEEDED,
                                    finished_at=now, notes={"test": True})  # fmt: skip
            session.add(cycle)
            session.flush()
            decision = m.JudgeDecision(cycle_id=cycle.id, as_of=now, market_summary="Manual test order.",
                                       model="manual")  # fmt: skip
            idea = m.TradeIdea(
                rank=1, ticker=ticker, action=m.TradeAction.BUY, judge_confidence=1.0, model_probability=None,
                agent_agreement=None, final_confidence=1.0, entry_low=round(price * 0.99, 2),
                entry_high=round(price * 1.01, 2), target_price=round(price * 1.02, 2),
                stop_price=round(price * 0.98, 2), size_pct=self.config.risk.max_position_pct, horizon_days=1,
                rationale="Test order placed by hand to check the order flow.",
            )  # fmt: skip
            decision.ideas = [idea]
            session.add(decision)
            session.commit()
            return int(decision.id), int(idea.id)


_FINAL = frozenset(
    {"filled", "canceled", "expired", "rejected", "replaced", "done_for_day", "stopped"}
)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(0), tzinfo=NEW_YORK)


__all__ = [
    "MonitorReport",
    "Placement",
    "SyncReport",
    "TradingEngine",
    "TradingRefused",
    "agents_verdict",
]


def agents_verdict(session: Session, trade: m.Trade) -> tuple[list[str], list[str]]:
    """Which agents were right: supporters if the trade beat SPY, opposers if not."""
    if trade.trade_idea_id is None:
        return [], []
    outcome = trade.excess_return_pct if trade.excess_return_pct is not None else trade.pnl_pct
    rows = session.execute(
        select(m.AgentReport.agent_name, m.AgentReport.sector, m.TradeIdeaReport.stance)
        .join(m.TradeIdeaReport, m.TradeIdeaReport.agent_report_id == m.AgentReport.id)
        .where(m.TradeIdeaReport.trade_idea_id == trade.trade_idea_id)
    ).all()
    won = (outcome or 0.0) > 0
    right: list[str] = []
    wrong: list[str] = []
    for name, sector, stance in rows:
        if stance == m.Stance.NEUTRAL:
            continue
        label = f"{name} ({sector})" if sector else name
        correct = won if stance == m.Stance.SUPPORT else not won
        (right if correct else wrong).append(label)
    return sorted(set(right)), sorted(set(wrong))
