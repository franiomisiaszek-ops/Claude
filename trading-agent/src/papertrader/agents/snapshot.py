"""Everything the agents may look at, read from the database as of one moment.

This is the single point-in-time gate for research: every query here filters on
`available_at <= as_of`. Agents never touch the database themselves, so nothing
published after the decision time can reach a prompt.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from papertrader.agents.indicators import Bar, Indicators, compute
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK, new_york_date

SECTOR_ETF = {
    "Communication Services": "XLC",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Energy": "XLE",
    "Financials": "XLF",
    "Health Care": "XLV",
    "Industrials": "XLI",
    "Information Technology": "XLK",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}
MACRO_LABELS = {
    "DFF": "Fed funds rate, %",
    "DGS2": "2-year Treasury yield, %",
    "DGS10": "10-year Treasury yield, %",
    "T10Y2Y": "Yield curve 10y minus 2y, pct points",
    "T10Y3M": "Yield curve 10y minus 3m, pct points",
    "VIXCLS": "VIX volatility index",
    "BAMLH0A0HYM2": "High-yield credit spread, pct points",
    "CPIAUCSL": "Consumer price index (level)",
    "CPILFESL": "Core CPI (level)",
    "PCEPILFE": "Core PCE price index (level)",
    "UNRATE": "Unemployment rate, %",
    "PAYEMS": "Nonfarm payrolls, thousands",
    "ICSA": "Initial jobless claims",
}
# Form 4 codes that are real buy/sell decisions. Grants (A), option exercises (M),
# tax withholding (F) and gifts (G) say little about what the insider thinks.
OPEN_MARKET_CODES = ("P", "S")
RESEARCH_FORMS = ("8-K", "8-K/A", "10-Q", "10-K")

BAR_HISTORY_DAYS = 400
STALE_AFTER_DAYS = 7
INSIDER_WINDOW = timedelta(days=30)
HOLDINGS_WINDOW = timedelta(days=20)
EARNINGS_BACK = timedelta(days=4)
EARNINGS_AHEAD_DAYS = 5
MAX_NEWS_ROWS = 4000


@dataclass(frozen=True)
class Member:
    ticker: str
    name: str
    sector: str | None
    market_cap: float | None


@dataclass(frozen=True)
class NewsView:
    id: int
    headline: str
    summary: str | None
    url: str | None
    available_at: datetime
    tickers: frozenset[str]


@dataclass(frozen=True)
class FilingView:
    id: int
    ticker: str
    form_type: str
    items: str | None
    url: str | None
    available_at: datetime
    excerpt: str | None


@dataclass
class EarningsView:
    ticker: str
    report_date: date
    time_of_day: str | None = None
    released_at: datetime | None = None  # None = not reported yet
    eps_actual: float | None = None
    eps_estimate: float | None = None
    revenue_actual: float | None = None
    revenue_estimate: float | None = None
    available_at: datetime | None = None
    row_id: int = 0


@dataclass
class InsiderView:
    ticker: str
    buyers: set[str] = field(default_factory=set)
    sellers: set[str] = field(default_factory=set)
    bought_usd: float = 0.0
    sold_usd: float = 0.0
    planned_sold_usd: float = 0.0  # sold under a pre-arranged 10b5-1 plan
    roles: set[str] = field(default_factory=set)
    latest: datetime | None = None


@dataclass(frozen=True)
class HoldingChange:
    ticker: str
    manager: str
    period: date
    shares: float
    previous_shares: float | None  # None = no earlier filing to compare with
    value_usd: float
    available_at: datetime


@dataclass(frozen=True)
class MacroView:
    series_id: str
    label: str
    latest_date: date
    latest: float
    month_ago: float | None
    quarter_ago: float | None
    year_ago: float | None
    available_at: datetime


@dataclass
class Snapshot:
    as_of: datetime
    since: datetime  # start of the "fresh news" window
    benchmark: str
    members: dict[str, Member]
    indicators: dict[str, Indicators]
    news: list[NewsView] = field(default_factory=list)
    filings: list[FilingView] = field(default_factory=list)
    earnings: list[EarningsView] = field(default_factory=list)
    insiders: dict[str, InsiderView] = field(default_factory=dict)
    holdings: list[HoldingChange] = field(default_factory=list)
    macro: list[MacroView] = field(default_factory=list)

    @property
    def covered(self) -> list[str]:
        """Universe stocks with recent prices: the only ones agents may recommend."""
        floor = new_york_date(self.as_of) - timedelta(days=STALE_AFTER_DAYS)
        return sorted(
            ticker
            for ticker in self.members
            if (ind := self.indicators.get(ticker)) is not None and ind.last_day >= floor
        )

    @property
    def prices_through(self) -> date | None:
        ind = self.indicators.get(self.benchmark)
        days = [i.last_day for i in self.indicators.values()]
        return ind.last_day if ind else (max(days) if days else None)

    def sector_tickers(self, sector: str) -> list[str]:
        return [t for t in self.covered if self.members[t].sector == sector]

    def news_for(self, ticker: str, limit: int) -> list[NewsView]:
        return [item for item in self.news if ticker in item.tickers][:limit]


def fresh_since(as_of: datetime, hours: int) -> datetime:
    """Start of the "fresh" window. Reaches back over the weekend on Sunday and Monday."""
    weekday = as_of.astimezone(NEW_YORK).weekday()
    extra = {0: 48, 6: 24}.get(weekday, 0)
    return as_of - timedelta(hours=hours + extra)


def _indicators(session: Session, tickers: list[str], as_of: datetime) -> dict[str, Indicators]:
    rows = session.execute(
        select(m.PriceBar.ticker, m.PriceBar.ts, m.PriceBar.open, m.PriceBar.high,
               m.PriceBar.low, m.PriceBar.close, m.PriceBar.volume)
        .where(
            m.PriceBar.timeframe == "1Day",
            m.PriceBar.ticker.in_(tickers),
            m.PriceBar.available_at <= as_of,  # point-in-time: finished bars only
            m.PriceBar.ts >= as_of - timedelta(days=BAR_HISTORY_DAYS),
        )
        .order_by(m.PriceBar.ticker, m.PriceBar.ts)
    )  # fmt: skip
    bars: dict[str, list[Bar]] = defaultdict(list)
    for ticker, ts, open_, high, low, close, volume in rows:
        bars[ticker].append(Bar(new_york_date(ts), open_, high, low, close, volume))
    return {t: ind for t, series in bars.items() if (ind := compute(series)) is not None}


def _news(session: Session, as_of: datetime, since: datetime) -> list[NewsView]:
    items = session.scalars(
        select(m.NewsItem)
        .where(
            m.NewsItem.available_at <= as_of,
            m.NewsItem.available_at > since,
            m.NewsItem.duplicate_of_id.is_(None),  # originals only
        )
        .options(selectinload(m.NewsItem.tickers))
        .order_by(m.NewsItem.available_at.desc(), m.NewsItem.id.desc())
        .limit(MAX_NEWS_ROWS)
    )
    return [
        NewsView(item.id, item.headline, item.summary, item.url, item.available_at,
                 frozenset(link.ticker for link in item.tickers))
        for item in items
    ]  # fmt: skip


def _filings(session: Session, as_of: datetime, since: datetime) -> list[FilingView]:
    rows = session.scalars(
        select(m.Filing)
        .where(
            m.Filing.form_type.in_(RESEARCH_FORMS),
            m.Filing.available_at <= as_of,
            m.Filing.available_at > since,
            m.Filing.ticker.is_not(None),
        )
        .order_by(m.Filing.available_at.desc(), m.Filing.id.desc())
    )
    return [
        FilingView(
            f.id, f.ticker or "", f.form_type, f.items, f.url, f.available_at, f.text_excerpt
        )
        for f in rows
    ]


def _earnings(session: Session, as_of: datetime) -> list[EarningsView]:
    today = new_york_date(as_of)
    rows = session.scalars(
        select(m.EarningsEvent)
        .where(
            m.EarningsEvent.available_at <= as_of,
            m.EarningsEvent.report_date >= today - EARNINGS_BACK,
            m.EarningsEvent.report_date <= today + timedelta(days=EARNINGS_AHEAD_DAYS),
        )
        .order_by(m.EarningsEvent.report_date, m.EarningsEvent.ticker, m.EarningsEvent.source)
    )
    merged: dict[tuple[str, date], EarningsView] = {}
    for row in rows:
        view = merged.setdefault(
            (row.ticker, row.report_date),
            EarningsView(row.ticker, row.report_date, available_at=row.available_at, row_id=row.id),
        )
        view.time_of_day = view.time_of_day or row.time_of_day
        # Results only count once they were public at `as_of`.
        if row.actual_available_at is not None and row.actual_available_at <= as_of:
            released = row.actual_available_at
            view.released_at = min(view.released_at, released) if view.released_at else released
            view.eps_actual = row.eps_actual if view.eps_actual is None else view.eps_actual
            if view.revenue_actual is None:
                view.revenue_actual = row.revenue_actual
        view.eps_estimate = row.eps_estimate if view.eps_estimate is None else view.eps_estimate
        if view.revenue_estimate is None:
            view.revenue_estimate = row.revenue_estimate
    return list(merged.values())


def _insiders(session: Session, as_of: datetime) -> dict[str, InsiderView]:
    rows = session.scalars(
        select(m.InsiderTransaction).where(
            m.InsiderTransaction.available_at <= as_of,
            m.InsiderTransaction.available_at > as_of - INSIDER_WINDOW,
            m.InsiderTransaction.transaction_code.in_(OPEN_MARKET_CODES),
        )
    )
    out: dict[str, InsiderView] = {}
    for row in rows:
        view = out.setdefault(row.ticker, InsiderView(row.ticker))
        value = row.shares * (row.price or 0.0)
        if row.transaction_code == "P":
            view.buyers.add(row.insider_name)
            view.bought_usd += value
        else:
            view.sellers.add(row.insider_name)
            view.sold_usd += value
            if row.under_10b5_1_plan:
                view.planned_sold_usd += value
        if row.insider_title:
            view.roles.add(row.insider_title)
        elif row.is_director:
            view.roles.add("Director")
        if view.latest is None or row.available_at > view.latest:
            view.latest = row.available_at
    return out


def _holdings(session: Session, as_of: datetime) -> list[HoldingChange]:
    fresh = session.scalars(
        select(m.InstitutionalHolding).where(
            m.InstitutionalHolding.available_at <= as_of,
            m.InstitutionalHolding.available_at > as_of - HOLDINGS_WINDOW,
        )
    ).all()
    if not fresh:
        return []
    managers = {row.manager_cik for row in fresh}
    earlier = session.scalars(
        select(m.InstitutionalHolding).where(
            m.InstitutionalHolding.manager_cik.in_(managers),
            m.InstitutionalHolding.available_at <= as_of - HOLDINGS_WINDOW,
        )
    )
    previous: dict[tuple[str, str], tuple[date, float]] = {}
    last_period: dict[str, date] = {}
    for row in earlier:
        key = (row.manager_cik, row.ticker or "")
        if key not in previous or row.report_period > previous[key][0]:
            previous[key] = (row.report_period, row.shares)
        if row.report_period > last_period.get(row.manager_cik, date.min):
            last_period[row.manager_cik] = row.report_period

    changes = []
    for row in fresh:
        if not row.ticker:
            continue
        before = previous.get((row.manager_cik, row.ticker))
        # Held in the manager's previous quarter -> that number; filed before but not
        # holding it then -> 0 (a new position); never filed before -> unknown.
        if before is not None and before[0] == last_period.get(row.manager_cik):
            prior: float | None = before[1]
        else:
            prior = 0.0 if row.manager_cik in last_period else None
        changes.append(
            HoldingChange(row.ticker, row.manager_name, row.report_period, row.shares, prior,
                          row.value_usd, row.available_at)
        )  # fmt: skip
    return changes


def _value_before(
    known: dict[date, tuple[float, datetime]], days: list[date], delta: int
) -> float | None:
    earlier = [d for d in days if d <= days[-1] - timedelta(days=delta)]
    return known[earlier[-1]][0] if earlier else None


def _macro(session: Session, config: AppConfig, as_of: datetime) -> list[MacroView]:
    views = []
    for series in config.ingest.fred_series:
        rows = session.execute(
            select(m.MacroObservation.observation_date, m.MacroObservation.realtime_start,
                   m.MacroObservation.value, m.MacroObservation.available_at)
            .where(
                m.MacroObservation.series_id == series.id,
                m.MacroObservation.available_at <= as_of,  # only versions published by then
                m.MacroObservation.value.is_not(None),
            )
            .order_by(m.MacroObservation.observation_date, m.MacroObservation.realtime_start)
        ).all()  # fmt: skip
        # For each date keep the newest version that had been published by `as_of`.
        known: dict[date, tuple[float, datetime]] = {
            row[0]: (row[2], row[3]) for row in rows if row[2] is not None
        }
        if not known:
            continue
        days = sorted(known)
        latest_day = days[-1]

        views.append(
            MacroView(series.id, MACRO_LABELS.get(series.id, series.id), latest_day,
                      known[latest_day][0], _value_before(known, days, 30),
                      _value_before(known, days, 91), _value_before(known, days, 365),
                      max(stamp for _value, stamp in known.values()))
        )  # fmt: skip
    return views


def build_snapshot(session: Session, config: AppConfig, as_of: datetime) -> Snapshot:
    members = {
        row.ticker: Member(row.ticker, row.name, row.gics_sector, row.market_cap)
        for row in session.scalars(
            select(m.UniverseMember).where(
                m.UniverseMember.index_name == config.universe.index, m.UniverseMember.is_active
            )
        )
    }
    price_tickers = [*members, config.universe.benchmark, *SECTOR_ETF.values()]
    since = fresh_since(as_of, config.agents.lookback_hours)
    return Snapshot(
        as_of=as_of,
        since=since,
        benchmark=config.universe.benchmark,
        members=members,
        indicators=_indicators(session, price_tickers, as_of),
        news=_news(session, as_of, since),
        filings=_filings(session, as_of, since),
        earnings=_earnings(session, as_of),
        insiders=_insiders(session, as_of),
        holdings=_holdings(session, as_of),
        macro=_macro(session, config, as_of),
    )
