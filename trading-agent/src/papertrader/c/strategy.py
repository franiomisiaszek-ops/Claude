"""System C's rules, as plain functions of daily prices. The backtest and the live system
use exactly these, so what is tested is what trades.

Every week, at Friday's close:

1. A fund is **eligible** when most of its 1-, 3-, 6- and 12-month returns are positive
   (3 of 4 by default) and its price is above its 200-day average. With upgrade 2 (dual
   momentum) its 12-month return must also beat T-bills.
2. The eligible funds are ranked by **momentum** (the average of their 3-, 6- and
   12-month returns) and the best `top_n` (8) are kept.
3. **Weights**: inversely to each fund's volatility (calmer funds get more), or, with
   upgrade 3, by risk parity (every fund adds the same risk, counting correlations).
4. **Caps**: at most 20% per fund (5% for oil), 60% in stocks, 30% in commodities and
   gold. Whatever a cap cuts off stays in cash; nothing is pushed into the other funds.
5. **Volatility target**: the whole portfolio is scaled down so it is expected to swing
   about 10% a year. It is never scaled up: no borrowing.
6. **Drawdown control**: 10% below the portfolio's peak, half the risk; 15% below, all
   out (with an alert), for `flat_weeks`, then a fresh start.
7. The rest is cash, or with upgrade 1 a T-bill fund (SGOV).

Trades happen only when a fund's weight is more than 20% (relatively) away from its
target, so small wiggles cost nothing.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import pandas as pd

from papertrader.config import PortfolioConfig

TRADING_DAYS = 252


@dataclass
class Target:
    weights: dict[str, float]  # funds to hold (the T-bill fund included with upgrade 1)
    cash: float  # left as plain cash
    scale: float  # the volatility target's scaling (1 = none)
    state: str  # normal | halved | flat
    vol: float | None  # expected yearly volatility after scaling
    reasons: dict[str, str] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)


def series(closes: pd.DataFrame, ticker: str) -> pd.Series:
    return closes[ticker].dropna() if ticker in closes else pd.Series(dtype=float)


def needed_days(cfg: PortfolioConfig) -> int:
    return max([*cfg.check_days, *cfg.score_days, cfg.trend_days, cfg.vol_days]) + 1


def candidates(closes: pd.DataFrame, cfg: PortfolioConfig) -> list[str]:
    """Funds that may be chosen now: enough history, not the cash fund, allowed by the upgrades."""
    out = []
    for fund in cfg.funds:
        if fund.ticker == cfg.cash_fund or fund.group == "cash":
            continue
        if fund.group == "managed_futures" and not cfg.upgrades.managed_futures:
            continue
        s = series(closes, fund.ticker)
        if len(s) >= needed_days(cfg) and s.index[-1] == closes.index[-1]:
            out.append(fund.ticker)
    return out


def ret(s: pd.Series, days: int) -> float:
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1)


def momentum(
    closes: pd.DataFrame, cfg: PortfolioConfig, tbill_12m: float | None
) -> tuple[list[str], dict[str, float], dict[str, str]]:
    """(eligible funds, momentum scores, a reason for every candidate)."""
    eligible, scores, reasons = [], {}, {}
    for ticker in candidates(closes, cfg):
        s = series(closes, ticker)
        checks = [ret(s, d) for d in cfg.check_days]
        positive = sum(1 for r in checks if r > 0)
        average = float(s.iloc[-cfg.trend_days :].mean())
        score = sum(ret(s, d) for d in cfg.score_days) / len(cfg.score_days)
        scores[ticker] = score
        twelve = ret(s, 252) if len(s) > 252 else None
        if positive < cfg.min_positive:
            reasons[ticker] = f"out: only {positive} of {len(checks)} returns positive"
        elif s.iloc[-1] <= average:
            reasons[ticker] = f"out: below its {cfg.trend_days}-day average"
        elif (
            cfg.upgrades.dual_momentum
            and tbill_12m is not None
            and twelve is not None
            and twelve <= tbill_12m
        ):
            reasons[ticker] = (
                f"out: 12-month return {twelve:+.1%} does not beat T-bills ({tbill_12m:+.1%})"
            )
        else:
            eligible.append(ticker)
            reasons[ticker] = (
                f"eligible: momentum {score:+.1%}, {positive} of {len(checks)} returns positive"
            )
    return eligible, scores, reasons


def daily_returns(closes: pd.DataFrame, tickers: list[str], days: int) -> pd.DataFrame:
    return closes[tickers].iloc[-(days + 1) :].pct_change().iloc[1:].dropna(how="any")


def inverse_vol(rets: pd.DataFrame) -> dict[str, float]:
    vols = rets.std()
    inverse = {t: 1 / v for t, v in vols.items() if v > 0}
    total = sum(inverse.values())
    return {t: w / total for t, w in inverse.items()} if total else {}


def risk_parity(rets: pd.DataFrame, iterations: int = 500) -> dict[str, float]:
    """Weights where every fund adds the same share of the portfolio's risk."""
    cov = rets.cov().to_numpy()
    n = cov.shape[0]
    if n == 0:
        return {}
    w = np.full(n, 1 / n)
    for _ in range(iterations):
        marginal = cov @ w
        contrib = w * marginal
        total = contrib.sum()
        if total <= 0:
            break
        w = w * np.sqrt((total / n) / np.maximum(contrib, 1e-18))
        w = w / w.sum()
    return {t: float(x) for t, x in zip(rets.columns, w, strict=True)}


