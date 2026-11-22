"""What the web app shows: plain functions over each system's read-only connection.

Nothing here writes (the connections refuse writes anyway). Money is in dollars,
percentages are fractions (0.012 = +1.2%), times are New York time, like the schedule.
Live prices come from the hub's snapshot (papertrader.web.live), never from a broker.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig
from papertrader.dashboard import data as dash
from papertrader.db import models as m
from papertrader.db.state import HEARTBEAT_KEY, KILL_SWITCH_KEY, TRADING_PAUSED_KEY, get_state
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
from papertrader.web import live as live_mod

SYSTEM_ORDER = ("A", "B", "C")
TITLES = {
    "A": "A · the original (frozen)",
    "B": "B · better decisions",
    "C": "C · ETF trend portfolio",
}
HEARTBEAT_STALE_SECONDS = 180


class View(Protocol):
    name: str
    config: AppConfig
    engine: Engine
    sessions: sessionmaker[Session]


def ordered(views: Mapping[str, View]) -> list[View]:
    return [views[n] for n in SYSTEM_ORDER if n in views]


def ny(moment: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return moment.astimezone(NEW_YORK).strftime(fmt) if moment else ""


def left(until: datetime | None, now: datetime) -> str:
    """'2d 4h', '3h 10m', '25m' or 'due'."""
    if until is None:
        return ""
    seconds = (until - now).total_seconds()
    if seconds <= 0:
        return "due"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def safe_url(url: object) -> str | None:
    """Only plain web links are ever turned into links."""
    text = str(url or "").strip()
    return text if text.startswith(("https://", "http://")) else None


# --------------------------------------------------------------------------- shared helpers


def heartbeat_age(session: Session, now: datetime) -> float | None:
    beat = get_state(session, HEARTBEAT_KEY)
    if not beat:
        return None
    try:
        return (now - datetime.fromisoformat(beat["at"])).total_seconds()
    except (KeyError, ValueError):
        return None


def spy_close_before(session: Session, benchmark: str, day: date) -> float | None:
    row = session.execute(
        select(m.PriceBar.close).where(m.PriceBar.ticker == benchmark, m.PriceBar.timeframe == "1Day",
                                       m.PriceBar.ts < at_new_york(day, time(0)))
        .order_by(m.PriceBar.ts.desc()).limit(1)
    ).first()  # fmt: skip
    return float(row[0]) if row else None


def equity_before(session: Session, day: date) -> float | None:
    """The closing equity just before `day`, else the first one after it."""
    row = session.scalars(select(m.EquitySnapshot).where(m.EquitySnapshot.kind == "eod",
                                                         m.EquitySnapshot.ts < at_new_york(day, time(0)))
                          .order_by(m.EquitySnapshot.ts.desc()).limit(1)).first()  # fmt: skip
    if row is None:
        row = session.scalars(select(m.EquitySnapshot).where(m.EquitySnapshot.ts >= at_new_york(day, time(0)))
                              .order_by(m.EquitySnapshot.ts).limit(1)).first()  # fmt: skip
    return float(row.equity) if row else None


def experiment_start(views: Mapping[str, View]) -> date | None:
    from papertrader.experiment.measure import get_start

    return get_start(views["A"].engine) if "A" in views else None


# --------------------------------------------------------------------------- today


@dataclass
class Card:
    name: str
    title: str
    running: bool
    heartbeat: str
    equity: float | None = None
    day_pct: float | None = None
    live_note: str = ""
    since_start_pct: float | None = None
    positions: int = 0
    paused: str | None = None
    kill_switch: bool = False
    month_spend: float = 0.0
    budget: float = 0.0
    closed: int = 0
    win_rate: float | None = None  # fraction
    realised: float = 0.0
    last_research: str = ""
    model: str = ""
    alerts: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Today:
    cards: list[Card]
    start: date | None
    spy_day_pct: float | None
    spy_since_start_pct: float | None
    spy_note: str


def today(views: Mapping[str, View], now: datetime) -> Today:
    start = experiment_start(views)
    cards, spy_now, spy_at = [], None, None
    for view in ordered(views):
        snap = live_mod.read(view.sessions)
        over = dash.overview(view.engine, view.config, now)
        with view.sessions() as session:
            age = heartbeat_age(session, now)
            alerts = session.scalars(select(m.Notification).where(
                m.Notification.kind == "alert", m.Notification.created_at >= now - timedelta(days=3))
                .order_by(m.Notification.id.desc()).limit(5)).all()  # fmt: skip
            base = equity_before(session, start) if start else None
            open_trades = len(session.scalars(select(m.Trade.id).where(
                m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False))).all())  # fmt: skip
        fresh = snap.fresh(now)
        card = Card(
            name=view.name, title=TITLES.get(view.name, view.name),
            running=age is not None and age < HEARTBEAT_STALE_SECONDS,
            heartbeat="no heartbeat yet" if age is None else f"last seen {max(0.0, age) / 60:.0f} min ago",
            equity=snap.equity if fresh else over.equity,
            day_pct=snap.day_change_pct if fresh else (over.day_pnl_pct / 100 if over.day_pnl_pct is not None else None),
            live_note=f"live, {ny(snap.at, '%H:%M')}" if fresh else (
                f"last close (live numbers from {ny(snap.at)})" if snap.at else "last snapshot"),
            positions=len(snap.positions) if snap.at else open_trades,
            paused=over.paused, kill_switch=over.kill_switch, month_spend=over.month_spend,
            budget=view.config.budget.monthly_usd, closed=over.closed_trades,
            win_rate=over.win_rate / 100 if over.win_rate is not None else None, realised=over.realised_usd,
            last_research=over.last_research if view.name != "C" else "", model=over.active_model if view.name != "C" else "",
            alerts=[(ny(a.created_at), clip(a.text.splitlines()[0] if a.text else "", 160)) for a in alerts],
        )  # fmt: skip
        if base and card.equity:
            card.since_start_pct = card.equity / base - 1
        if snap.spy and (spy_at is None or (snap.at and snap.at > spy_at)):
            spy_now, spy_at = snap.spy, snap.at
        cards.append(card)
    spy_day = spy_start = None
    note = "no SPY price yet"
    if views:
        any_view = ordered(views)[0]
        benchmark = any_view.config.universe.benchmark
        with any_view.sessions() as session:
            prev = spy_close_before(session, benchmark, new_york_date(now))
            first = spy_close_before(session, benchmark, start) if start else None
            last_close = spy_close_before(
                session, benchmark, new_york_date(now) + timedelta(days=1)
            )
        current = (
            spy_now
            if spy_now and spy_at and (now - spy_at) <= timedelta(minutes=live_mod.STALE_MINUTES)
            else None
        )
        if current and prev:
            spy_day, note = current / prev - 1, f"live, {ny(spy_at, '%H:%M')}"
        elif last_close:
            note = "last close"
        price = current or last_close
        if first and price:
            spy_start = price / first - 1
    return Today(cards, start, spy_day, spy_start, note)


# --------------------------------------------------------------------------- trades


@dataclass
class OpenRow:
    ticker: str
    side: str
    qty: float
    entry: float | None
    price: float | None
    pnl_usd: float | None
    pnl_pct: float | None
    target: float | None
    stop: float | None
    opened: str
    left: str
    note: str = ""


@dataclass
class SystemTrades:
    name: str
    title: str
    live_note: str
    open: list[OpenRow]
    closed: list[dict[str, Any]]
    closed_columns: list[str]


C_STOP_LIVE = ("new", "accepted", "pending_new", "held", "open", "partially_filled", "submitted")


def trades(views: Mapping[str, View], now: datetime) -> list[SystemTrades]:
    out = []
    for view in ordered(views):
        snap = live_mod.read(view.sessions)
        note = (f"prices live at {ny(snap.at, '%H:%M')}" if snap.fresh(now) else
                f"prices from {ny(snap.at)} (not live)" if snap.at else "no live prices yet (the hub stores them every 2 minutes)")  # fmt: skip
        if view.name == "C":
            rows, closed = _c_positions(view, snap), _c_orders(view)
            out.append(SystemTrades(view.name, TITLES["C"], note, rows, closed,
                                    ["when", "fund", "kind", "side", "shares", "price", "status"]))  # fmt: skip
            continue
        rows = []
        with view.sessions() as session:
            open_trades = session.scalars(select(m.Trade).where(
                m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False)).order_by(m.Trade.entered_at)).all()  # fmt: skip
            for t in open_trades:
                pos = snap.positions.get(t.ticker)
                price = pos.price if pos else None
                sign = 1 if t.direction == m.Direction.LONG else -1
                pnl = (price - t.entry_price) * t.qty * sign if price is not None else None
                pct = (
                    (price / t.entry_price - 1) * sign
                    if price is not None and t.entry_price
                    else None
                )
                rows.append(OpenRow(t.ticker, t.direction.value, t.qty, t.entry_price, price, pnl, pct, t.target_price,
                                    t.stop_price, ny(t.entered_at), left(t.horizon_ends_at, now)))  # fmt: skip
        closed = dash.trades(view.engine, open_only=False, limit=40)
        records = closed.to_dict("records") if not closed.empty else []
        columns = ["closed", "ticker", "P&L $", "P&L %", "vs SPY %", "exit by", "days held", "agents right",
                   "agents wrong", "reason"]  # fmt: skip
        out.append(SystemTrades(view.name, TITLES[view.name], note, rows, records, columns))
    return out


def _c_positions(view: View, snap: live_mod.Live) -> list[OpenRow]:
    from papertrader.c.models import Order

    rows = []
    with view.sessions() as session:
        stops = {o.ticker: o.stop_price for o in session.scalars(select(Order).where(
            Order.kind == "stop", Order.status.in_(C_STOP_LIVE)).order_by(Order.id)).all()}  # fmt: skip
    equity = snap.equity or 0.0
    for p in sorted(snap.positions.values(), key=lambda p: -abs(p.qty * p.price)):
        weight = p.qty * p.price / equity if equity else None
        rows.append(OpenRow(p.symbol, "long" if p.qty > 0 else "short", p.qty, p.avg_entry, p.price, p.pnl_usd,
                            p.pnl_pct, None, stops.get(p.symbol), "", "",
                            (f"{weight:.0%} of the account · " if weight is not None else "") + "rebalanced weekly"))  # fmt: skip
    return rows


def _c_orders(view: View) -> list[dict[str, Any]]:
    from papertrader.c.models import Order

    with view.sessions() as session:
        rows = session.scalars(
            select(Order).where(Order.filled_qty > 0).order_by(Order.updated_at.desc()).limit(40)
        ).all()
    return [{"when": ny(o.updated_at), "fund": o.ticker, "kind": o.kind, "side": o.side, "shares": round(o.filled_qty, 3),
             "price": o.filled_avg_price, "status": o.status} for o in rows]  # fmt: skip


# --------------------------------------------------------------------------- planned


@dataclass
class Planned:
    system: str
    when: str
    action: str  # BUY | SELL | SHORT | HOLD
    ticker: str
    detail: str
    why: str
    against: str = ""
    status: str = ""
    skip_ticker: str | None = None  # set when the web app may skip it (B's intraday plans)
    skip_order: int | None = None  # an announced order of B or C (its heads-up number)


@dataclass
class CPlan:
    signal_day: str
    trade_day: str
    when: str
    state: str
    status: str
    rows: list[dict[str, Any]]
    cash: float
    note: str
    avanza: list[dict[str, Any]]


def planned(views: Mapping[str, View], now: datetime) -> tuple[list[Planned], CPlan | None]:
    items: list[Planned] = []
    today_ny = new_york_date(now)
    for view in ordered(views):
        if view.name == "C":
            continue
        place_at = at_new_york(today_ny, view.config.schedule.place_orders)
        with view.sessions() as session:
            decision = session.scalars(
                select(m.JudgeDecision).join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
                .where(m.DecisionCycle.kind == m.CycleKind.DAILY, m.JudgeDecision.as_of >= at_new_york(today_ny, time(0)))
                .order_by(m.JudgeDecision.id.desc()).limit(1)).first()  # fmt: skip
            if decision is not None:
                for idea in sorted(decision.ideas, key=lambda i: i.rank):
                    if idea.status != m.IdeaStatus.PROPOSED:
                        continue
                    action = "BUY" if idea.action == m.TradeAction.BUY else "SHORT"
                    entry = (f"entry {idea.entry_low:.2f}-{idea.entry_high:.2f}" if idea.entry_low and idea.entry_high
                             else "at the market")  # fmt: skip
                    items.append(Planned(view.name, f"today {ny(place_at, '%H:%M')}" if now < place_at else "at the next run",
                                         action, idea.ticker,
                                         f"{entry} · target {idea.target_price or 0:.2f} · stop {idea.stop_price or 0:.2f} · "
                                         f"{(idea.size_pct or 0):.1%} of equity · {idea.horizon_days} days · "
                                         f"confidence {(idea.final_confidence or 0):.0%}",
                                         clip(idea.rationale, 400), clip(idea.dissent, 300),
                                         "information only (A is frozen)" if view.name == "A" else "planned"))  # fmt: skip
            for signal in session.scalars(select(m.ExitSignal).where(m.ExitSignal.acted_on.is_(False))
                                          .order_by(m.ExitSignal.id.desc()).limit(20)).all():  # fmt: skip
                items.append(Planned(view.name, "next check (every 10 minutes)", "SELL", signal.ticker,
                                     f"exit: {signal.trigger.value.replace('_', ' ')}", clip(signal.reason, 400)))  # fmt: skip
        if view.name == "B":
            items.extend(_b_intraday(view, today_ny))
    for name in ("B", "C"):
        if name in views:
            items.extend(_announced(views[name]))
    return items, (_c_plan(views["C"], now) if "C" in views else None)


def _announced(view: View) -> list[Planned]:
    """Orders with a Telegram heads-up that have not gone yet (X6): Skip works here too."""
    from papertrader.notify.warnings import GO, SOURCE_WORDS, WAITING, OrderWarning

    with view.sessions() as session:
        rows = session.scalars(select(OrderWarning).where(OrderWarning.status.in_((WAITING, GO)))
                               .order_by(OrderWarning.due_at)).all()  # fmt: skip
        return [Planned(view.name, f"at {ny(r.due_at, '%H:%M')} unless skipped" + (" (Go now)" if r.status == GO else ""),
                        r.side, r.ticker, f"announced on Telegram · {SOURCE_WORDS.get(r.source, r.source)}",
                        "", "", "announced", None, r.id) for r in rows]  # fmt: skip


def _b_intraday(view: View, day: date) -> list[Planned]:
    from papertrader.b.models import IntradayPlan

    out = []
    with view.sessions() as session:
        plans = session.scalars(
            select(IntradayPlan).where(IntradayPlan.day == day).order_by(IntradayPlan.id)
        ).all()
        for p in plans:
            if p.status not in ("planned", "shadow", "skipped", "refused", "placed"):
                continue
            when = {"planned": f"placed at {ny(p.place_after, '%H:%M')} unless skipped",
                    "shadow": "shadow: never placed"}.get(p.status, p.status)  # fmt: skip
            out.append(Planned("B", when, "BUY", p.ticker,
                               f"entry {p.entry_low:.2f}-{p.entry_high:.2f} · target {p.target_price:.2f} · stop "
                               f"{p.stop_price:.2f} · {p.size_pct:.1%} of equity · out by {ny(p.time_stop_at, '%H:%M')} · "
                               f"confidence {p.confidence:.0%} · rule {p.rule.replace('_', ' ')}",
                               clip(p.why, 400), clip(p.invalidation, 300), p.note or p.status,
                               p.ticker if p.status == "planned" else None))  # fmt: skip
    return out


def _c_plan(view: View, now: datetime) -> CPlan | None:
    from papertrader.c.models import Plan

    cfg = view.config.portfolio
    with view.sessions() as session:
        plan = session.scalars(select(Plan).order_by(Plan.signal_day.desc()).limit(1)).first()
        if plan is None:
            return None
        session.expunge(plan)
    snap = live_mod.read(view.sessions)
    equity = snap.equity or 0.0
    current = {t: p.qty * p.price / equity for t, p in snap.positions.items()} if equity else {}
    funds = cfg.by_ticker()
    rows = []
    for ticker in sorted(set(plan.targets) | {t for t, w in current.items() if w > 0.001},
                         key=lambda t: -plan.targets.get(t, 0.0)):  # fmt: skip
        goal, now_w = plan.targets.get(ticker, 0.0), current.get(ticker, 0.0)
        action = (
            "SELL"
            if goal == 0
            else "BUY"
            if goal > now_w + 0.005
            else "SELL"
            if goal < now_w - 0.005
            else "HOLD"
        )
        review = plan.review.get(ticker) if isinstance(plan.review, dict) else None
        reason = (
            str(
                plan.reasons.get(
                    ticker, "the T-bill fund (cash)" if ticker == cfg.cash_fund else ""
                )
            )
            .removeprefix("in: ")
            .removeprefix("out: ")
        )
        rows.append({"fund": ticker, "action": action, "now": now_w, "target": goal, "why": reason,
                     "review": f"{review['verdict']}: {review['reason']}" if review else "",
                     "group": funds[ticker].group if ticker in funds else ""})  # fmt: skip
    avanza = [{"product": (funds[t].avanza if t in funds else "") or "no Avanza equivalent: keep it in cash",
               "usd": w * cfg.mirror_capital_usd, "sek": w * cfg.mirror_capital_usd * cfg.sek_per_usd, "fund": t}
              for t, w in sorted(plan.targets.items(), key=lambda kv: -kv[1])]  # fmt: skip
    when = at_new_york(plan.trade_day, cfg.rebalance_time)
    return CPlan(f"{plan.signal_day:%a %d %b}", f"{plan.trade_day:%a %d %b}",
                 f"{ny(when)}" + (f" (in {left(when, now)})" if when > now else ""), plan.state, plan.status, rows,
                 max(0.0, 1 - sum(plan.targets.values())), plan.note, avanza)  # fmt: skip


# --------------------------------------------------------------------------- news


@dataclass
class NewsLine:
    when: str
    headline: str
    summary: str
    source: str
    url: str | None


def held_tickers(views: Mapping[str, View]) -> dict[str, list[str]]:
    """Ticker -> the systems holding it (live positions, else open trades)."""
    held: dict[str, list[str]] = {}
    for view in ordered(views):
        snap = live_mod.read(view.sessions)
        names = set(snap.positions)
        if not snap.at and view.name != "C":
            with view.sessions() as session:
                names = set(session.scalars(select(m.Trade.ticker).where(
                    m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False))).all())  # fmt: skip
        for t in names:
            held.setdefault(t, []).append(view.name)
    return dict(sorted(held.items()))


def news(
    views: Mapping[str, View], now: datetime, days: int = 3, per_ticker: int = 6
) -> list[tuple[str, list[str], list[NewsLine]]]:
    if not views:
        return []
    held = held_tickers(views)
    out = []
    with ordered(views)[0].sessions() as session:
        for ticker, systems in held.items():
            items = session.scalars(
                select(m.NewsItem).join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
                .where(m.NewsTicker.ticker == ticker, m.NewsItem.duplicate_of_id.is_(None),
                       m.NewsItem.available_at >= now - timedelta(days=days), m.NewsItem.available_at <= now)
                .order_by(m.NewsItem.published_at.desc()).limit(per_ticker)).all()  # fmt: skip
            out.append((ticker, systems, [NewsLine(ny(n.published_at), clip(n.headline, 200), clip(n.summary, 260),
                                                   n.source, safe_url(n.url)) for n in items]))  # fmt: skip
    return out


# --------------------------------------------------------------------------- post-mortems


def postmortems(views: Mapping[str, View], limit: int = 40) -> list[dict[str, Any]]:
    if "B" not in views:
        return []
    from papertrader.b.models import PostMortem

    with views["B"].sessions() as session:
        rows = session.scalars(
            select(PostMortem).order_by(PostMortem.day.desc(), PostMortem.id.desc()).limit(limit)
        ).all()
        return [{
            "day": f"{p.day:%a %d %b}", "ticker": p.ticker, "kind": p.kind.replace("_", " "), "move": p.move_pct,
            "vs_spy": p.move_vs_spy, "vs_sector": p.move_vs_sector, "what_b_did": p.what_b_did,
            "causes": [{"cause": c.get("cause", ""), "likelihood": c.get("likelihood"),
                        "sources": [str(s) for s in c.get("sources", [])]} for c in (p.causes or [])],
            "quality": p.decision_quality, "why": p.why_b_acted, "outcome": p.outcome,
            "change": p.what_would_change, "lesson": p.lesson, "evidence": p.evidence, "retired": p.retired,
        } for p in rows]  # fmt: skip


# --------------------------------------------------------------------------- the comparison


@dataclass
class Compared:
    verdict: list[str]
    start: date | None
    phase: str
    days: int
    rows: list[dict[str, Any]]
    returns: dict[str, list[tuple[str, float]]]  # cumulative % since the start, after costs
    vs_spy: dict[str, list[tuple[str, float]]]
    drawdown: dict[str, list[tuple[str, float]]]
    tests: list[dict[str, Any]]
    real_capital: float


def _drawdown(daily: Mapping[date, float]) -> list[tuple[date, float]]:
    total, peak, out = 1.0, 1.0, []
    for day in sorted(daily):
        total *= 1 + daily[day]
        peak = max(peak, total)
        out.append((day, total / peak - 1))
    return out


def _sharpe(daily: Mapping[date, float]) -> float | None:
    values = list(daily.values())
    if len(values) < 10:
        return None
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean / var**0.5 * 252**0.5 if var > 0 else None


def _trading(view: View, start: date) -> tuple[int, float]:
    """(round trips or orders since the start, dollars traded)."""
    since = at_new_york(start, time(0))
    with view.sessions() as session:
        if view.name == "C":
            from papertrader.c.models import Order

            rows = session.scalars(
                select(Order).where(Order.filled_qty > 0, Order.updated_at >= since)
            ).all()
            return len(rows), sum(o.filled_qty * (o.filled_avg_price or 0) for o in rows)
        count = len(
            session.scalars(
                select(m.Trade.id).where(m.Trade.entered_at >= since, m.Trade.is_test.is_(False))
            ).all()
        )
        fills = session.execute(
            select(m.Fill.qty, m.Fill.price).where(m.Fill.filled_at >= since)
        ).all()
        return count, sum(float(q) * float(p) for q, p in fills)


def compare(views: Mapping[str, View], config: AppConfig, now: datetime) -> Compared:
    from papertrader.experiment.measure import compare as measure

    start = experiment_start(views)
    engines = {v.name: v.engine for v in ordered(views)}
    if not engines:
        return Compared(["No system is set up yet."], None, "not_started", 0, [], {}, {}, {}, [],
                        config.experiment.real_capital_usd)  # fmt: skip
    c = measure(engines, config, start, now)
    capital = config.experiment.real_capital_usd
    spy_curve = _cumulative(c.spy_daily)
    rows, returns, vs_spy, drawdowns = [], {}, {}, {}
    for name, s in c.systems.items():
        curve = _cumulative(s.net)
        returns[name] = [(d.isoformat(), v * 100) for d, v in curve]
        spy_by_day = dict(spy_curve)
        vs_spy[name] = [
            (d.isoformat(), (v - spy_by_day[d]) * 100) for d, v in curve if d in spy_by_day
        ]
        dd = _drawdown(s.net)
        drawdowns[name] = [(d.isoformat(), v * 100) for d, v in dd]
        count, traded = _trading(views[name], start) if start else (0, 0.0)
        money = s.at_real_capital(capital)
        rows.append({
            "system": name, "days": s.days, "net": s.net_total, "gross": s.gross_total,
            "vs_spy": (s.net_total - c.spy_total) if s.net_total is not None and c.spy_total is not None else None,
            "worst_fall": min((v for _, v in dd), default=None), "sharpe": _sharpe(s.net),
            "hit_rate": s.signal.hit_rate.get(5), "ic": s.signal.mean_ic.get(5), "brier": s.signal.brier.get(5),
            "trades": count, "traded_usd": traded, "spread_cost_usd": traded * 0.0005,
            "claude_usd": s.spend_usd, "data_usd": s.data_usd,
            "net_at_capital": money[2] if money else None,
        })  # fmt: skip
    if spy_curve:
        returns["SPY"] = [(d.isoformat(), v * 100) for d, v in spy_curve]
        drawdowns["SPY"] = [(d.isoformat(), v * 100) for d, v in _drawdown(c.spy_daily)]
    tests = [{"name": t.name, "question": t.question, "mean": t.mean, "days": t.days, "interval": t.interval,
              "p": t.p_holm} for t in c.tests]  # fmt: skip
    return Compared(
        c.verdict,
        c.start,
        c.phase,
        c.trading_days,
        rows,
        returns,
        vs_spy,
        drawdowns,
        tests,
        capital,
    )


def _cumulative(daily: Mapping[date, float]) -> list[tuple[date, float]]:
    total, out = 1.0, []
    for day in sorted(daily):
        total *= 1 + daily[day]
        out.append((day, total - 1))
    return out


# --------------------------------------------------------------------------- per-system pages


def since_first_day(views: Mapping[str, View]) -> dict[str, list[tuple[str, float]]]:
    """Each system's % change since its own first snapshot, and SPY's over A's days."""
    out: dict[str, list[tuple[str, float]]] = {}
    for view in ordered(views):
        frame = dash.equity_curve(view.engine)
        if frame.empty:
            continue
        out[view.name] = [
            (day.date().isoformat(), float(v))
            for day, v in zip(frame["day"], frame["papertrader_pct"], strict=True)
        ]
        if "SPY" not in out and frame["spy_pct"].notna().any():
            out["SPY"] = [(day.date().isoformat(), float(v)) for day, v in zip(frame["day"], frame["spy_pct"], strict=True)
                          if v == v and v is not None]  # fmt: skip
    return out


