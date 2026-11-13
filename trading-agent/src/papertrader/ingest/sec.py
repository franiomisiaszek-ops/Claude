"""SEC EDGAR: filings (8-K, 10-Q, 10-K), insider trades (Form 4) and 13F holdings.

Rules the SEC sets and this module follows: every request carries the contact
string from SEC_USER_AGENT, and there are never more than 10 requests a second
(the shared `Fetcher` enforces the rate). All formats here were checked against
live EDGAR data on 2026-11-13.
"""

from __future__ import annotations

import asyncio
import io
import logging
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, time, timedelta
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET
from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.db.models import (
    EarningsEvent,
    Filing,
    InsiderTransaction,
    InstitutionalHolding,
    SecurityIdentifier,
    UniverseMember,
)
from papertrader.db.types import utcnow
from papertrader.ingest.context import IngestContext, StepStats
from papertrader.ingest.http import FetchError, NotFoundError
from papertrader.ingest.store import get_cursor, set_cursor, upsert
from papertrader.ingest.timeutil import at_new_york, new_york_date, parse_utc, session_part
from papertrader.ingest.universe import plain_symbol

log = logging.getLogger(__name__)

SUBMISSIONS_URL = "https://data.sec.gov/submissions"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data"
FTD_URL = "https://www.sec.gov/files/data/fails-deliver-data"

COMPANY_FORMS = frozenset({"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A", "4", "4/A"})
INSIDER_FORMS = ("4", "4/A")
MANAGER_FORMS = frozenset({"13F-HR", "13F-HR/A"})
EARNINGS_ITEM = "2.02"  # 8-K item "Results of Operations and Financial Condition"
# Before this date 13F values were reported in thousands of dollars; since then in dollars.
VALUE_IN_DOLLARS_FROM = date(2023, 1, 3)

DONE, FAILED, SKIPPED = "done", "failed", "skipped"


def filing_folder(cik: str, accession_no: str) -> str:
    return f"{ARCHIVES_URL}/{int(cik)}/{accession_no.replace('-', '')}"


def raw_document(primary_document: str) -> str:
    """'xslF345X06/form4.xml' is the styled view; the data is the same name without the folder."""
    return primary_document.rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- submissions


def iter_filings(block: dict[str, list[Any]]) -> Iterator[dict[str, Any]]:
    """EDGAR sends filings as parallel lists (one per field); turn them into one dict each."""
    for index in range(len(block.get("accessionNumber", []))):
        yield {key: values[index] for key, values in block.items() if index < len(values)}


async def fetch_filings(
    ctx: IngestContext, cik: str, *, since: date, forms: frozenset[str], deep: bool
) -> list[dict[str, Any]]:
    """Filings of one company/manager on or after `since`.

    The main file holds roughly the latest 1000 filings. `deep=True` also reads the
    older pages when they are needed to reach back to `since`.
    """
    assert ctx.sec is not None
    data = await ctx.sec.get_json(f"{SUBMISSIONS_URL}/CIK{cik.zfill(10)}.json")
    filings = data.get("filings", {})
    rows = list(iter_filings(filings.get("recent", {})))
    if deep:
        for page in filings.get("files", []):
            if str(page.get("filingTo", "")) >= since.isoformat():
                older = await ctx.sec.get_json(f"{SUBMISSIONS_URL}/{page['name']}")
                rows.extend(iter_filings(older))
    return [
        row for row in rows
        if row.get("form") in forms and str(row.get("filingDate", "")) >= since.isoformat()
    ]  # fmt: skip


def _filing_row(row: dict[str, Any], cik: str, ticker: str | None) -> dict[str, Any] | None:
    accession = str(row.get("accessionNumber") or "")
    accepted = str(row.get("acceptanceDateTime") or "")
    if not accession or not accepted:
        return None
    primary = str(row.get("primaryDocument") or "")
    form = str(row["form"])
    document = raw_document(primary) if form in INSIDER_FORMS or form in MANAGER_FORMS else primary
    report_date = str(row.get("reportDate") or "")
    accepted_at = parse_utc(accepted)  # true UTC (verified against the filing index page)
    return {
        "accession_no": accession,
        "cik": cik.zfill(10),
        "ticker": ticker,
        "form_type": form,
        "title": str(row.get("primaryDocDescription") or "") or None,
        "items": str(row.get("items") or "")[:255] or None,
        "period_of_report": date.fromisoformat(report_date) if report_date else None,
        "url": f"{filing_folder(cik, accession)}/{document}" if document else None,
        "text_path": None,
        "primary_document": document or None,
        "detail_status": None,
        "published_at": accepted_at,
        "available_at": accepted_at,
        "ingested_at": utcnow(),
    }


