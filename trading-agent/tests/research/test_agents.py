from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import Engine

from papertrader.agents import prompts
from papertrader.agents.context import ContextItem, ContextPack
from papertrader.agents.indicators import Bar, compute, rsi, unusual_score
from papertrader.agents.roster import build, build_contrarian, specs
from papertrader.agents.schemas import AgentOutput, AgentReport, CitedEvidence, ReportDraft
from papertrader.agents.snapshot import build_snapshot, fresh_since
from papertrader.agents.validate import finalise
from papertrader.db.session import make_session_factory
from papertrader.ingest.filing_text import extract_excerpt
from papertrader.llm.pricing import estimate_tokens

from ..ingest.helpers import shipped_config
from .helpers import AS_OF, THURSDAY, report, seed_market

CFG = shipped_config()


def snapshot(engine: Engine, as_of=AS_OF):  # type: ignore[no-untyped-def]
    with make_session_factory(engine)() as session:
        return build_snapshot(session, CFG, as_of)


def spec(label: str):  # type: ignore[no-untyped-def]
    return next(s for s in specs() if s.label == label)


# ----------------------------------------------------------------------------- indicators


def bars(closes: list[float], volume: float = 1000.0) -> list[Bar]:
    start = date(2026, 1, 1)
    return [
        Bar(start + timedelta(days=i), c, c * 1.01, c * 0.99, c, volume)
        for i, c in enumerate(closes)
    ]


def test_rsi_extremes_and_midpoint() -> None:
    assert rsi([float(i) for i in range(1, 40)]) == 100.0  # only gains
    assert rsi([float(40 - i) for i in range(1, 40)]) == pytest.approx(0.0)  # only losses
    zigzag = [100.0 + (1 if i % 2 else -1) for i in range(60)]
    assert rsi(zigzag) == pytest.approx(50.0, abs=4)
    assert rsi([1.0, 2.0, 3.0]) is None  # not enough history


def test_indicators_from_a_steady_uptrend_with_a_volume_spike() -> None:
    series = bars([100.0 + i for i in range(120)])
    series[-1] = Bar(series[-1].day, 225.0, 232.0, 224.0, 230.0, 5000.0)  # gap up on 5x volume
    ind = compute(series)
    assert ind is not None
    assert ind.last_close == 230.0 and ind.bars == 120
    assert ind.ret_1d == pytest.approx(230 / 218 - 1)
    assert ind.ret_20d == pytest.approx(230 / 199 - 1)
    assert ind.volume_ratio == pytest.approx(5.0)
    assert ind.gap_pct == pytest.approx(225 / 218 - 1)
    assert ind.from_52w_high == pytest.approx(230 / 232 - 1)
    assert ind.above_sma50 is True and ind.rsi14 == 100.0
    assert unusual_score(ind) > unusual_score(compute(bars([100.0] * 120)))  # type: ignore[arg-type]
    assert compute(bars([100.0])) is None


# ----------------------------------------------------------------------------- point in time


def test_snapshot_only_contains_what_was_public_at_the_decision_time(engine: Engine) -> None:
    seed_market(engine)
    snap = snapshot(engine)

    # Prices: Friday's bar exists in the database but the session has not happened yet.
    assert snap.prices_through == THURSDAY
    assert snap.indicators["AAPL"].last_day == THURSDAY
    assert snap.covered == ["AAPL", "JPM", "MSFT", "PFE", "XOM"]

    # News: no story published after the decision, no week-old story, no repost.
    assert sorted(n.id for n in snap.news) == [1, 2]
    # Filings: the 8-K accepted two hours later is invisible; Form 4s are not research filings.
    assert [f.id for f in snap.filings] == [1]
    # Insider trades: Eve's purchase was filed after the decision; Dan's grant is not a trade.
    assert sorted(snap.insiders) == ["AAPL", "XOM"]
    assert (
        snap.insiders["XOM"].buyers == {"Alice", "Bob"}
        and snap.insiders["XOM"].bought_usd == 150_000
    )
    assert snap.insiders["AAPL"].planned_sold_usd == snap.insiders["AAPL"].sold_usd == 400_000
    # Macro: the CPI revision published tomorrow must not replace today's known value.
    cpi = next(v for v in snap.macro if v.series_id == "CPIAUCSL")
    assert cpi.latest == 330.0
    rates = next(v for v in snap.macro if v.series_id == "DGS10")
    assert (rates.latest, rates.month_ago) == (4.20, 4.00)
    # 13F: the new filing against the same fund's previous quarter.
    (change,) = snap.holdings
    assert (change.ticker, change.shares, change.previous_shares) == ("XOM", 1500.0, 1000.0)

    earnings = {e.ticker: e for e in snap.earnings}
    assert earnings["MSFT"].eps_actual == 3.3 and earnings["MSFT"].released_at is not None
    assert earnings["JPM"].released_at is None
    # XOM's numbers are in the database but only become public tonight.
    assert earnings["XOM"].released_at is None and earnings["XOM"].eps_actual is None


