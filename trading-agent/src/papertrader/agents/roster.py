"""The research agents. Each one turns the shared data snapshot into its own context.

An agent that has nothing to look at returns None and is skipped without an API call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from papertrader.agents import prompts
from papertrader.agents.context import ContextItem, ContextPack, clip, pct, stamp
from papertrader.agents.indicators import unusual_score
from papertrader.agents.schemas import AgentReport
from papertrader.agents.snapshot import SECTOR_ETF, EarningsView, NewsView, Snapshot
from papertrader.config import GICS_SECTORS, SECTOR_AGENT, AgentsConfig
from papertrader.ingest.timeutil import daily_bar_available_at

# What the item numbers on an 8-K mean (the common ones).
FORM_8K_ITEMS = {
    "1.01": "entry into a material agreement",
    "1.02": "termination of a material agreement",
    "1.03": "bankruptcy or receivership",
    "2.01": "completed acquisition or disposal of assets",
    "2.02": "results of operations (earnings release)",
    "2.03": "new debt or financial obligation",
    "2.05": "exit or restructuring costs",
    "2.06": "material impairment",
    "3.01": "delisting notice",
    "4.01": "change of auditor",
    "4.02": "earlier financial statements can no longer be relied on",
    "5.02": "director or officer departure / appointment",
    "5.03": "change to articles or bylaws",
    "5.07": "shareholder vote results",
    "7.01": "Regulation FD disclosure",
    "8.01": "other events",
    "9.01": "financial statements and exhibits",
}


SESSION_WORDS = {"bmo": "before the open", "amc": "after the close", "dmh": "during market hours"}


@dataclass(frozen=True)
class AgentSpec:
    name: str  # configuration name, e.g. "sector_analyst"
    label: str  # unique within a cycle, e.g. "sector_analyst:Energy"
    prompt: str
    sector: str | None = None


def specs() -> list[AgentSpec]:
    """All agents except the contrarian, which runs after them."""
    first = [
        AgentSpec("macro", "macro", prompts.MACRO),
        AgentSpec("earnings", "earnings", prompts.EARNINGS),
        AgentSpec("sec_filings", "sec_filings", prompts.SEC_FILINGS),
        AgentSpec("news_event", "news_event", prompts.NEWS_EVENT),
        AgentSpec("technicals", "technicals", prompts.TECHNICALS),
        AgentSpec("insider_institutional", "insider_institutional", prompts.INSIDER),
    ]
    sectors = [
        AgentSpec(SECTOR_AGENT, f"{SECTOR_AGENT}:{sector}", prompts.SECTOR, sector)
        for sector in GICS_SECTORS
    ]
    return first + sectors


CONTRARIAN = AgentSpec("contrarian", "contrarian", prompts.CONTRARIAN)


# --------------------------------------------------------------------------- shared items


def price_item(snap: Snapshot, ticker: str) -> ContextItem | None:
    ind = snap.indicators.get(ticker)
    if ind is None:
        return None
    member = snap.members.get(ticker)
    parts = [f"{ticker}" + (f" ({member.name})" if member else "")]
    parts.append(f"close {ind.last_close:.2f} on {ind.last_day:%m-%d}")
    parts.append(
        f"return 1d {pct(ind.ret_1d)}, 5d {pct(ind.ret_5d)}, 20d {pct(ind.ret_20d)}, "
        f"60d {pct(ind.ret_60d)}"
    )
    etf = SECTOR_ETF.get(member.sector or "") if member else None
    peer = snap.indicators.get(etf) if etf else None
    if peer and ind.ret_20d is not None and peer.ret_20d is not None:
        parts.append(f"20d vs sector fund {etf} {pct(ind.ret_20d - peer.ret_20d)}")
    if ind.rsi14 is not None:
        parts.append(f"RSI {ind.rsi14:.0f}")
    if ind.volume_ratio is not None:
        parts.append(f"volume {ind.volume_ratio:.1f}x its 20-day average")
    if ind.gap_pct is not None:
        parts.append(f"opening gap {pct(ind.gap_pct)}")
    if ind.from_52w_high is not None and ind.from_52w_low is not None:
        parts.append(f"52-week high {pct(ind.from_52w_high)}, low {pct(ind.from_52w_low)}")
    if ind.atr_pct is not None:
        parts.append(f"daily volatility (ATR) {ind.atr_pct * 100:.1f}%")
    if ind.above_sma50 is not None:
        parts.append(("above" if ind.above_sma50 else "below") + " its 50-day average")
    # The benchmark and the sector funds are market-level context: any report may cite
    # them for comparison. A stock's own price line belongs to that stock only.
    about = frozenset({ticker}) if member else frozenset()
    return ContextItem(
        f"px:{ticker}", " | ".join(parts), daily_bar_available_at(ind.last_day), tickers=about
    )


def price_items(snap: Snapshot, tickers: Sequence[str]) -> list[ContextItem]:
    return [item for t in tickers if (item := price_item(snap, t)) is not None]


def news_item(news: NewsView, covered: frozenset[str]) -> ContextItem:
    about = sorted(news.tickers & covered) or sorted(news.tickers)[:4]
    who = ", ".join(about[:6]) or "general"
    text = f"{stamp(news.available_at)} | {who} | {clip(news.headline, 200)}"
    if news.summary and news.summary.strip() and news.summary.strip() != news.headline.strip():
        text += f" - {clip(news.summary, 220)}"
    return ContextItem(f"news:{news.id}", text, news.available_at, news.url, news.tickers)


def focused_news(snap: Snapshot, covered: frozenset[str], limit: int) -> list[NewsView]:
    """Fresh news about covered stocks. Roundups naming many companies go last."""
    relevant = [item for item in snap.news if item.tickers & covered]
    relevant.sort(key=lambda item: (len(item.tickers) > 4, -item.available_at.timestamp()))
    return relevant[:limit]


def earnings_item(view: EarningsView) -> ContextItem:
    parts = [f"{view.ticker} | report date {view.report_date:%m-%d}"]
    if view.time_of_day:
        parts.append(SESSION_WORDS.get(view.time_of_day, view.time_of_day))
    if view.released_at is None:
        parts.append("NOT YET REPORTED")
        if view.eps_estimate is not None:
            parts.append(f"EPS estimate {view.eps_estimate:.2f}")
    else:
        parts.append(f"results public from {stamp(view.released_at)}")
        if view.eps_actual is not None and view.eps_estimate:
            surprise = view.eps_actual / view.eps_estimate - 1
            parts.append(f"EPS {view.eps_actual:.2f} vs estimate {view.eps_estimate:.2f} "
                         f"({pct(surprise)})")  # fmt: skip
        elif view.eps_actual is not None:
            parts.append(f"EPS {view.eps_actual:.2f}, no estimate available")
        else:
            parts.append("figures not available here (release time known from the 8-K filing)")
        if view.revenue_actual and view.revenue_estimate:
            parts.append(f"revenue {view.revenue_actual / 1e9:.2f}bn vs estimate "
                         f"{view.revenue_estimate / 1e9:.2f}bn "
                         f"({pct(view.revenue_actual / view.revenue_estimate - 1)})")  # fmt: skip
    available = view.released_at or view.available_at or daily_bar_available_at(view.report_date)
    return ContextItem(
        f"earn:{view.ticker}:{view.report_date:%Y%m%d}", " | ".join(parts),
        available, tickers=frozenset({view.ticker}),
    )  # fmt: skip


# --------------------------------------------------------------------------- one builder per agent


def build_macro(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    funds = [snap.benchmark, *SECTOR_ETF.values()]
    available = [t for t in funds if t in snap.indicators]
    if not available:
        return None
    pack = ContextPack(snap.as_of, frozenset(available))
    macro = []
    for view in snap.macro:

        def fmt(value: float | None) -> str:
            return "n/a" if value is None else f"{value:,.2f}"

        macro.append(
            ContextItem(
                f"macro:{view.series_id}",
                f"{view.label}: {fmt(view.latest)} on {view.latest_date:%Y-%m-%d} | about a month "
                f"earlier {fmt(view.month_ago)} | three months earlier {fmt(view.quarter_ago)} | "
                f"a year earlier {fmt(view.year_ago)}",
                view.available_at,
            )
        )
    pack.add("Macro readings (as published by the decision time)", macro)
    if not macro:
        pack.notes.append("No macro series are available; only fund performance is shown.")
    names = {etf: sector for sector, etf in SECTOR_ETF.items()}
    fund_items = []
    for ticker in available:
        item = price_item(snap, ticker)
        if item is not None:
            label = names.get(ticker, "S&P 500")
            fund_items.append(ContextItem(item.source_id, f"[{label}] {item.text}",
                                          item.available_at, tickers=item.tickers))  # fmt: skip
    pack.add("Sector funds", fund_items)
    return pack


def build_earnings(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    covered = frozenset(snap.covered)
    events = [e for e in snap.earnings if e.ticker in covered]
    reported = [e for e in events if e.released_at is not None]
    upcoming = [e for e in events if e.released_at is None]
    if not reported and not upcoming:
        return None
    reported.sort(key=lambda e: e.released_at, reverse=True)  # type: ignore[arg-type,return-value]
    reported = reported[: cfg.max_tickers_in_context]
    upcoming = upcoming[: cfg.max_tickers_in_context]
    tickers = sorted({e.ticker for e in reported + upcoming})
    pack = ContextPack(snap.as_of, frozenset(tickers))
    pack.add("Results just reported", [earnings_item(e) for e in reported])
    pack.add("Reports due in the next few days", [earnings_item(e) for e in upcoming])
    pack.add("Prices", price_items(snap, tickers))
    news = [n for t in sorted({e.ticker for e in reported}) for n in snap.news_for(t, 4)]
    unique = list({n.id: n for n in news}.values())[: cfg.max_news_items]
    pack.add("Related news", [news_item(n, covered) for n in unique])
    return pack


def build_sec_filings(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    covered = frozenset(snap.covered)
    filings = [f for f in snap.filings if f.ticker in covered][: cfg.max_tickers_in_context * 2]
    if not filings:
        return None
    items = []
    for filing in filings:
        text = f"{stamp(filing.available_at)} | {filing.ticker} | form {filing.form_type}"
        if filing.items:
            meanings = [f"{code} {FORM_8K_ITEMS.get(code.strip(), 'other')}"
                        for code in filing.items.split(",")]  # fmt: skip
            text += " | items: " + "; ".join(meanings)
        if filing.excerpt:
            text += f" | excerpt: {clip(filing.excerpt, 1500)}"
        elif filing.form_type.startswith("8-K"):
            text += " | (document text not available, item codes only)"
        items.append(ContextItem(f"filing:{filing.id}", text, filing.available_at, filing.url,
                                 frozenset({filing.ticker})))  # fmt: skip
    tickers = sorted({f.ticker for f in filings})
    pack = ContextPack(snap.as_of, frozenset(tickers))
    pack.add("New SEC filings", items)
    pack.add("Prices", price_items(snap, tickers))
    return pack


def build_sector(snap: Snapshot, cfg: AgentsConfig, sector: str) -> ContextPack | None:
    tickers = snap.sector_tickers(sector)
    if not tickers:
        return None
    members = frozenset(tickers)
    news = focused_news(snap, members, cfg.max_news_items // 3)
    filings = [f for f in snap.filings if f.ticker in members]
    earnings = [e for e in snap.earnings if e.ticker in members]
    if not news and not filings and not earnings:
        return None  # prices alone are the technical analyst's job

    # Show every stock that has news, plus the biggest movers, up to the limit.
    with_news = {t for item in news for t in item.tickers & members}
    with_news |= {f.ticker for f in filings} | {e.ticker for e in earnings}

    def move(ticker: str) -> float:
        return abs(snap.indicators[ticker].ret_5d or 0.0)

    ordered = sorted(with_news, key=move, reverse=True)
    ordered += sorted((t for t in tickers if t not in with_news), key=move, reverse=True)
    shown = sorted(ordered[: cfg.max_tickers_in_context])

    pack = ContextPack(snap.as_of, frozenset(shown))
    pack.notes.append(f"Your sector: {sector}. Sector fund: {SECTOR_ETF[sector]}.")
    fund = price_item(snap, SECTOR_ETF[sector])
    pack.add("Sector fund", [fund] if fund else [])
    pack.add("Stocks", price_items(snap, shown))
    pack.add("Fresh news", [news_item(n, members) for n in news])
    pack.add("Earnings dates", [earnings_item(e) for e in earnings if e.ticker in shown])
    pack.add(
        "New filings",
        [ContextItem(f"filing:{f.id}", f"{stamp(f.available_at)} | {f.ticker} | form "
                     f"{f.form_type}" + (f" items {f.items}" if f.items else ""),
                     f.available_at, f.url, frozenset({f.ticker}))
         for f in filings if f.ticker in shown],
    )  # fmt: skip
    return pack


def build_news(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    covered = frozenset(snap.covered)
    news = focused_news(snap, covered, cfg.max_news_items)
    if not news:
        return None
    tickers = sorted({t for item in news for t in item.tickers & covered})
    pack = ContextPack(snap.as_of, frozenset(tickers))
    pack.add("Fresh headlines", [news_item(n, covered) for n in news])
    # Prices only for stocks named in few-company stories, to keep the prompt bounded.
    focus = sorted({t for item in news if len(item.tickers) <= 4 for t in item.tickers & covered})
    pack.add("Prices", price_items(snap, focus[: cfg.max_tickers_in_context * 2]))
    return pack


def build_technicals(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    ranked = sorted(snap.covered, key=lambda t: unusual_score(snap.indicators[t]), reverse=True)
    shown = sorted(ranked[: cfg.max_tickers_in_context])
    if not shown:
        return None
    pack = ContextPack(snap.as_of, frozenset(shown))
    pack.notes.append("These are the stocks with the most unusual readings today.")
    market = price_item(snap, snap.benchmark)
    pack.add("Market", [market] if market else [])
    pack.add("Stocks", price_items(snap, shown))
    return pack


def build_insider(snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    covered = frozenset(snap.covered)
    views = [v for v in snap.insiders.values() if v.ticker in covered]
    # Purchases first (rarer and more telling), then by money involved.
    views.sort(key=lambda v: (len(v.buyers), v.bought_usd + v.sold_usd), reverse=True)
    insider_items = []
    for view in views[: cfg.max_tickers_in_context]:
        text = (
            f"{view.ticker} | last 30 days, open-market trades | {len(view.buyers)} insider(s) "
            f"bought ${view.bought_usd:,.0f} | {len(view.sellers)} sold ${view.sold_usd:,.0f}"
        )
        if view.sold_usd > 0:
            text += f" ({view.planned_sold_usd / view.sold_usd:.0%} under pre-arranged plans)"
        if view.roles:
            text += " | roles: " + ", ".join(sorted(view.roles)[:4])
        insider_items.append(ContextItem(f"insider:{view.ticker}", text, view.latest or snap.as_of,
                                         tickers=frozenset({view.ticker})))  # fmt: skip

    by_ticker: dict[str, list[str]] = {}
    latest: dict[str, datetime] = {}
    for change in sorted(snap.holdings, key=lambda c: c.value_usd, reverse=True):
        if change.ticker not in covered:
            continue
        if change.previous_shares is None:
            what = f"holds {change.shares:,.0f} shares (no earlier filing to compare)"
        elif change.previous_shares == 0:
            what = f"NEW position, {change.shares:,.0f} shares"
        else:
            what = (f"{pct(change.shares / change.previous_shares - 1, 0)} to "
                    f"{change.shares:,.0f} shares")  # fmt: skip
        by_ticker.setdefault(change.ticker, []).append(
            f"{change.manager} {what} (${change.value_usd / 1e6:,.0f}m, quarter ending "
            f"{change.period:%Y-%m-%d})"
        )
        latest[change.ticker] = max(
            latest.get(change.ticker, change.available_at), change.available_at
        )
    fund_items = [
        ContextItem(f"13f:{ticker}", f"{ticker} | " + "; ".join(lines[:4]), latest[ticker],
                    tickers=frozenset({ticker}))
        for ticker, lines in list(by_ticker.items())[: cfg.max_tickers_in_context]
    ]  # fmt: skip
    if not insider_items and not fund_items:
        return None
    tickers = sorted({next(iter(i.tickers)) for i in insider_items + fund_items})
    pack = ContextPack(snap.as_of, frozenset(tickers))
    pack.add("Insider trades (Form 4)", insider_items)
    pack.add("Fund position changes (13F, newly published)", fund_items)
    pack.add("Prices", price_items(snap, tickers))
    return pack


def build_contrarian(
    snap: Snapshot, cfg: AgentsConfig, reports: Sequence[AgentReport]
) -> ContextPack | None:
    """The strongest ideas of the other agents, plus the raw material to test them."""
    covered = frozenset(snap.covered)
    ideas = [r for r in reports if r.direction != "neutral" and r.ticker in covered]
    ideas.sort(key=lambda r: r.confidence, reverse=True)
    targets: list[str] = []
    for report in ideas:
        if report.ticker not in targets:
            targets.append(report.ticker)
    targets = targets[: cfg.contrarian_targets]
    if not targets:
        return None

    pack = ContextPack(snap.as_of, frozenset(targets))
    claims = []
    for index, report in enumerate(r for r in ideas if r.ticker in targets):
        cited = ", ".join(e.source_id for e in report.evidence)
        claims.append(
            ContextItem(
                f"idea:{index + 1}",
                f"{report.agent_name} says {report.direction.upper()} {report.ticker}, confidence "
                f"{report.confidence:.2f}, {report.horizon_days} days: {report.thesis} "
                f"(their sources: {cited})",
                snap.as_of, tickers=frozenset({report.ticker}),
            )
        )  # fmt: skip
    pack.add("Ideas to challenge", claims)
    pack.add("Prices", price_items(snap, [*targets, snap.benchmark]))
    news = [n for t in targets for n in snap.news_for(t, 6)]
    pack.add("Fresh news on these stocks",
             [news_item(n, covered) for n in {n.id: n for n in news}.values()])  # fmt: skip
    pack.add("Earnings", [earnings_item(e) for e in snap.earnings if e.ticker in targets])
    insiders = [snap.insiders[t] for t in targets if t in snap.insiders]
    pack.add(
        "Insider trades, last 30 days",
        [ContextItem(f"insider:{v.ticker}",
                     f"{v.ticker} | {len(v.buyers)} bought ${v.bought_usd:,.0f} | "
                     f"{len(v.sellers)} sold ${v.sold_usd:,.0f}",
                     v.latest or snap.as_of, tickers=frozenset({v.ticker}))
         for v in insiders],
    )  # fmt: skip
    return pack


def build(spec: AgentSpec, snap: Snapshot, cfg: AgentsConfig) -> ContextPack | None:
    if spec.sector is not None:
        return build_sector(snap, cfg, spec.sector)
    builder = {
        "macro": build_macro,
        "earnings": build_earnings,
        "sec_filings": build_sec_filings,
        "news_event": build_news,
        "technicals": build_technicals,
        "insider_institutional": build_insider,
    }[spec.name]
    return builder(snap, cfg)