def apply_caps(weights: Mapping[str, float], cfg: PortfolioConfig) -> dict[str, float]:
    """Per-fund caps, then group caps by scaling the group down. What is cut stays in cash."""
    funds = cfg.by_ticker()
    out = {t: min(w, funds[t].cap) for t, w in weights.items()}
    for group, cap in cfg.group_caps.items():
        members = [t for t in out if funds[t].group == group]
        total = sum(out[t] for t in members)
        if total > cap > 0:
            for t in members:
                out[t] *= cap / total
    return out


def portfolio_vol(weights: Mapping[str, float], rets: pd.DataFrame) -> float | None:
    tickers = [t for t in weights if t in rets.columns]
    if not tickers:
        return None
    w = np.array([weights[t] for t in tickers])
    cov = rets[tickers].cov().to_numpy() * TRADING_DAYS
    return float(math.sqrt(max(float(w @ cov @ w), 0.0)))


def target_weights(closes: pd.DataFrame, cfg: PortfolioConfig, *, tbill_12m: float | None = None,
                   state: str = "normal") -> Target:  # fmt: skip
    eligible, scores, reasons = momentum(closes, cfg, tbill_12m)
    chosen = sorted(eligible, key=lambda t: -scores[t])[: cfg.top_n]
    for ticker in eligible:
        if ticker not in chosen:
            reasons[ticker] = (
                f"out: momentum {scores[ticker]:+.1%} is not among the best {cfg.top_n}"
            )
    weights: dict[str, float] = {}
    scale, vol = 1.0, None
    if chosen:
        rets = daily_returns(closes, chosen, cfg.vol_days)
        raw = risk_parity(rets) if cfg.upgrades.risk_parity else inverse_vol(rets)
        capped = apply_caps(raw, cfg)
        expected = portfolio_vol(capped, rets)
        if expected and expected > cfg.vol_target:
            scale = cfg.vol_target / expected
        weights = {t: w * scale for t, w in capped.items()}
        vol = expected * scale if expected else None
        for ticker in chosen:
            reasons[ticker] = f"in: {reasons[ticker].removeprefix('eligible: ')}"
    if state == "halved":
        weights = {t: w / 2 for t, w in weights.items()}
        vol = vol / 2 if vol else None
    elif state == "flat":
        weights, vol = {}, 0.0
    weights = {t: round(w, 4) for t, w in weights.items() if w >= 1e-4}
    cash = max(0.0, 1.0 - sum(weights.values()))
    if (
        cfg.upgrades.cash_in_tbills
        and cash > 0
        and cfg.cash_fund in closes
        and not math.isnan(closes[cfg.cash_fund].iloc[-1])
    ):
        weights[cfg.cash_fund] = round(cash, 4)
        cash = 0.0
    return Target(weights, round(cash, 4), round(scale, 4), state, vol, reasons, scores)


# --------------------------------------------------------------------------- drawdown control


def drawdown_state(state: str, peak: float, equity: float, flat_since: date | None, today: date,
                   cfg: PortfolioConfig) -> tuple[str, float, date | None, float]:  # fmt: skip
    """(new state, new peak, flat since, drawdown) after seeing today's equity."""
    if state == "flat" and flat_since is not None:
        if today - flat_since >= timedelta(weeks=cfg.flat_weeks):
            return "normal", equity, None, 0.0  # a fresh start from here
        return "flat", peak, flat_since, equity / peak - 1 if peak else 0.0
    peak = max(peak, equity)
    drawdown = equity / peak - 1 if peak else 0.0
    if drawdown <= -cfg.drawdown_flat:
        return "flat", peak, today, drawdown
    if drawdown <= -cfg.drawdown_halve:
        return "halved", peak, None, drawdown
    return "normal", peak, None, drawdown


# --------------------------------------------------------------------------- orders


@dataclass(frozen=True)
class OrderPlan:
    ticker: str
    side: str  # buy | sell
    qty: float
    limit: float
    weight_now: float
    weight_target: float


def rebalance_orders(targets: Mapping[str, float], holdings: Mapping[str, float], prices: Mapping[str, float],
                     equity: float, cfg: PortfolioConfig) -> list[OrderPlan]:  # fmt: skip
    """Orders that bring the funds that drifted too far back to target. Sells come first."""
    fractional = cfg.upgrades.fractional
    plans: list[OrderPlan] = []
    for ticker in sorted(set(targets) | {t for t, q in holdings.items() if q}):
        price = prices.get(ticker)
        if not price or equity <= 0:
            continue
        qty = holdings.get(ticker, 0.0)
        now, goal = qty * price / equity, targets.get(ticker, 0.0)
        if goal <= 0 and qty > 0:
            delta = -qty
        elif goal >= cfg.min_trade_weight and (qty == 0 or abs(now - goal) / goal > cfg.drift_band):
            delta = goal * equity / price - qty
        else:
            continue
        if fractional:
            amount = round(abs(delta), 3)
        else:
            amount = math.floor(abs(delta)) if delta > 0 else min(qty, round(abs(delta)))
        if amount <= 0:
            continue
        side = "buy" if delta > 0 else "sell"
        slip = cfg.limit_slippage_pct
        limit = round(price * (1 + slip if side == "buy" else 1 - slip), 2)
        plans.append(OrderPlan(ticker, side, amount, limit, round(now, 4), round(goal, 4)))
    return sorted(plans, key=lambda p: p.side != "sell")
