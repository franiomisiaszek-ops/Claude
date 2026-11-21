"""System C's prices: its own table (c_prices); nothing goes into the shared market tables.

* Live: every trading day after the close, the last ~15 months of daily bars for C's
  funds from Alpaca (with C's own paper keys), adjusted for dividends and splits, are
  fetched again in full, because adjusted prices change whenever a fund pays out.
* Backtest: the full daily history since 2003 from Tiingo (free key, TIINGO_API_KEY),
  with dividends (its adjClose), and the 3-month T-bill rate from FRED (DTB3).

Tiingo's reply is a list of days with date, adjOpen, adjHigh, adjLow, adjClose and
adjVolume; every field is checked as it arrives and a wrong reply is named, not guessed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from papertrader.c.models import Price
from papertrader.config import PortfolioConfig

TIINGO_PRICES = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
FRED_OBSERVATIONS = "https://api.stlouisfed.org/fred/series/observations"
TBILL = "DTB3"
LIVE_DAYS = (
    460  # calendar days: more than the 252 trading days + the 200-day average the rules need
)


class DataError(RuntimeError):
    pass


def store(session: Session, source: str, rows: Iterable[dict[str, Any]]) -> int:
    """Replace a source's rows for the given tickers and days (simple and portable)."""
    rows = list(rows)
    if not rows:
        return 0
    by_ticker: dict[str, list[date]] = {}
    for row in rows:
        by_ticker.setdefault(row["ticker"], []).append(row["day"])
    for ticker, days in by_ticker.items():
        session.execute(delete(Price).where(Price.source == source, Price.ticker == ticker,
                                            Price.day >= min(days), Price.day <= max(days)))  # fmt: skip
    session.add_all(Price(source=source, **row) for row in rows)
    session.flush()
    return len(rows)


def frames(
    session: Session, source: str, tickers: Sequence[str], start: date | None = None
) -> dict[str, pd.DataFrame]:
    """{"close": ..., "open": ..., "high": ..., "low": ...}: days down, tickers across."""
    query = select(Price.ticker, Price.day, Price.open, Price.high, Price.low, Price.close).where(
        Price.source == source, Price.ticker.in_(sorted(set(tickers))))  # fmt: skip
    if start is not None:
        query = query.where(Price.day >= start)
    rows = session.execute(query).all()
    data = pd.DataFrame(rows, columns=["ticker", "day", "open", "high", "low", "close"])
    out = {}
    for field in ("close", "open", "high", "low"):
        if data.empty:
            out[field] = pd.DataFrame()
            continue
        table = data.pivot_table(index="day", columns="ticker", values=field, aggfunc="last")
        out[field] = table.sort_index().astype(float)
    return out


# --------------------------------------------------------------------------- Tiingo and FRED


def parse_tiingo(ticker: str, body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict) and body.get("detail"):
        raise DataError(f"Tiingo ({ticker}): {body['detail']}")
    if not isinstance(body, list):
        raise DataError(f"Tiingo ({ticker}): the reply is not a list of days")
    rows = []
    for item in body:
        try:
            rows.append({"ticker": ticker, "day": date.fromisoformat(str(item["date"])[:10]),
                         "open": float(item["adjOpen"]), "high": float(item["adjHigh"]),
                         "low": float(item["adjLow"]), "close": float(item["adjClose"]),
                         "volume": float(item.get("adjVolume") or 0)})  # fmt: skip
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(
                f"Tiingo ({ticker}): a day without the expected adjusted prices ({exc})"
            ) from exc
    return rows


def fetch_tiingo(client: httpx.Client, key: str, ticker: str, start: date) -> list[dict[str, Any]]:
    try:
        response = client.get(TIINGO_PRICES.format(ticker=ticker), timeout=30,
                              params={"startDate": start.isoformat(), "format": "json"},
                              headers={"Authorization": f"Token {key}"})  # fmt: skip
    except httpx.HTTPError as exc:
        raise DataError(f"could not reach Tiingo ({type(exc).__name__})") from exc
    if response.status_code in (401, 403):
        raise DataError("Tiingo refused the key: check TIINGO_API_KEY")
    if response.status_code == 404:
        return []  # no such fund at Tiingo
    if response.status_code == 429:
        raise DataError("Tiingo: too many requests for the free plan today; try again tomorrow")
    if response.status_code >= 400:
        raise DataError(f"Tiingo answered HTTP {response.status_code}")
    return parse_tiingo(ticker, response.json())


