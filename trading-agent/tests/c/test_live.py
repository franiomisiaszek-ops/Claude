"""System C day to day: the weekly plan, Monday's orders, stops, the drawdown limit, the sleeve."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select

from papertrader.c import live
from papertrader.c.data import store
from papertrader.c.models import Order, Plan, SleevePlan
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.scheduler.jobs import specs_for
from papertrader.trading.broker import BrokerOrder

from ..ingest.helpers import shipped_config
from ..research.helpers import FakeClaude, make_claude
from ..trading.helpers import FakeBroker
from .helpers import as_rows, market_until, portfolio

FRIDAY = date(2026, 11, 20)
FRIDAY_EVENING = datetime(2026, 11, 20, 21, 45, tzinfo=UTC)  # 16:45 New York
MONDAY_OPEN = datetime(2026, 11, 23, 14, 40, tzinfo=UTC)  # 09:40 New York


@pytest.fixture
def c_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    c = make_system_engine(database_url, "C")
    provision(c, "C")
    yield c
    c.dispose()


class Broker(FakeBroker):
    """The fake paper account, with system C's limit and stop orders."""

    def submit_limit(
        self, *, symbol: str, qty: float, side: str, limit_price: float, client_order_id: str
    ) -> BrokerOrder:
        return replace(self._new(client_order_id=client_order_id, symbol=symbol, side=side, order_type="limit",
                                 order_class="simple", time_in_force="day", qty=qty, status="new",
                                 limit_price=limit_price))  # fmt: skip

    def submit_stop(
        self, *, symbol: str, qty: float, stop_price: float, client_order_id: str
    ) -> BrokerOrder:
        return replace(self._new(client_order_id=client_order_id, symbol=symbol, side="sell", order_type="stop",
                                 order_class="simple", time_in_force="gtc", qty=qty, status="new",
                                 stop_price=stop_price))  # fmt: skip

    def fill_all(self, kind: str = "limit") -> None:
        for order in list(self.orders.values()):
            if order.order_type == kind and order.status == "new":
                self._fill(order, order.limit_price or self.prices[order.symbol])


def c_config(**portfolio_changes: Any) -> Any:
    cfg = shipped_config()
    return cfg.model_copy(update={"portfolio": portfolio(**portfolio_changes)})


def seeded(c_engine: Engine) -> tuple[Broker, dict[str, Any]]:
    data = market_until(FRIDAY)
    with make_session_factory(c_engine)() as session:
        store(session, "alpaca", as_rows(data))
        session.commit()
    clock = [FRIDAY_EVENING]
    broker = Broker(clock, equity=25_000.0)
    broker.prices = {t: float(data["close"][t].iloc[-1]) for t in data["close"].columns}
    return broker, {"clock": clock, "data": data}


def test_the_weekly_plan_with_its_avanza_list(c_engine: Engine) -> None:
    broker, _ = seeded(c_engine)
    config = c_config()
    sessions = make_session_factory(c_engine)
    out = asyncio.run(live.plan_week(sessions, config, broker, None, FRIDAY_EVENING))
    assert out["state"] == "normal" and out["orders"] > 0
    with sessions() as session:
        plan = session.scalars(select(Plan)).one()
        text = session.scalars(select(m.Notification)).one().text
    assert plan.trade_day == date(2026, 11, 23) and plan.status == "planned"
    assert plan.targets == plan.shadow_targets  # no review: the same
    assert "EEM" not in plan.targets  # falling all year
    assert text.startswith("WEEKLY PLAN · orders Mon 23 Nov at 09:35 New York")
    assert "Copy on Avanza (for $1,000.00, about 10,500 SEK" in text
    assert (
        "no Avanza equivalent: keep this part in cash" in text
    )  # the test funds have no Avanza names
    with pytest.raises(live.NotReady, match="already made"):
        asyncio.run(live.plan_week(sessions, config, broker, None, FRIDAY_EVENING))


