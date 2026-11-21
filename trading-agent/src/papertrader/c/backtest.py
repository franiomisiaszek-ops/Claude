"""System C's 20-year backtest, run before C trades (docs/EXPERIMENT.md, "Plan for system C").

How it is kept honest:

* The very same rules as live trading (`c/strategy.py`), on prices with dividends.
* Signals at Friday's close, trades at Monday's open, with costs (`cost_bps` per side)
  and the same 20% drift band. Idle cash earns nothing (as at the broker), unless
  upgrade 1 puts it in the T-bill fund.
* **Walk-forward**: the few settings (momentum periods, volatility target, number of
  funds) are chosen on 2004-2015 only, then tested ONCE on 2016 to today, which they
  never saw. 12 combinations only, so the rules cannot memorise the past.
* Upgrades and the daily chart trades are recommended only if they help in BOTH
  periods: testing several ideas on the same unseen years would otherwise let luck pick.
* Compared with holding SPY and with a 60/40 portfolio (SPY / IEF, rebalanced monthly).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from papertrader.c import sleeve as sleeve_rules
from papertrader.c.data import tbill_12m, with_cash_history
from papertrader.c.strategy import drawdown_state, target_weights
from papertrader.config import PortfolioConfig

IN_SAMPLE_START, OUT_OF_SAMPLE_START = date(2004, 1, 1), date(2016, 1, 1)
SCORE_DAYS: list[tuple[int, ...]] = [(63, 126, 252), (126, 252)]
VOL_TARGETS = [0.08, 0.10, 0.12]
TOP_NS = [6, 8]
UPGRADES = ("cash_in_tbills", "dual_momentum", "risk_parity", "managed_futures")


@dataclass(frozen=True)
class Stats:
    start: str
    end: str
    years: float
    total: float
    cagr: float
    vol: float
    sharpe: float
    max_drawdown: float
    worst_year: float
    turnover_per_year: float = 0.0


def stats(
    equity: pd.Series, rf_daily: pd.Series | None = None, turnover: float = 0.0
) -> Stats | None:
    equity = equity.dropna()
    if len(equity) < 30:
        return None
    rets = equity.pct_change().dropna()
    years = len(rets) / 252
    total = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    excess = rets - (rf_daily.reindex(rets.index).fillna(0) if rf_daily is not None else 0)
    sharpe = float(excess.mean() / excess.std() * math.sqrt(252)) if excess.std() > 0 else 0.0
    drawdown = float((equity / equity.cummax() - 1).min())
    yearly = equity.groupby(pd.Index([d.year for d in equity.index])).agg(["first", "last"])
    worst = float((yearly["last"] / yearly["first"] - 1).min())
    return Stats(str(equity.index[0]), str(equity.index[-1]), round(years, 2), round(total, 4), round(cagr, 4),
                 round(float(rets.std() * math.sqrt(252)), 4), round(sharpe, 3), round(drawdown, 4), round(worst, 4),
                 round(turnover / years, 2) if years else 0.0)  # fmt: skip


def week_ends(days: list[date]) -> set[date]:
    """The last trading day of each week (a Friday, or earlier before a holiday)."""
    return {
        d
        for d, nxt in zip(days, [*days[1:], None], strict=True)
        if nxt is None or nxt.isocalendar()[:2] != d.isocalendar()[:2]
    }


def _drifted_targets(
    current: Mapping[str, float], goal: Mapping[str, float], cfg: PortfolioConfig
) -> dict[str, float]:
    """The weights after applying the drift band, in weight terms (the backtest trades fractions)."""
    out = {}
    for t in set(current) | set(goal):
        now, want = current.get(t, 0.0), goal.get(t, 0.0)
        if want <= 0:
            continue  # sold
        if now == 0 and want < cfg.min_trade_weight:
            continue
        out[t] = want if now == 0 or abs(now - want) / want > cfg.drift_band else now
    return out


def run_portfolio(close: pd.DataFrame, open_: pd.DataFrame, cfg: PortfolioConfig, start: date, end: date,
                  tbill: pd.Series | None = None) -> tuple[pd.Series, float]:  # fmt: skip
    """(daily equity from 1.0, total turnover). Uses all history before `start` for signals."""
    close = with_cash_history(close, cfg, tbill)
    open_ = open_.reindex(close.index).copy()
    if cfg.cash_fund in close:
        open_[cfg.cash_fund] = close[cfg.cash_fund]  # a T-bill fund barely moves overnight
    days = [d for d in close.index if d <= end]
    run_days = [d for d in days if d >= start]
    if len(run_days) < 2:
        return pd.Series(dtype=float), 0.0
    ends = week_ends(days)
    values: dict[str, float] = {}
    cash, turnover, cost = 1.0, 0.0, cfg.cost_bps / 10_000
    state, peak, flat_since = "normal", 1.0, None
    pending: dict[str, float] | None = None
    curve = {}
    positions = {d: i for i, d in enumerate(close.index)}
    for k, day in enumerate(run_days):
        i = positions[day]
        if k > 0:
            prev = close.index[i - 1]
            if pending is not None:
                for t in list(values):
                    o, pc = open_.at[day, t], close.at[prev, t]
                    values[t] *= o / pc if pc and not math.isnan(o) and not math.isnan(pc) else 1.0
                equity = cash + sum(values.values())
                current = {t: v / equity for t, v in values.items()}
                new = _drifted_targets(current, pending, cfg)
                traded = sum(
                    abs(new.get(t, 0) - current.get(t, 0)) for t in set(new) | set(current)
                )
                turnover += traded
                equity -= traded * cost * equity
                values = {t: w * equity for t, w in new.items()}
                cash = equity - sum(values.values())
                for t in list(values):
                    o, c = open_.at[day, t], close.at[day, t]
                    values[t] *= c / o if o and not math.isnan(o) and not math.isnan(c) else 1.0
                pending = None
            else:
                for t in list(values):
                    c, pc = close.at[day, t], close.at[prev, t]
                    values[t] *= c / pc if pc and not math.isnan(c) and not math.isnan(pc) else 1.0
        equity = cash + sum(values.values())
        curve[day] = equity
        if state != "flat" and equity / max(peak, equity) - 1 <= -cfg.drawdown_flat:
            state, flat_since, pending = "flat", day, {}  # all out at the next open
        if day in ends and pending is None:
            state, peak, flat_since, _ = drawdown_state(state, peak, equity, flat_since, day, cfg)
            history = close.iloc[: i + 1]
            target = target_weights(history, cfg, tbill_12m=tbill_12m(history, cfg), state=state)
            pending = target.weights
        else:
            peak = max(peak, equity) if state != "flat" else peak
    return pd.Series(curve), turnover


def buy_and_hold(close: pd.DataFrame, ticker: str, start: date, end: date) -> pd.Series:
    s = close[ticker].dropna()
    s = s[(s.index >= start) & (s.index <= end)]
    return s / s.iloc[0] if len(s) else s


def sixty_forty(close: pd.DataFrame, start: date, end: date) -> pd.Series:
    frame = close[["SPY", "IEF"]].dropna()
    frame = frame[(frame.index >= start) & (frame.index <= end)]
    if frame.empty:
        return pd.Series(dtype=float)
    equity, curve, w = 1.0, {}, np.array([0.6, 0.4])
    held = w * equity
    for k, day in enumerate(frame.index):
        if k:
            held = held * (frame.iloc[k].to_numpy() / frame.iloc[k - 1].to_numpy())
            equity = float(held.sum())
            if day.month != frame.index[k - 1].month:
                held = w * equity  # monthly rebalance
        curve[day] = equity
    return pd.Series(curve)


def run_sleeve(close: pd.DataFrame, open_: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame,
               cfg: PortfolioConfig, start: date, end: date) -> tuple[pd.Series, list[sleeve_rules.Outcome]]:  # fmt: skip
    """The daily chart trades alone, on their share of C's money: (equity from 1.0, trades)."""
    s_cfg = cfg.sleeve
    slots = max(1, round(s_cfg.max_share / s_cfg.per_trade))
    days = [d for d in close.index if start <= d <= end]
    pnl: dict[date, float] = {}
    trades: list[sleeve_rules.Outcome] = []
    busy_until: list[date] = []
    for day in days:
        busy_until = [d for d in busy_until if d >= day]
        i = close.index.get_loc(day)
        if len(busy_until) >= slots:
            continue
        found = sleeve_rules.signals(
            close.iloc[: i + 1], high.iloc[: i + 1], low.iloc[: i + 1], cfg
        )
        for signal in found[: slots - len(busy_until)]:
            bars = pd.DataFrame({"open": open_[signal.ticker], "high": high[signal.ticker],
                                 "low": low[signal.ticker], "close": close[signal.ticker]})  # fmt: skip
            outcome = sleeve_rules.play(
                signal, bars[bars.index <= end], s_cfg.hold_days, cfg.cost_bps
            )
            if outcome is None:
                continue
            trades.append(outcome)
            busy_until.append(outcome.exit_day)
            # each trade uses 1/slots of the sleeve's money
            pnl[outcome.exit_day] = pnl.get(outcome.exit_day, 0.0) + outcome.net / slots
    curve, equity = {}, 1.0
    for day in days:
        equity *= 1 + pnl.get(day, 0.0)
        curve[day] = equity
    return pd.Series(curve), trades


