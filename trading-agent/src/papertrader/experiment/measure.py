"""Measuring A, B and C against each other and against SPY, as pre-registered.

Everything is read-only: each system's own tables (equity, Claude spend, judged ideas)
through its own database connection, and the shared prices. Ideas are scored whether
they were traded or not ("shadow scoring") at 1, 5 and 10 trading days, against their
sector fund, from their decision time on.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from papertrader.b.outcomes import IdeaSample, resolve
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.state import get_state, set_state
from papertrader.experiment import stats
from papertrader.ingest.timeutil import at_new_york, new_york_date

START_KEY = "experiment.start"  # kept in system A's state table, like the hub's offset
HORIZONS = (1, 5, 10)


# --------------------------------------------------------------------------- the start


def get_start(engine_a: Engine) -> date | None:
    with Session(engine_a) as session:
        state = get_state(session, START_KEY)
    return date.fromisoformat(state["start"]) if state else None


def set_start(engine_a: Engine, day: date, now: datetime) -> None:
    with Session(engine_a) as session:
        set_state(session, START_KEY, {"start": day.isoformat(), "recorded_at": now.isoformat()})
        session.commit()


# --------------------------------------------------------------------------- loading


@dataclass(frozen=True)
class Idea:
    as_of: datetime
    ticker: str
    action: str  # buy | sell_short
    confidence: float
    status: str


@dataclass
class Record:
    name: str
    equity: dict[date, float] = field(default_factory=dict)  # end-of-day equity
    spend: dict[date, float] = field(default_factory=dict)  # Claude dollars per New York day
    ideas: list[Idea] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return len(self.equity) >= 2


def load(engine: Engine, name: str, start: date) -> Record:
    since = at_new_york(start - timedelta(days=7), time(0))
    record = Record(name)
    with Session(engine) as session:
        for ts, equity in session.execute(
            select(m.EquitySnapshot.ts, m.EquitySnapshot.equity)
            .where(m.EquitySnapshot.kind == "eod", m.EquitySnapshot.ts >= since).order_by(m.EquitySnapshot.ts)
        ):  # fmt: skip
            record.equity[new_york_date(ts)] = float(equity)  # the last one of a day wins
        for ts, cost in session.execute(
            select(m.ApiUsage.ts, m.ApiUsage.cost_usd).where(m.ApiUsage.ts >= at_new_york(start, time(0)))
        ):  # fmt: skip
            day = new_york_date(ts)
            record.spend[day] = record.spend.get(day, 0.0) + float(cost or 0.0)
        for as_of, payload in session.execute(
            select(m.JudgeDecision.as_of, m.JudgeDecision.payload)
            .join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
            .where(m.DecisionCycle.kind == m.CycleKind.DAILY, m.JudgeDecision.as_of >= at_new_york(start, time(0)))
        ):  # fmt: skip
            payload = payload or {}
            if "intraday_plan" in payload:
                continue  # B's scanner: judged on its own scorecard
            for trade in payload.get("trades", []):
                if trade.get("status") == "rejected_by_risk":
                    continue  # failed the judge's own checks: not a real idea
                record.ideas.append(Idea(as_of, trade["ticker"], trade["action"], float(trade["final_confidence"]),
                                         trade.get("status", "")))  # fmt: skip
    return record


def spy_returns(session: Session, benchmark: str, start: date, now: datetime) -> dict[date, float]:
    """SPY's daily total return (the bars are adjusted for dividends and splits)."""
    rows = session.execute(
        select(m.PriceBar.ts, m.PriceBar.close)
        .where(m.PriceBar.ticker == benchmark, m.PriceBar.timeframe == "1Day",
               m.PriceBar.ts >= at_new_york(start - timedelta(days=7), time(0)), m.PriceBar.available_at <= now)
        .order_by(m.PriceBar.ts)
    ).all()  # fmt: skip
    closes = [(new_york_date(ts), float(c)) for ts, c in rows]
    return {d: c / p - 1 for (_, p), (d, c) in pairwise(closes) if d >= start and p > 0}


