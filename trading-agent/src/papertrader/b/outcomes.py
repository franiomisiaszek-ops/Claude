"""What happened after a trade idea, on the same yardstick as the agents' outcomes.

    entry  = the open of the first trading day whose decision time is at or after the idea
    exit   = the close `horizon_days` trading days later, counting the entry day
    excess = the stock's return minus its benchmark's over the same span

The benchmark is SPY, or the stock's sector ETF when sector-relative scoring (switch 6)
is on. A buy idea "worked" when the excess is above zero, a short when it is below.

The ideas come from two places: B's own decision log, and system A's stored decisions
(read-only), which go back further. Where both judged the same stock on the same day,
B's own row is used.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from papertrader.agents.snapshot import SECTOR_ETF
from papertrader.b.models import DecisionLog
from papertrader.config import AppConfig, BlendWeights
from papertrader.db import models as m
from papertrader.db.provision import a_tables
from papertrader.ingest.timeutil import new_york_date
from papertrader.judge.blend import final_confidence
from papertrader.learning.outcomes import _daily_bars, entry_index


@dataclass(frozen=True)
class IdeaSample:
    as_of: datetime
    ticker: str
    action: str  # buy | sell_short
    horizon_days: int
    raw: float  # judge + agreement blend, without the pattern model
    source: str  # "A" or "B"
    features: dict[str, float] = field(default_factory=dict)

    @property
    def key(self) -> tuple[date, str]:
        return new_york_date(self.as_of), self.ticker


@dataclass(frozen=True)
class Resolved:
    sample: IdeaSample
    excess: float
    worked: bool


def raw_blend(judge: float, agreement: float, blend: BlendWeights) -> float:
    return final_confidence(judge, None, agreement, blend)


def b_samples(session: Session, since: datetime) -> list[IdeaSample]:
    rows = session.scalars(
        select(DecisionLog).where(DecisionLog.as_of >= since,
                                  DecisionLog.a_status != "rejected_by_risk")
        .order_by(DecisionLog.id)
    ).all()  # fmt: skip
    return [
        IdeaSample(row.as_of, row.ticker, row.action, row.horizon_days, row.raw_confidence, "B",
                   {"judge": row.judge_confidence, "agreement": row.agreement,
                    "atr_pct": row.atr_pct or 0.0,
                    "market_up": 1.0 if row.market_trend == "above" else 0.0})
        for row in rows
    ]  # fmt: skip


def a_samples(engine: Engine, since: datetime, blend: BlendWeights) -> list[IdeaSample]:
    """A's judged ideas of its daily runs, as the judge first scored them."""
    out: list[IdeaSample] = []
    with engine.connect() as conn:
        view = conn.execution_options(schema_translate_map=a_tables(engine))
        with Session(bind=view) as session:
            rows = session.execute(
                select(m.JudgeDecision.as_of, m.JudgeDecision.payload)
                .join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
                .where(m.DecisionCycle.kind == m.CycleKind.DAILY, m.JudgeDecision.as_of >= since)
                .order_by(m.JudgeDecision.id)
            ).all()  # fmt: skip
    for as_of, payload in rows:
        for trade in (payload or {}).get("trades", []):
            if trade.get("status") == "rejected_by_risk":
                continue
            judge, agree = float(trade["judge_confidence"]), float(trade["agent_agreement"])
            out.append(IdeaSample(as_of, trade["ticker"], trade["action"], int(trade["horizon_days"]),
                                  raw_blend(judge, agree, blend), "A",
                                  {"judge": judge, "agreement": agree}))  # fmt: skip
    return out


def all_samples(
    engine: Engine, session: Session, config: AppConfig, now: datetime, window_days: int
) -> list[IdeaSample]:
    since = now - timedelta(days=window_days)
    own = b_samples(session, since)
    seen = {s.key for s in own}
    theirs = [s for s in a_samples(engine, since, config.judge.blend) if s.key not in seen]
    return sorted([*own, *theirs], key=lambda s: s.as_of)


def benchmarks(
    session: Session, config: AppConfig, tickers: Iterable[str], sector_relative: bool
) -> dict[str, str]:
    """Ticker -> the fund it is measured against."""
    default = config.universe.benchmark
    if not sector_relative:
        return dict.fromkeys(tickers, default)
    sectors = dict(session.execute(
        select(m.UniverseMember.ticker, m.UniverseMember.gics_sector)
        .where(m.UniverseMember.index_name == config.universe.index)
    ).all())  # fmt: skip
    return {t: SECTOR_ETF.get(sectors.get(t) or "", default) for t in tickers}


def resolve(
    session: Session,
    config: AppConfig,
    samples: Sequence[IdeaSample],
    now: datetime,
    *,
    sector_relative: bool,
) -> list[Resolved]:
    """The samples whose horizon is over and whose prices are known, with their result."""
    if not samples:
        return []
    default = config.universe.benchmark
    against = benchmarks(session, config, {s.ticker for s in samples}, sector_relative)
    start = min(new_york_date(s.as_of) for s in samples) - timedelta(days=5)
    bars = _daily_bars(session, {s.ticker for s in samples} | set(against.values()) | {default},
                       start, now)  # fmt: skip
    days = sorted(bars.get(default, {}))  # the market's trading days
    decision_time = config.schedule.score_and_judge
    out: list[Resolved] = []
    for sample in samples:
        first = entry_index(days, sample.as_of, decision_time)
        last = first + sample.horizon_days - 1
        if last >= len(days):
            continue
        entry_day, exit_day = days[first], days[last]
        stock = bars.get(sample.ticker, {})
        fund = bars.get(against[sample.ticker], {})
        if not (entry_day in fund and exit_day in fund):
            fund = bars.get(default, {})
        if entry_day not in stock or exit_day not in stock or stock[entry_day].open <= 0:
            continue
        ret = stock[exit_day].close / stock[entry_day].open - 1
        bench = fund[exit_day].close / fund[entry_day].open - 1
        excess = ret - bench
        worked = excess > 0 if sample.action == "buy" else excess < 0
        out.append(Resolved(sample, excess, worked))
    return out