def _earnings_rows(filing: dict[str, Any], tickers: Sequence[str]) -> list[dict[str, Any]]:
    """An 8-K with item 2.02 marks the exact moment results were released."""
    items = {item.strip() for item in (filing["items"] or "").split(",")}
    if filing["form_type"] != "8-K" or EARNINGS_ITEM not in items:
        return []
    accepted: datetime = filing["published_at"]
    return [
        {
            "source": "sec_8k", "ticker": ticker, "report_date": new_york_date(accepted),
            "time_of_day": session_part(accepted), "fiscal_year": None, "fiscal_quarter": None,
            "eps_estimate": None, "eps_actual": None, "revenue_estimate": None,
            "revenue_actual": None, "actual_available_at": accepted, "available_at": accepted,
            "updated_at": utcnow(),
        }
        for ticker in tickers
    ]  # fmt: skip


async def ingest_filings(
    ctx: IngestContext, members: Sequence[UniverseMember], *, since: date
) -> StepStats:
    """Filing lists for every company. One company at a time, committed as it goes."""
    stats = StepStats()
    by_cik: dict[str, list[str]] = {}
    for member in members:
        if member.cik:
            by_cik.setdefault(member.cik, []).append(member.ticker)

    for cik, tickers in sorted(by_cik.items()):
        tickers = sorted(tickers)
        with ctx.session_factory() as session:
            covered = get_cursor(session, "sec.filings", cik).get("since")
        deep = covered is None or since < date.fromisoformat(covered)
        try:
            raw = await fetch_filings(ctx, cik, since=since, forms=COMPANY_FORMS, deep=deep)
        except FetchError as exc:
            stats.note(f"{tickers[0]}: {exc}")
            continue
        stats.requests += 1
        filings = [f for f in (_filing_row(row, cik, tickers[0]) for row in raw) if f]
        earnings = [event for f in filings for event in _earnings_rows(f, tickers)]

        with ctx.session_factory() as session:
            stats.rows += upsert(session, Filing, filings, conflict=("accession_no",))
            stats.rows += upsert(
                session, EarningsEvent, earnings, conflict=("source", "ticker", "report_date")
            )
            reached = min(since, date.fromisoformat(covered)) if covered else since
            set_cursor(session, "sec.filings", cik, {"since": reached.isoformat()})
            session.commit()
    return stats


# --------------------------------------------------------------------------- Form 4


@dataclass
class InsiderTrade:
    transaction_date: date
    code: str
    acquired: bool
    shares: float
    price: float | None
    shares_after: float | None
    security_title: str | None
    direct: bool | None


@dataclass
class Form4:
    owner_name: str
    owner_cik: str | None
    owner_title: str | None
    is_director: bool
    is_officer: bool
    is_ten_percent_owner: bool
    under_10b5_1_plan: bool | None
    trades: list[InsiderTrade] = dc_field(default_factory=list)


def _text(node: Element | None, path: str) -> str | None:
    if node is None:
        return None
    found = node.find(path)
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


def _flag(node: Element | None, path: str) -> bool:
    return (_text(node, path) or "").lower() in ("1", "true")