def fetch_tbill(client: httpx.Client, key: str, start: date) -> list[dict[str, Any]]:
    response = client.get(FRED_OBSERVATIONS, timeout=30, params={
        "series_id": TBILL, "api_key": key, "file_type": "json", "observation_start": start.isoformat()})  # fmt: skip
    if response.status_code >= 400:
        raise DataError(f"FRED answered HTTP {response.status_code} (check FRED_API_KEY)")
    rows = []
    for item in response.json().get("observations", []):
        if item.get("value") in (None, ".", ""):
            continue
        rows.append({"ticker": TBILL, "day": date.fromisoformat(item["date"]), "open": None, "high": None,
                     "low": None, "close": float(item["value"]), "volume": None})  # fmt: skip
    return rows


# --------------------------------------------------------------------------- Alpaca (live)


def parse_alpaca(raw: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for ticker, bars in raw.items():
        for bar in bars or []:
            day = datetime.fromisoformat(str(bar["t"]).replace("Z", "+00:00")).date()
            rows.append({"ticker": ticker, "day": day, "open": float(bar["o"]), "high": float(bar["h"]),
                         "low": float(bar["l"]), "close": float(bar["c"]), "volume": float(bar.get("v", 0))})  # fmt: skip
    return rows


def live_tickers(cfg: PortfolioConfig) -> list[str]:
    return sorted({f.ticker for f in cfg.funds} | {"SPY"})


def refresh_live(session: Session, bars: Any, cfg: PortfolioConfig, today: date) -> int:
    """`bars(tickers, timeframe, start, end)` is the shared Alpaca data client's method."""
    from papertrader.ingest.timeutil import at_new_york

    start = today - timedelta(days=LIVE_DAYS)
    raw = bars(live_tickers(cfg), "1Day", at_new_york(start, datetime.min.time()),
               at_new_york(today + timedelta(days=1), datetime.min.time()))  # fmt: skip
    return store(session, "alpaca", parse_alpaca(raw))


# --------------------------------------------------------------------------- the cash fund before it existed


def with_cash_history(
    closes: pd.DataFrame, cfg: PortfolioConfig, tbill_rate: pd.Series | None
) -> pd.DataFrame:
    """The T-bill fund's history extended back: its stand-in (BIL) first, then the T-bill rate.

    So that upgrade 1 can be tested over the whole period, although SGOV only started in
    2020. Before BIL (2007) the fund grows at the 3-month T-bill rate.
    """
    fund = cfg.by_ticker().get(cfg.cash_fund)
    out = closes.copy()
    own = series_or_empty(out, cfg.cash_fund)
    pieces: list[pd.Series] = []
    if tbill_rate is not None and not tbill_rate.empty:
        daily = (1 + tbill_rate.reindex(out.index).ffill().fillna(0) / 100 / 252).cumprod()
        pieces.append(daily)
    if fund is not None and fund.stand_in:
        pieces.append(series_or_empty(out, fund.stand_in))
    pieces.append(own)
    combined = pd.Series(dtype=float)
    for piece in pieces:
        if piece.empty:
            continue
        if combined.empty:
            combined = piece.copy()
            continue
        first = piece.index[0]
        if first in combined.index and combined[first] > 0:
            piece = piece * (
                combined[first] / piece.iloc[0]
            )  # chain on the day the newer one starts
        combined = pd.concat([combined[combined.index < first], piece])
    if not combined.empty:
        out[cfg.cash_fund] = combined.reindex(out.index)
    return out


def series_or_empty(frame: pd.DataFrame, ticker: str) -> pd.Series:
    return frame[ticker].dropna() if ticker in frame else pd.Series(dtype=float)


def tbill_12m(closes: pd.DataFrame, cfg: PortfolioConfig) -> float | None:
    """The T-bill fund's last 12-month return (for upgrade 2), when known."""
    s = series_or_empty(closes, cfg.cash_fund)
    if len(s) <= 252:
        return None
    return float(s.iloc[-1] / s.iloc[-253] - 1)
