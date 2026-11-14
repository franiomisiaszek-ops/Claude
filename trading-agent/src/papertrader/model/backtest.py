"""A simple portfolio simulation of the model's out-of-sample picks, with baselines.

The strategy mirrors the live rules as far as daily bars allow: buy at the open of the
entry day, hold for the horizon, sell at the close; a fixed share of equity per
position; a cap on open positions; no leverage; costs and slippage on every trade.

It is compared with (a) holding SPY over the same days and (b) picking at random from
the same candidate stocks on the same days. If the model cannot beat random picks, it
has learned nothing, whatever its return looks like.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from papertrader.config import PatternModelConfig, RiskConfig

TRADING_DAYS = 252
RANDOM_RUNS = 200


@dataclass
class Path:
    """Daily close/open prices as arrays, for fast lookups."""

    days: list[date]
    index: dict[date, int]
    opens: dict[str, np.ndarray]
    closes: dict[str, np.ndarray]


def price_paths(bars: pd.DataFrame, benchmark: str) -> Path:
    days = sorted(bars.loc[bars["ticker"] == benchmark, "day"].unique())
    index = {day: i for i, day in enumerate(days)}
    opens, closes = {}, {}
    for ticker, frame in bars.groupby("ticker", sort=False):
        aligned = frame.set_index("day").reindex(days)
        opens[ticker] = aligned["open"].to_numpy(dtype=float)
        closes[ticker] = aligned["close"].to_numpy(dtype=float)
    return Path(days, index, opens, closes)


def simulate(
    picks: pd.DataFrame,
    paths: Path,
    *,
    horizon: int,
    weight: float,
    max_positions: int,
    cost_per_side: float,
    first_day: date,
    last_day: date,
) -> pd.Series:
    """Daily portfolio returns from `picks` (columns: day, ticker), in day order."""
    start, end = paths.index[first_day], paths.index[last_day]
    returns = np.zeros(end - start + 1)
    open_until: list[int] = []  # exit index of each open position
    traded = 0

    by_day = {day: group["ticker"].tolist() for day, group in picks.groupby("day")}
    for i in range(start, end + 1):
        open_until = [exit_i for exit_i in open_until if exit_i >= i]
        for ticker in by_day.get(paths.days[i], []):
            exit_i = i + horizon - 1
            if len(open_until) >= max_positions or exit_i > end or ticker not in paths.opens:
                continue
            opens, closes = paths.opens[ticker], paths.closes[ticker]
            if np.isnan(opens[i]) or np.isnan(closes[i : exit_i + 1]).any():
                continue
            # Day by day: open->close on the entry day, then close->close.
            previous = opens[i]
            for j in range(i, exit_i + 1):
                returns[j - start] += weight * (closes[j] / previous - 1)
                previous = closes[j]
            returns[i - start] -= weight * cost_per_side
            returns[exit_i - start] -= weight * cost_per_side
            open_until.append(exit_i)
            traded += 1
    series = pd.Series(returns, index=paths.days[start : end + 1])
    series.attrs["trades"] = traded
    return series


def stats(returns: pd.Series, *, weight: float = 0.0) -> dict[str, Any]:
    if returns.empty:
        return {}
    equity = (1 + returns).cumprod()
    years = len(returns) / TRADING_DAYS
    drawdown = equity / equity.cummax() - 1
    volatility = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    trades = int(returns.attrs.get("trades", 0))
    return {
        "total_return": float(equity.iloc[-1] - 1),
        "annual_return": float(equity.iloc[-1] ** (1 / years) - 1) if years > 0 else 0.0,
        "sharpe": float(returns.mean() / volatility * np.sqrt(TRADING_DAYS))
        if volatility > 0
        else 0.0,
        "max_drawdown": float(drawdown.min()),
        "trades": trades,
        # Share of the account bought and sold per year (each trade is bought once, sold once).
        "turnover": float(2 * weight * trades / years) if years > 0 else 0.0,
        "days": len(returns),
    }


def select(scored: pd.DataFrame, *, top_n: int, threshold: float) -> pd.DataFrame:
    """Each day: the highest-probability candidates at or above the threshold."""
    chosen = scored[scored["prob"] >= threshold].sort_values(
        ["day", "prob"], ascending=[True, False]
    )
    return chosen.groupby("day").head(top_n)[["day", "ticker"]]


def run_backtest(
    scored: pd.DataFrame,
    bars: pd.DataFrame,
    *,
    benchmark: str,
    cfg: PatternModelConfig,
    risk: RiskConfig,
    horizon: int,
    threshold: float,
    top_n: int,
) -> dict[str, Any]:
    """Strategy vs SPY buy-and-hold vs random picks, over the out-of-sample period."""
    if scored.empty:
        return {}
    paths = price_paths(bars, benchmark)
    first, last = scored["day"].min(), scored["day"].max()
    cost = (cfg.transaction_cost_bps + cfg.slippage_bps) / 10_000
    common = {
        "horizon": horizon, "weight": risk.max_position_pct,
        "max_positions": risk.max_open_positions, "cost_per_side": cost,
        "first_day": first, "last_day": last,
    }  # fmt: skip

    picks = select(scored, top_n=top_n, threshold=threshold)
    strategy = simulate(picks, paths, **common)

    start, end = paths.index[first], paths.index[last]
    closes, opens = paths.closes[benchmark], paths.opens[benchmark]
    spy_returns = pd.Series(
        [closes[i] / (closes[i - 1] if i > 0 else opens[i]) - 1 for i in range(start, end + 1)],
        index=paths.days[start : end + 1],
    )

    # Random baseline: the same NUMBER of picks on the same days, from the same candidates.
    rng = np.random.default_rng(11)
    per_day = picks.groupby("day").size().to_dict()
    candidates = {day: group["ticker"].to_numpy() for day, group in scored.groupby("day")}
    random_stats = []
    for _ in range(RANDOM_RUNS):
        rows = [
            (day, ticker)
            for day, count in per_day.items()
            for ticker in rng.choice(
                candidates[day], size=min(count, len(candidates[day])), replace=False
            )
        ]
        random_picks = pd.DataFrame(rows, columns=["day", "ticker"])
        random_stats.append(
            stats(simulate(random_picks, paths, **common), weight=risk.max_position_pct)
        )
    totals = (
        np.array([s["total_return"] for s in random_stats]) if random_stats else np.array([0.0])
    )
    sharpes = np.array([s["sharpe"] for s in random_stats]) if random_stats else np.array([0.0])

    result = stats(strategy, weight=risk.max_position_pct)
    return {
        "from": str(first),
        "to": str(last),
        "threshold": threshold,
        "top_n": top_n,
        "cost_bps_per_side": cfg.transaction_cost_bps + cfg.slippage_bps,
        "strategy": result,
        "benchmark": stats(spy_returns),
        "random": {
            "runs": RANDOM_RUNS,
            "total_return_mean": float(totals.mean()),
            "total_return_p05": float(np.quantile(totals, 0.05)),
            "total_return_p95": float(np.quantile(totals, 0.95)),
            "sharpe_mean": float(sharpes.mean()),
            # Share of random runs the strategy did better than: 0.5 = no better than chance.
            "strategy_percentile": float((totals < result.get("total_return", 0.0)).mean()),
        },
        "candidate_days": int(scored["day"].nunique()),
        "picks": len(picks),
    }
