"""System C day to day.

* After Friday's close (`plan_week`): signals from the rules, the drawdown state, the
  weekly review (which may only reduce buys), and the plan on Telegram, with the
  matching Avanza products and amounts for your own capital.
* Monday 09:35 (`rebalance`): limit orders for the funds that drifted more than 20%,
  sells first; anything unfilled is tried once more the next day.
* Every few minutes (`watch_risk`): order status, the wide emergency stops under every
  holding, and the drawdown control (15% below the peak: all out, with an alert). The
  -3% daily kill switch is the engine's, as for A and B.
* After each close, if switched on (`sleeve_day`): the daily chart trades, in shadow mode
  until they have proven themselves.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.c import sleeve as sleeve_rules
from papertrader.c.data import frames, live_tickers, tbill_12m
from papertrader.c.models import Order, Plan, SleevePlan
from papertrader.c.review import review
from papertrader.c.strategy import Target, drawdown_state, rebalance_orders, target_weights
from papertrader.config import AppConfig, PortfolioConfig
from papertrader.db.state import get_state, set_state
from papertrader.ingest.timeutil import new_york_date
from papertrader.llm.client import ClaudeClient
from papertrader.notify.messages import alert, money
from papertrader.notify.outbox import queue
from papertrader.trading import control
from papertrader.trading.broker import BrokerError, PortfolioBroker

STATE_KEY = "c.drawdown"
FINAL = {"filled", "canceled", "cancelled", "expired", "rejected", "done_for_day"}


class NotReady(RuntimeError):
    """Nothing to do now, for a reason worth showing (no plan, prices missing...)."""


def holdings(broker: PortfolioBroker) -> tuple[dict[str, float], dict[str, float]]:
    qty, price = {}, {}
    for p in broker.positions():
        if p.qty:
            qty[p.symbol], price[p.symbol] = float(p.qty), float(p.current_price)
    return qty, price


def _state(session: Session, equity: float) -> dict[str, Any]:
    return get_state(session, STATE_KEY) or {"state": "normal", "peak": equity, "flat_since": None}


def next_trading_day(broker: PortfolioBroker, day: date) -> date:
    days = broker.calendar(day + timedelta(days=1), day + timedelta(days=10))
    return days[0].day if days else day + timedelta(days=3 if day.weekday() == 4 else 1)


# --------------------------------------------------------------------------- the weekly plan


async def plan_week(sessions: sessionmaker[Session], config: AppConfig, broker: PortfolioBroker,
                    claude: ClaudeClient | None, now: datetime) -> dict[str, Any]:  # fmt: skip
    cfg = config.portfolio
    day = new_york_date(now)
    with sessions() as session:
        if session.scalar(select(Plan.id).where(Plan.signal_day == day)):
            raise NotReady("this week's plan is already made")
        close = frames(session, "alpaca", live_tickers(cfg))["close"]
        if close.empty or close.index[-1] != day:
            raise NotReady(
                "today's closing prices are not loaded yet (the c_data job runs at the close)"
            )
        equity = broker.account().equity
        saved = _state(session, equity)
        flat_since = date.fromisoformat(saved["flat_since"]) if saved.get("flat_since") else None
        state, peak, flat_since, drawdown = drawdown_state(saved["state"], float(saved["peak"]), equity,
                                                           flat_since, day, cfg)  # fmt: skip
        set_state(session, STATE_KEY, {"state": state, "peak": peak,
                                       "flat_since": flat_since.isoformat() if flat_since else None})  # fmt: skip
        target = target_weights(close, cfg, tbill_12m=tbill_12m(close, cfg), state=state)
        qty, prices = holdings(broker)
        current = {t: q * prices[t] / equity for t, q in qty.items() if equity > 0}
        targets, verdicts, review_note = dict(target.weights), {}, ""
        if cfg.veto.enabled and claude is not None:
            reviewed = await review(session, claude, cfg.veto, now, targets=targets, current=current,
                                    reasons=target.reasons)  # fmt: skip
            targets, verdicts, review_note = reviewed.targets, reviewed.verdicts, reviewed.note
        plan = Plan(signal_day=day, trade_day=next_trading_day(broker, day), status="planned", state=state,
                    equity=equity, drawdown=round(drawdown, 4), targets=targets, shadow_targets=dict(target.weights),
                    reasons=target.reasons, review=verdicts, note=review_note)  # fmt: skip
        session.add(plan)
        session.flush()
        last = {
            t: float(close[t].dropna().iloc[-1]) for t in close.columns if close[t].notna().any()
        }
        preview = rebalance_orders(targets, qty, last, equity, cfg)
        effect = review_effect(session, cfg)
        queue(
            session,
            "c_plan",
            plan_text(plan, target, preview, current, cfg, effect),
            dedup_key=f"c-plan:{day}",
        )
        session.commit()
        return {"plan_id": plan.id, "state": state, "targets": targets, "orders": len(preview),
                "reviewed": verdicts, "summary": f"{len(targets)} funds, {len(preview)} order(s) on {plan.trade_day:%a %d %b}"}  # fmt: skip


def plan_text(plan: Plan, target: Target, preview: list[Any], current: Mapping[str, float], cfg: PortfolioConfig,
              effect: dict[str, Any] | None = None) -> str:  # fmt: skip
    funds = cfg.by_ticker()
    lines = [f"WEEKLY PLAN · orders {plan.trade_day:%a %d %b} at {cfg.rebalance_time:%H:%M} New York "
             f"(signals from {plan.signal_day:%a %d %b}'s close)"]  # fmt: skip
    words = {
        "normal": "normal",
        "halved": "HALF RISK (10%+ below the peak)",
        "flat": "ALL OUT (15%+ below the peak)",
    }
    lines.append(f"State: {words[plan.state]}, {plan.drawdown or 0:+.1%} from the peak"
                 + (f"; expected swings {target.vol:.0%} a year" if target.vol else ""))  # fmt: skip
    lines.append("Target portfolio:")
    for t, w in sorted(plan.targets.items(), key=lambda kv: -kv[1]):
        note = target.reasons.get(t, "the T-bill fund (cash)").removeprefix("in: ")
        change = f" (now {current.get(t, 0):.0%})" if abs(current.get(t, 0) - w) > 0.005 else ""
        lines.append(f"  {t} {w:.0%}{change}: {note}")
    cash = max(0.0, 1 - sum(plan.targets.values()))
    if cash > 0.005:
        lines.append(f"  cash {cash:.0%}")
    sold = [t for t in current if t not in plan.targets and current[t] > 0.001]
    for t in sold:
        lines.append(
            f"  selling {t}: {target.reasons.get(t, 'no longer chosen').removeprefix('out: ')}"
        )
    if preview:
        lines.append("Orders (only funds 20%+ off target): " + ", ".join(
            f"{p.side} {p.qty:g} {p.ticker} at {money(p.limit)}" for p in preview))  # fmt: skip
    else:
        lines.append("No orders: nothing drifted enough from its target.")
    for t, v in plan.review.items():
        lines.append(f"Review: {v['verdict']} the {t} buy: {v['reason']}")
    if plan.note:
        lines.append(f"Review: {plan.note}")
    if effect and effect.get("weeks"):
        lines.append(f"Review so far: {effect['with']:+.2%} with it vs {effect['without']:+.2%} without, "
                     f"over {effect['weeks']} weeks")  # fmt: skip
    lines.append(f"Copy on Avanza (for {money(cfg.mirror_capital_usd)}, about "
                 f"{cfg.mirror_capital_usd * cfg.sek_per_usd:,.0f} SEK; buy the whole number of shares closest to each amount):")  # fmt: skip
    for t, w in sorted(plan.targets.items(), key=lambda kv: -kv[1]):
        usd = w * cfg.mirror_capital_usd
        product = funds[t].avanza if t in funds else ""
        what = product or "no Avanza equivalent: keep this part in cash"
        lines.append(f"  {what}: {money(usd)} (about {usd * cfg.sek_per_usd:,.0f} SEK)")
    return "\n".join(lines)


def review_effect(session: Session, cfg: PortfolioConfig) -> dict[str, Any]:
    """C as planned vs a shadow C that ignored the review: each plan held from its trade
    day's open to the next plan's (on the planned weights, before costs)."""
    plans = session.scalars(
        select(Plan).where(Plan.status != "cancelled").order_by(Plan.signal_day)
    ).all()
    if len(plans) < 2:
        return {"weeks": 0}
    opens = frames(session, "alpaca", live_tickers(cfg))["open"]
    grow = {"with": 1.0, "without": 1.0}
    weeks = 0
    for this, nxt in pairwise(plans):
        if this.trade_day not in opens.index or nxt.trade_day not in opens.index:
            continue
        weeks += 1
        for key, weights in (("with", this.targets), ("without", this.shadow_targets)):
            r = 0.0
            for t, w in weights.items():
                a, b = (
                    opens.at[this.trade_day, t] if t in opens else None,
                    opens.at[nxt.trade_day, t] if t in opens else None,
                )
                if a and b and not (math.isnan(a) or math.isnan(b)):
                    r += w * (b / a - 1)
            grow[key] *= 1 + r
    return {"weeks": weeks, "with": grow["with"] - 1, "without": grow["without"] - 1}


# --------------------------------------------------------------------------- orders


def rebalance(
    sessions: sessionmaker[Session], config: AppConfig, broker: PortfolioBroker, now: datetime
) -> dict[str, Any]:
    cfg = config.portfolio
    day = new_york_date(now)
    with sessions() as session:
        plan = session.scalars(select(Plan).where(Plan.status == "planned", Plan.trade_day <= day)
                               .order_by(Plan.signal_day.desc()).limit(1)).first()  # fmt: skip
        if plan is None:
            raise NotReady("no plan is waiting for its orders")
        if plan.attempts >= 2 or (day - plan.trade_day).days > 4:
            plan.status, plan.note = (
                "cancelled",
                (plan.note + " Not completed within two trading days.").strip(),
            )
            session.commit()
            raise NotReady(
                "the plan's orders were not completed within two days; next week's plan takes over"
            )
        if (paused := control.paused(sessions)) is not None:
            raise NotReady(f"trading is paused ({paused.get('reason', '')})")
        if not broker.clock().is_open:
            raise NotReady("the market is closed")
        account = broker.account()
        qty, _ = holdings(broker)
        prices = broker.latest_prices(sorted(set(plan.targets) | set(qty)))
        orders = rebalance_orders(plan.targets, qty, prices, account.equity, cfg)
        plan.attempts += 1
        if not orders:
            plan.status = "done"
            session.commit()
            return {"orders": 0, "summary": "nothing drifted enough to trade"}
        trading = {o.ticker for o in orders}
        for stop in session.scalars(select(Order).where(Order.kind == "stop", Order.ticker.in_(trading),
                                                        Order.status.not_in(sorted(FINAL)))):  # fmt: skip
            _cancel(broker, stop)
        available = account.cash + sum(o.qty * o.limit for o in orders if o.side == "sell")
        placed = []
        for o in orders:
            amount = o.qty
            if o.side == "buy":
                affordable = available / o.limit
                amount = min(
                    amount,
                    round(affordable, 3) if cfg.upgrades.fractional else math.floor(affordable),
                )
                if amount <= 0:
                    continue
                available -= amount * o.limit
            client_id = f"c-p{plan.id}-{plan.attempts}-{o.ticker}"
            try:
                sent = broker.submit_limit(symbol=o.ticker, qty=amount, side=o.side, limit_price=o.limit,
                                           client_order_id=client_id)  # fmt: skip
            except BrokerError as exc:
                queue(session, "alert", alert(f"C could not {o.side} {o.ticker}: {exc}"))
                continue
            session.add(Order(plan_id=plan.id, kind="rebalance", ticker=o.ticker, side=o.side, qty=amount,
                              limit_price=o.limit, client_order_id=client_id, alpaca_order_id=sent.id, status=sent.status,
                              created_at=now, updated_at=now))  # fmt: skip
            placed.append(f"{o.side} {amount:g} {o.ticker} at {money(o.limit)}")
        plan.status = "trading"
        if placed:
            queue(session, "c_orders", "ORDERS PLACED (weekly rebalance): " + ", ".join(placed),
                  dedup_key=f"c-orders:{plan.id}:{plan.attempts}")  # fmt: skip
        session.commit()
        return {"orders": len(placed), "summary": f"{len(placed)} order(s) placed"}


def _cancel(broker: PortfolioBroker, order: Order) -> None:
    if order.alpaca_order_id:
        with contextlib.suppress(BrokerError):
            broker.cancel(order.alpaca_order_id)
    order.status = "canceled"


def sync(session: Session, broker: PortfolioBroker, now: datetime) -> list[str]:
    changes = []
    for order in session.scalars(select(Order).where(Order.status.not_in(sorted(FINAL)))):
        if not order.alpaca_order_id:
            continue
        try:
            fresh = broker.order(order.alpaca_order_id)
        except BrokerError:
            continue
        if fresh.status != order.status or (fresh.filled_qty or 0) != order.filled_qty:
            order.status, order.filled_qty = fresh.status, float(fresh.filled_qty or 0)
            order.filled_avg_price, order.updated_at = fresh.filled_avg_price, now
            changes.append(f"{order.ticker} {order.side} {order.status}")
    for plan in session.scalars(select(Plan).where(Plan.status == "trading")):
        mine = session.scalars(
            select(Order).where(Order.plan_id == plan.id, Order.kind == "rebalance")
        ).all()
        if mine and all(o.status in FINAL for o in mine):
            unfilled = any(o.filled_qty < o.qty for o in mine)
            plan.status = "planned" if unfilled and plan.attempts < 2 else "done"
    return changes


def ensure_stops(
    session: Session, broker: PortfolioBroker, cfg: PortfolioConfig, now: datetime
) -> list[str]:
    """A resting stop `emergency_stop_pct` below every holding (whole shares)."""
    done = []
    qty, price = holdings(broker)
    busy = {o.ticker for o in session.scalars(select(Order).where(Order.kind.in_(("rebalance", "flatten")),
                                                                  Order.status.not_in(sorted(FINAL))))}  # fmt: skip
    stops = {o.ticker: o for o in session.scalars(select(Order).where(Order.kind == "stop",
                                                                      Order.status.not_in(sorted(FINAL))))}  # fmt: skip
    for ticker, resting in list(stops.items()):
        if ticker not in qty:
            _cancel(broker, resting)
            done.append(f"{ticker}: stop removed")
    for ticker, held in qty.items():
        whole = math.floor(held)
        if ticker in busy or whole < 1:
            continue
        stop = stops.get(ticker)
        if stop is not None and stop.qty == whole:
            continue
        if stop is not None:
            _cancel(broker, stop)
        level = round(price[ticker] * (1 - cfg.emergency_stop_pct), 2)
        client_id = f"c-stop-{ticker}-{now:%Y%m%d%H%M%S}"
        try:
            sent = broker.submit_stop(
                symbol=ticker, qty=whole, stop_price=level, client_order_id=client_id
            )
        except BrokerError:
            continue
        session.add(Order(kind="stop", ticker=ticker, side="sell", qty=whole, stop_price=level,
                          client_order_id=client_id, alpaca_order_id=sent.id, status=sent.status,
                          created_at=now, updated_at=now))  # fmt: skip
        done.append(f"{ticker}: stop at {money(level)}")
    return done


def watch_risk(
    sessions: sessionmaker[Session], config: AppConfig, broker: PortfolioBroker, now: datetime
) -> dict[str, Any]:
    """Orders, emergency stops and the drawdown control. Runs with the monitor."""
    cfg = config.portfolio
    with sessions() as session:
        changes = sync(session, broker, now)
        equity = broker.account().equity
        saved = _state(session, equity)
        out: dict[str, Any] = {"orders": changes}
        if saved["state"] != "flat":
            peak = max(float(saved["peak"]), equity)
            saved["peak"] = peak
            if peak > 0 and equity / peak - 1 <= -cfg.drawdown_flat:
                out["flattened"] = flatten(session, broker, now, equity / peak - 1)
                saved |= {"state": "flat", "flat_since": new_york_date(now).isoformat()}
            set_state(session, STATE_KEY, saved)
        if control.paused(sessions) is not None:  # e.g. the kill switch: no new rebalancing orders
            for o in session.scalars(
                select(Order).where(Order.kind == "rebalance", Order.status.not_in(sorted(FINAL)))
            ):
                _cancel(broker, o)
        if saved["state"] != "flat":
            out["stops"] = ensure_stops(session, broker, cfg, now)
        session.commit()
        return out


def flatten(session: Session, broker: PortfolioBroker, now: datetime, drawdown: float) -> list[str]:
    for o in session.scalars(select(Order).where(Order.status.not_in(sorted(FINAL)))):
        _cancel(broker, o)
    sold = []
    qty, _ = holdings(broker)
    for ticker, held in qty.items():
        client_id = f"c-flat-{ticker}-{now:%Y%m%d%H%M}"
        try:
            whole = (
                int(held) if float(held).is_integer() else held
            )  # fractional shares only with upgrade 5
            sent = broker.submit_market(
                symbol=ticker, qty=whole, side="sell", client_order_id=client_id
            )
        except BrokerError:
            continue
        session.add(Order(kind="flatten", ticker=ticker, side="sell", qty=held, client_order_id=client_id,
                          alpaca_order_id=sent.id, status=sent.status, created_at=now, updated_at=now))  # fmt: skip
        sold.append(ticker)
    queue(session, "alert", alert(f"SYSTEM C ALL OUT: {drawdown:.1%} below its peak, past the 15% limit. Sold "
                                  f"{', '.join(sold) or 'nothing'}; back in after a few weeks, by the rules."),
          dedup_key=f"c-flat:{new_york_date(now)}")  # fmt: skip
    return sold


# --------------------------------------------------------------------------- the daily chart trades


def sleeve_day(sessions: sessionmaker[Session], config: AppConfig, now: datetime) -> dict[str, Any]:
    cfg = config.portfolio
    day = new_york_date(now)
    with sessions() as session:
        px = frames(session, "alpaca", live_tickers(cfg))
        if px["close"].empty or px["close"].index[-1] != day:
            raise NotReady("today's closing prices are not loaded yet")
        new = []
        for s in sleeve_rules.signals(px["close"], px["high"], px["low"], cfg):
            if session.scalar(
                select(SleevePlan.id).where(
                    SleevePlan.signal_day == day, SleevePlan.ticker == s.ticker
                )
            ):
                continue
            session.add(SleevePlan(signal_day=day, ticker=s.ticker, rule=s.rule, mode=cfg.sleeve.mode,
                                   reference=s.reference, stop_price=s.stop, target_price=s.target,
                                   hold_days=cfg.sleeve.hold_days))  # fmt: skip
            new.append(s)
        scored = score_sleeve(session, px, cfg)
        for s in new:
            words = {
                "breakout": "broke above its recent high in an uptrend",
                "dip": "a sharp dip inside an uptrend",
            }
            queue(session, "c_sleeve", f"SHADOW (no order, not for copying) · C daily chart trade · WOULD BUY {s.ticker} "
                  f"at tomorrow's open (about {money(s.reference)}): {words[s.rule]}. Stop {money(s.stop)}, target "
                  f"{money(s.target)}, at most {cfg.sleeve.hold_days} days.", dedup_key=f"c-sleeve:{day}:{s.ticker}")  # fmt: skip
        session.commit()
        return {
            "signals": [s.ticker for s in new],
            "scored": scored,
            "summary": f"{len(new)} signal(s), {scored} scored",
        }


def score_sleeve(session: Session, px: Mapping[str, Any], cfg: PortfolioConfig) -> int:
    import pandas as pd

    scored = 0
    for plan in session.scalars(select(SleevePlan).where(SleevePlan.status == "open")):
        t = plan.ticker
        if t not in px["close"]:
            continue
        bars = pd.DataFrame(
            {
                "open": px["open"][t],
                "high": px["high"][t],
                "low": px["low"][t],
                "close": px["close"][t],
            }
        )
        signal = sleeve_rules.Signal(
            plan.signal_day, t, plan.rule, plan.reference, plan.stop_price, plan.target_price
        )
        outcome = sleeve_rules.play(signal, bars, plan.hold_days, cfg.cost_bps)
        if outcome is None:
            continue
        plan.status, plan.entry_price, plan.exit_price = "done", outcome.entry, outcome.exit
        plan.exit_day, plan.exit_reason, plan.net_return_pct = (
            outcome.exit_day,
            outcome.reason,
            outcome.net,
        )
        scored += 1
    return scored


def sleeve_record(session: Session) -> dict[str, Any]:
    done = session.scalars(select(SleevePlan).where(SleevePlan.status == "done")).all()
    if not done:
        return {"trades": 0}
    nets = [p.net_return_pct or 0.0 for p in done]
    return {
        "trades": len(done),
        "wins": sum(1 for n in nets if n > 0),
        "average": sum(nets) / len(nets),
    }
