"""The "Match on Avanza" page: only the trades you do on Avanza to copy system C.

C's orders are mirrored in proportion: an order worth 4% of C's paper account becomes a
trade worth 4% of your Avanza money. Before Monday's orders this uses C's plan and its
holdings now (the same rule C uses: only funds 20%+ away from their target are traded);
once C has traded, it uses C's actual orders and fills. Avanza's funds are the European
(UCITS) versions of C's US funds, so they track closely but not exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from papertrader.ingest.timeutil import NEW_YORK, at_new_york

MIN_SEK = 1_000
MAX_SEK = 10_000_000


@dataclass
class MatchTrade:
    fund: str
    product: str
    action: str  # BUY | SELL
    sek: float
    all_of_it: bool  # sell everything you hold of it
    target_weight: float
    status: str  # what C did or will do
    done_key: str  # for the "I did this" tick on this device
    note: str = ""


@dataclass
class Holding:
    fund: str
    product: str
    weight: float
    sek: float


@dataclass
class AvanzaView:
    capital_sek: float
    default_sek: float
    sek_per_usd: float
    has_plan: bool
    signal_day: str = ""
    when_local: str = ""
    when_ny: str = ""
    status: str = ""  # waiting | done | none
    state: str = "normal"
    trades: list[MatchTrade] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    cash_weight: float = 0.0
    stop_pct: float = 0.20
    notes: list[str] = field(default_factory=list)


def _product(funds: dict[str, Any], ticker: str) -> str:
    fund = funds.get(ticker)
    return (
        fund.avanza if fund is not None else ""
    ) or f"{ticker}: no Avanza version, keep this part in cash"


def capital_from(value: str | None, default: float) -> float:
    try:
        sek = float(str(value).replace(" ", "").replace(",", ""))
    except (TypeError, ValueError):
        return default
    return sek if MIN_SEK <= sek <= MAX_SEK else default


def match(
    view: Any, capital_sek: float | None, now: datetime, local_tz: str = "Europe/Warsaw"
) -> AvanzaView:
    """`view`: system C's read-only SystemView."""
    from papertrader.c.live import FINAL
    from papertrader.c.models import Order, Plan, Price
    from papertrader.c.strategy import rebalance_orders
    from papertrader.web import live as live_mod

    cfg = view.config.portfolio
    default = round(cfg.mirror_capital_usd * cfg.sek_per_usd)
    capital = capital_sek or default
    out = AvanzaView(capital, default, cfg.sek_per_usd, False, stop_pct=cfg.emergency_stop_pct)
    funds = cfg.by_ticker()
    with view.sessions() as session:
        plan = session.scalars(select(Plan).where(Plan.status != "cancelled")
                               .order_by(Plan.signal_day.desc()).limit(1)).first()  # fmt: skip
        if plan is None:
            out.notes.append(
                "C makes its first weekly plan on Friday after the close; its trades show here then."
            )
            return out
        orders = session.scalars(
            select(Order).where(Order.plan_id == plan.id, Order.kind == "rebalance")
        ).all()
        flattened = session.scalars(select(Order).where(Order.kind == "flatten",
                                                        Order.created_at >= plan.created_at)).all()  # fmt: skip
        closes: dict[str, float] = {}
        for ticker in set(plan.targets) | {o.ticker for o in orders}:
            row = session.scalars(select(Price).where(Price.source == "alpaca", Price.ticker == ticker)
                                  .order_by(Price.day.desc()).limit(1)).first()  # fmt: skip
            if row is not None:
                closes[ticker] = row.close
        session.expunge_all()
    out.has_plan = True
    trade_at = at_new_york(plan.trade_day, cfg.rebalance_time)
    out.signal_day = f"{plan.signal_day:%a %d %b}"
    out.when_local = trade_at.astimezone(ZoneInfo(local_tz)).strftime("%a %d %b %H:%M")
    out.when_ny = trade_at.astimezone(NEW_YORK).strftime("%H:%M")
    out.state = plan.state
    equity = plan.equity or 0.0

    if orders:  # C has traded: copy exactly what it did
        out.status = "done" if all(o.status in FINAL for o in orders) else "waiting"
        for o in sorted(orders, key=lambda o: o.side != "sell"):
            price = o.filled_avg_price or o.limit_price or closes.get(o.ticker) or 0.0
            qty = o.filled_qty if o.filled_qty else o.qty
            sek = qty * price / equity * capital if equity else 0.0
            if o.filled_qty:
                status = f"C {'bought' if o.side == 'buy' else 'sold'} {o.updated_at.astimezone(ZoneInfo(local_tz)):%a %H:%M}"
            elif o.status in FINAL:
                status = f"C's order {o.status}: nothing to do"
                sek = 0.0
            else:
                status = "C's order is in; copy it now"
            target = plan.targets.get(o.ticker, 0.0)
            out.trades.append(MatchTrade(o.ticker, _product(funds, o.ticker), o.side.upper(), round(sek, -1),
                                         o.side == "sell" and target == 0, target, status,
                                         f"{plan.id}:{o.ticker}:{o.side}"))  # fmt: skip
    else:  # before Monday: what C will do, from its holdings now
        out.status = "waiting"
        snap = live_mod.read(view.sessions)
        holdings = {t: p.qty for t, p in snap.positions.items()}
        prices = {**closes, **{t: p.price for t, p in snap.positions.items()}}
        equity = snap.equity or equity
        for o in rebalance_orders(plan.targets, holdings, prices, equity, cfg):
            sek = o.qty * prices.get(o.ticker, o.limit) / equity * capital if equity else 0.0
            out.trades.append(MatchTrade(o.ticker, _product(funds, o.ticker), o.side.upper(), round(sek, -1),
                                         o.side == "sell" and o.weight_target == 0, o.weight_target,
                                         f"C orders {out.when_local}", f"{plan.id}:{o.ticker}:{o.side}"))  # fmt: skip
    for o in flattened:
        out.trades.insert(0, MatchTrade(o.ticker, _product(funds, o.ticker), "SELL", 0.0, True, 0.0,
                                        "C sold everything (15% below its peak)", f"flat:{o.id}",
                                        "Sell all you hold of it."))  # fmt: skip
    for ticker, weight in sorted(plan.targets.items(), key=lambda kv: -kv[1]):
        out.holdings.append(
            Holding(ticker, _product(funds, ticker), weight, round(weight * capital, -1))
        )
    out.cash_weight = max(0.0, 1 - sum(plan.targets.values()))
    if plan.state == "halved":
        out.notes.append(
            "C is at half risk (10% below its peak): the amounts are smaller on purpose."
        )
    if plan.state == "flat":
        out.notes.append("C is all out (15% below its peak): hold cash until its fresh start.")
    if not out.trades:
        out.notes.append(
            "Nothing to trade this week: no fund drifted far enough from its target. Keep what you have."
        )
    return out
