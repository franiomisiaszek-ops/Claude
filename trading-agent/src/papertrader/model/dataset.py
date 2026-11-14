"""Reads prices and events from the database and builds the pattern-model dataset."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from papertrader.agents.snapshot import OPEN_MARKET_CODES, SECTOR_ETF
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import new_york_date
from papertrader.model.features import Events, assemble
from papertrader.model.text import lexicon_sentiment, rule_event_type

# News naming more companies than this is a market roundup, not a story about any of them.
MAX_TICKERS_PER_STORY = 3
# Live scoring only needs enough history for the slowest feature (the 52-week range).
LIVE_HISTORY_DAYS = 420


def _utc(frame: pd.DataFrame, column: str = "time") -> pd.DataFrame:
    """SQLite hands timestamps back without a zone; they are UTC by construction."""
    if len(frame):
        frame[column] = frame[column].map(
            lambda value: (
                value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
            )
        )
    return frame


def load_bars(
    session: Session, tickers: list[str], start: date | None, as_of: datetime
) -> pd.DataFrame:
    query = select(
        m.PriceBar.ticker, m.PriceBar.ts, m.PriceBar.open, m.PriceBar.high, m.PriceBar.low,
        m.PriceBar.close, m.PriceBar.volume,
    ).where(
        m.PriceBar.timeframe == "1Day",
        m.PriceBar.ticker.in_(tickers),
        m.PriceBar.available_at <= as_of,  # finished bars only
    )  # fmt: skip
    if start is not None:
        query = query.where(m.PriceBar.ts >= datetime.combine(start, time(0), tzinfo=UTC))
    frame = pd.DataFrame(session.execute(query).all(),
                         columns=["ticker", "ts", "open", "high", "low", "close", "volume"])  # fmt: skip
    frame = _utc(frame, "ts")
    frame["day"] = frame["ts"].map(new_york_date) if len(frame) else []
    return frame.drop(columns=["ts"])


def load_events(
    session: Session, tickers: list[str], since: datetime | None, as_of: datetime
) -> Events:
    def window(column: Any) -> list[Any]:
        conditions: list[Any] = [column <= as_of]
        if since is not None:
            conditions.append(column > since)
        return conditions

    sizes = (
        select(m.NewsTicker.news_id, func.count().label("n_tickers"))
        .group_by(m.NewsTicker.news_id)
        .subquery()
    )
    news = pd.DataFrame(
        session.execute(
            select(m.NewsItem.id, m.NewsTicker.ticker, m.NewsItem.available_at, m.NewsItem.headline,
                   m.NewsItem.summary, sizes.c.n_tickers, m.NewsFeature.sentiment,
                   m.NewsFeature.materiality)
            .join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
            .join(sizes, sizes.c.news_id == m.NewsItem.id)
            .outerjoin(m.NewsFeature, m.NewsFeature.news_id == m.NewsItem.id)
            .where(
                m.NewsItem.duplicate_of_id.is_(None),
                m.NewsTicker.ticker.in_(tickers),
                sizes.c.n_tickers <= MAX_TICKERS_PER_STORY,
                *window(m.NewsItem.available_at),
            )
        ).all(),
        columns=["news_id", "ticker", "time", "headline", "summary", "n_tickers", "llm_sentiment",
                 "llm_materiality"],
    )  # fmt: skip
    news = _utc(news)
    text = (news["headline"].fillna("") + ". " + news["summary"].fillna("")) if len(news) else []
    news["sentiment"] = [lexicon_sentiment(t) for t in text]
    news["event_type"] = [rule_event_type(t) for t in text]
    news = news.drop(columns=["headline", "summary"])

    rows = session.execute(
        select(m.EarningsEvent.ticker, m.EarningsEvent.actual_available_at,
               m.EarningsEvent.eps_actual, m.EarningsEvent.eps_estimate)
        .where(m.EarningsEvent.actual_available_at.is_not(None), m.EarningsEvent.ticker.in_(tickers),
               *window(m.EarningsEvent.actual_available_at))
    ).all()  # fmt: skip
    earnings = pd.DataFrame(rows, columns=["ticker", "time", "eps_actual", "eps_estimate"])
    earnings = _utc(earnings)
    estimate = earnings["eps_estimate"].where(earnings["eps_estimate"].abs() > 0.01)
    earnings["eps_surprise"] = ((earnings["eps_actual"] - estimate) / estimate.abs()).clip(-3, 3)

    filings = pd.DataFrame(
        session.execute(
            select(m.Filing.ticker, m.Filing.available_at, m.Filing.items)
            .where(m.Filing.form_type.in_(("8-K", "8-K/A")), m.Filing.ticker.in_(tickers),
                   *window(m.Filing.available_at))
        ).all(),
        columns=["ticker", "time", "items"],
    )  # fmt: skip
    filings = _utc(filings)

    insiders = pd.DataFrame(
        session.execute(
            select(m.InsiderTransaction.ticker, m.InsiderTransaction.available_at,
                   m.InsiderTransaction.transaction_code, m.InsiderTransaction.shares,
                   m.InsiderTransaction.price, m.InsiderTransaction.insider_name)
            .where(m.InsiderTransaction.transaction_code.in_(OPEN_MARKET_CODES),
                   m.InsiderTransaction.ticker.in_(tickers),
                   *window(m.InsiderTransaction.available_at))
        ).all(),
        columns=["ticker", "time", "code", "shares", "price", "insider"],
    )  # fmt: skip
    insiders = _utc(insiders)
    insiders["usd"] = insiders["shares"] * insiders["price"].fillna(0.0)
    return Events(news=news, earnings=earnings, filings=filings, insiders=insiders)


def build_dataset(
    session: Session, config: AppConfig, *, as_of: datetime, live: bool = False
) -> pd.DataFrame:
    """Historical dataset (live=False) or today's rows for scoring (live=True)."""
    members = session.execute(
        select(m.UniverseMember.ticker, m.UniverseMember.gics_sector).where(
            m.UniverseMember.index_name == config.universe.index, m.UniverseMember.is_active
        )
    ).all()
    sectors: dict[str, str | None] = {row[0]: row[1] for row in members}
    benchmark = config.universe.benchmark
    price_tickers = [*sectors, benchmark, *SECTOR_ETF.values()]
    start = new_york_date(as_of) - timedelta(days=LIVE_HISTORY_DAYS) if live else None
    since = as_of - timedelta(days=12) if live else None

    bars = load_bars(session, price_tickers, start, as_of)
    if bars.empty or benchmark not in set(bars["ticker"]):
        return pd.DataFrame()
    events = load_events(session, list(sectors), since, as_of)
    table = assemble(
        bars=bars, events=events, sectors=sectors, benchmark=benchmark,
        horizons=config.pattern_model.horizons_days,
        decision_time=config.schedule.score_and_judge, as_of=as_of if live else None,
    )  # fmt: skip
    if live and len(table):
        table = table[table["day"] == table["day"].max()]
    return table


def save_dataset(table: pd.DataFrame, folder: Path, *, as_of: datetime) -> Path:
    """Parquet file plus a small description next to it."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"dataset_{as_of:%Y%m%d_%H%M%S}.parquet"
    table.to_parquet(path, index=False)
    summary = {
        "built_at": as_of.isoformat(), "rows": len(table),
        "tickers": int(table["ticker"].nunique()) if len(table) else 0,
        "first_day": str(table["day"].min()) if len(table) else None,
        "last_day": str(table["day"].max()) if len(table) else None,
    }  # fmt: skip
    path.with_suffix(".json").write_text(json.dumps(summary, indent=2))
    return path