def test_the_same_data_appears_once_the_clock_moves_past_it(engine: Engine) -> None:
    seed_market(engine)
    later = snapshot(engine, AS_OF + timedelta(days=2))
    assert 5 in {n.id for n in later.news}
    assert {f.id for f in later.filings} >= {2}
    assert "JPM" in later.insiders
    assert next(v for v in later.macro if v.series_id == "CPIAUCSL").latest == 331.5
    assert {e.ticker: e for e in later.earnings}["XOM"].eps_actual == 9.9
    assert later.indicators["AAPL"].last_day == date(2026, 11, 13)


def test_fresh_window_reaches_back_over_the_weekend() -> None:
    assert AS_OF - fresh_since(AS_OF, 26) == timedelta(hours=26)  # a Friday
    monday = AS_OF + timedelta(days=3)
    assert monday - fresh_since(monday, 26) == timedelta(hours=74)


# ----------------------------------------------------------------------------- contexts


def test_each_agent_gets_its_own_material_with_citable_ids(engine: Engine) -> None:
    seed_market(engine)
    snap = snapshot(engine)

    def ids(label: str) -> set[str]:
        pack = build(spec(label), snap, CFG.agents)
        assert pack is not None, label
        return set(pack.items)

    assert {"macro:DGS10", "macro:CPIAUCSL", "px:SPY", "px:XLK"} <= ids("macro")
    assert {"earn:MSFT:20261112", "earn:JPM:20261117", "px:MSFT"} <= ids("earnings")
    assert {"filing:1", "px:PFE"} == ids("sec_filings")
    assert {"news:1", "news:2", "px:AAPL", "px:JPM"} <= ids("news_event")
    assert {"px:AAPL", "px:XOM", "px:SPY"} <= ids("technicals")
    assert {"insider:XOM", "insider:AAPL", "13f:XOM", "px:XOM"} <= ids("insider_institutional")
    assert {"px:XLK", "px:AAPL", "px:MSFT", "news:1", "earn:MSFT:20261112"} <= ids(
        "sector_analyst:Information Technology"
    )

    filings = build(spec("sec_filings"), snap, CFG.agents)
    assert filings is not None
    text = filings.render(5)
    assert "AS OF: 2026-11-13 08:20 New York time" in text
    assert "5.02 director or officer departure / appointment" in text
    assert "Chief Financial Officer resigned" in text
    assert filings.allowed_tickers == frozenset({"PFE"})
    macro = build(spec("macro"), snap, CFG.agents)
    assert (
        macro is not None and "XLK" in macro.allowed_tickers and "AAPL" not in macro.allowed_tickers
    )


def test_agents_with_nothing_to_look_at_are_skipped(engine: Engine) -> None:
    seed_market(engine, with_events=False)
    snap = snapshot(engine)
    built = {s.label: build(s, snap, CFG.agents) for s in specs()}
    assert (
        built["earnings"] is None and built["sec_filings"] is None and built["news_event"] is None
    )
    assert built["insider_institutional"] is None
    assert all(built[label] is None for label in built if label.startswith("sector_analyst"))
    assert built["technicals"] is not None and built["macro"] is not None  # prices are enough


def test_no_prompt_contains_anything_dated_after_the_decision(engine: Engine) -> None:
    seed_market(engine)
    snap = snapshot(engine)
    for agent in specs():
        pack = build(agent, snap, CFG.agents)
        if pack is None:
            continue
        assert all(item.available_at <= AS_OF for item in pack.items.values()), agent.label
        text = pack.render(5)
        assert "AFTER the decision" not in text and "EPS 9.90" not in text


def test_contrarian_is_shown_the_ideas_and_the_raw_material(engine: Engine) -> None:
    seed_market(engine)
    snap = snapshot(engine)

    def idea(ticker: str, direction: str, confidence: float) -> AgentReport:
        return AgentReport(agent_name="news_event", as_of=AS_OF, ticker=ticker, direction=direction,  # type: ignore[arg-type]
                           confidence=confidence, horizon_days=5, thesis=f"{ticker} story",
                           evidence=[CitedEvidence(claim="c", source_id="news:1")], risks=[], catalysts=[])  # fmt: skip

    pack = build_contrarian(snap, CFG.agents, [idea("AAPL", "long", 0.8), idea("XOM", "long", 0.6),
                                               idea("PFE", "neutral", 0.9), idea("XLK", "long", 0.9)])  # fmt: skip
    assert pack is not None
    assert pack.allowed_tickers == frozenset(
        {"AAPL", "XOM"}
    )  # neutral views and funds are not targets
    text = pack.render(5)
    assert "news_event says LONG AAPL, confidence 0.80" in text
    assert {"news:1", "px:AAPL", "insider:XOM", "idea:1"} <= set(pack.items)
    assert build_contrarian(snap, CFG.agents, []) is None


# ----------------------------------------------------------------------------- citations


def pack_with(*items: ContextItem, allowed: tuple[str, ...] = ("AAPL", "MSFT")) -> ContextPack:
    pack = ContextPack(AS_OF, frozenset(allowed))
    pack.add("Items", list(items))
    return pack


