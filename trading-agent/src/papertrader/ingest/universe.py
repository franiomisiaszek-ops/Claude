"""The stock universe: index members with sector, SEC identifier and market cap."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from html.parser import HTMLParser
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.config import GICS_SECTORS, AppConfig
from papertrader.db.models import UniverseMember
from papertrader.db.types import utcnow
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.http import FetchError
from papertrader.ingest.store import get_cursor, set_cursor
from papertrader.ingest.timeutil import new_york_date, parse_utc

log = logging.getLogger(__name__)

INDEX_PAGES = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "sp100": "https://en.wikipedia.org/wiki/S%26P_100",
}
# A parsed list far outside these sizes means the page layout changed; we then keep
# the old universe rather than wiping it.
EXPECTED_SIZE = {"sp500": (480, 520), "sp100": (95, 105)}
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FINNHUB_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"

_SECTOR_BY_LOWER = {sector.lower(): sector for sector in GICS_SECTORS}


class UniverseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Constituent:
    ticker: str
    name: str
    sector: str | None = None
    sub_industry: str | None = None
    cik: str | None = None
    added_on: date | None = None


def plain_symbol(ticker: str) -> str:
    """BRK.B, BRK-B and BRKB all become BRKB, for matching across data sources."""
    return "".join(ch for ch in ticker.upper() if ch.isalnum())


class _Tables(HTMLParser):
    """Collects every HTML table as rows of plain cell text."""

    _SKIP = frozenset({"style", "script", "sup"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._stack: list[list[list[str]]] = []
        self._cell: list[str] | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "table":
            self._stack.append([])
        elif tag == "tr" and self._stack:
            self._stack[-1].append([])
        elif tag in ("td", "th") and self._stack and self._stack[-1]:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("td", "th") and self._cell is not None and self._stack:
            self._stack[-1][-1].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "table" and self._stack:
            self.tables.append(self._stack.pop())

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._skip_depth:
            self._cell.append(data)


def _column(header: list[str], *names: str) -> int | None:
    lowered = [cell.lower() for cell in header]
    for name in names:
        for index, cell in enumerate(lowered):
            if cell == name:
                return index
    for name in names:
        for index, cell in enumerate(lowered):
            if name in cell:
                return index
    return None


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def parse_constituents(html: str) -> list[Constituent]:
    """Find the index-members table on a Wikipedia index page and read it."""
    parser = _Tables()
    parser.feed(html)

    best: list[Constituent] = []
    for table in parser.tables:
        if len(table) < 2:
            continue
        header = table[0]
        sym = _column(header, "symbol", "ticker")
        name = _column(header, "security", "company", "name")
        if sym is None or name is None:
            continue
        sector = _column(header, "gics sector", "sector")
        sub = _column(header, "gics sub-industry", "sub-industry")
        cik = _column(header, "cik")
        added = _column(header, "date added")

        def cell(row: list[str], index: int | None) -> str:
            return row[index].strip() if index is not None and index < len(row) else ""

        found: dict[str, Constituent] = {}
        for row in table[1:]:
            ticker = cell(row, sym).upper().replace(" ", "")
            if not ticker or not ticker[0].isalpha() or len(ticker) > 8:
                continue
            raw_cik = "".join(ch for ch in cell(row, cik) if ch.isdigit())
            found[ticker] = Constituent(
                ticker=ticker,
                name=cell(row, name),
                sector=_SECTOR_BY_LOWER.get(cell(row, sector).lower()),
                sub_industry=cell(row, sub) or None,
                cik=raw_cik.zfill(10) if raw_cik else None,
                added_on=_parse_date(cell(row, added)),
            )
        if len(found) > len(best):
            best = list(found.values())
    return best


def parse_sec_tickers(payload: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """SEC company_tickers.json -> {plain symbol: (10-digit CIK, company name)}."""
    out: dict[str, tuple[str, str]] = {}
    for entry in payload.values():
        symbol = plain_symbol(str(entry["ticker"]))
        # The file lists the main share class first; keep the first one seen.
        out.setdefault(symbol, (str(entry["cik_str"]).zfill(10), str(entry["title"])))
    return out


def sync_universe(
    session: Session, index_name: str, constituents: list[Constituent], today: date
) -> tuple[int, int]:
    """Make the table match the list. Returns (added, removed). Removed rows are kept, inactive."""
    existing = {
        member.ticker: member
        for member in session.scalars(
            select(UniverseMember).where(UniverseMember.index_name == index_name)
        )
    }
    wanted = {c.ticker: c for c in constituents}
    added = removed = 0

    for ticker, c in wanted.items():
        member = existing.get(ticker)
        if member is None:
            session.add(
                UniverseMember(
                    index_name=index_name, ticker=ticker, name=c.name, gics_sector=c.sector,
                    gics_sub_industry=c.sub_industry, cik=c.cik, added_on=c.added_on,
                    is_active=True,
                )
            )  # fmt: skip
            added += 1
            continue
        member.name = c.name or member.name
        member.gics_sector = c.sector or member.gics_sector
        member.gics_sub_industry = c.sub_industry or member.gics_sub_industry
        member.cik = c.cik or member.cik
        member.added_on = c.added_on or member.added_on
        if not member.is_active:  # re-joined the index
            member.is_active = True
            member.removed_on = None
            added += 1

    for ticker, member in existing.items():
        if ticker not in wanted and member.is_active:
            member.is_active = False
            member.removed_on = today
            removed += 1
    return added, removed


async def ingest_universe(ctx: IngestContext) -> StepStats:
    """Refresh index membership (Wikipedia) and SEC identifiers (SEC)."""
    stats = StepStats()
    cfg: AppConfig = ctx.config
    index = cfg.universe.index

    sec_lookup: dict[str, tuple[str, str]] = {}
    if ctx.sec is not None:
        sec_lookup = parse_sec_tickers(await ctx.sec.get_json(SEC_TICKERS_URL))
        stats.requests += 1
    else:
        stats.note("SEC_USER_AGENT not set: SEC identifiers only where the index page has them")

    if index == "custom":
        constituents = [
            Constituent(ticker=t.upper(), name=sec_lookup.get(plain_symbol(t), ("", t.upper()))[1])
            for t in cfg.universe.custom_tickers
        ]
    else:
        html = await ctx.web.get_text(INDEX_PAGES[index])
        stats.requests += 1
        constituents = parse_constituents(html)
        low, high = EXPECTED_SIZE[index]
        if not low <= len(constituents) <= high:
            raise UniverseError(
                f"read {len(constituents)} members for {index}, expected {low}-{high}; the page "
                "layout may have changed. The stored universe was left untouched."
            )

    constituents = [
        c if c.cik else replace(c, cik=sec_lookup.get(plain_symbol(c.ticker), (None, ""))[0])
        for c in constituents
    ]

    with ctx.session_factory() as session:
        added, removed = sync_universe(session, index, constituents, new_york_date(ctx.now()))
        session.commit()
    stats.rows = added + removed
    missing_cik = sum(1 for c in constituents if not c.cik)
    stats.note(f"{len(constituents)} members ({added} added, {removed} removed)")
    if missing_cik:
        stats.note(f"{missing_cik} without an SEC identifier (no filings for those)")
    return stats


async def ingest_market_caps(
    ctx: IngestContext, tickers: list[str], max_age_days: int = 7
) -> StepStats:
    """Market capitalisation from Finnhub, refreshed at most once per `max_age_days` per ticker."""
    stats = StepStats()
    assert ctx.finnhub is not None
    now = ctx.now()
    for ticker in tickers:
        with ctx.session_factory() as session:
            last = get_cursor(session, "finnhub.profile", ticker).get("at")
        if last and (now - parse_utc(last)).days < max_age_days:
            continue
        try:
            profile = await ctx.finnhub.get_json(FINNHUB_PROFILE_URL, params={"symbol": ticker})
        except FetchError as exc:
            stats.note(f"{ticker}: {exc}")
            continue
        stats.requests += 1
        cap_millions = profile.get("marketCapitalization") if isinstance(profile, dict) else None
        with ctx.session_factory() as session:
            if cap_millions:
                for member in session.scalars(
                    select(UniverseMember).where(UniverseMember.ticker == ticker)
                ):
                    member.market_cap = float(cap_millions) * 1_000_000
                    member.updated_at = utcnow()
                stats.rows += 1
            set_cursor(session, "finnhub.profile", ticker, {"at": now.isoformat()})
            session.commit()
    return stats


def active_members(session: Session, index_name: str) -> list[UniverseMember]:
    return list(
        session.scalars(
            select(UniverseMember)
            .where(UniverseMember.index_name == index_name, UniverseMember.is_active)
            .order_by(UniverseMember.ticker)
        )
    )


def pick_spread(members: list[UniverseMember], limit: int) -> list[UniverseMember]:
    """Pick `limit` members spread evenly across sectors (for small test backfills)."""
    by_sector: dict[str, list[UniverseMember]] = {}
    for member in sorted(members, key=lambda m: m.ticker):
        by_sector.setdefault(member.gics_sector or "", []).append(member)
    picked: list[UniverseMember] = []
    queues = [by_sector[key] for key in sorted(by_sector)]
    while len(picked) < limit and any(queues):
        for queue in queues:
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked
