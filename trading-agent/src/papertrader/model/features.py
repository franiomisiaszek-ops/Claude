"""Turns prices and events into model features. Used for training AND for live scoring.

One row = one stock on one trading day on which something happened (news, a filing,
an earnings release or an insider trade) between the previous decision time and that
day's decision time. Using the same code for history and for today is deliberate: it
is what keeps the model from being trained on one thing and asked about another.

Timing, which is where leakage hides:

    events up to the decision time on day D  ->  features (prices up to the close of D-1)
    enter at the OPEN of day D               ->  hold h trading days
    exit at the CLOSE of day D+h-1           ->  label = return minus SPY's, same window

Nothing dated after the decision time on day D goes into a feature. In particular the
overnight gap into day D is NOT a feature: at decision time, before the open, the
system does not know it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from papertrader.ingest.timeutil import at_new_york, new_york_date
from papertrader.model.text import EVENT_TYPES

FILING_ITEMS = ("1.01", "1.02", "2.01", "2.05", "2.06", "4.01", "4.02", "5.02")
TRADING_DAYS_PER_YEAR = 252

PRICE_FEATURES = [
    "mom_1d", "mom_5d", "mom_20d", "mom_60d", "xmom_5d", "xmom_20d", "vol_20d", "volume_ratio",
    "from_52w_high", "from_52w_low", "above_sma50",
]  # fmt: skip
MARKET_FEATURES = ["mkt_mom_20d", "mkt_vol_20d", "mkt_above_sma200"]
EVENT_FEATURES = [
    "news_count", "news_solo", "news_7d", "sent_mean", "sent_min", "sent_max",
    *(f"ev_{name}" for name in EVENT_TYPES),
    "has_earnings", "eps_surprise", "has_8k", *(f"item_{i.replace('.', '')}" for i in FILING_ITEMS),
    "insider_buyers", "insider_buy_usd", "insider_sell_usd",
    "llm_count", "llm_sent_mean", "llm_material_max",
]  # fmt: skip
CALENDAR_FEATURES = ["day_of_week"]
CATEGORICAL = ["sector"]
NUMERIC = [*PRICE_FEATURES, *MARKET_FEATURES, *EVENT_FEATURES, *CALENDAR_FEATURES]
FEATURES = [*NUMERIC, *CATEGORICAL]


@dataclass
class Events:
    """Raw events, one DataFrame per kind. Every frame has `ticker` and `time` (UTC)."""

    news: pd.DataFrame  # + n_tickers, sentiment, event_type, llm_sentiment, llm_materiality
    earnings: pd.DataFrame  # + eps_surprise
    filings: pd.DataFrame  # + items
    insiders: pd.DataFrame  # + code, usd, insider


def price_features(bars: pd.DataFrame, benchmark: str) -> pd.DataFrame:
    """Per (ticker, day): what is known at that day's CLOSE. Indexed by ticker, day."""
    frames = []
    for ticker, frame in bars.groupby("ticker", sort=False):
        f = frame.sort_values("day").set_index("day")
        close, ret = f["close"], f["close"].pct_change()
        out = pd.DataFrame(index=f.index)
        for n in (1, 5, 20, 60):
            out[f"mom_{n}d"] = close.pct_change(n)
        out["vol_20d"] = ret.rolling(20).std() * np.sqrt(TRADING_DAYS_PER_YEAR)
        out["volume_ratio"] = f["volume"] / f["volume"].shift(1).rolling(20).mean()
        out["from_52w_high"] = close / f["high"].rolling(252, min_periods=60).max() - 1
        out["from_52w_low"] = close / f["low"].rolling(252, min_periods=60).min() - 1
        out["above_sma50"] = (close > close.rolling(50).mean()).astype(float)
        out.loc[close.rolling(50).mean().isna(), "above_sma50"] = np.nan
        out["sma200_ok"] = (close > close.rolling(200, min_periods=120).mean()).astype(float)
        out["ticker"] = ticker
        frames.append(out.reset_index())
    table = pd.concat(frames, ignore_index=True)

    market = table[table["ticker"] == benchmark].set_index("day")
    table = table.join(
        market[["mom_5d", "mom_20d", "vol_20d", "sma200_ok"]].rename(
            columns={"mom_5d": "mkt_mom_5d", "mom_20d": "mkt_mom_20d", "vol_20d": "mkt_vol_20d",
                     "sma200_ok": "mkt_above_sma200"}
        ),
        on="day",
    )  # fmt: skip
    table["xmom_5d"] = table["mom_5d"] - table["mkt_mom_5d"]
    table["xmom_20d"] = table["mom_20d"] - table["mkt_mom_20d"]
    return table.drop(columns=["sma200_ok", "mkt_mom_5d"]).set_index(["ticker", "day"])


