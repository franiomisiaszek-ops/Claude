"""SEC tests. The sample documents copy the structure of real EDGAR files (checked live
on 2026-11-13) with made-up people and numbers."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Engine, func, select

from papertrader.config import Manager13F
from papertrader.db.models import (
    EarningsEvent,
    Filing,
    InsiderTransaction,
    InstitutionalHolding,
    SecurityIdentifier,
)
from papertrader.ingest.context import IngestContext
from papertrader.ingest.sec import (
    filings_for,
    ftd_file_names,
    ingest_13f,
    ingest_filings,
    ingest_form4_details,
    iter_filings,
    parse_13f,
    parse_form4,
    parse_ftd,
    pick_information_table,
    raw_document,
    refresh_cusip_map,
)
from papertrader.ingest.universe import active_members

from .helpers import NOW, add_members, fetcher, make_ctx, shipped_config

FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
    <schemaVersion>X0609</schemaVersion>
    <documentType>4</documentType>
    <periodOfReport>2026-11-10</periodOfReport>
    <issuer>
        <issuerCik>0000001111</issuerCik>
        <issuerName>Acme Corp</issuerName>
        <issuerTradingSymbol>ACME</issuerTradingSymbol>
    </issuer>
    <reportingOwner>
        <reportingOwnerId>
            <rptOwnerCik>0009990001</rptOwnerCik>
            <rptOwnerName>Doe Jane</rptOwnerName>
        </reportingOwnerId>
        <reportingOwnerRelationship>
            <isDirector>false</isDirector>
            <isOfficer>true</isOfficer>
            <isTenPercentOwner>0</isTenPercentOwner>
            <officerTitle>Chief Financial Officer</officerTitle>
        </reportingOwnerRelationship>
    </reportingOwner>
    <aff10b5One>true</aff10b5One>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value><footnoteId id="F1"/></securityTitle>
            <transactionDate><value>2026-11-10</value></transactionDate>
            <transactionCoding>
                <transactionFormType>4</transactionFormType>
                <transactionCode>S</transactionCode>
            </transactionCoding>
            <transactionAmounts>
                <transactionShares><value>2399</value></transactionShares>
                <transactionPricePerShare><value>351.36</value></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <postTransactionAmounts>
                <sharesOwnedFollowingTransaction><value>27598</value></sharesOwnedFollowingTransaction>
            </postTransactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
            <securityTitle><value>Common Stock</value></securityTitle>
            <transactionDate><value>2026-11-11</value></transactionDate>
            <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>100</value></transactionShares>
                <transactionPricePerShare><footnoteId id="F2"/></transactionPricePerShare>
                <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
            <ownershipNature><directOrIndirectOwnership><value>I</value></directOrIndirectOwnership></ownershipNature>
        </nonDerivativeTransaction>
        <nonDerivativeHolding>
            <securityTitle><value>Common Stock</value></securityTitle>
        </nonDerivativeHolding>
    </nonDerivativeTable>
    <derivativeTable>
        <derivativeTransaction>
            <securityTitle><value>Stock Option</value></securityTitle>
        </derivativeTransaction>
    </derivativeTable>
</ownershipDocument>"""

INFO_TABLE_XML = """<informationTable xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>00000A100</cusip><value>{v1}</value>
    <shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>DFND</investmentDiscretion>
    <votingAuthority><Sole>1000</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable>
  <infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>00000A100</cusip><value>{v2}</value>
    <shrsOrPrnAmt><sshPrnamt>500</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>00000A100</cusip><value>999</value>
    <shrsOrPrnAmt><sshPrnamt>7000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Put</putCall></infoTable>
  <infoTable><nameOfIssuer>ACME CORP</nameOfIssuer><titleOfClass>NOTE 1.5%</titleOfClass>
    <cusip>00000ABB2</cusip><value>123</value>
    <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>PRN</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>NOT IN UNIVERSE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
    <cusip>99999Z109</cusip><value>42</value>
    <shrsOrPrnAmt><sshPrnamt>10</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>"""


