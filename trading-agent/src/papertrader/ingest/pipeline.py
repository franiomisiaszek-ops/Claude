"""Runs the ingestion steps in order, for a historical backfill or the daily refresh.

Each step is independent: a missing API key skips its step, an error in one step
is reported and the others still run. Every step can be re-run safely.
"""

from __future__ import annotations

import logging
import time as clock
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select

from papertrader.db import models as m
from papertrader.ingest.bars import ingest_bars
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.earnings import ingest_earnings
from papertrader.ingest.http import FetchError
from papertrader.ingest.macro import ingest_macro
from papertrader.ingest.news import dedupe_news, ingest_alpaca_news, ingest_finnhub_news
from papertrader.ingest.sec import (
    ingest_13f,
    ingest_filings,
    ingest_form4_details,
    lookback,
    refresh_cusip_map,
)
from papertrader.ingest.timeutil import new_york_date
from papertrader.ingest.universe import (
    UniverseError,
    active_members,
    ingest_market_caps,
    ingest_universe,
    pick_spread,
)

log = logging.getLogger(__name__)

STEPS = ("universe", "cusip_map", "bars", "news", "finnhub_news", "filings", "insiders",
         "holdings_13f", "earnings", "macro", "market_caps", "dedupe")  # fmt: skip


DAILY_NEWS_LOOKBACK = timedelta(days=7)
DAILY_FILINGS_LOOKBACK = timedelta(days=30)
DAILY_FORM4_LOOKBACK = timedelta(days=14)
DAILY_HOLDINGS_LOOKBACK = timedelta(days=200)


NO_STOCKS = "no stocks to work on: the universe is empty (the universe step must succeed first)"


class SelectionError(ValueError):
    pass


@dataclass
class StepResult:
    name: str
    status: str  # ok | skipped | failed
    rows: int = 0
    requests: int = 0
    seconds: float = 0.0
    detail: str = ""


Step = tuple[str, str | None, Callable[[], Awaitable[StepStats]]]


async def _run(steps: Sequence[Step], only: Sequence[str] | None) -> list[StepResult]:
    results: list[StepResult] = []
    for name, missing, action in steps:
        if only and name not in only:
            continue
        if missing:
            results.append(StepResult(name, "skipped", detail=missing))
            log.info("ingest step skipped", extra={"step": name, "reason": missing})
            continue
        started = clock.monotonic()
        try:
            stats = await action()
        except Exception as exc:
            # A refused or failed download is an expected kind of failure: one clear line.
            # Anything else is a bug, so the full traceback is logged.
            expected = isinstance(exc, FetchError | UniverseError)
            log.error(
                "ingest step failed",
                extra={"step": name, "error": f"{type(exc).__name__}: {exc}"},
                exc_info=not expected,
            )
            results.append(
                StepResult(name, "failed", seconds=clock.monotonic() - started,
                           detail=f"{type(exc).__name__}: {exc}"[:300])
            )  # fmt: skip
            continue
        result = StepResult(name, "ok", stats.rows, stats.requests, clock.monotonic() - started,
                            "; ".join(stats.notes)[:300])  # fmt: skip
        results.append(result)
        log.info(
            "ingest step done",
            extra={"step": name, "rows": result.rows, "requests": result.requests,
                   "seconds": round(result.seconds, 1)},
        )  # fmt: skip
    return results


def _select(
    ctx: IngestContext, tickers: Sequence[str] | None, limit: int | None
) -> list[m.UniverseMember]:
    with ctx.session_factory() as session:
        members = active_members(session, ctx.config.universe.index)
    if tickers:
        by_ticker = {member.ticker: member for member in members}
        wanted = [t.strip().upper() for t in tickers if t.strip()]
        unknown = [t for t in wanted if t not in by_ticker]
        if unknown and not members:
            raise SelectionError(
                "the universe is empty, so tickers cannot be checked; the universe step "
                "has to succeed once first"
            )
        if unknown:
            raise SelectionError(
                f"not in the {ctx.config.universe.index} universe: {', '.join(unknown)}"
            )
        members = [by_ticker[t] for t in dict.fromkeys(wanted)]
    if limit:
        members = pick_spread(members, limit)
    return members