# --------------------------------------------------------------------------- the numbers


def net_returns(
    record: Record, start: date, data_cost_monthly: float
) -> tuple[dict[date, float], dict[date, float]]:
    """(net, gross) daily returns from the end-of-day equity; costs per day as pre-registered."""
    days = sorted(record.equity)
    net, gross = {}, {}
    for before, day in pairwise(days):
        if day < start:
            continue
        base = record.equity[before]
        if base <= 0:
            continue
        change = record.equity[day] - base
        gross[day] = change / base
        net[day] = (change - record.spend.get(day, 0.0) - data_cost_monthly / 21) / base
    return net, gross


def compound(returns: Mapping[date, float]) -> float:
    total = 1.0
    for value in returns.values():
        total *= 1 + value
    return total - 1


@dataclass
class Signal:
    ic: dict[date, float] = field(default_factory=dict)  # 5-day, per day
    brier: dict[int, float | None] = field(default_factory=dict)  # per horizon
    hit_rate: dict[int, float | None] = field(default_factory=dict)
    mean_ic: dict[int, float | None] = field(default_factory=dict)
    scored: dict[int, int] = field(default_factory=dict)


def signal(session: Session, config: AppConfig, ideas: Sequence[Idea], now: datetime) -> Signal:
    out = Signal()
    for horizon in HORIZONS:
        pairs = [
            (idea, IdeaSample(idea.as_of, idea.ticker, idea.action, horizon, idea.confidence, "x"))
            for idea in ideas
        ]
        by_sample = {id(s): idea for idea, s in pairs}
        done = resolve(session, config, [s for _, s in pairs], now, sector_relative=True)
        out.scored[horizon] = len(done)
        if not done:
            out.brier[horizon] = out.hit_rate[horizon] = out.mean_ic[horizon] = None
            continue
        out.brier[horizon] = sum(
            (r.sample.raw - (1.0 if r.worked else 0.0)) ** 2 for r in done
        ) / len(done)
        out.hit_rate[horizon] = sum(1 for r in done if r.worked) / len(done)
        days: dict[date, list[tuple[float, float]]] = defaultdict(list)
        for r in done:
            idea = by_sample[id(r.sample)]
            score = idea.confidence if idea.action == "buy" else -idea.confidence
            days[new_york_date(idea.as_of)].append((score, r.excess))
        ics = {
            d: ic
            for d, v in days.items()
            if (ic := stats.spearman([a for a, _ in v], [b for _, b in v])) is not None
        }
        out.mean_ic[horizon] = sum(ics.values()) / len(ics) if ics else None
        if horizon == 5:
            out.ic = ics
    return out


# --------------------------------------------------------------------------- the comparison


@dataclass
class SystemResult:
    name: str
    running: bool
    days: int = 0
    net: dict[date, float] = field(default_factory=dict)
    net_total: float | None = None
    gross_total: float | None = None
    spend_usd: float = 0.0
    data_usd: float = 0.0
    signal: Signal = field(default_factory=Signal)

    def at_real_capital(self, capital: float) -> tuple[float, float, float] | None:
        """(gross $, costs $, net $) had the same returns been made on `capital`."""
        if self.gross_total is None:
            return None
        gross = self.gross_total * capital
        costs = self.spend_usd + self.data_usd
        return gross, costs, gross - costs


@dataclass
class Comparison:
    start: date | None
    trading_days: int
    systems: dict[str, SystemResult]
    spy_total: float | None
    tests: list[stats.Test]
    phase: str  # not_started | pilot | running | decision
    verdict: list[str]
    spy_daily: dict[date, float] = field(default_factory=dict)

    def curves(self) -> list[dict[str, Any]]:
        """Cumulative return since the start, per day: each system after costs, and SPY."""
        rows = []
        for name, daily in [
            *((s.name, s.net) for s in self.systems.values() if s.net),
            ("SPY", self.spy_daily),
        ]:
            total = 1.0
            for day in sorted(daily):
                total *= 1 + daily[day]
                rows.append({"day": day, "series": name, "return %": round((total - 1) * 100, 3)})
        return rows


