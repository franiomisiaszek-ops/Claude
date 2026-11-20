"""Price facts B's switches need: the market's trend and how stocks did against their sector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.agents.snapshot import SECTOR_ETF, Snapshot
from papertrader.db import models as m


@dataclass(frozen=True)
class Trend:
    ticker: str
    close: float
    average: float
    days: int

    @property
    def above(self) -> bool:
        return self.close >= self.average

    @property
    def label(self) -> str:
        return "above" if self.above else "below"


def trend(session: Session, ticker: str, as_of: datetime, days: int = 200) -> Trend | None:
    """The last close against the average of the last `days` closes known at `as_of`."""
    closes = session.scalars(
        select(m.PriceBar.close)
        .where(m.PriceBar.ticker == ticker, m.PriceBar.timeframe == "1Day",
               m.PriceBar.available_at <= as_of)
        .order_by(m.PriceBar.ts.desc())
        .limit(days)
    ).all()  # fmt: skip
    if len(closes) < days:
        return None
    return Trend(ticker, float(closes[0]), float(sum(closes)) / days, days)


def sector_fund(snap: Snapshot, ticker: str) -> str | None:
    member = snap.members.get(ticker)
    return SECTOR_ETF.get(member.sector or "") if member else None


def relative_lines(snap: Snapshot, tickers: list[str]) -> list[str]:
    """One line per stock: its 5- and 20-day return minus its sector fund's."""
    lines = []
    for ticker in tickers:
        fund = sector_fund(snap, ticker)
        stock, etf = snap.indicators.get(ticker), snap.indicators.get(fund or "")
        if stock is None or etf is None:
            continue
        parts = []
        for label, a, b in (
            ("5 days", stock.ret_5d, etf.ret_5d),
            ("20 days", stock.ret_20d, etf.ret_20d),
        ):
            if a is not None and b is not None:
                parts.append(f"{label} {a - b:+.1%}")
        if parts:
            lines.append(f"- {ticker} vs {fund}: " + ", ".join(parts))
    return lines
