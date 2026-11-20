"""B's extra signals.

Insider cluster buying (switch 5): several different insiders buying their own company's
shares on the open market within a few weeks. Research (Lakonishok and Lee 2001; Cohen,
Malloy and Pomorski 2012) found such clusters were followed by better-than-market returns,
while routine or pre-scheduled trades told little. The rule here:

    at least `min_buyers` different insiders made open-market purchases (Form 4 code P,
    not under a pre-arranged 10b5-1 plan) in the last `window_days` days, together worth
    at least `min_usd`, and more was bought than sold

It only uses filings already public at the decision time (`available_at`).

Options and analyst signals (switches 8 and 9) are fetched by the 08:00 job from paid
services and stored in `b_signals`; this module reads them back for the judge.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.b.models import Signal
from papertrader.config import InsiderSwitch
from papertrader.db import models as m


@dataclass
class Cluster:
    ticker: str
    buyers: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    bought_usd: float = 0.0
    sold_usd: float = 0.0
    first: date | None = None
    last: date | None = None

    def summary(self, window_days: int) -> str:
        roles = f" ({', '.join(sorted(self.roles))})" if self.roles else ""
        sold = f"; sold ${self.sold_usd:,.0f}" if self.sold_usd else "; no open-market sales"
        return (f"{len(self.buyers)} insiders{roles} bought ${self.bought_usd:,.0f} of shares "
                f"on the open market in the last {window_days} days{sold}")  # fmt: skip


def _role(row: m.InsiderTransaction) -> str | None:
    if row.insider_title:
        return row.insider_title.strip()[:40]
    if row.is_officer:
        return "officer"
    if row.is_director:
        return "director"
    return None


def insider_clusters(session: Session, as_of: datetime, cfg: InsiderSwitch,
                     tickers: Iterable[str] | None = None) -> dict[str, Cluster]:  # fmt: skip
    """Clusters known at `as_of`, keyed by ticker."""
    since = (as_of - timedelta(days=cfg.window_days)).date()
    query = select(m.InsiderTransaction).where(
        m.InsiderTransaction.available_at <= as_of,
        m.InsiderTransaction.transaction_date >= since,
        m.InsiderTransaction.transaction_code.in_(("P", "S")),
    )
    if tickers is not None:
        query = query.where(m.InsiderTransaction.ticker.in_(sorted(set(tickers))))
    found: dict[str, Cluster] = defaultdict(lambda: Cluster(""))
    for row in session.scalars(query):
        if row.under_10b5_1_plan:
            continue  # pre-scheduled: says little about what the insider expects
        cluster = found[row.ticker]
        cluster.ticker = row.ticker
        value = row.shares * (row.price or 0.0)
        if row.transaction_code == "P" and row.acquired:
            cluster.buyers.add(row.owner_cik or row.insider_name)
            cluster.bought_usd += value
            if (role := _role(row)) is not None:
                cluster.roles.add(role)
            cluster.first = min(cluster.first or row.transaction_date, row.transaction_date)
            cluster.last = max(cluster.last or row.transaction_date, row.transaction_date)
        elif row.transaction_code == "S" and not row.acquired:
            cluster.sold_usd += value
    return {
        t: c for t, c in found.items()
        if len(c.buyers) >= cfg.min_buyers and c.bought_usd >= cfg.min_usd and c.bought_usd > c.sold_usd
    }  # fmt: skip


def stored(session: Session, day: date, kinds: Iterable[str]) -> dict[str, list[Signal]]:
    """Signals the 08:00 job stored for `day`, by ticker."""
    out: dict[str, list[Signal]] = defaultdict(list)
    for row in session.scalars(
        select(Signal).where(Signal.day == day, Signal.kind.in_(sorted(set(kinds))))
        .order_by(Signal.ticker, Signal.kind)
    ):  # fmt: skip
        out[row.ticker].append(row)
    return dict(out)