def test_the_review_may_only_reduce_buys(c_engine: Engine) -> None:
    broker, _ = seeded(c_engine)
    config = c_config(veto={"enabled": True})
    sessions = make_session_factory(c_engine)
    fake = FakeClaude()

    def answer(call: Any) -> dict[str, Any]:
        tickers = [
            line.split(":")[0].removeprefix("- ")
            for line in call.user.splitlines()
            if line.startswith("- ")
        ]
        return {"verdicts": [{"ticker": tickers[0], "verdict": "skip", "reason": "the Fed decides on Wednesday"},
                             {"ticker": "EEM", "verdict": "keep", "reason": "not planned: must be ignored"}]}  # fmt: skip

    fake.responses["c_review"] = answer
    claude = make_claude(c_engine, fake, config=config, now=lambda: FRIDAY_EVENING)
    asyncio.run(live.plan_week(sessions, config, broker, claude, FRIDAY_EVENING))
    with sessions() as session:
        plan = session.scalars(select(Plan)).one()
        text = session.scalars(select(m.Notification)).one().text
    skipped = next(iter(plan.review))
    assert plan.review[skipped]["verdict"] == "skip" and "EEM" not in plan.review
    assert (
        skipped in plan.shadow_targets and skipped not in plan.targets
    )  # nothing held: skipped means none
    assert all(
        plan.targets[t] <= plan.shadow_targets[t] for t in plan.targets
    )  # never more than the rules
    assert f"Review: skip the {skipped} buy: the Fed decides on Wednesday" in text


def test_mondays_orders_stops_and_the_drawdown_limit(c_engine: Engine) -> None:
    broker, env = seeded(c_engine)
    config = c_config()
    sessions = make_session_factory(c_engine)
    asyncio.run(live.plan_week(sessions, config, broker, None, FRIDAY_EVENING))
    with pytest.raises(live.NotReady, match="no plan is waiting"):
        live.rebalance(sessions, config, broker, FRIDAY_EVENING)  # not before Monday
    env["clock"][0] = MONDAY_OPEN
    out = live.rebalance(sessions, config, broker, MONDAY_OPEN)
    assert out["orders"] > 0
    with sessions() as session:
        orders = session.scalars(select(Order).where(Order.kind == "rebalance")).all()
        plan = session.scalars(select(Plan)).one()
    assert all(o.side == "buy" for o in orders) and plan.status == "trading" and plan.attempts == 1
    assert sum(o.qty * (o.limit_price or 0) for o in orders) <= 25_000  # never more than the cash
    broker.fill_all()
    risk = live.watch_risk(sessions, config, broker, MONDAY_OPEN)
    assert risk["stops"] and all(
        "stop at" in s for s in risk["stops"]
    )  # a resting stop under each holding
    with sessions() as session:
        assert session.scalars(select(Plan)).one().status == "done"
        stops = session.scalars(select(Order).where(Order.kind == "stop")).all()
    held = {p.symbol: p.qty for p in broker.positions()}
    assert {s.ticker: s.qty for s in stops} == held
    assert all(abs(s.stop_price - broker.prices[s.ticker] * 0.8) < 0.02 for s in stops)
    assert (
        live.watch_risk(sessions, config, broker, MONDAY_OPEN)["stops"] == []
    )  # nothing to change

    broker.equity = 20_000.0  # 20% below the peak: past the 15% limit
    flat = live.watch_risk(sessions, config, broker, MONDAY_OPEN)
    assert set(flat["flattened"]) == set(held)
    with sessions() as session:
        texts = [n.text for n in session.scalars(select(m.Notification))]
        open_stops = session.scalars(
            select(Order).where(Order.kind == "stop", Order.status == "new")
        ).all()
    assert any("SYSTEM C ALL OUT" in t for t in texts) and not open_stops


