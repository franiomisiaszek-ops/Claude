"""Synthetic prices for system C's tests: steady trends, so the right answers are known."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from papertrader.config import CUpgrades, FundSpec, PortfolioConfig

FUNDS = (
    FundSpec(ticker="SPY", group="equity"), FundSpec(ticker="QQQ", group="equity"),
    FundSpec(ticker="EEM", group="equity"), FundSpec(ticker="IEF", group="bonds"),
    FundSpec(ticker="TLT", group="bonds"), FundSpec(ticker="GLD", group="commodity"),
    FundSpec(ticker="SLV", group="commodity"), FundSpec(ticker="USO", group="commodity", cap=0.05),
    FundSpec(ticker="SGOV", group="cash", cap=1.0, stand_in="BIL"),
)  # fmt: skip
# yearly drift and volatility of each fund in the synthetic market
DRIFT = {"SPY": 0.12, "QQQ": 0.18, "EEM": -0.10, "IEF": 0.03, "TLT": -0.06, "GLD": 0.15, "SLV": 0.10,
         "USO": 0.20, "BIL": 0.03, "SGOV": 0.04}  # fmt: skip
VOL = {"SPY": 0.15, "QQQ": 0.22, "EEM": 0.20, "IEF": 0.06, "TLT": 0.14, "GLD": 0.15, "SLV": 0.25,
       "USO": 0.35, "BIL": 0.002, "SGOV": 0.002}  # fmt: skip


def portfolio(**changes: object) -> PortfolioConfig:
    """The test universe; nested settings are merged and checked, e.g. veto={"enabled": True}."""
    data = PortfolioConfig(funds=FUNDS, upgrades=CUpgrades()).model_dump()
    for key, value in changes.items():
        data[key] = (
            data[key] | value
            if isinstance(value, dict) and isinstance(data.get(key), dict)
            else value
        )
    return PortfolioConfig.model_validate(data)


def business_days(first: date, n: int) -> list[date]:
    out, day = [], first
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def market(
    n: int = 700,
    first: date = date(2022, 1, 3),
    seed: int = 7,
    starts: dict[str, int] | None = None,
) -> dict[str, pd.DataFrame]:
    """open/high/low/close frames; `starts` lets a fund begin later (NaN before)."""
    rng = np.random.default_rng(seed)
    days = business_days(first, n)
    closes = {}
    for t in DRIFT:
        steps = rng.normal(DRIFT[t] / 252, VOL[t] / np.sqrt(252), n)
        closes[t] = 100 * np.exp(np.cumsum(steps))
    close = pd.DataFrame(closes, index=days)
    for t, k in (starts or {}).items():
        close.loc[close.index[:k], t] = np.nan
    open_ = close.shift(1).fillna(close) * (1 + rng.normal(0, 0.001, close.shape))
    return {
        "close": close,
        "open": open_.where(close.notna()),
        "high": close * 1.01,
        "low": close * 0.99,
    }


def market_until(end: date, n: int = 700, seed: int = 7) -> dict[str, pd.DataFrame]:
    """The same synthetic market, ending on `end`."""
    data = market(n=n, seed=seed)
    days = list(pd.bdate_range(end=end, periods=n).date)
    return {k: v.set_axis(days) for k, v in data.items()}


def as_rows(data: dict[str, pd.DataFrame]) -> list[dict[str, object]]:
    rows = []
    close = data["close"]
    for ticker in close.columns:
        for day in close.index:
            c = close.at[day, ticker]
            if pd.isna(c):
                continue
            rows.append({"ticker": ticker, "day": day, "open": float(data["open"].at[day, ticker]),
                         "high": float(data["high"].at[day, ticker]), "low": float(data["low"].at[day, ticker]),
                         "close": float(c), "volume": 1e6})  # fmt: skip
    return rows