def table_counts(view: View) -> list[tuple[str, int]]:
    status = dash.load_status(view.engine)
    return sorted(status.table_rows.items())


def equity_series(view: View) -> list[tuple[str, float]]:
    frame = dash.equity_curve(view.engine)
    if frame.empty:
        return []
    return [
        (d.date().isoformat(), float(e)) for d, e in zip(frame["day"], frame["equity"], strict=True)
    ]


def decisions(
    view: View, decision_id: int | None
) -> tuple[list[tuple[int, str]], dash.DecisionView | None]:
    choices = dash.decision_choices(view.engine)
    if not choices:
        return [], None
    picked = decision_id if decision_id in {i for i, _ in choices} else choices[0][0]
    return choices, dash.decision_view(view.engine, picked)


def agents(
    view: View, window: int
) -> tuple[dash.Leaderboard, list[dict[str, Any]], tuple[str, str] | None]:
    board = dash.leaderboard(view.engine, window)
    weights = dash.weights(view.engine)
    latest = []
    if not weights.empty:
        last = weights[weights["week"] == weights["week"].max()]
        latest = [
            {"agent": a, "weight": round(float(w), 3)}
            for a, w in zip(last["agent"], last["weight"], strict=True)
        ]
    return board, latest, dash.latest_lesson(view.engine)


