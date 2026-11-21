"""C's daily chart trades: a separate part of C with its own switch (off until the
backtest says yes, then shadow first, and marked "not for copying").

Two rules on C's own funds, checked after each close, only in an uptrend (price above
its 200-day average):

    breakout  the close is above the highest high of the previous `breakout_days` days
    dip       the 2-day RSI is below `dip_rsi` (a sharp short-term drop)

A signal buys at the next open; the stop is `stop_atr` average daily ranges below, the
target `target_atr` above; after `hold_days` days the trade is closed at the close. If
one day touches both the stop and the target, the stop is assumed (the cautious choice).
At most `max_share` / `per_trade` trades are open at once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from papertrader.config import PortfolioConfig


@dataclass(frozen=True)
class Signal:
    day: date
    ticker: str
    rule: str
    reference: float  # the signal day's close
    stop: float
    target: float


@dataclass(frozen=True)
class Outcome:
    entry_day: date
    entry: float
    exit_day: date
    exit: float
    reason: str  # stop | target | time
    net: float


def rsi2(closes: pd.Series) -> float | None:
    if len(closes) < 4:
        return None
    changes = closes.diff().dropna()
    gain = changes.clip(lower=0).ewm(alpha=1 / 2, adjust=False).mean().iloc[-1]
    loss = (-changes.clip(upper=0)).ewm(alpha=1 / 2, adjust=False).mean().iloc[-1]
    if loss == 0:
        return 100.0
    return float(100 - 100 / (1 + gain / loss))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, days: int = 14) -> float | None:
    frame = pd.concat([high, low, close], axis=1, keys=["h", "l", "c"]).dropna()
    if len(frame) <= days:
        return None
    prev = frame["c"].shift(1)
    ranges = pd.concat(
        [frame["h"] - frame["l"], (frame["h"] - prev).abs(), (frame["l"] - prev).abs()], axis=1
    ).max(axis=1)
    return float(ranges.iloc[-days:].mean())


def signals(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, cfg: PortfolioConfig
) -> list[Signal]:
    """Today's signals (the last row of the frames)."""
    s_cfg = cfg.sleeve
    out = []
    for fund in cfg.funds:
        t = fund.ticker
        if fund.group in ("cash", "managed_futures") or t not in close:
            continue
        c = close[t].dropna()
        if len(c) <= max(cfg.trend_days, s_cfg.breakout_days) + 1 or c.index[-1] != close.index[-1]:
            continue
        if c.iloc[-1] <= c.iloc[-cfg.trend_days :].mean():
            continue  # uptrends only
        h, lo = high[t].reindex(c.index), low[t].reindex(c.index)
        rule = None
        if c.iloc[-1] > h.iloc[-1 - s_cfg.breakout_days : -1].max():
            rule = "breakout"
        elif (r := rsi2(c.iloc[-30:])) is not None and r < s_cfg.dip_rsi:
            rule = "dip"
        daily_range = atr(h, lo, c)
        if rule is None or not daily_range:
            continue
        ref = float(c.iloc[-1])
        out.append(Signal(c.index[-1], t, rule, ref, round(ref - s_cfg.stop_atr * daily_range, 2),
                          round(ref + s_cfg.target_atr * daily_range, 2)))  # fmt: skip
    return out


def play(signal: Signal, bars: pd.DataFrame, hold_days: int, cost_bps: float) -> Outcome | None:
    """Follow one signal on the days after it. `bars` has open/high/low/close columns."""
    after = bars[bars.index > signal.day].dropna()
    if after.empty:
        return None
    entry_day = after.index[0]
    entry = float(after["open"].iloc[0])
    for i, (day, bar) in enumerate(after.iterrows()):
        if bar["low"] <= signal.stop:
            return _done(
                entry_day,
                entry,
                day,
                min(float(bar["open"]) if i else entry, signal.stop),
                "stop",
                cost_bps,
            )
        if bar["high"] >= signal.target:
            return _done(
                entry_day,
                entry,
                day,
                max(float(bar["open"]) if i else entry, signal.target),
                "target",
                cost_bps,
            )
        if i + 1 >= hold_days:
            return _done(entry_day, entry, day, float(bar["close"]), "time", cost_bps)
    return None  # still running


def _done(
    entry_day: date, entry: float, day: date, price: float, reason: str, cost_bps: float
) -> Outcome:
    return Outcome(entry_day, entry, day, price, reason, price / entry - 1 - 2 * cost_bps / 10_000)
