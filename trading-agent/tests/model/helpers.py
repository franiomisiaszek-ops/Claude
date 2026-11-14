"""Synthetic markets for the pattern-model tests.

`effect` plants a known relationship: after a positive headline a stock drifts up
relative to the market over the next five days (and down after a negative one). With
`effect=0` the headlines carry no information at all.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.store import upsert
from papertrader.ingest.timeutil import at_new_york, daily_bar_available_at
from papertrader.model.features import Events

SECTORS = ["Information Technology", "Financials", "Energy", "Health Care"]
GOOD = "{t} shares surge after record profit beats estimates"
BAD = "{t} shares plunge after weak results miss and lawsuit"


def business_days(start: date, count: int) -> list[date]:
    days, day = [], start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def synth_market(
    *,
    tickers: int = 30,
    days: int = 420,
    effect: float = 0.0,
    event_rate: float = 0.35,
    seed: int = 1,
) -> tuple[pd.DataFrame, Events, dict[str, str | None]]:
    rng = np.random.default_rng(seed)
    calendar = business_days(date(2024, 1, 2), days)
    names = [f"S{i:02d}" for i in range(tickers)]
    sectors: dict[str, str | None] = {name: SECTORS[i % 4] for i, name in enumerate(names)}
    market = rng.normal(0.0004, 0.008, days)

    bars, news = [], []
    for name in ["SPY", *names]:
        own = market if name == "SPY" else market + rng.normal(0, 0.015, days)
        own = own.copy()
        if name != "SPY":
            for i in range(60, days - 6):
                if rng.random() < event_rate:
                    sign = 1.0 if rng.random() < 0.5 else -1.0
                    own[i : i + 5] += sign * effect / 5  # the planted drift, day i..i+4
                    news.append({
                        "news_id": len(news) + 1, "ticker": name,
                        "time": at_new_york(calendar[i], time(7, 0)),  # before the 08:20 decision
                        "n_tickers": 1, "sentiment": 0.8 * sign,
                        "event_type": "earnings", "llm_sentiment": np.nan, "llm_materiality": np.nan,
                        "headline": (GOOD if sign > 0 else BAD).format(t=name),
                    })  # fmt: skip
        close = 100 * np.cumprod(1 + own)
        opens = np.concatenate([[100.0], close[:-1]])  # no overnight gap: open = previous close
        for i, day in enumerate(calendar):
            bars.append({"ticker": name, "day": day, "open": opens[i], "high": max(opens[i], close[i]) * 1.002,
                         "low": min(opens[i], close[i]) * 0.998, "close": close[i],
                         "volume": 1e6 * (1 + rng.random())})  # fmt: skip
    empty = pd.DataFrame(columns=["ticker", "time"])
    events = Events(
        news=pd.DataFrame(news).drop(columns=["headline"]),
        earnings=empty.assign(eps_surprise=[]),
        filings=empty.assign(items=[]),
        insiders=empty.assign(code=[], usd=[], insider=[]),
    )
    events.news.attrs["headlines"] = [row["headline"] for row in news]
    return pd.DataFrame(bars), events, sectors


def store_market(
    engine: Engine, bars: pd.DataFrame, events: Events, sectors: dict[str, str | None]
) -> None:
    """Write a synthetic market into the database, as ingestion would have."""
    sessions = make_session_factory(engine)
    with sessions() as session:
        for ticker, sector in sectors.items():
            session.add(m.UniverseMember(index_name="sp500", ticker=ticker, name=f"{ticker} Corp",
                                         gics_sector=sector, is_active=True))  # fmt: skip
        rows = [
            {"ticker": r.ticker, "timeframe": "1Day", "ts": at_new_york(r.day, time(0)), "open": r.open,
             "high": r.high, "low": r.low, "close": r.close, "volume": r.volume, "vwap": None,
             "trade_count": None, "available_at": daily_bar_available_at(r.day)}
            for r in bars.itertuples()
        ]  # fmt: skip
        upsert(session, m.PriceBar, rows, conflict=("ticker", "timeframe", "ts"))
        headlines = events.news.attrs["headlines"]
        for row, headline in zip(events.news.itertuples(), headlines, strict=True):
            session.add(m.NewsItem(
                id=row.news_id, source="alpaca", source_item_id=str(row.news_id), headline=headline,
                published_at=row.time, available_at=row.time, content_hash=f"{row.news_id:064d}",
                tickers=[m.NewsTicker(ticker=row.ticker)],
            ))  # fmt: skip
        session.commit()