def submissions(
    rows: list[dict[str, Any]], files: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """Build EDGAR's column-per-field layout from ordinary row dicts."""
    keys = ["accessionNumber", "filingDate", "reportDate", "acceptanceDateTime", "form", "items",
            "primaryDocument", "primaryDocDescription"]  # fmt: skip
    block = {key: [row.get(key, "") for row in rows] for key in keys}
    return {"cik": "1111", "filings": {"recent": block, "files": files or []}}


def filing(accession: str, form: str, accepted: str, **extra: str) -> dict[str, str]:
    return {"accessionNumber": accession, "form": form, "acceptanceDateTime": accepted,
            "filingDate": accepted[:10], **extra}  # fmt: skip


RECENT = [
    filing("0001111-26-000009", "4", "2026-11-12T23:58:20.000Z", reportDate="2026-11-10",
           primaryDocument="xslF345X06/form4.xml", primaryDocDescription="FORM 4"),
    filing("0001111-26-000008", "8-K", "2026-11-02T21:30:23.000Z", items="2.02,9.01",
           primaryDocument="acme_8k.htm"),
    filing("0001111-26-000007", "8-K", "2026-10-01T12:05:00.000Z", items="5.02",
           primaryDocument="acme_8k2.htm"),
    filing("0001111-26-000006", "10-Q", "2026-08-01T10:00:00.000Z", reportDate="2026-06-30",
           primaryDocument="acme-10q.htm"),
    filing("0001111-26-000005", "424B2", "2026-07-01T10:00:00.000Z", primaryDocument="x.htm"),
    filing("0001111-20-000001", "10-K", "2020-02-01T10:00:00.000Z", primaryDocument="old.htm"),
]  # fmt: skip
OLDER = [filing("0001111-22-000002", "10-K", "2022-02-01T11:00:00.000Z", reportDate="2021-12-31",
                primaryDocument="acme-10k.htm")]  # fmt: skip


def sec_site(routes: dict[str, Any], log: list[str] | None = None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if log is not None:
            log.append(url)
        for suffix, body in routes.items():
            if url.endswith(suffix):
                if isinstance(body, int):
                    return httpx.Response(body)
                if isinstance(body, bytes | str):
                    return httpx.Response(200, content=body)
                return httpx.Response(200, json=body)
        return httpx.Response(404)

    return fetcher(handler, "sec")


def company_ctx(
    engine: Engine, routes: dict[str, Any], log: list[str] | None = None
) -> IngestContext:
    ctx = make_ctx(engine, sec=sec_site(routes, log))
    add_members(ctx, ("ACME", "1111", "Industrials"), ("ACME.B", "1111", "Industrials"))
    return ctx


def members(ctx: IngestContext) -> list[Any]:
    with ctx.session_factory() as session:
        return active_members(session, "sp500")


def count(ctx: IngestContext, model: Any) -> int:
    with ctx.session_factory() as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


# ----------------------------------------------------------------------------- filing lists


def test_iter_filings_turns_columns_into_rows() -> None:
    rows = list(iter_filings(submissions(RECENT)["filings"]["recent"]))
    assert len(rows) == 6
    assert rows[1]["form"] == "8-K" and rows[1]["items"] == "2.02,9.01"


def test_raw_document_drops_the_styled_view_folder() -> None:
    assert raw_document("xslF345X06/form4.xml") == "form4.xml"
    assert raw_document("xslForm13F_X02/primary_doc.xml") == "primary_doc.xml"
    assert raw_document("acme-10k.htm") == "acme-10k.htm"


async def test_filings_are_stored_with_utc_acceptance_time_and_are_idempotent(
    engine: Engine,
) -> None:
    log: list[str] = []
    older_page = {"name": "CIK0000001111-submissions-001.json", "filingFrom": "2015-01-01",
                  "filingTo": "2022-06-30"}  # fmt: skip
    ctx = company_ctx(engine, {
        "CIK0000001111.json": submissions(RECENT, [older_page]),
        "CIK0000001111-submissions-001.json": submissions(OLDER)["filings"]["recent"],
    }, log)  # fmt: skip
    since = date(2021, 11, 13)

    await ingest_filings(ctx, members(ctx), since=since)
    with ctx.session_factory() as session:
        stored = {f.accession_no: f for f in session.scalars(select(Filing))}
    # 424B2 is not a form we track; the 2020 10-K is older than `since`.
    assert sorted(f.form_type for f in stored.values()) == ["10-K", "10-Q", "4", "8-K", "8-K"]
    form4 = stored["0001111-26-000009"]
    assert (
        form4.published_at == form4.available_at == datetime(2026, 11, 12, 23, 58, 20, tzinfo=UTC)
    )
    assert form4.url == "https://www.sec.gov/Archives/edgar/data/1111/000111126000009/form4.xml"
    assert form4.ticker == "ACME"  # two share classes share one SEC identifier
    assert form4.detail_status is None
    assert stored["0001111-26-000006"].period_of_report == date(2026, 6, 30)
    assert len(log) == 2  # one company request (not one per share class) plus the older page

    log.clear()
    again = await ingest_filings(ctx, members(ctx), since=since)
    assert again.rows == 0
    assert count(ctx, Filing) == 5
    assert len(log) == 1  # history already covered: the older page is not downloaded again


async def test_an_8k_with_item_202_becomes_an_earnings_event(engine: Engine) -> None:
    ctx = company_ctx(engine, {"CIK0000001111.json": submissions(RECENT)})
    await ingest_filings(ctx, members(ctx), since=date(2026, 1, 1))
    with ctx.session_factory() as session:
        events = session.scalars(select(EarningsEvent).order_by(EarningsEvent.ticker)).all()
    assert [e.ticker for e in events] == ["ACME", "ACME.B"]  # only the 2.02 filing, both classes
    event = events[0]
    assert (event.source, event.report_date, event.time_of_day) == (
        "sec_8k",
        date(2026, 11, 2),
        "amc",
    )
    assert event.actual_available_at == datetime(
        2026, 11, 2, 21, 30, 23, tzinfo=UTC
    )  # 16:30 New York
    assert event.eps_actual is None


async def test_one_failing_company_does_not_stop_the_others(engine: Engine) -> None:
    ctx = company_ctx(
        engine, {"CIK0000001111.json": submissions(RECENT), "CIK0000002222.json": 403}
    )
    add_members(ctx, ("BETA", "2222", "Energy"))
    stats = await ingest_filings(ctx, members(ctx), since=date(2026, 1, 1))
    assert count(ctx, Filing) == 4
    assert any("BETA" in note for note in stats.notes)


async def test_point_in_time_read_only_sees_filings_public_at_that_moment(engine: Engine) -> None:
    ctx = company_ctx(engine, {"CIK0000001111.json": submissions(RECENT)})
    await ingest_filings(ctx, members(ctx), since=date(2026, 1, 1))
    with ctx.session_factory() as session:
        before = filings_for(session, "ACME", as_of=datetime(2026, 11, 2, 21, 30, 22, tzinfo=UTC))
        after = filings_for(session, "ACME", as_of=datetime(2026, 11, 2, 21, 30, 23, tzinfo=UTC))
    assert "0001111-26-000008" not in {f.accession_no for f in before}  # one second too early
    assert after[0].accession_no == "0001111-26-000008"


# ----------------------------------------------------------------------------- Form 4


def test_parse_form4_reads_share_transactions_only() -> None:
    form = parse_form4(FORM4_XML)
    assert (form.owner_name, form.owner_cik, form.owner_title) == (
        "Doe Jane", "0009990001", "Chief Financial Officer")  # fmt: skip
    assert (form.is_officer, form.is_director, form.is_ten_percent_owner) == (True, False, False)
    assert form.under_10b5_1_plan is True
    assert len(form.trades) == 2  # holdings and option rows are not transactions

    sale, purchase = form.trades
    assert (sale.code, sale.acquired, sale.shares, sale.price) == ("S", False, 2399.0, 351.36)
    assert (sale.transaction_date, sale.shares_after, sale.direct) == (
        date(2026, 11, 10),
        27598.0,
        True,
    )
    assert (purchase.code, purchase.acquired, purchase.price) == (
        "P",
        True,
        None,
    )  # price in footnote
    assert purchase.direct is False


async def test_form4_details_record_status_and_can_be_resumed(engine: Engine) -> None:
    rows = [
        filing("0001111-26-000001", "4", "2026-11-12T23:00:00.000Z", primaryDocument="xslF345X06/a.xml"),
        filing("0001111-26-000002", "4", "2026-11-11T23:00:00.000Z", primaryDocument="xslF345X06/gone.xml"),
        filing("0001111-26-000003", "4", "2026-11-10T23:00:00.000Z", primaryDocument="scan.pdf"),
        filing("0001111-26-000004", "4", "2026-11-09T23:00:00.000Z", primaryDocument="xslF345X06/busy.xml"),
        filing("0001111-26-000005", "4", "2026-11-08T23:00:00.000Z", primaryDocument="xslF345X06/junk.xml"),
        filing("0001111-21-000006", "4", "2022-01-05T23:00:00.000Z", primaryDocument="xslF345X06/old.xml"),
    ]  # fmt: skip
    routes: dict[str, Any] = {"CIK0000001111.json": submissions(rows), "/a.xml": FORM4_XML,
                              "/busy.xml": 503, "/junk.xml": "<not-xml", "/old.xml": FORM4_XML}  # fmt: skip
    ctx = company_ctx(engine, routes)
    ctx.sec._max_retries = 0  # type: ignore[union-attr]
    await ingest_filings(ctx, members(ctx), since=date(2021, 11, 13))

    since = NOW - timedelta(days=730)
    stats = await ingest_form4_details(ctx, since=since)
    with ctx.session_factory() as session:
        status = {f.accession_no[-1]: f.detail_status for f in session.scalars(select(Filing))}
        trades = session.scalars(
            select(InsiderTransaction).order_by(InsiderTransaction.line_no)
        ).all()
    assert status == {
        "1": "done",
        "2": "failed",
        "3": "skipped",
        "4": None,
        "5": "failed",
        "6": None,
    }
    assert stats.rows == 2
    assert any("will retry" in note for note in stats.notes)

    sale = trades[0]
    assert (sale.ticker, sale.insider_name, sale.insider_title) == (
        "ACME",
        "Doe Jane",
        "Chief Financial Officer",
    )
    assert (sale.transaction_code, sale.acquired, sale.under_10b5_1_plan) == ("S", False, True)
    assert sale.available_at == datetime(
        2026, 11, 12, 23, 0, tzinfo=UTC
    )  # public when filed, not when traded

    # Next run: the SEC is back, so the one that failed temporarily is read; nothing is repeated.
    ctx.sec = sec_site({**routes, "/busy.xml": FORM4_XML})
    again = await ingest_form4_details(ctx, since=since)
    assert again.rows == 2
    assert again.requests == 1
    assert count(ctx, InsiderTransaction) == 4


# ----------------------------------------------------------------------------- CUSIP map + 13F


def ftd_zip(*lines: str) -> bytes:
    text = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n" + "\n".join(lines)
    text += "\nTrailer record count 3\nTrailer total quantity of shares 99\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("cnsfails.txt", text)
    return buffer.getvalue()


FTD = ftd_zip(
    "20261001|00000A100|ACME|630|ACME CORP COM|10.58",
    "20261001|00000B209|ACMEB|12|ACME CORP CL B|11.00",
    "20261002|99999Z109|ZZZZ|5|NOT IN UNIVERSE INC|1.00",
)


def test_ftd_file_names_step_back_by_half_months() -> None:
    assert ftd_file_names(date(2026, 11, 13), 4) == [
        "cnsfails202611a.zip", "cnsfails202610b.zip", "cnsfails202610a.zip", "cnsfails202609b.zip"]  # fmt: skip
    assert ftd_file_names(date(2026, 1, 20), 3) == [
        "cnsfails202601b.zip", "cnsfails202601a.zip", "cnsfails202512b.zip"]  # fmt: skip


def test_parse_ftd_skips_header_and_trailer() -> None:
    assert parse_ftd(FTD) == {
        "00000A100": ("ACME", "ACME CORP COM"),
        "00000B209": ("ACMEB", "ACME CORP CL B"),
        "99999Z109": ("ZZZZ", "NOT IN UNIVERSE INC"),
    }


async def test_cusip_map_keeps_universe_tickers_and_skips_unpublished_files(engine: Engine) -> None:
    log: list[str] = []
    ctx = company_ctx(engine, {"cnsfails202610a.zip": FTD}, log)  # every other file: 404
    stats = await refresh_cusip_map(ctx)
    with ctx.session_factory() as session:
        mapping = dict(
            session.execute(select(SecurityIdentifier.cusip, SecurityIdentifier.ticker)).all()
        )
    # "ACMEB" in the SEC file is our "ACME.B"; the stock outside the universe is ignored.
    assert mapping == {"00000A100": "ACME", "00000B209": "ACME.B"}
    assert stats.rows == 2

    log.clear()
    await refresh_cusip_map(ctx)
    assert not any("202610a" in url for url in log)  # a finished file is not downloaded twice


async def test_cusip_files_read_against_an_empty_universe_are_read_again(engine: Engine) -> None:
    """A real first run did this: the stock list failed, the files were fetched, nothing matched."""
    log: list[str] = []
    ctx = make_ctx(engine, sec=sec_site({"cnsfails202610a.zip": FTD}, log))

    empty = await refresh_cusip_map(ctx)
    assert empty.rows == 0 and "universe is empty" in empty.notes[0]
    assert log == []  # nothing downloaded, nothing marked as finished

    add_members(ctx, ("ACME", "1111", "Industrials"))
    assert (await refresh_cusip_map(ctx)).rows == 1

    add_members(ctx, ("ACME.B", "1111", "Industrials"))  # the universe grew
    assert (await refresh_cusip_map(ctx)).rows == 1
    assert count(ctx, SecurityIdentifier) == 2


def test_parse_13f_sums_share_rows_per_cusip_and_ignores_options_and_bonds() -> None:
    holdings = {h.cusip: h for h in parse_13f(INFO_TABLE_XML.format(v1=300, v2=200))}
    assert set(holdings) == {"00000A100", "99999Z109"}
    acme = holdings["00000A100"]
    assert (acme.shares, acme.value, acme.issuer, acme.title_of_class) == (
        1500.0,
        500.0,
        "ACME CORP",
        "COM",
    )


def test_pick_information_table_is_the_xml_that_is_not_the_cover_page() -> None:
    index = {"directory": {"item": [
        {"name": "0001-26-1-index.html", "size": ""}, {"name": "primary_doc.xml", "size": "5555"},
        {"name": "56757.xml", "size": "44724"}]}}  # fmt: skip
    assert pick_information_table(index) == "56757.xml"
    assert pick_information_table({"directory": {"item": [{"name": "primary_doc.xml"}]}}) is None


async def test_13f_holdings_are_matched_to_tickers_and_old_values_are_rescaled(
    engine: Engine,
) -> None:
    rows = [
        filing("0009999-26-000002", "13F-HR", "2026-08-14T20:05:04.000Z", reportDate="2026-06-30",
               primaryDocument="xslForm13F_X02/primary_doc.xml"),
        filing("0009999-22-000001", "13F-HR", "2022-08-15T20:05:04.000Z", reportDate="2022-06-30",
               primaryDocument="xslForm13F_X02/primary_doc.xml"),
        filing("0009999-26-000003", "4", "2026-08-01T20:00:00.000Z"),
    ]  # fmt: skip
    index = {"directory": {"item": [{"name": "primary_doc.xml", "size": "1"},
                                    {"name": "info.xml", "size": "9"}]}}  # fmt: skip
    cfg = shipped_config(managers_13f=(Manager13F(cik="9999", name="Big Fund"),))
    routes = {
        "CIK0000009999.json": submissions(rows),
        "000999926000002/index.json": index, "000999926000002/info.xml": INFO_TABLE_XML.format(v1=300_000, v2=200_000),
        "000999922000001/index.json": index, "000999922000001/info.xml": INFO_TABLE_XML.format(v1=300, v2=200),
        "cnsfails202610a.zip": FTD,
    }  # fmt: skip
    ctx = make_ctx(engine, config=cfg, sec=sec_site(routes))
    add_members(ctx, ("ACME", "1111", "Industrials"), ("ACME.B", "1111", "Industrials"))

    skipped = await ingest_13f(ctx, since=date(2021, 11, 13))
    assert skipped.rows == 0 and "CUSIP map" in skipped.notes[0]  # nothing to match against yet

    await refresh_cusip_map(ctx)
    stats = await ingest_13f(ctx, since=date(2021, 11, 13))
    assert stats.rows == 2
    with ctx.session_factory() as session:
        holdings = session.scalars(
            select(InstitutionalHolding).order_by(InstitutionalHolding.report_period)
        ).all()
        forms = session.scalars(select(Filing.form_type)).all()
    assert forms == ["13F-HR", "13F-HR"]  # the manager's Form 4 is not ours to track
    old, new = holdings
    assert (new.ticker, new.manager_name, new.shares, new.value_usd) == (
        "ACME",
        "Big Fund",
        1500.0,
        500_000.0,
    )
    # Filed before 2023: the SEC form reported thousands of dollars back then.
    assert (old.report_period, old.value_usd) == (date(2022, 6, 30), 500_000.0)
    assert new.available_at == datetime(
        2026, 8, 14, 20, 5, 4, tzinfo=UTC
    )  # 45 days after quarter end

    assert (await ingest_13f(ctx, since=date(2021, 11, 13))).rows == 0
    assert count(ctx, InstitutionalHolding) == 2