def _number(node: Element | None, path: str) -> float | None:
    text = _text(node, path)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def parse_form4(xml: str | bytes) -> Form4:
    """Read the ordinary (non-derivative) share transactions out of a Form 4."""
    root = SafeET.fromstring(xml)
    owners = root.findall("reportingOwner")
    first = owners[0] if owners else None
    relationships = [owner.find("reportingOwnerRelationship") for owner in owners]
    plan = _text(root, "aff10b5One")

    form = Form4(
        owner_name=_text(first, "reportingOwnerId/rptOwnerName") or "unknown",
        owner_cik=_text(first, "reportingOwnerId/rptOwnerCik"),
        owner_title=next((t for r in relationships if (t := _text(r, "officerTitle"))), None),
        is_director=any(_flag(r, "isDirector") for r in relationships),
        is_officer=any(_flag(r, "isOfficer") for r in relationships),
        is_ten_percent_owner=any(_flag(r, "isTenPercentOwner") for r in relationships),
        under_10b5_1_plan=None if plan is None else plan.lower() in ("1", "true"),
    )
    for node in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        when = _text(node, "transactionDate/value")
        shares = _number(node, "transactionAmounts/transactionShares/value")
        code = _text(node, "transactionCoding/transactionCode")
        direction = _text(node, "transactionAmounts/transactionAcquiredDisposedCode/value")
        if not when or shares is None or not code or direction not in ("A", "D"):
            continue
        ownership = _text(node, "ownershipNature/directOrIndirectOwnership/value")
        form.trades.append(
            InsiderTrade(
                transaction_date=date.fromisoformat(when[:10]),
                code=code[:4],
                acquired=direction == "A",
                shares=shares,
                price=_number(node, "transactionAmounts/transactionPricePerShare/value"),
                shares_after=_number(
                    node, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"
                ),
                security_title=(_text(node, "securityTitle/value") or "")[:128] or None,
                direct=None if ownership is None else ownership == "D",
            )
        )
    return form


async def _read_details(
    ctx: IngestContext, filings: Sequence[Filing], parallel: int = 5
) -> list[tuple[Filing, bytes | None, str | None]]:
    """Download one document per filing. Result: (filing, content, final status or None=retry)."""
    assert ctx.sec is not None
    sec = ctx.sec
    gate = asyncio.Semaphore(parallel)

    async def one(filing: Filing) -> tuple[Filing, bytes | None, str | None]:
        if not filing.url or not filing.url.lower().endswith(".xml"):
            return filing, None, SKIPPED  # old paper-style filing without structured data
        async with gate:
            try:
                return filing, await sec.get_bytes(filing.url), DONE
            except NotFoundError:
                return filing, None, FAILED
            except FetchError as exc:
                log.warning("filing download failed", extra={"filing": filing.accession_no,
                                                              "error": str(exc)})  # fmt: skip
                return filing, None, None  # temporary problem: try again next run

    return list(await asyncio.gather(*(one(f) for f in filings)))


async def ingest_form4_details(
    ctx: IngestContext, *, since: datetime, batch: int = 25
) -> StepStats:
    """Read each not-yet-read Form 4. Progress is saved per batch, so it can be interrupted."""
    stats = StepStats()
    attempted: set[int] = set()
    while True:
        with ctx.session_factory() as session:
            query = (
                select(Filing)
                .where(
                    Filing.form_type.in_(INSIDER_FORMS),
                    Filing.detail_status.is_(None),
                    Filing.published_at >= since,
                    Filing.ticker.is_not(None),
                )
                .order_by(Filing.published_at.desc())
                .limit(batch + len(attempted))
            )
            pending = [f for f in session.scalars(query) if f.id not in attempted][:batch]
            if not pending:
                break
            attempted.update(f.id for f in pending)

            for filing, content, status in await _read_details(ctx, pending):
                stats.requests += content is not None
                rows: list[dict[str, Any]] = []
                if content is not None:
                    try:
                        form = parse_form4(content)
                    except Exception:
                        log.warning("unreadable Form 4", extra={"filing": filing.accession_no})
                        status = FAILED
                    else:
                        rows = [
                            {
                                "filing_id": filing.id, "line_no": line, "ticker": filing.ticker,
                                "insider_name": form.owner_name[:255],
                                "insider_title": (form.owner_title or "")[:255] or None,
                                "owner_cik": form.owner_cik, "is_director": form.is_director,
                                "is_officer": form.is_officer,
                                "is_ten_percent_owner": form.is_ten_percent_owner,
                                "security_title": trade.security_title,
                                "direct_ownership": trade.direct,
                                "under_10b5_1_plan": form.under_10b5_1_plan,
                                "transaction_date": trade.transaction_date,
                                "transaction_code": trade.code, "acquired": trade.acquired,
                                "shares": trade.shares, "price": trade.price,
                                "shares_owned_after": trade.shares_after,
                                "available_at": filing.available_at,
                            }
                            for line, trade in enumerate(form.trades)
                        ]  # fmt: skip
                stats.rows += upsert(
                    session, InsiderTransaction, rows, conflict=("filing_id", "line_no")
                )
                if status is None:
                    stats.note(f"{filing.accession_no}: download failed, will retry next run")
                filing.detail_status = status
            session.commit()
    return stats