def labels(bars: pd.DataFrame, benchmark: str, horizons: tuple[int, ...]) -> pd.DataFrame:
    """Per (ticker, entry day): forward return, open of the day to the close h-1 days later,
    minus the benchmark's over the same window. NaN where the future is not known yet."""
    frames = []
    for ticker, frame in bars.groupby("ticker", sort=False):
        f = frame.sort_values("day").set_index("day")
        out = pd.DataFrame(index=f.index)
        for h in horizons:
            out[f"ret_{h}d"] = f["close"].shift(-(h - 1)) / f["open"] - 1
        out["ticker"] = ticker
        frames.append(out.reset_index())
    table = pd.concat(frames, ignore_index=True)
    market = table[table["ticker"] == benchmark].set_index("day")
    for h in horizons:
        table[f"excess_{h}d"] = table[f"ret_{h}d"] - table["day"].map(market[f"ret_{h}d"])
    return table.set_index(["ticker", "day"])


def entry_days(bar_days: list[date], decision_time: time, as_of: datetime | None) -> list[date]:
    """Trading days an event can be assigned to. For live scoring, today is added even
    though it has no price bar yet."""
    days = sorted(bar_days)
    if as_of is not None and days:
        today = new_york_date(as_of)
        if today > days[-1] and today.weekday() < 5:
            days.append(today)
    return days


def assign_entry_day(times: pd.Series, days: list[date], decision_time: time) -> pd.Series:
    """The first trading day whose decision time is at or after the event."""
    cutoffs = np.array([at_new_york(day, decision_time).timestamp() for day in days])
    stamps = times.map(lambda moment: moment.timestamp()).to_numpy(dtype=float)
    index = np.searchsorted(cutoffs, stamps, side="left")
    day_array = np.array(days, dtype=object)
    result = np.where(index < len(days), day_array[np.minimum(index, len(days) - 1)], None)
    return pd.Series(result, index=times.index, dtype=object)


def _count(frame: pd.DataFrame, name: str) -> pd.Series:
    return frame.groupby(["ticker", "day"]).size().rename(name)


def event_features(events: Events, days: list[date], decision_time: time) -> pd.DataFrame:
    """Per (ticker, entry day): what happened since the previous decision time."""
    parts: list[pd.Series | pd.DataFrame] = []

    news = events.news.copy()
    if len(news):
        news["day"] = assign_entry_day(news["time"], days, decision_time)
        news = news.dropna(subset=["day"])
        group = news.groupby(["ticker", "day"])
        parts.append(group.size().rename("news_count"))
        parts.append(group["n_tickers"].apply(lambda s: float((s == 1).sum())).rename("news_solo"))
        parts.append(group["sentiment"].agg(sent_mean="mean", sent_min="min", sent_max="max"))
        kinds = pd.crosstab([news["ticker"], news["day"]], news["event_type"]).astype(float)
        parts.append(kinds.rename(columns=lambda name: f"ev_{name}"))
        scored = news.dropna(subset=["llm_sentiment"])
        if len(scored):
            llm = scored.groupby(["ticker", "day"])
            parts.append(llm.size().rename("llm_count").astype(float))
            parts.append(llm["llm_sentiment"].mean().rename("llm_sent_mean"))
            parts.append(llm["llm_materiality"].max().rename("llm_material_max"))

    earnings = events.earnings.copy()
    if len(earnings):
        earnings["day"] = assign_entry_day(earnings["time"], days, decision_time)
        earnings = earnings.dropna(subset=["day"])
        group = earnings.groupby(["ticker", "day"])
        parts.append(group.size().clip(upper=1).rename("has_earnings").astype(float))
        parts.append(group["eps_surprise"].mean().rename("eps_surprise"))

    filings = events.filings.copy()
    if len(filings):
        filings["day"] = assign_entry_day(filings["time"], days, decision_time)
        filings = filings.dropna(subset=["day"])
        parts.append(_count(filings, "has_8k").clip(upper=1).astype(float))
        for item in FILING_ITEMS:
            hit = filings[filings["items"].fillna("").str.contains(item, regex=False)]
            if len(hit):
                parts.append(
                    _count(hit, f"item_{item.replace('.', '')}").clip(upper=1).astype(float)
                )

    insiders = events.insiders.copy()
    if len(insiders):
        insiders["day"] = assign_entry_day(insiders["time"], days, decision_time)
        insiders = insiders.dropna(subset=["day"])
        buys, sells = insiders[insiders["code"] == "P"], insiders[insiders["code"] == "S"]
        if len(buys):
            group = buys.groupby(["ticker", "day"])
            parts.append(group["insider"].nunique().rename("insider_buyers").astype(float))
            parts.append(group["usd"].sum().rename("insider_buy_usd"))
        if len(sells):
            parts.append(sells.groupby(["ticker", "day"])["usd"].sum().rename("insider_sell_usd"))

    if not parts:
        return pd.DataFrame(
            columns=EVENT_FEATURES,
            index=pd.MultiIndex.from_arrays([[], []], names=["ticker", "day"]),
        )
    table = pd.concat(parts, axis=1)
    table.index = table.index.set_names(["ticker", "day"])

    # Counts and flags: "nothing happened" is 0. Scores (sentiment, surprise) stay NaN
    # when there was nothing to score; the model handles missing values itself.
    zero_fill = [
        c
        for c in EVENT_FEATURES
        if c.startswith(("news_", "ev_", "has_", "item_", "insider_", "llm_count"))
    ]
    for column in EVENT_FEATURES:
        if column not in table:
            table[column] = 0.0 if column in zero_fill else np.nan
    table[zero_fill] = table[zero_fill].fillna(0.0)

    # Attention: how much news the stock had over the previous week (event-days only).
    counts = table["news_count"].unstack("ticker").reindex(days).fillna(0.0)
    week = counts.rolling(5, min_periods=1).sum().shift(1).stack().rename("news_7d")
    week.index = week.index.swaplevel().set_names(["ticker", "day"])
    table["news_7d"] = week.reindex(table.index).fillna(0.0)
    return table[EVENT_FEATURES]


def assemble(
    *,
    bars: pd.DataFrame,
    events: Events,
    sectors: dict[str, str | None],
    benchmark: str,
    horizons: tuple[int, ...],
    decision_time: time,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """The dataset: one row per (ticker, entry day) with events, features and labels.

    With `as_of` set (live scoring) the last entry day is today, which has features but
    no labels yet.
    """
    bar_days = sorted(bars.loc[bars["ticker"] == benchmark, "day"].unique())
    days = entry_days(bar_days, decision_time, as_of)
    previous = dict(zip(days[1:], days[:-1], strict=True))

    table = event_features(events, days, decision_time)
    table = table[table.index.get_level_values("ticker").isin(sectors)]
    if table.empty:
        return pd.DataFrame(columns=["ticker", "day", *FEATURES])
    table = table.reset_index()

    # Prices are joined as of the close of the day BEFORE the entry day.
    table["prev_day"] = table["day"].map(previous)
    table = table.dropna(subset=["prev_day"])
    prices = price_features(bars, benchmark)
    table = table.join(prices, on=["ticker", "prev_day"])
    table = table.dropna(subset=["mom_1d"])  # no price history: cannot say anything

    table = table.join(labels(bars, benchmark, horizons), on=["ticker", "day"])
    table["sector"] = table["ticker"].map(sectors).fillna("Unknown")
    table["day_of_week"] = table["day"].map(lambda d: float(d.weekday()))
    for h in horizons:
        table[f"up_{h}d"] = (
            (table[f"excess_{h}d"] > 0).astype(float).where(table[f"excess_{h}d"].notna())
        )
    return table.drop(columns=["prev_day"]).sort_values(["day", "ticker"]).reset_index(drop=True)