def _steps(
    ctx: IngestContext, members: list[m.UniverseMember], *, years: int, backfill: bool
) -> list[Step]:
    cfg = ctx.config
    now = ctx.now()
    start = lookback(now, years)
    start_day = new_york_date(start)
    if backfill:
        news_start, filings_since, holdings_since = start, start_day, start_day
        form4_since = lookback(now, min(years, cfg.ingest.form4_detail_years))
    else:
        # The morning run must finish well within the hour, so it only looks a short way
        # back. Deep history is the backfill's job; nothing already stored is lost.
        news_start = now - DAILY_NEWS_LOOKBACK
        filings_since = new_york_date(now - DAILY_FILINGS_LOOKBACK)
        holdings_since = new_york_date(now - DAILY_HOLDINGS_LOOKBACK)
        form4_since = now - DAILY_FORM4_LOOKBACK
    # Steps that work per stock have nothing to do without a stock list. Saying "ok"
    # there would hide the real problem, so they are reported as skipped.
    no_stocks = None if members else NO_STOCKS
    no_alpaca = None if ctx.alpaca else "ALPACA_API_KEY / ALPACA_SECRET_KEY not set"
    no_sec = (None if ctx.sec else "SEC_USER_AGENT not set") or no_stocks
    no_finnhub = (None if ctx.finnhub else "FINNHUB_API_KEY not set (optional)") or no_stocks
    no_fred = None if ctx.fred else "FRED_API_KEY not set (optional)"

    tickers = [member.ticker for member in members]
    price_tickers = [*tickers, cfg.universe.benchmark, *cfg.ingest.sector_etfs]

    async def dedupe() -> StepStats:
        stats = StepStats()
        since = start if backfill else now - timedelta(days=7)
        with ctx.session_factory() as session:
            stats.rows = dedupe_news(
                session, since=since, window_hours=cfg.ingest.news_dedupe_window_hours
            )
            session.commit()
        return stats

    return [
        ("cusip_map", no_sec, lambda: refresh_cusip_map(ctx)),
        ("bars", no_alpaca, lambda: ingest_bars(ctx, price_tickers, start=start_day)),
        (
            "news",
            no_alpaca or no_stocks,
            lambda: ingest_alpaca_news(ctx, tickers, start=news_start),
        ),
        (
            "finnhub_news",
            no_finnhub or (None if cfg.ingest.finnhub_news else "turned off in config.yaml"),
            lambda: ingest_finnhub_news(ctx, tickers),
        ),
        ("filings", no_sec, lambda: ingest_filings(ctx, members, since=filings_since)),
        ("insiders", no_sec, lambda: ingest_form4_details(ctx, since=form4_since)),
        ("holdings_13f", no_sec, lambda: ingest_13f(ctx, since=holdings_since)),
        (
            "earnings",
            no_finnhub,
            lambda: ingest_earnings(ctx, tickers, start=start_day, per_symbol=backfill),
        ),
        ("macro", no_fred, lambda: ingest_macro(ctx, start=start_day)),
        ("market_caps", no_finnhub, lambda: ingest_market_caps(ctx, tickers)),
        ("dedupe", None, dedupe),
    ]


async def run_backfill(
    ctx: IngestContext,
    *,
    years: int,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    only: Sequence[str] | None = None,
) -> list[StepResult]:
    """Load `years` of history. Safe to stop and start again: it continues where it was."""
    return await _universe_then(ctx, tickers, limit, only, years=years, backfill=True)


async def run_daily(ctx: IngestContext, *, only: Sequence[str] | None = None) -> list[StepResult]:
    """The morning refresh: everything new since the last run, for the whole universe."""
    years = ctx.config.backfill.years
    return await _universe_then(ctx, None, None, only, years=years, backfill=False)


async def _universe_then(
    ctx: IngestContext,
    tickers: Sequence[str] | None,
    limit: int | None,
    only: Sequence[str] | None,
    *,
    years: int,
    backfill: bool,
) -> list[StepResult]:
    """Refresh the universe first, then pick the tickers, then run everything else."""
    results = await _run([("universe", None, lambda: ingest_universe(ctx))], only)
    members = _select(ctx, tickers, limit)  # raises SelectionError before any other work
    return results + await _run(_steps(ctx, members, years=years, backfill=backfill), only)


def coverage(ctx: IngestContext) -> list[tuple[str, int, str]]:
    """(table, rows, date range) for the ingested tables - shown by `papertrader ingest status`."""
    spans = [
        ("universe_members", m.UniverseMember, None),
        ("price_bars", m.PriceBar, m.PriceBar.ts),
        ("news_items", m.NewsItem, m.NewsItem.published_at),
        ("filings", m.Filing, m.Filing.published_at),
        ("insider_transactions", m.InsiderTransaction, m.InsiderTransaction.transaction_date),
        ("institutional_holdings", m.InstitutionalHolding, m.InstitutionalHolding.report_period),
        ("earnings_events", m.EarningsEvent, m.EarningsEvent.report_date),
        ("macro_observations", m.MacroObservation, m.MacroObservation.observation_date),
        ("security_identifiers", m.SecurityIdentifier, None),
    ]
    out: list[tuple[str, int, str]] = []
    with ctx.session_factory() as session:
        for name, model, column in spans:
            count = session.scalar(select(func.count()).select_from(model)) or 0
            span = ""
            if column is not None and count:
                low, high = session.execute(select(func.min(column), func.max(column))).one()
                span = f"{str(low)[:10]} to {str(high)[:10]}"
            out.append((name, count, span))
    return out
