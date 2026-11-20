"""B's extra signals: insider clusters, the paid data adapters, the backtest, the Fable test."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from papertrader.b import paid
from papertrader.b.backtest import find_events, run
from papertrader.b.daily import fable_comparison, run_fable, run_signals, score_shadow
from papertrader.b.models import ShadowReport, Signal
from papertrader.b.signals import insider_clusters
from papertrader.config import InsiderSwitch, Settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.llm.governor import CostGovernor
from papertrader.research.reuse import copy_a_research

from ..research.helpers import AS_OF, FRIDAY, FakeClaude, make_claude, seed_market
from .helpers import CFG, a_has_researched, b_config, b_judges


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


def signals(engine: Engine) -> list[Signal]:
    with make_session_factory(engine)() as session:
        rows = list(session.scalars(select(Signal).order_by(Signal.ticker, Signal.kind)))
        session.expunge_all()
    return rows


# --------------------------------------------------------------------------- insiders


def test_insider_clusters_follow_the_rule(engine: Engine) -> None:
    seed_market(engine)
    with make_session_factory(engine)() as session:
        two = insider_clusters(session, AS_OF, InsiderSwitch(min_buyers=2, min_usd=50_000))
        three = insider_clusters(session, AS_OF, InsiderSwitch(min_buyers=3))
    # XOM: Alice and Bob bought $150k. JPM's purchase was filed after the decision time,
    # AAPL's sale was pre-scheduled (10b5-1), and the AAPL award is not a purchase.
    assert set(two) == {"XOM"} and two["XOM"].bought_usd == 150_000 and len(two["XOM"].buyers) == 2
    assert "2 insiders (Director) bought $150,000" in two["XOM"].summary(30)
    assert three == {}


def test_the_signals_job_records_clusters_and_reports_missing_keys(
    engine: Engine, b_engine: Engine
) -> None:
    seed_market(engine)
    config = b_config(
        insiders={"min_buyers": 2}, options={"enabled": True}, analysts={"enabled": True}
    )
    details = run_signals(b_engine, make_session_factory(b_engine), config, Settings(), AS_OF)
    assert details["insider_clusters"] == 1 and [s.ticker for s in signals(b_engine)] == ["XOM"]
    assert details["problems"] == ["switch on but MARKETDATA_API_KEY is not set in .env",
                                   "switch on but FMP_API_KEY is not set in .env"]  # fmt: skip
    again = run_signals(b_engine, make_session_factory(b_engine), config, Settings(), AS_OF)
    assert again["insider_clusters"] == 0  # already stored today


# --------------------------------------------------------------------------- paid data

CHAIN = {
    "s": "ok", "underlyingPrice": [100.0] * 6, "dte": [30] * 6,
    "side": ["call", "put", "call", "put", "call", "put"],
    "strike": [95.0, 95.0, 100.0, 100.0, 105.0, 105.0],
    "iv": [0.33, 0.30, 0.31, 0.29, 0.30, 0.34],
    "delta": [0.7, -0.3, 0.5, -0.5, 0.25, -0.75],
}  # fmt: skip


def test_the_option_chain_becomes_three_numbers() -> None:
    view = paid.parse_chain("AAPL", CHAIN)
    assert view.atm_iv == pytest.approx(0.30) and view.days == 30
    assert view.call_put_spread == pytest.approx((0.03 + 0.02 - 0.04) / 3)
    assert view.put_skew == pytest.approx(0.30 - 0.30)  # the 25-delta put minus the 25-delta call
    assert "implied volatility 30% a year (30 days out)" in view.summary()
    assert paid.parse_chain("AAPL", {"s": "no_data"}).atm_iv is None


def test_an_unexpected_reply_is_named_not_guessed() -> None:
    with pytest.raises(paid.PaidDataError, match="no side, strike, iv list"):
        paid.parse_chain("AAPL", {"s": "ok"})
    with pytest.raises(paid.PaidDataError, match="different lengths"):
        paid.parse_chain("AAPL", CHAIN | {"iv": [0.3]})
    with pytest.raises(paid.PaidDataError, match="status 'error'"):
        paid.parse_chain("AAPL", {"s": "error", "errmsg": "bad token"})


def test_a_refused_key_is_reported_plainly() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={})))
    with pytest.raises(paid.PaidDataError, match="refused the key \\(HTTP 402\\)"):
        paid.fetch_options(client, "k", "AAPL")


def test_analyst_grades_targets_and_revisions() -> None:
    today = date(2026, 11, 20)
    grades = [{"date": "2026-11-10", "action": "upgrade"}, {"date": "2026-11-12", "action": "upgrade"},
              {"date": "2026-11-01", "action": "downgrade"}, {"date": "2026-09-01", "action": "downgrade"}]  # fmt: skip
    view = paid.parse_analysts("AAPL", today, grades, [{"targetConsensus": 220.0}],
                               [{"date": "2026-09-27", "epsAvg": 7.5}, {"date": "2027-09-27", "epsAvg": 8.4}],
                               price=200.0, month_ago_eps=7.2)  # fmt: skip
    assert (view.upgrades, view.downgrades, view.net_grades) == (2, 1, 1)
    assert view.eps_revision == pytest.approx(7.5 / 7.2 - 1)
    assert "consensus target +10% from the price" in view.summary()
    with pytest.raises(paid.PaidDataError, match="Invalid API KEY"):
        paid.parse_analysts("AAPL", today, {"Error Message": "Invalid API KEY"}, [], [], price=None,
                            month_ago_eps=None)  # fmt: skip


def test_paid_signals_are_stored_and_shown_to_bs_judge(engine: Engine, b_engine: Engine) -> None:
    fake = FakeClaude()
    asyncio.run(a_has_researched(engine, fake))

    def answer(request: httpx.Request) -> httpx.Response:
        if "marketdata" in request.url.host:
            assert request.headers["authorization"] == "Bearer md-key"
            return httpx.Response(200, json=CHAIN)
        assert request.url.params["apikey"] == "fmp-key"
        path = request.url.path
        if path.endswith("/grades"):
            return httpx.Response(200, json=[{"date": "2026-11-12", "action": "upgrade"}])
        if path.endswith("/price-target-consensus"):
            return httpx.Response(200, json=[{"targetConsensus": 500.0}])
        return httpx.Response(200, json=[{"date": "2026-09-27", "epsAvg": 7.5}])

    config = b_config(
        options={"enabled": True, "max_tickers": 2}, analysts={"enabled": True, "max_tickers": 2}
    )
    settings = Settings(marketdata_api_key="md-key", fmp_api_key="fmp-key")  # type: ignore[arg-type]
    client = httpx.Client(transport=httpx.MockTransport(answer))
    details = run_signals(
        b_engine, make_session_factory(b_engine), config, settings, AS_OF, http=client
    )
    assert details["options"] == 2 and details["analysts"] == 2 and not details["problems"]
    asyncio.run(b_judges(b_engine, fake, config))
    prompt = fake.calls[-1].user
    assert (
        "## Options and analyst signals" in prompt
        and "AAPL (options): implied volatility" in prompt
    )
    assert "AAPL (analysts): last 30 days: 1 upgrades" in prompt


# --------------------------------------------------------------------------- backtest


def test_the_insider_backtest_replays_the_rule_without_look_ahead(
    engine: Engine, b_engine: Engine
) -> None:
    seed_market(engine)
    with make_session_factory(engine)() as session:
        session.add(m.Filing(id=99, accession_no="0000-26-000099", cik="1", ticker="AAPL", form_type="4",
                             url="u", published_at=AS_OF, available_at=AS_OF))  # fmt: skip
        start = date(2026, 3, 2)
        for month in range(6):  # a three-buyer cluster in AAPL and PFE every month
            for ticker in ("AAPL", "PFE"):
                for k, name in enumerate(("Ann", "Ben", "Cid")):
                    day = start + timedelta(days=30 * month + k)
                    at = AS_OF.replace(year=day.year, month=day.month, day=day.day) + timedelta(
                        hours=1
                    )
                    session.add(m.InsiderTransaction(
                        filing_id=99, line_no=month * 10 + k + (100 if ticker == "PFE" else 0), ticker=ticker,
                        insider_name=name, transaction_date=day, transaction_code="P", acquired=True,
                        shares=1000, price=100.0, available_at=at))  # fmt: skip
        session.commit()
    with make_session_factory(b_engine)() as session:
        events = find_events(session, InsiderSwitch(), AS_OF)
        assert len(events) == 12 and all(e.buyers == 3 for e in events)
        assert events[0].at.date() == date(2026, 3, 4)  # when the third buyer's filing was public
        row = run(session, CFG, AS_OF)
        session.commit()
    later = row.result["later_20d_sector"]
    assert (
        later["n"] >= 4 and later["mean_excess"] > 0
    )  # in the test market small stocks rise faster
    assert row.verdict.startswith("keep OFF: only")  # far too few clusters to switch it on


# --------------------------------------------------------------------------- the Fable test


def test_the_fable_test_runs_on_bs_copy_and_never_touches_the_main_budget(
    engine: Engine, b_engine: Engine
) -> None:
    fake = FakeClaude()
    asyncio.run(a_has_researched(engine, fake))
    copy_a_research(b_engine, make_session_factory(b_engine), CFG, FRIDAY)
    config = b_config(fable_shadow={"enabled": True, "agents": ["news_event"], "monthly_usd": 10})
    fable_budget = config.budget.model_copy(update={"monthly_usd": 10})
    claude = make_claude(b_engine, fake, config=config)
    claude.governor = CostGovernor(make_session_factory(b_engine), fable_budget, only=("fable_shadow",),
                                   now=lambda: AS_OF)  # fmt: skip
    out = asyncio.run(
        run_fable(make_session_factory(b_engine), config, claude, AS_OF + timedelta(hours=2))
    )
    assert out["agents"] == {"news_event": "1 reports"}  # the made-up JPM citation is dropped again
    last = fake.calls[-1]
    assert last.label == "news_event" and last.body["model"] == "claude-fable-5-2"
    with make_session_factory(b_engine)() as session:
        rows = session.scalars(select(ShadowReport)).all()
        assert [(r.ticker, r.model) for r in rows] == [("AAPL", "claude-fable-5-2")]
        assert [u.purpose for u in session.scalars(select(m.ApiUsage))] == ["fable_shadow"]
    main = CostGovernor(
        make_session_factory(b_engine), config.budget, exclude=("fable_shadow",), now=lambda: AS_OF
    )
    assert main.status().month_spent == 0  # the Fable test has its own $10
    assert (
        asyncio.run(run_fable(make_session_factory(b_engine), config, claude, AS_OF))["skipped"]
        == "already ran today"
    )


def test_fable_reports_are_scored_and_compared(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    old = AS_OF - timedelta(days=30)
    with make_session_factory(b_engine)() as session:
        for ticker, direction in (("AAPL", "long"), ("PFE", "short"), ("JPM", "neutral")):
            session.add(ShadowReport(day=old.date(), as_of=old, agent_name="news_event", model="claude-fable-5-2",
                                     ticker=ticker, direction=direction, confidence=0.7, horizon_days=5,
                                     thesis="t", cost_usd=0.05))  # fmt: skip
        session.commit()
        assert score_shadow(session, CFG, AS_OF) == 2
        session.commit()
        result: dict[str, Any] = fable_comparison(b_engine, session)
    # Small stocks rise faster than SPY in the test market: the long call wins, the short loses.
    assert result["fable"]["scored"] == 2 and result["fable"]["hit_rate"] == 0.5
    assert result["sonnet"] == {"scored": 0} and result["agents"] == ["news_event"]