def compare(
    engines: Mapping[str, Engine], config: AppConfig, start: date | None, now: datetime
) -> Comparison:
    exp = config.experiment
    if start is None:
        return Comparison(None, 0, {}, None, [], "not_started",
                          ["The experiment has not started yet: run `papertrader experiment start`."])  # fmt: skip
    any_engine = next(iter(engines.values()))
    with Session(any_engine) as session:
        spy = spy_returns(session, config.universe.benchmark, start, now)
    systems: dict[str, SystemResult] = {}
    for name, engine in engines.items():
        record = load(engine, name, start)
        result = SystemResult(name, record.running)
        net, gross = net_returns(record, start, exp.data_costs_usd.get(name, 0.0))
        result.net, result.days = net, len(net)
        result.net_total = compound(net) if net else None
        result.gross_total = compound(gross) if gross else None
        result.spend_usd = sum(v for d, v in record.spend.items() if d >= start)
        months = max(1, len(net)) / 21
        result.data_usd = exp.data_costs_usd.get(name, 0.0) * months
        with Session(engine) as session:
            result.signal = signal(session, config, record.ideas, now)
        systems[name] = result
    trading_days = len(spy)
    tests = hypotheses(systems, spy)
    phase = (
        "pilot"
        if trading_days < exp.pilot_days
        else "running"
        if trading_days < exp.test_days
        else "decision"
    )
    comparison = Comparison(
        start, trading_days, systems, compound(spy) if spy else None, tests, phase, [], spy
    )
    comparison.verdict = verdict(comparison, config)
    return comparison


def hypotheses(systems: Mapping[str, SystemResult], spy: Mapping[date, float]) -> list[stats.Test]:
    a, b = systems.get("A"), systems.get("B")
    tests: list[stats.Test] = []
    if a is not None and b is not None:
        days = sorted(set(a.signal.ic) & set(b.signal.ic))
        tests.append(stats.paired_test("H1", "B ranks its ideas better than A (daily IC, B minus A)",
                                       [b.signal.ic[d] - a.signal.ic[d] for d in days]))  # fmt: skip
        days = sorted(set(a.net) & set(b.net))
        tests.append(stats.paired_test("H2", "B makes more than A after costs (daily net return, B minus A)",
                                       [b.net[d] - a.net[d] for d in days]))  # fmt: skip
    candidates = [s for s in (a, b) if s is not None and s.net]
    if candidates:
        best = max(candidates, key=lambda s: s.net_total or -1.0)
        days = sorted(set(best.net) & set(spy))
        tests.append(stats.paired_test("H3", f"{best.name} beats holding SPY after costs (daily, {best.name} minus SPY)",
                                       [best.net[d] - spy[d] for d in days]))  # fmt: skip
    adjusted = stats.holm([t.p for t in tests])
    return [
        stats.Test(t.name, t.question, t.days, t.mean, t.interval, t.p, p)
        for t, p in zip(tests, adjusted, strict=True)
    ]