def costs(views: Mapping[str, View], now: datetime) -> list[dict[str, Any]]:
    out = []
    for view in ordered(views):
        spend = dash.api_spend(view.engine, now=now)
        try:
            status = dash.budget_status(view.engine, view.config)
            mode, reason, allowance = status.mode, status.reason, status.day_budget
        except Exception as exc:  # the budget check must not break the page
            mode, reason, allowance = "unknown", type(exc).__name__, None
        daily = []
        if not spend.daily.empty:
            totals = spend.daily.sum(axis=1)
            daily = [(d.date().isoformat(), float(v)) for d, v in totals.items()]
        out.append({"name": view.name, "title": TITLES[view.name], "month": spend.month_total, "today": spend.today_total,
                    "budget": view.config.budget.monthly_usd, "mode": mode, "reason": reason, "allowance": allowance,
                    "by_purpose": spend.by_purpose.to_dict("records") if not spend.by_purpose.empty else [],
                    "by_model": spend.by_model.to_dict("records") if not spend.by_model.empty else [],
                    "daily": daily})  # fmt: skip
    return out


def jobs(view: View, now: datetime, include_monitor: bool) -> dict[str, Any]:
    with view.sessions() as session:
        age = heartbeat_age(session, now)
        paused = get_state(session, TRADING_PAUSED_KEY) or {}
        kill = get_state(session, KILL_SWITCH_KEY) or {}
    runs = dash.job_runs(view.engine, days=3, include_monitor=include_monitor)
    messages = dash.notifications(view.engine, limit=40)
    return {
        "running": age is not None and age < HEARTBEAT_STALE_SECONDS,
        "heartbeat": "no heartbeat yet"
        if age is None
        else f"last seen {max(0.0, age) / 60:.0f} min ago",
        "paused": paused.get("reason") if paused.get("paused") else None,
        "kill_switch": bool(kill.get("active")),
        "runs": runs.to_dict("records") if not runs.empty else [],
        "messages": messages.to_dict("records") if not messages.empty else [],
    }


