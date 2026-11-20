"""The insider cluster backtest: would switch 5 have helped in the past?

It replays the rule over the insider filings already in the database (Form 4, about the
last two years) with no look-ahead: a cluster counts from the moment the filing that
completes it became public, and the trade starts at the next decision time's open.
For each cluster it measures the stock against its sector fund (and against SPY) over
5, 20 and 60 trading days.

The rule's numbers come from the research, not from this data, so nothing is tuned
here. The verdict rule was fixed before the first run:

    switch it on only if the LATER half of the clusters (by date) shows a positive
    20-day return against the sector fund, with the lower end of a 90% bootstrap
    interval above zero, and there are at least 30 clusters in that half.

Clusters in the same month are resampled together (they share the same market), so the
interval is not made to look narrower than it is.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.b.models import Backtest
from papertrader.b.outcomes import IdeaSample, Resolved, resolve
from papertrader.config import AppConfig, InsiderSwitch
from papertrader.db import models as m

HORIZONS = (5, 20, 60)
MIN_LATER = 30
BOOTSTRAPS = 2000


@dataclass(frozen=True)
class Event:
    ticker: str
    at: datetime  # when the completing filing became public
    buyers: int
    bought_usd: float


def find_events(session: Session, cfg: InsiderSwitch, until: datetime) -> list[Event]:
    rows = session.scalars(
        select(m.InsiderTransaction)
        .where(m.InsiderTransaction.transaction_code.in_(("P", "S")),
               m.InsiderTransaction.available_at <= until)
        .order_by(m.InsiderTransaction.ticker, m.InsiderTransaction.available_at)
    ).all()  # fmt: skip
    by_ticker: dict[str, list[m.InsiderTransaction]] = defaultdict(list)
    for row in rows:
        if not row.under_10b5_1_plan:
            by_ticker[row.ticker].append(row)
    events: list[Event] = []
    window = timedelta(days=cfg.window_days)
    for ticker, trades in by_ticker.items():
        last_event: datetime | None = None
        for row in trades:
            if row.transaction_code != "P" or not row.acquired:
                continue
            at = row.available_at
            if last_event is not None and at - last_event < window:
                continue  # one event per cluster
            since = (at - window).date()
            known = [t for t in trades if t.available_at <= at and t.transaction_date >= since]
            buys = [t for t in known if t.transaction_code == "P" and t.acquired]
            buyers = {t.owner_cik or t.insider_name for t in buys}
            bought = sum(t.shares * (t.price or 0.0) for t in buys)
            sold = sum(
                t.shares * (t.price or 0.0)
                for t in known
                if t.transaction_code == "S" and not t.acquired
            )
            if len(buyers) >= cfg.min_buyers and bought >= cfg.min_usd and bought > sold:
                events.append(Event(ticker, at, len(buyers), bought))
                last_event = at
    return sorted(events, key=lambda e: e.at)


def _block_bootstrap(results: Sequence[Resolved], seed: int = 7) -> tuple[float, float] | None:
    if len(results) < 5:
        return None
    months: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in results:
        months[(r.sample.as_of.year, r.sample.as_of.month)].append(r.excess)
    blocks = list(months.values())
    rng = random.Random(seed)  # noqa: S311 - statistics, not security: a fixed seed on purpose
    means = []
    for _ in range(BOOTSTRAPS):
        picked = [x for _ in blocks for x in rng.choice(blocks)]
        means.append(mean(picked))
    means.sort()
    return means[int(0.05 * BOOTSTRAPS)], means[int(0.95 * BOOTSTRAPS) - 1]


def _stats(results: Sequence[Resolved]) -> dict[str, Any]:
    if not results:
        return {"n": 0}
    excess = [r.excess for r in results]
    ci = _block_bootstrap(results)
    return {"n": len(results), "mean_excess": round(mean(excess), 5),
            "hit_rate": round(sum(1 for x in excess if x > 0) / len(excess), 3),
            "ci90": [round(ci[0], 5), round(ci[1], 5)] if ci else None}  # fmt: skip


def run(session: Session, config: AppConfig, now: datetime) -> Backtest:
    cfg = config.decision.insiders
    events = find_events(session, cfg, now)
    result: dict[str, Any] = {"events": len(events)}
    if events:
        result["first"], result["last"] = (
            events[0].at.date().isoformat(),
            events[-1].at.date().isoformat(),
        )
    split = len(events) // 2
    halves = {"earlier": events[:split], "later": events[split:], "all": events}
    for horizon in HORIZONS:
        for benchmark, sector in (("sector", True), ("spy", False)):
            for half, chosen in halves.items():
                samples = [
                    IdeaSample(e.at, e.ticker, "buy", horizon, 1.0, "backtest") for e in chosen
                ]
                resolved = resolve(session, config, samples, now, sector_relative=sector)
                result[f"{half}_{horizon}d_{benchmark}"] = _stats(resolved)
    later = result.get("later_20d_sector", {"n": 0})
    ci = later.get("ci90")
    if later["n"] < MIN_LATER:
        verdict = (f"keep OFF: only {later['n']} clusters in the later half with a known 20-day result "
                   f"(the rule needs {MIN_LATER}). Run again when there is more history.")  # fmt: skip
    elif ci and ci[0] > 0 and later["mean_excess"] > 0:
        verdict = (f"switch ON: in the later half, {later['n']} clusters beat their sector fund by "
                   f"{later['mean_excess']:+.2%} on average over 20 days (90% interval "
                   f"{ci[0]:+.2%} to {ci[1]:+.2%}).")  # fmt: skip
    else:
        shown = f"{later['mean_excess']:+.2%}" if "mean_excess" in later else "n/a"
        verdict = (f"keep OFF: in the later half the 20-day result against the sector was {shown}, "
                   "and the interval includes zero or below: no reliable edge.")  # fmt: skip
    row = Backtest(
        kind="insider_clusters", ran_at=now, params=cfg.model_dump(), result=result, verdict=verdict
    )
    session.add(row)
    session.flush()
    return row