def _variant(cfg: PortfolioConfig, **changes: object) -> PortfolioConfig:
    upgrades = {k: v for k, v in changes.items() if k in UPGRADES}
    rest = {k: v for k, v in changes.items() if k not in UPGRADES}
    out = cfg.model_copy(update=rest)
    if upgrades:
        out = out.model_copy(update={"upgrades": cfg.upgrades.model_copy(update=upgrades)})
    return out


def _helps(on: Stats | None, off: Stats | None) -> bool:
    if on is None or off is None:
        return False
    better_sharpe = on.sharpe > off.sharpe + 0.02
    calmer = on.max_drawdown > off.max_drawdown + 0.01 and on.sharpe >= off.sharpe - 0.05
    return better_sharpe or calmer


def walk_forward(prices: Mapping[str, pd.DataFrame], cfg: PortfolioConfig, tbill: pd.Series | None = None, *,
                 split: date = OUT_OF_SAMPLE_START, first: date = IN_SAMPLE_START,
                 last: date | None = None) -> dict[str, Any]:  # fmt: skip
    close, open_, high, low = prices["close"], prices["open"], prices["high"], prices["low"]
    last = last or close.index[-1]
    in_end = max(d for d in close.index if d < split)
    rf = None
    if tbill is not None and not tbill.empty:
        rf = tbill.reindex(close.index).ffill().fillna(0) / 100 / 252

    def measured(variant: PortfolioConfig, a: date, b: date) -> Stats | None:
        curve, turnover = run_portfolio(close, open_, variant, a, b, tbill)
        return stats(curve, rf, turnover)

    grid: list[tuple[dict[str, Any], Stats | None]] = []
    for score_days, vol_target, top_n in itertools.product(SCORE_DAYS, VOL_TARGETS, TOP_NS):
        variant = _variant(cfg, score_days=score_days, vol_target=vol_target, top_n=top_n)
        settings: dict[str, Any] = {
            "score_days": list(score_days),
            "vol_target": vol_target,
            "top_n": top_n,
        }
        grid.append((settings, measured(variant, first, in_end)))
    scored = [(settings, s) for settings, s in grid if s is not None]
    if not scored:
        return {"error": "not enough data for the in-sample period"}
    chosen, chosen_in = max(scored, key=lambda x: x[1].sharpe)
    best = _variant(
        cfg,
        score_days=tuple(chosen["score_days"]),
        vol_target=chosen["vol_target"],
        top_n=chosen["top_n"],
    )
    out_stats = measured(best, split, last)
    result: dict[str, Any] = {
        "chosen": chosen,
        "grid": [{"settings": g, "in_sample": asdict(s) if s else None} for g, s in grid],
        "c": {"in_sample": asdict(chosen_in), "out_of_sample": asdict(out_stats) if out_stats else None},
        "spy": {"in_sample": _asdict(stats(buy_and_hold(close, "SPY", first, in_end), rf)),
                "out_of_sample": _asdict(stats(buy_and_hold(close, "SPY", split, last), rf))},
        "sixty_forty": {"in_sample": _asdict(stats(sixty_forty(close, first, in_end), rf)),
                        "out_of_sample": _asdict(stats(sixty_forty(close, split, last), rf))},
    }  # fmt: skip
    upgrades: dict[str, Any] = {}
    for name in UPGRADES:
        on = _variant(best, **{name: True})
        off = _variant(best, **{name: False})
        periods = {p: (measured(on, a, b), measured(off, a, b)) for p, (a, b) in
                   {"in_sample": (first, in_end), "out_of_sample": (split, last)}.items()}  # fmt: skip
        upgrades[name] = {
            "in_sample": {"on": _asdict(periods["in_sample"][0]), "off": _asdict(periods["in_sample"][1])},
            "out_of_sample": {"on": _asdict(periods["out_of_sample"][0]), "off": _asdict(periods["out_of_sample"][1])},
            "recommend": all(_helps(*periods[p]) for p in periods),
        }  # fmt: skip
    result["upgrades"] = upgrades
    sleeve_result: dict[str, Any] = {}
    for period, (a, b) in {"in_sample": (first, in_end), "out_of_sample": (split, last)}.items():
        curve, trades = run_sleeve(close, open_, high, low, best, a, b)
        s = stats(curve, rf)
        sleeve_result[period] = {"stats": _asdict(s), "trades": len(trades),
                                 "hit_rate": round(sum(1 for t in trades if t.net > 0) / len(trades), 3) if trades else None,
                                 "average_net": round(sum(t.net for t in trades) / len(trades), 5) if trades else None}  # fmt: skip
    sleeve_result["recommend"] = all(
        (v["stats"] or {}).get("total", -1) > 0 and (v["stats"] or {}).get("sharpe", -1) > 0
        for k, v in sleeve_result.items() if k != "recommend"
    )  # fmt: skip
    result["sleeve"] = sleeve_result
    result["verdict"] = verdict(result)
    return result


def _asdict(s: Stats | None) -> dict[str, object] | None:
    return asdict(s) if s is not None else None


def verdict(result: Mapping[str, Any]) -> str:
    c = (result.get("c") or {}).get("out_of_sample")
    spy = (result.get("spy") or {}).get("out_of_sample")
    if not c or not spy:
        return "not enough data for the years the settings never saw"
    meets = c["sharpe"] >= spy["sharpe"] and c["max_drawdown"] >= spy["max_drawdown"] / 2
    text = (f"On 2016 to today (unseen): C {c['cagr']:+.1%} a year, Sharpe {c['sharpe']:.2f}, worst fall "
            f"{c['max_drawdown']:.0%}; SPY {spy['cagr']:+.1%} a year, Sharpe {spy['sharpe']:.2f}, worst fall "
            f"{spy['max_drawdown']:.0%}. ")  # fmt: skip
    if meets:
        return text + "C meets its target (Sharpe at least SPY's, at most half SPY's worst fall)."
    return text + ("C does NOT meet its target on the unseen years. Expect it to give up return for "
                   "calmer swings, and judge it over years, not months.")  # fmt: skip