def test_the_daily_chart_trades_in_shadow(c_engine: Engine) -> None:
    seeded(c_engine)
    config = c_config(sleeve={"enabled": True, "dip_rsi": 50})
    out = live.sleeve_day(make_session_factory(c_engine), config, FRIDAY_EVENING)
    with make_session_factory(c_engine)() as session:
        plans = session.scalars(select(SleevePlan)).all()
        texts = [n.text for n in session.scalars(select(m.Notification))]
    assert [p.ticker for p in plans] == out["signals"]
    assert all(
        t.startswith("SHADOW (no order, not for copying) · C daily chart trade") for t in texts
    )
    with make_session_factory(c_engine)() as session:
        assert live.sleeve_record(session) == {"trades": 0}


def test_cs_jobs() -> None:
    names = {s.name for s in specs_for("C", c_config())}
    assert names == {
        "c_warn",  # X6: the heads-up before the weekly orders
        "c_rebalance",
        "monitor",
        "reconcile_eod",
        "c_data",
        "c_plan",
    }  # watch and sleeve are off
    assert {"c_watch", "c_sleeve"} <= {
        s.name
        for s in specs_for("C", c_config(veto={"watch_enabled": True}, sleeve={"enabled": True}))
    }
    plan = next(s for s in specs_for("C", c_config()) if s.name == "c_plan")
    assert plan.weekday is not None and plan.weekday(c_config()) == "fri"
    others = {s.name for s in specs_for("A")} | {s.name for s in specs_for("B")}
    assert not {"c_plan", "c_data", "c_rebalance"} & others


def test_the_weekly_orders_wait_for_their_heads_up(c_engine: Engine) -> None:
    """X6: a heads-up at 09:32 with a Skip per fund; the orders go in at 09:35."""
    from papertrader.config import WarningsConfig
    from papertrader.notify.warnings import SENT, SKIPPED, Gate, OrderWarning, tap

    broker, env = seeded(c_engine)
    config = c_config()
    sessions = make_session_factory(c_engine)
    asyncio.run(live.plan_week(sessions, config, broker, None, FRIDAY_EVENING))
    clock = env["clock"]
    clock[0] = datetime(2026, 11, 23, 14, 32, tzinfo=UTC)  # Monday 09:32 New York
    gate = Gate(sessions, WarningsConfig(), now=lambda: clock[0])
    assert live.warn_week(sessions, config, broker, gate, clock[0])["orders"] > 0
    with sessions() as session:
        rows = session.scalars(select(OrderWarning).order_by(OrderWarning.id)).all()
        text = (
            session.scalars(select(m.Notification).where(m.Notification.kind == "order_warning"))
            .one()
            .text
        )
        skipped = rows[0].ticker
        tap(session, f"ow:{rows[0].id}:skip", clock[0])
        session.commit()
    assert "HEADS-UP" in text and "weekly rebalance" in text and "limit $" in text
    clock[0] = datetime(2026, 11, 23, 14, 33, tzinfo=UTC)
    waiting = live.rebalance(sessions, config, broker, clock[0], gate)
    assert waiting["orders"] == 0 and waiting["waiting"] == len(rows) - 1
    with sessions() as session:
        assert session.scalars(select(Plan)).one().attempts == 0  # waiting is not an attempt
    clock[0] = datetime(2026, 11, 23, 14, 36, tzinfo=UTC)
    out = live.rebalance(sessions, config, broker, clock[0], gate)
    with sessions() as session:
        ordered = {
            o.ticker for o in session.scalars(select(Order).where(Order.kind == "rebalance"))
        }
        plan = session.scalars(select(Plan)).one()
        statuses = {r.ticker: r.status for r in session.scalars(select(OrderWarning))}
    assert (
        out["orders"] == len(rows) - 1
        and skipped not in ordered
        and f"Skipped by you: buy {skipped}" in plan.note
    )
    assert statuses[skipped] == SKIPPED and all(
        s == SENT for t, s in statuses.items() if t != skipped
    )