# --------------------------------------------------------------------------- CUSIP map


def ftd_file_names(today: date, count: int = 6) -> list[str]:
    """The newest `count` half-month fails-to-deliver files (a = days 1-15, b = the rest)."""
    names: list[str] = []
    year, month, half = today.year, today.month, "b" if today.day > 15 else "a"
    while len(names) < count:
        names.append(f"cnsfails{year}{month:02d}{half}.zip")
        if half == "b":
            half = "a"
        else:
            half = "b"
            month -= 1
            if month == 0:
                year, month = year - 1, 12
    return names


def parse_ftd(content: bytes) -> dict[str, tuple[str, str]]:
    """{CUSIP: (symbol, description)} from one fails-to-deliver zip file."""
    out: dict[str, tuple[str, str]] = {}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for name in archive.namelist():
            for line in archive.read(name).decode("latin-1").splitlines()[1:]:
                parts = line.split("|")
                if len(parts) >= 5 and len(parts[1]) == 9 and parts[2]:
                    out[parts[1]] = (parts[2].strip().upper(), parts[4].strip())
    return out


async def refresh_cusip_map(ctx: IngestContext) -> StepStats:
    """Learn which CUSIP belongs to which universe ticker (13F filings only give CUSIPs).

    The SEC's fails-to-deliver files list CUSIP and ticker side by side for most
    actively traded shares, with no key needed.
    """
    assert ctx.sec is not None
    stats = StepStats()
    with ctx.session_factory() as session:
        wanted = {plain_symbol(t): t for t in session.scalars(select(UniverseMember.ticker))}

    for name in ftd_file_names(new_york_date(ctx.now())):
        with ctx.session_factory() as session:
            if get_cursor(session, "sec.ftd", name).get("done"):
                continue
        try:
            content = await ctx.sec.get_bytes(f"{FTD_URL}/{name}")
            pairs = parse_ftd(content)
        except NotFoundError:
            continue  # not published yet (files appear a few weeks after the period)
        except (FetchError, zipfile.BadZipFile) as exc:
            stats.note(f"{name}: {exc}")
            continue
        stats.requests += 1
        rows = [
            {"cusip": cusip, "ticker": wanted[plain_symbol(symbol)],
             "description": description[:128] or None, "updated_at": utcnow()}
            for cusip, (symbol, description) in pairs.items()
            if plain_symbol(symbol) in wanted
        ]  # fmt: skip
        with ctx.session_factory() as session:
            stats.rows += upsert(
                session, SecurityIdentifier, rows, conflict=("cusip",), update=("ticker",),
                also_set=("description", "updated_at"),
            )  # fmt: skip
            set_cursor(session, "sec.ftd", name, {"done": True})
            session.commit()
    return stats


# --------------------------------------------------------------------------- 13F


@dataclass
class Holding:
    cusip: str
    issuer: str
    title_of_class: str | None
    shares: float
    value: float


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(node: Element, name: str) -> str | None:
    for child in node.iter():
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def parse_13f(xml: str | bytes) -> list[Holding]:
    """Share positions from a 13F information table, summed per CUSIP (options are left out)."""
    root = SafeET.fromstring(xml)
    totals: dict[str, Holding] = {}
    for node in root.iter():
        if _local(node.tag) != "infoTable":
            continue
        cusip = (_child_text(node, "cusip") or "").upper()
        if len(cusip) != 9 or _child_text(node, "putCall"):
            continue
        if (_child_text(node, "sshPrnamtType") or "SH").upper() != "SH":
            continue  # principal amount of bonds, not shares
        try:
            shares = float(_child_text(node, "sshPrnamt") or 0)
            value = float(_child_text(node, "value") or 0)
        except ValueError:
            continue
        holding = totals.get(cusip)
        if holding is None:
            totals[cusip] = Holding(
                cusip, _child_text(node, "nameOfIssuer") or "",
                (_child_text(node, "titleOfClass") or "")[:64] or None, shares, value,
            )  # fmt: skip
        else:
            holding.shares += shares
            holding.value += value
    return list(totals.values())


