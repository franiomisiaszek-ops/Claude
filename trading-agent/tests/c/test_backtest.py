"""System C's backtest and daily chart trades on synthetic prices."""

from __future__ import annotations

from datetime import date

import httpx
import pandas as pd
import pytest

from papertrader.c import backtest as bt
from papertrader.c.data import DataError, fetch_tiingo, parse_alpaca, parse_tiingo
from papertrader.c.sleeve import Signal, play, rsi2, signals

from .helpers import market, portfolio


def test_the_weekly_simulation_trades_at_mondays_open_with_costs() -> None:
    data = market()
    cfg = portfolio()
    start, end = data["close"].index[300], data["close"].index[-1]
    curve, turnover = bt.run_portfolio(data["close"], data["open"], cfg, start, end)
    assert curve.iloc[0] == pytest.approx(1.0) and turnover > 0
    free, _ = bt.run_portfolio(
        data["close"], data["open"], cfg.model_copy(update={"cost_bps": 0}), start, end
    )
    assert free.iloc[-1] > curve.iloc[-1]  # costs are paid
    s = bt.stats(curve)
    assert s is not None and s.max_drawdown <= 0 and s.turnover_per_year == 0
    assert bt.stats(curve, turnover=turnover).turnover_per_year > 0  # type: ignore[union-attr]
    spy = bt.buy_and_hold(data["close"], "SPY", start, end)
    assert spy.iloc[0] == 1.0 and len(bt.sixty_forty(data["close"], start, end)) == len(spy)


def test_week_ends_are_the_last_trading_day_of_each_week() -> None:
    days = [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]
    assert bt.week_ends(days) == {
        date(2026, 11, 27),
        date(2026, 11, 30),
    }  # Thanksgiving week ends on Friday 27


def test_the_walk_forward_test_chooses_on_the_past_and_tests_once_on_the_rest() -> None:
    data = market(n=760)
    days = data["close"].index
    result = bt.walk_forward(data, portfolio(), first=days[300], split=days[520], last=days[-1])
    assert len(result["grid"]) == 12 and set(result["chosen"]) == {
        "score_days",
        "vol_target",
        "top_n",
    }
    chosen_sharpe = max(g["in_sample"]["sharpe"] for g in result["grid"] if g["in_sample"])
    assert result["c"]["in_sample"]["sharpe"] == chosen_sharpe  # picked on the in-sample years only
    assert result["c"]["out_of_sample"] is not None and result["spy"]["out_of_sample"] is not None
    assert set(result["upgrades"]) == set(bt.UPGRADES)
    assert all(isinstance(u["recommend"], bool) for u in result["upgrades"].values())
    assert "recommend" in result["sleeve"] and result["verdict"].startswith(
        "On 2016 to today (unseen):"
    )


def test_the_daily_chart_rules() -> None:
    days = pd.bdate_range("2025-01-01", periods=260).date
    rising = pd.Series(
        [100 + i * 0.2 for i in range(259)] + [160.0], index=days
    )  # a jump above the recent high
    close = pd.DataFrame({"SPY": rising})
    found = signals(close, close * 1.005, close * 0.995, portfolio())
    assert [(s.ticker, s.rule) for s in found] == [("SPY", "breakout")]
    assert found[0].stop < found[0].reference < found[0].target
    assert rsi2(pd.Series([10, 9, 8, 7, 6.0])) == pytest.approx(0.0)


def test_one_chart_trade_from_signal_to_exit() -> None:
    days = list(pd.bdate_range("2026-01-05", periods=6).date)
    bars = pd.DataFrame({"open": [100, 101, 102, 103, 104, 105], "high": [101, 103, 104, 108, 105, 106],
                         "low": [99, 100, 101, 102, 103, 104], "close": [100, 102, 103, 107, 104, 105]},
                        index=days, dtype=float)  # fmt: skip
    hit = play(Signal(days[0], "GLD", "breakout", 100, 95, 107), bars, 5, cost_bps=5)
    assert hit is not None and (hit.entry, hit.exit, hit.reason) == (101, 107, "target")
    assert hit.net == pytest.approx(107 / 101 - 1 - 0.001)
    timed = play(Signal(days[0], "GLD", "dip", 100, 90, 200), bars, 2, cost_bps=0)
    assert timed is not None and (timed.reason, timed.exit) == ("time", 103)
    assert (
        play(Signal(days[-1], "GLD", "dip", 100, 90, 200), bars, 2, 0) is None
    )  # nothing after it yet


def test_price_sources_are_read_carefully() -> None:
    rows = parse_tiingo("GLD", [{"date": "2026-01-05T00:00:00.000Z", "adjOpen": 1, "adjHigh": 2, "adjLow": 0.5,
                                 "adjClose": 1.5, "adjVolume": 10}])  # fmt: skip
    assert rows[0]["day"] == date(2026, 1, 5) and rows[0]["close"] == 1.5
    with pytest.raises(DataError, match="without the expected adjusted prices"):
        parse_tiingo("GLD", [{"date": "2026-01-05", "close": 1}])
    with pytest.raises(DataError, match="Invalid token"):
        parse_tiingo("GLD", {"detail": "Invalid token."})
    refused = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={})))
    with pytest.raises(DataError, match="refused the key"):
        fetch_tiingo(refused, "k", "GLD", date(2004, 1, 1))
    missing = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404, json={})))
    assert fetch_tiingo(missing, "k", "NOPE", date(2004, 1, 1)) == []
    alpaca = parse_alpaca(
        {"GLD": [{"t": "2026-01-05T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}]}
    )
    assert alpaca[0]["day"] == date(2026, 1, 5) and alpaca[0]["volume"] == 9