def verdict(c: Comparison, config: AppConfig) -> list[str]:
    exp = config.experiment
    lines = []
    leader = max((s for s in c.systems.values() if s.net_total is not None), key=lambda s: s.net_total or 0.0,
                 default=None)  # fmt: skip
    beats = [t for t in c.tests if t.name == "H3" and t.holds]
    if c.phase == "pilot":
        lines.append(
            f"Pilot, trading day {c.trading_days} of {exp.pilot_days}: checking that everything works. No verdict."
        )
    elif c.phase == "running":
        lines.append(f"Trading day {c.trading_days} of {exp.test_days}. Early numbers only: differences this "
                     "early are mostly noise.")  # fmt: skip
    if leader is not None and c.spy_total is not None:
        ahead = (leader.net_total or 0.0) > c.spy_total
        lines.append(f"So far {leader.name} leads after costs ({(leader.net_total or 0):+.1%}), "
                     f"{'ahead of' if ahead else 'behind'} holding SPY ({c.spy_total:+.1%}).")  # fmt: skip
    if c.phase != "decision":
        return lines
    if beats:
        lines.append(f"Decision: {beats[0].question.split(' beats')[0]} beats SPY after costs, significantly. "
                     "It is the candidate; still paper.")  # fmt: skip
    else:
        lines.append("Decision: no system beats SPY after costs with confidence. The recommendation is to "
                     "hold SPY, the cheapest option.")  # fmt: skip
    b_helps = any(t.holds for t in c.tests if t.name in ("H1", "H2"))
    lines.append("B vs A: B's decision layer helps; keep it." if b_helps else
                 "B vs A: B's layer did not help significantly; cut B back to the switches that added net "
                 "profit (see the switch review).")  # fmt: skip
    for s in c.systems.values():
        money = s.at_real_capital(exp.real_capital_usd)
        if money is not None and money[2] < 0:
            lines.append(f"{s.name} at your ${exp.real_capital_usd:,.0f}: its costs would eat the profit "
                         f"(net {money[2]:+,.0f} $). Not for real money.")  # fmt: skip
    return lines


# --------------------------------------------------------------------------- text


def report_text(c: Comparison, config: AppConfig, *, title: str = "COMPARISON") -> str:
    exp = config.experiment
    if c.start is None:
        return f"{title}\n" + "\n".join(c.verdict)
    lines = [f"{title} · experiment since {c.start:%d %b %Y}, {c.trading_days} trading days"]
    lines.append("Since the start, after Claude and data costs:")
    parts = []
    for name in ("A", "B", "C"):
        s = c.systems.get(name)
        if s is None or not s.running or s.net_total is None:
            parts.append(f"{name} not running")
        else:
            parts.append(f"{name} {s.net_total:+.1%} (costs ${s.spend_usd + s.data_usd:,.0f})")
    parts.append(f"SPY {c.spy_total:+.1%}" if c.spy_total is not None else "SPY n/a")
    lines.append(" · ".join(parts))
    lines.append(f"With your ${exp.real_capital_usd:,.0f} instead:")
    for s in c.systems.values():
        money = s.at_real_capital(exp.real_capital_usd)
        if money is not None:
            gross, costs, net = money
            lines.append(
                f"  {s.name}: {gross:+,.0f} $ from trading - {costs:,.0f} $ costs = {net:+,.0f} $"
            )
    lines.append("Signal quality, 5-day rank IC (0 = no skill; above about 0.05 is useful):")
    for s in c.systems.values():
        ic = s.signal.mean_ic.get(5)
        hit = s.signal.hit_rate.get(5)
        if ic is not None:
            lines.append(f"  {s.name}: IC {ic:+.3f} over {len(s.signal.ic)} days, hit rate {hit:.0%} "
                         f"({s.signal.scored.get(5, 0)} ideas)")  # fmt: skip
    for t in c.tests:
        if t.mean is None:
            continue
        band = f", 95% interval {t.interval[0]:+.4f} to {t.interval[1]:+.4f}" if t.interval else ""
        p = f", p {t.p_holm:.2f} after Holm" if t.p_holm is not None else ""
        lines.append(f"{t.name} ({t.question}): mean {t.mean:+.4f} over {t.days} days{band}{p}"
                     f"{' - HOLDS' if t.holds else ''}")  # fmt: skip
    lines += c.verdict
    return "\n".join(lines)


def as_rows(c: Comparison) -> list[dict[str, Any]]:
    """For the dashboard's table."""
    rows = []
    for s in c.systems.values():
        rows.append({"System": s.name, "Running": "yes" if s.running else "no", "Days": s.days,
                     "Net return": s.net_total, "Gross return": s.gross_total,
                     "Claude $": round(s.spend_usd, 2), "Data $": round(s.data_usd, 2),
                     "IC (5 days)": s.signal.mean_ic.get(5), "Brier (5 days)": s.signal.brier.get(5),
                     "Hit rate (5 days)": s.signal.hit_rate.get(5)})  # fmt: skip
    return rows
