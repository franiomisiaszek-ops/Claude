"""System C's rules on synthetic prices whose right answers are known."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from papertrader.c.data import tbill_12m, with_cash_history
from papertrader.c.strategy import (
    apply_caps,
    drawdown_state,
    momentum,
    rebalance_orders,
    risk_parity,
    target_weights,
)

from .helpers import market, portfolio


def test_trending_funds_are_chosen_and_capped() -> None:
    close = market()["close"]
    cfg = portfolio()
    eligible, scores, reasons = momentum(close, cfg, None)
    assert "EEM" not in eligible and "TLT" not in eligible  # falling: never eligible
    assert reasons["EEM"].startswith("out:") and "SGOV" not in scores  # cash is not ranked
    target = target_weights(close, cfg)
    assert set(target.weights) <= set(eligible) and target.weights
    assert all(w <= 0.20 + 1e-9 for w in target.weights.values())
    assert target.weights.get("USO", 0) <= 0.05 + 1e-9
    equities = sum(w for t, w in target.weights.items() if t in ("SPY", "QQQ", "EEM"))
    assert equities <= 0.60 + 1e-9
    assert (
        target.vol is not None and target.vol <= 0.10 + 1e-6
    )  # the volatility target, never above
    assert target.cash == pytest.approx(1 - sum(target.weights.values()), abs=1e-3)
    assert all(reasons[t].startswith("eligible") for t in eligible)
    assert all(target.reasons[t].startswith("in:") for t in target.weights)


def test_upgrades_and_drawdown_states_change_the_target() -> None:
    close = market()["close"]
    cfg = portfolio()
    base = target_weights(close, cfg)
    tbills = target_weights(
        close,
        cfg.model_copy(
            update={"upgrades": cfg.upgrades.model_copy(update={"cash_in_tbills": True})}
        ),
    )
    assert tbills.cash == 0 and tbills.weights["SGOV"] == pytest.approx(base.cash, abs=1e-3)
    halved = target_weights(close, cfg, state="halved")
    assert sum(halved.weights.values()) == pytest.approx(sum(base.weights.values()) / 2, abs=1e-3)
    assert target_weights(close, cfg, state="flat").weights == {}
    dual = cfg.model_copy(
        update={"upgrades": cfg.upgrades.model_copy(update={"dual_momentum": True})}
    )
    eligible, _, reasons = momentum(
        close, dual, tbill_12m=5.0
    )  # T-bills "paying 500%": nothing beats them
    assert eligible == [] and any("does not beat T-bills" in r for r in reasons.values())


def test_risk_parity_gives_every_fund_the_same_risk() -> None:
    rng = np.random.default_rng(1)
    rets = pd.DataFrame(
        {
            "a": rng.normal(0, 0.01, 500),
            "b": rng.normal(0, 0.02, 500),
            "c": rng.normal(0, 0.04, 500),
        }
    )
    w = risk_parity(rets)
    cov = rets.cov().to_numpy()
    vec = np.array([w["a"], w["b"], w["c"]])
    contributions = vec * (cov @ vec)
    assert contributions.max() / contributions.min() == pytest.approx(1.0, abs=0.02)
    assert w["a"] > w["b"] > w["c"]


def test_caps_leave_the_excess_in_cash() -> None:
    cfg = portfolio()
    capped = apply_caps({"SPY": 0.4, "QQQ": 0.3, "EEM": 0.2, "GLD": 0.1}, cfg)
    assert capped["SPY"] == pytest.approx(0.2) and capped["GLD"] == pytest.approx(0.1)
    assert (
        sum(capped[t] for t in ("SPY", "QQQ", "EEM")) <= 0.60 + 1e-9
    )  # 0.2 + 0.2 + 0.2 = 0.6: fits


def test_drawdown_control() -> None:
    cfg = portfolio()
    day = date(2026, 1, 9)
    assert drawdown_state("normal", 100, 95, None, day, cfg)[0] == "normal"
    assert drawdown_state("normal", 100, 89, None, day, cfg)[0] == "halved"
    state, _, since, dd = drawdown_state("halved", 100, 84, None, day, cfg)
    assert (state, since, round(dd, 2)) == ("flat", day, -0.16)
    assert (
        drawdown_state("flat", 100, 84, since, date(2026, 1, 30), cfg)[0] == "flat"
    )  # 3 weeks: still out
    fresh = drawdown_state("flat", 100, 84, since, date(2026, 2, 6), cfg)
    assert fresh[:3] == ("normal", 84, None)  # 4 weeks later: a fresh start from today's equity


def test_orders_respect_the_drift_band() -> None:
    cfg = portfolio()
    holdings = {"SPY": 10.0, "GLD": 20.0, "TLT": 5.0}
    prices = {"SPY": 500.0, "GLD": 200.0, "TLT": 100.0, "QQQ": 400.0}
    # SPY at 20% wants 21% (5% off: no trade); GLD at 16% wants 25% (way off); TLT is dropped; QQQ is new.
    plans = rebalance_orders({"SPY": 0.21, "GLD": 0.25, "QQQ": 0.15}, holdings, prices, 25_000, cfg)
    by = {p.ticker: p for p in plans}
    assert "SPY" not in by
    assert (
        by["TLT"].side == "sell" and by["TLT"].qty == 5 and plans[0].ticker == "TLT"
    )  # sells first
    assert (
        by["GLD"].side == "buy" and by["GLD"].qty == 11
    )  # 31.25 shares wanted, 20 held: buy 11 whole
    assert by["QQQ"].qty == 9 and by["QQQ"].limit == pytest.approx(401.2)
    fractional = cfg.model_copy(
        update={"upgrades": cfg.upgrades.model_copy(update={"fractional": True})}
    )
    assert {
        p.ticker: p.qty for p in rebalance_orders({"QQQ": 0.15}, {}, prices, 1_000, fractional)
    } == {"QQQ": 0.375}


def test_the_cash_fund_history_is_chained_back() -> None:
    data = market(starts={"SGOV": 500, "BIL": 200})["close"]
    rate = pd.Series(4.0, index=data.index)  # 4% a year before BIL existed
    out = with_cash_history(data, portfolio(), rate)["SGOV"]
    assert out.notna().all()  # the whole period now has a T-bill fund
    k = 100
    assert out.iloc[k + 1] / out.iloc[k] - 1 == pytest.approx(
        0.04 / 252
    )  # grows at the T-bill rate
    assert out.iloc[600] / out.iloc[599] == pytest.approx(
        data["SGOV"].iloc[600] / data["SGOV"].iloc[599]
    )
    assert tbill_12m(data.assign(SGOV=out), portfolio()) is not None