def pick_information_table(index: dict[str, Any]) -> str | None:
    """In a 13F folder the holdings are the XML file that is not the cover page."""
    candidates = [
        (int(item.get("size") or 0), str(item["name"]))
        for item in index.get("directory", {}).get("item", [])
        if str(item.get("name", "")).lower().endswith(".xml")
        and str(item["name"]).lower() != "primary_doc.xml"
    ]
    return max(candidates)[1] if candidates else None


async def ingest_13f(ctx: IngestContext, *, since: date) -> StepStats:
    """Quarterly holdings of the tracked institutions, for universe stocks only."""
    assert ctx.sec is not None
    stats = StepStats()

    with ctx.session_factory() as session:
        cusip_to_ticker = dict(
            session.execute(select(SecurityIdentifier.cusip, SecurityIdentifier.ticker)).all()
        )
    if not cusip_to_ticker:
        stats.note("no CUSIP map yet, so holdings cannot be matched to tickers; skipped")
        return stats

    for manager in ctx.config.ingest.managers_13f:
        try:
            raw = await fetch_filings(
                ctx, manager.cik, since=since, forms=MANAGER_FORMS, deep=False
            )
        except FetchError as exc:
            stats.note(f"{manager.name}: {exc}")
            continue
        stats.requests += 1
        filings = [f for f in (_filing_row(row, manager.cik, None) for row in raw) if f]

        with ctx.session_factory() as session:
            upsert(session, Filing, filings, conflict=("accession_no",))
            session.commit()
            pending = session.scalars(
                select(Filing).where(
                    Filing.cik == manager.cik,
                    Filing.form_type.in_(MANAGER_FORMS),
                    Filing.detail_status.is_(None),
                    Filing.published_at >= at_new_york(since, time(0, 0)),
                )
            ).all()
            for filing in pending:
                try:
                    holdings = await _read_13f(ctx, filing)
                except NotFoundError:
                    filing.detail_status = FAILED
                    continue
                except FetchError as exc:
                    stats.note(f"{manager.name} {filing.accession_no}: {exc} (will retry)")
                    continue
                stats.requests += 2
                filed_on = new_york_date(filing.published_at)
                scale = 1.0 if filed_on >= VALUE_IN_DOLLARS_FROM else 1000.0
                rows = [
                    {
                        "filing_id": filing.id, "line_no": 0, "manager_cik": manager.cik,
                        "manager_name": manager.name, "ticker": cusip_to_ticker[h.cusip],
                        "cusip": h.cusip, "title_of_class": h.title_of_class,
                        "report_period": filing.period_of_report or filed_on,
                        "shares": h.shares, "value_usd": h.value * scale,
                        "available_at": filing.available_at,
                    }
                    for h in holdings
                    if h.cusip in cusip_to_ticker
                ]  # fmt: skip
                stats.rows += upsert(
                    session, InstitutionalHolding, rows, conflict=("filing_id", "cusip", "line_no")
                )
                filing.detail_status = DONE
            session.commit()
    return stats


async def _read_13f(ctx: IngestContext, filing: Filing) -> list[Holding]:
    assert ctx.sec is not None
    folder = filing_folder(filing.cik, filing.accession_no)
    index = await ctx.sec.get_json(f"{folder}/index.json")
    name = pick_information_table(index)
    if name is None:
        raise NotFoundError(f"sec: no information table in {filing.accession_no}", status=404)
    return parse_13f(await ctx.sec.get_bytes(f"{folder}/{name}"))


def filings_for(session: Session, ticker: str, *, as_of: datetime) -> list[Filing]:
    """Filings of one company that were public at `as_of` (point-in-time read helper)."""
    cik = session.scalar(select(UniverseMember.cik).where(UniverseMember.ticker == ticker))
    if cik is None:
        return []
    return list(
        session.scalars(
            select(Filing)
            .where(Filing.cik == cik, Filing.available_at <= as_of)
            .order_by(Filing.available_at.desc())
        )
    )


def lookback(now: datetime, years: int) -> datetime:
    return now - timedelta(days=round(365.25 * years))