def model_versions(view: View) -> list[dict[str, Any]]:
    frame = dash.model_versions(view.engine)
    return frame.to_dict("records") if not frame.empty else []


def settings_rows(view: View) -> list[tuple[str, str]]:
    c = view.config
    rows = [("Monthly Claude budget", f"${c.budget.monthly_usd:,.0f}"),
            ("Daily loss kill switch", f"-{c.risk.daily_loss_kill_switch_pct:.0%}")]  # fmt: skip
    if view.name == "C":
        p = c.portfolio
        rows += [("Funds held", f"best {p.top_n} of {len(p.funds)}"),
                 ("Momentum look-back", " and ".join(f"{d // 21} months" for d in p.score_days)),
                 ("Volatility target", f"{p.vol_target:.0%} a year"),
                 ("Drawdown rules", f"half risk at -{p.drawdown_halve:.0%}, all out at -{p.drawdown_flat:.0%}"),
                 ("Upgrades on", ", ".join(k for k, v in p.upgrades.model_dump().items() if v) or "none"),
                 ("Weekly review (may only reduce buys)", "on" if p.veto.enabled else "off"),
                 ("Daily chart trades", "on" if p.sleeve.enabled else "off")]  # fmt: skip
        return rows
    rows += [("Universe", c.universe.index),
             ("Research agents", f"{c.agents.defaults.model} (effort {c.agents.defaults.effort})"),
             ("Judge", f"{c.judge.model} (effort {c.judge.effort})"),
             ("Trade bar", f"{c.judge.confidence_threshold:.0%} confidence"),
             ("Max per position", f"{c.risk.max_position_pct:.0%} of equity"),
             ("Max positions", str(c.risk.max_open_positions)),
             ("Shorts", "allowed" if c.risk.allow_shorts else "off")]  # fmt: skip
    if view.name == "B":
        on = [k.split("_", 1)[1].replace("_", " ") for k, v in c.decision.switches().items() if v]
        rows.append(("Decision switches on", ", ".join(on) or "none"))
    return rows


def c_extras(views: Mapping[str, View]) -> dict[str, Any] | None:
    """C's latest backtest and the daily chart trades' record."""
    if "C" not in views:
        return None
    from papertrader.c.models import BacktestRun, SleevePlan

    with views["C"].sessions() as session:
        run = session.scalars(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(1)).first()
        sleeve = session.scalars(select(SleevePlan).where(SleevePlan.status == "done")).all()
        backtest = None
        if run is not None:
            result = run.result or {}
            backtest = {"ran": ny(run.ran_at, "%d %b %Y"), "verdict": run.verdict, "chosen": run.chosen,
                        "rows": [(label, period, (result.get(key) or {}).get(period) or {})
                                 for key, label in (("c", "C"), ("spy", "SPY"), ("sixty_forty", "60/40"))
                                 for period in ("in_sample", "out_of_sample")]}  # fmt: skip
        wins = [s for s in sleeve if (s.net_return_pct or 0) > 0]
        return {"backtest": backtest, "sleeve_done": len(sleeve), "sleeve_wins": len(wins),
                "sleeve_avg": sum(s.net_return_pct or 0 for s in sleeve) / len(sleeve) if sleeve else None}  # fmt: skip
