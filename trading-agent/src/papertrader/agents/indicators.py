"""Technical indicators computed from daily bars. Plain arithmetic, no chart images."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise


@dataclass(frozen=True)
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Indicators:
    last_day: date
    last_close: float
    bars: int
    ret_1d: float | None
    ret_5d: float | None
    ret_20d: float | None
    ret_60d: float | None
    rsi14: float | None
    volume_ratio: float | None  # latest volume / average of the 20 days before it
    gap_pct: float | None  # latest open vs the previous close
    from_52w_high: float | None  # 0 = at the high, -0.10 = 10% below it
    from_52w_low: float | None
    atr_pct: float | None  # average true range (14) as a share of price: daily volatility
    above_sma50: bool | None


def _ret(closes: Sequence[float], days: int) -> float | None:
    if len(closes) <= days or closes[-1 - days] <= 0:
        return None
    return closes[-1] / closes[-1 - days] - 1


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder's RSI. 70+ is usually read as overbought, 30- as oversold."""
    if len(closes) <= period:
        return None
    changes = [b - a for a, b in pairwise(closes)]
    gain = sum(max(c, 0.0) for c in changes[:period]) / period
    loss = sum(max(-c, 0.0) for c in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0.0)) / period
        loss = (loss * (period - 1) + max(-change, 0.0)) / period
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def atr_pct(bars: Sequence[Bar], period: int = 14) -> float | None:
    if len(bars) <= period:
        return None
    ranges = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(bars[-period - 1 :], bars[-period:], strict=False)
    ]
    return sum(ranges) / period / bars[-1].close if bars[-1].close else None


def compute(bars: Sequence[Bar]) -> Indicators | None:
    """Indicators as of the last bar given. `bars` must be oldest first."""
    if len(bars) < 2:
        return None
    closes = [bar.close for bar in bars]
    last, prev = bars[-1], bars[-2]
    year = bars[-252:]
    high, low = max(bar.high for bar in year), min(bar.low for bar in year)
    earlier_volume = [bar.volume for bar in bars[-21:-1]]
    average_volume = sum(earlier_volume) / len(earlier_volume) if earlier_volume else 0.0
    return Indicators(
        last_day=last.day,
        last_close=last.close,
        bars=len(bars),
        ret_1d=_ret(closes, 1),
        ret_5d=_ret(closes, 5),
        ret_20d=_ret(closes, 20),
        ret_60d=_ret(closes, 60),
        rsi14=rsi(closes),
        volume_ratio=last.volume / average_volume if average_volume > 0 else None,
        gap_pct=last.open / prev.close - 1 if prev.close > 0 else None,
        from_52w_high=last.close / high - 1 if high > 0 else None,
        from_52w_low=last.close / low - 1 if low > 0 else None,
        atr_pct=atr_pct(bars),
        above_sma50=last.close > sum(closes[-50:]) / 50 if len(closes) >= 50 else None,
    )


def unusual_score(ind: Indicators) -> float:
    """How much is going on technically. Used only to choose which stocks to show."""
    score = 0.0
    if ind.volume_ratio is not None:
        score += max(ind.volume_ratio - 1.5, 0.0)
    if ind.gap_pct is not None:
        score += abs(ind.gap_pct) * 25
    if ind.ret_5d is not None:
        score += abs(ind.ret_5d) * 8
    if ind.rsi14 is not None:
        score += max(abs(ind.rsi14 - 50) - 20, 0.0) / 10
    if ind.from_52w_high is not None and ind.from_52w_high > -0.01:
        score += 1.0
    if ind.from_52w_low is not None and ind.from_52w_low < 0.02:
        score += 1.0
    return score