AAPL_NEWS = ContextItem(
    "news:1", "Apple story", AS_OF, "https://news.example/1", frozenset({"AAPL"})
)
MSFT_NEWS = ContextItem("news:2", "Microsoft story", AS_OF, None, frozenset({"MSFT"}))
GENERAL = ContextItem("macro:DGS10", "10-year yield", AS_OF)


def check(*drafts: dict, max_reports: int = 5):  # type: ignore[no-untyped-def,type-arg]
    output = AgentOutput(reports=[ReportDraft(**d) for d in drafts], summary="s")
    return finalise(agent_name="news_event", sector=None, as_of=AS_OF, output=output,
                    pack=pack_with(AAPL_NEWS, MSFT_NEWS, GENERAL), max_reports=max_reports)  # fmt: skip


def test_claims_without_a_real_citation_are_deleted() -> None:
    result = check(report("AAPL", "long", 0.7, "news:1", "news:999", "[macro:DGS10]", ""))
    (kept,) = result.reports
    assert [e.source_id for e in kept.evidence] == ["news:1", "macro:DGS10"]
    assert kept.evidence[0].url == "https://news.example/1"  # the URL comes from the program
    assert (result.dropped_claims, kept.dropped_claims, result.dropped_reports) == (2, 2, 0)
    assert (kept.agent_name, kept.as_of) == ("news_event", AS_OF)  # never taken from the model


def test_a_source_about_another_company_does_not_count() -> None:
    result = check(report("AAPL", "long", 0.7, "news:2"))  # news:2 is about Microsoft
    assert result.reports == [] and result.dropped_claims == 1 and result.dropped_reports == 1
    assert result.removed == [
        "AAPL: claim citing 'news:2' (source is about another company)",
        "AAPL: report left with no valid evidence",
    ]


def test_the_market_and_sector_funds_can_be_cited_for_any_stock(engine: Engine) -> None:
    """Found on the first real run: "20 points behind its sector fund" was being deleted."""
    seed_market(engine)
    snap = snapshot(engine)
    pack = build(spec("technicals"), snap, CFG.agents)
    assert pack is not None
    output = AgentOutput(reports=[ReportDraft(**report("AAPL", "long", 0.6, "px:SPY", "px:AAPL", "px:MSFT"))],
                         summary="s")  # fmt: skip
    result = finalise(agent_name="technicals", sector=None, as_of=AS_OF, output=output, pack=pack,
                      max_reports=5)  # fmt: skip
    (kept,) = result.reports
    # The benchmark is fair comparison; another company's price line is not evidence.
    assert [e.source_id for e in kept.evidence] == ["px:SPY", "px:AAPL"]
    assert result.dropped_claims == 1


def test_reports_with_no_evidence_or_a_forbidden_ticker_are_deleted() -> None:
    result = check(
        report("AAPL", "long", 0.7),  # no evidence at all
        report("TSLA", "long", 0.9, "macro:DGS10"),  # not an allowed ticker
        report("msft", "short", 0.6, "news:2"),  # fine (case is normalised)
        report("MSFT", "long", 0.9, "news:2"),  # a second report on the same ticker
    )
    assert [(r.ticker, r.direction) for r in result.reports] == [("MSFT", "short")]
    assert result.dropped_reports == 3


def test_out_of_range_numbers_are_pulled_into_range_and_extras_cut() -> None:
    wild = report("AAPL", "long", 1.7, "news:1", horizon=400)
    result = check(wild, report("MSFT", "long", -0.2, "news:2"), max_reports=1)
    (kept,) = result.reports
    assert (kept.ticker, kept.confidence, kept.horizon_days) == ("AAPL", 1.0, 20)
    assert result.dropped_reports == 1  # only the strongest is kept


# ----------------------------------------------------------------------------- prompts + text


def test_shared_rules_are_long_enough_to_cache_and_free_of_changing_text() -> None:
    assert estimate_tokens(prompts.SHARED_RULES) > 700  # the cache needs at least 512 tokens
    assert estimate_tokens(prompts.JUDGE_SHARED) > 700
    for forbidden in ("2026", "{", "MAXIMUM REPORTS:"):
        assert forbidden not in prompts.SHARED_RULES
    assert len({s.prompt.split()[1] for s in specs()}) == 7  # every agent announces who it is


def test_8k_excerpt_starts_at_the_item_and_skips_hidden_markup() -> None:
    html = (
        "<html><head><title>8-K</title><style>p{color:red}</style></head><body>"
        "<ix:header><ix:hidden>0000320193 false</ix:hidden></ix:header>"
        "<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>"
        + "<p>cover page text</p>"
        * 60
        + "<p><b>Item&nbsp;5.02</b> Departure of Directors or Certain Officers.</p>"
        "<p>On November 10, the Chief Financial Officer resigned.</p><script>x()</script></body></html>"
    )
    excerpt = extract_excerpt(html)
    assert excerpt.startswith("Item 5.02 Departure of Directors")
    assert "resigned" in excerpt and "0000320193" not in excerpt and "color" not in excerpt
    assert len(extract_excerpt(html, limit=50)) == 50
