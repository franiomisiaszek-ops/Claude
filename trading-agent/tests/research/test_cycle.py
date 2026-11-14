"""The judge, the confidence blend, a whole research cycle and the dry-run command."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from sqlalchemy import Engine, func, select
from typer.testing import CliRunner

from papertrader.agents.schemas import AgentReport, CitedEvidence
from papertrader.cli import app
from papertrader.config import AgentOverride, BlendWeights
from papertrader.db import models as m
from papertrader.db.session import make_engine, make_session_factory
from papertrader.judge.blend import agreement, final_confidence
from papertrader.research.cycle import run_cycle
from papertrader.research.report import render

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from .helpers import AS_OF, FakeClaude, agent_answer, judge_buys, make_claude, report, seed_market

CFG = shipped_config()


def rep(agent: str, direction: str, confidence: float, ticker: str = "AAPL") -> AgentReport:
    return AgentReport(agent_name=agent, as_of=AS_OF, ticker=ticker, direction=direction,  # type: ignore[arg-type]
                       confidence=confidence, horizon_days=5, thesis="t",
                       evidence=[CitedEvidence(claim="c", source_id="news:1")], risks=[], catalysts=[])  # fmt: skip


# ----------------------------------------------------------------------------- the blend


def test_agreement_rewards_breadth_and_penalises_dissent() -> None:
    one = agreement([rep("news_event", "long", 0.8)], "buy", {})
    three = agreement(
        [rep(a, "long", 0.8) for a in ("news_event", "earnings", "technicals")], "buy", {}
    )
    split = agreement([rep("news_event", "long", 0.8), rep("contrarian", "short", 0.8)], "buy", {})
    assert one == pytest.approx(0.8 / 1.3)  # one voice alone: 0.62
    assert three == pytest.approx(2.4 / 2.9)  # three voices: 0.83
    assert split == pytest.approx(0.8 / 2.1)  # an objection pulls it to 0.38
    assert split < one < three
    assert agreement([], "buy", {}) == 0.0
    assert agreement([rep("x", "short", 0.8)], "sell_short", {}) == pytest.approx(one)


def test_agent_reliability_weights_shift_the_agreement() -> None:
    reports = [rep("news_event", "long", 0.8), rep("contrarian", "short", 0.8)]
    trusted_bull = agreement(reports, "buy", {"news_event": 1.5, "contrarian": 0.5})
    trusted_bear = agreement(reports, "buy", {"news_event": 0.5, "contrarian": 1.5})
    assert trusted_bull > agreement(reports, "buy", {}) > trusted_bear


def test_final_confidence_follows_the_documented_formula() -> None:
    blend = BlendWeights(judge=0.5, model=0.3, agreement=0.2)
    assert final_confidence(0.8, 0.6, 0.7, blend) == pytest.approx(
        0.5 * 0.8 + 0.3 * 0.6 + 0.2 * 0.7
    )
    # No pattern model yet: its weight is shared out, it is not treated as zero.
    assert final_confidence(0.8, None, 0.7, blend) == pytest.approx((0.5 * 0.8 + 0.2 * 0.7) / 0.7)


# ----------------------------------------------------------------------------- a full cycle


def busy_market(fake: FakeClaude) -> None:
    """Scripted analysts: three back AAPL, one backs XOM, one writes rubbish."""
    fake.responses["news_event"] = agent_answer(
        report("AAPL", "long", 0.75, "news:1", "px:AAPL", event_type="product"),
        report("JPM", "long", 0.6, "news:999"),  # cites a source that does not exist
    )
    fake.responses["technicals"] = agent_answer(
        report("AAPL", "long", 0.65, "px:AAPL", event_type="technical")
    )
    fake.responses["sector_analyst:Information Technology"] = agent_answer(
        report("AAPL", "long", 0.7, "news:1"), report("MSFT", "neutral", 0.5, "earn:MSFT:20261112")
    )
    fake.responses["insider_institutional"] = agent_answer(
        report("XOM", "long", 0.6, "insider:XOM", "13f:XOM", event_type="insider_activity")
    )
    fake.responses["macro"] = agent_answer(
        report("XLK", "long", 0.55, "macro:DGS10", event_type="macro")
    )
    fake.responses["contrarian"] = agent_answer(report("XOM", "short", 0.7, "px:XOM"))
    fake.responses["judge"] = judge_buys("AAPL", "XOM", confidence=0.75)


async def test_full_cycle_from_agents_to_a_stored_decision(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    fake.delay = 0.005
    busy_market(fake)
    claude = make_claude(engine, fake)
    sessions = make_session_factory(engine)

    result = await run_cycle(config=CFG, sessions=sessions, claude=claude, as_of=AS_OF,
                             kind=m.CycleKind.DRY_RUN)  # fmt: skip
    assert result.status == "succeeded"

    # Who was asked: sectors with nothing fresh were skipped without an API call.
    labels = fake.labels()
    assert labels[0] == "macro" and labels[-2:] == ["contrarian", "judge"]
    assert (
        "sector_analyst:Information Technology" in labels
        and "sector_analyst:Utilities" not in labels
    )
    skipped = {r.label: r.detail for r in result.runs if r.status == "skipped"}
    assert skipped["sector_analyst:Utilities"] == "nothing new to look at"
    assert (
        len(labels) == 12
    )  # 6 single agents + 4 sectors with something fresh + contrarian + judge

    # The first agent finished before any other started (it fills the prompt cache);
    # after that, agents ran side by side but never more than the configured number.
    assert fake.events[:2] == [("start", "macro"), ("end", "macro")]
    assert 1 < fake.max_in_flight <= CFG.agents.concurrency

    # Citations: JPM's only claim cited a missing source, so that report is gone.
    by_agent = {r.label: r for r in result.runs}
    assert (by_agent["news_event"].reports, by_agent["news_event"].dropped_claims,
            by_agent["news_event"].dropped_reports) == (1, 1, 1)  # fmt: skip
    assert "JPM" not in {r.ticker for r in result.reports}

    # The judge saw real report ids, the contrarian's objection and the limits.
    judge_prompt = fake.call("judge").user
    assert (
        "shorts are NOT allowed" in judge_prompt
        and "pattern model is not available" in judge_prompt
    )
    assert "contrarian | SHORT XOM" in judge_prompt
    assert fake.call("judge").body["model"] == "claude-opus-5-5"
    assert fake.call("judge").body["output_config"]["effort"] == "high"
    assert fake.call("macro").body["model"] == "claude-sonnet-5-5"

    # Decision: AAPL has three backers and no dissent; XOM has one backer and an objection.
    decision = result.decision
    assert decision is not None
    aapl, xom = decision.trades
    assert (aapl.ticker, aapl.rank, aapl.status) == ("AAPL", 1, "proposed")
    assert aapl.agent_agreement == pytest.approx((0.75 + 0.65 + 0.7) / (0.75 + 0.65 + 0.7 + 0.5))
    assert aapl.final_confidence == pytest.approx((0.5 * 0.75 + 0.2 * aapl.agent_agreement) / 0.7)
    assert aapl.model_probability is None and len(aapl.supporting) == 3 and aapl.size_pct == 0.08
    assert (xom.ticker, xom.status) == ("XOM", "below_threshold")
    assert xom.agent_agreement == pytest.approx(0.6 / (0.6 + 0.7 + 0.5))
    assert xom.final_confidence < 0.65 <= aapl.final_confidence
    assert len(xom.opposing) == 1

    # Stored: the cycle, every report, the decision, and the links between them.
    with sessions() as session:
        cycle = session.get(m.DecisionCycle, result.cycle_id)
        assert (
            cycle is not None
            and cycle.status == m.RunStatus.SUCCEEDED
            and cycle.kind == m.CycleKind.DRY_RUN
        )
        assert (
            session.scalar(select(func.count()).select_from(m.AgentReport))
            == len(result.reports)
            == 7
        )
        ideas = {i.ticker: i for i in session.scalars(select(m.TradeIdea))}
        assert ideas["AAPL"].status == m.IdeaStatus.PROPOSED
        assert ideas["XOM"].status == m.IdeaStatus.BELOW_THRESHOLD
        links = session.execute(
            select(m.AgentReport.agent_name, m.TradeIdeaReport.stance)
            .join(m.TradeIdeaReport, m.TradeIdeaReport.agent_report_id == m.AgentReport.id)
            .where(m.TradeIdeaReport.trade_idea_id == ideas["XOM"].id)
        ).all()
        assert sorted(links) == [
            ("contrarian", m.Stance.OPPOSE),
            ("insider_institutional", m.Stance.SUPPORT),
        ]
        stored = session.scalars(
            select(m.AgentReport).where(m.AgentReport.agent_name == "sector_analyst")
        ).first()
        assert stored is not None and stored.sector == "Information Technology"
        assert stored.evidence[0]["url"] == "https://news.example/1"
        usage = session.scalars(select(m.ApiUsage)).all()
        assert len(usage) == 12 and all(row.cycle_id == result.cycle_id for row in usage)
        assert result.cost_usd == pytest.approx(sum(row.cost_usd for row in usage))

    # Prompt caching: every research agent sent the same first system block, and all
    # but the first read it from the cache.
    agents = [c for c in fake.calls if c.label != "judge"]
    assert len({c.body["system"][0]["text"] for c in agents}) == 1
    with sessions() as session:
        reads = {
            row.purpose: row.cache_read_input_tokens for row in session.scalars(select(m.ApiUsage))
        }
    assert reads["macro"] == 0 and all(reads[c.label] == 900 for c in agents[1:])

    text = render(result, now=AS_OF)
    assert "1. BUY AAPL" in text and "[WOULD TRADE]" in text and "[not traded]" in text
    assert "removed 1 uncited claim(s) and 1 report(s)" in text
    assert (
        "Prompt cache: 10 of 11 agent calls read the shared rules from the cache (9,000 tokens"
        in text
    )
    assert "run on a past date" not in text


async def test_judges_trades_are_checked_by_the_program(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    honest = judge_buys("AAPL", "XOM", "MSFT", "PFE")

    def sloppy(call: Any) -> dict[str, Any]:
        answer = honest(call)
        aapl, xom, msft, pfe = answer["trades"]
        xom |= {
            "action": "sell_short",
            "target_price": xom["stop_price"],
            "stop_price": xom["target_price"],
        }
        msft["supporting_reports"] = ["report:99999"]  # an id that does not exist
        pfe |= {
            "entry_low": 1.0,
            "entry_high": 2.0,
            "stop_price": 0.5,
            "supporting_reports": aapl["supporting_reports"],
        }
        answer["trades"] = [aapl, xom, msft, pfe, dict(aapl), {**aapl, "ticker": "TSLA"}]
        aapl["size_pct"] = 0.9  # nine times the per-position limit
        return answer

    fake.responses["judge"] = sloppy
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    assert result.decision is not None
    trades = {t.ticker: t for t in result.decision.trades}
    assert len(result.decision.trades) == 5  # the repeated AAPL entry was dropped
    assert trades["AAPL"].status == "proposed" and trades["AAPL"].size_pct == 0.10  # capped
    assert "short selling is disabled" in (trades["XOM"].status_reason or "")
    assert "no valid supporting report" in (trades["MSFT"].status_reason or "")
    assert "no valid supporting report" in (
        trades["PFE"].status_reason or ""
    )  # AAPL's reports do not count
    assert "not a stock in the universe" in (trades["TSLA"].status_reason or "")
    assert [t.rank for t in result.decision.trades] == [1, 2, 3, 4, 5] and trades["AAPL"].rank == 1


async def test_made_up_prices_are_rejected(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    honest = judge_buys("AAPL")

    def wrong_prices(call: Any) -> dict[str, Any]:
        answer = honest(call)
        answer["trades"][0] |= {
            "entry_low": 10.0,
            "entry_high": 11.0,
            "target_price": 15.0,
            "stop_price": 9.0,
        }
        return answer

    fake.responses["judge"] = wrong_prices
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    (trade,) = result.decision.trades  # type: ignore[union-attr]
    assert trade.status == "rejected_by_risk" and "far from the last close" in (
        trade.status_reason or ""
    )


async def test_one_failing_agent_does_not_stop_the_cycle(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    fake.responses["news_event"] = 529  # Claude API overloaded
    fake.responses["technicals"] = "refusal"
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    status = {r.label: r.status for r in result.runs}
    assert (status["news_event"], status["technicals"], status["judge"]) == (
        "failed",
        "failed",
        "ok",
    )
    assert result.status == "succeeded" and result.decision is not None
    assert "declined" in next(r.detail for r in result.runs if r.label == "technicals")


async def test_a_bad_api_key_stops_the_cycle_at_once(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    fake.responses["macro"] = 401
    sessions = make_session_factory(engine)
    result = await run_cycle(
        config=CFG, sessions=sessions, claude=make_claude(engine, fake), as_of=AS_OF
    )
    assert result.status == "failed" and "ANTHROPIC_API_KEY" in result.notes[-1]
    assert fake.labels() == ["macro"]  # nobody else was called with the bad key
    with sessions() as session:
        assert session.scalars(select(m.DecisionCycle.status)).one() == m.RunStatus.FAILED


async def test_a_quiet_day_costs_almost_nothing_and_proposes_nothing(engine: Engine) -> None:
    seed_market(engine, with_events=False)
    fake = FakeClaude()  # every agent answers with an empty list
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    assert fake.labels() == ["macro", "technicals"]  # nothing to judge, so the judge was not called
    assert (
        result.status == "succeeded"
        and result.decision is not None
        and result.decision.trades == []
    )
    assert "nothing to judge" in " ".join(result.notes)


# ----------------------------------------------------------------------------- budget


def spend(engine: Engine, usd: float) -> None:
    with make_session_factory(engine)() as session:
        session.add(m.ApiUsage(ts=datetime(2026, 11, 3, 15, tzinfo=UTC), purpose="earlier",
                               model="claude-sonnet-5-5", cost_usd=usd))  # fmt: skip
        session.commit()


async def test_close_to_the_budget_fewer_agents_run_at_lower_effort(engine: Engine) -> None:
    seed_market(engine)
    spend(engine, 90.0)  # 90% of the month's budget
    fake = FakeClaude()
    busy_market(fake)
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    assert result.status == "succeeded" and result.budget.mode == "degraded"
    assert "Reduced run" in result.notes[0]
    labels = fake.labels()
    assert not any(label.startswith("sector_analyst") for label in labels)
    assert "insider_institutional" not in labels and "news_event" in labels
    assert fake.call("news_event").body["output_config"]["effort"] == "low"  # medium -> low
    assert fake.call("judge").body["output_config"]["effort"] == "medium"  # high -> medium
    saved = {r.label: r.detail for r in result.runs if r.status == "skipped"}
    assert saved["insider_institutional"] == "skipped to save budget"


async def test_with_the_budget_used_up_nothing_is_called_and_nothing_breaks(engine: Engine) -> None:
    seed_market(engine)
    spend(engine, 100.0)
    fake = FakeClaude()
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    assert result.status == "skipped" and fake.calls == []
    assert "monthly budget used up" in result.notes[0]
    assert "No research today" in render(result, now=AS_OF)


async def test_a_run_cap_stops_agents_but_keeps_room_for_the_judge(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    busy_market(fake)
    fake.delay = 0.01  # calls overlap, as they do against the real API
    claude = make_claude(engine, fake, cycle_cap=0.25)  # the judge alone is held at about $0.16
    result = await run_cycle(
        config=CFG, sessions=make_session_factory(engine), claude=claude, as_of=AS_OF
    )
    statuses = {r.label: r.status for r in result.runs}
    assert "skipped" in statuses.values() and statuses["macro"] == "ok"
    assert any("spending cap" in r.detail for r in result.runs if r.status == "skipped")
    assert statuses["judge"] == "ok"  # the agents could not spend the judge's share
    assert result.cost_usd <= 0.25


async def test_agent_settings_come_from_config(engine: Engine) -> None:
    seed_market(engine)
    agents = CFG.agents.model_copy(update={"overrides": {
        "technicals": AgentOverride(model="claude-haiku-5-5", effort="low"),
        "news_event": AgentOverride(enabled=False),
    }})  # fmt: skip
    config = CFG.model_copy(update={"agents": agents})
    fake = FakeClaude()
    busy_market(fake)
    result = await run_cycle(config=config, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake, config=config), as_of=AS_OF)  # fmt: skip
    technicals = fake.call("technicals").body
    assert (technicals["model"], technicals["output_config"]["effort"]) == (
        "claude-haiku-5-5",
        "low",
    )
    assert "news_event" not in fake.labels()
    assert {r.label: r.detail for r in result.runs}["news_event"] == "disabled in config.yaml"

    only = FakeClaude()
    await run_cycle(config=CFG, sessions=make_session_factory(engine), claude=make_claude(engine, only),
                    as_of=AS_OF, only=["macro", "technicals"])  # fmt: skip
    assert only.labels() == ["macro", "technicals"]


async def test_estimate_only_makes_no_api_calls(engine: Engine) -> None:
    seed_market(engine)
    fake = FakeClaude()
    sessions = make_session_factory(engine)
    result = await run_cycle(config=CFG, sessions=sessions, claude=make_claude(engine, fake),
                             as_of=AS_OF, estimate_only=True)  # fmt: skip
    assert fake.calls == [] and result.status == "estimated"
    assert 0.2 < result.cost_usd < 3.0
    assert {r.label for r in result.runs if r.status == "estimated"} >= {
        "macro",
        "judge",
        "news_event",
    }
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(m.DecisionCycle)) == 0
    assert "no API calls were made" in render(result, now=AS_OF)


async def test_without_prices_the_cycle_explains_what_to_do(engine: Engine) -> None:
    fake = FakeClaude()
    result = await run_cycle(config=CFG, sessions=make_session_factory(engine),
                             claude=make_claude(engine, fake), as_of=AS_OF)  # fmt: skip
    assert result.status == "skipped" and "ingest daily" in result.notes[0] and fake.calls == []


def test_runs_on_past_dates_carry_the_leakage_warning(engine: Engine) -> None:
    from papertrader.research.cycle import CycleResult

    claude = make_claude(engine, FakeClaude())
    result = CycleResult(
        as_of=AS_OF, kind="dry_run", status="succeeded", budget=claude.governor.status()
    )
    text = render(result, now=AS_OF + timedelta(days=30))
    assert "run on a past date" in text and "Only forward paper trading is a real test" in text


# ----------------------------------------------------------------------------- CLI

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'app.db'}")
    return tmp_path


def seeded(project: Path) -> None:
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    engine = make_engine(f"sqlite:///{project / 'data' / 'app.db'}")
    seed_market(engine)
    engine.dispose()


def test_dry_run_needs_a_key_unless_estimating(project: Path) -> None:
    seeded(project)
    result = runner.invoke(app, ["dry-run", "--date", "2026-11-13"])
    assert result.exit_code == 1 and "ANTHROPIC_API_KEY is not set" in result.output

    result = runner.invoke(app, ["dry-run", "--date", "2026-11-13", "--estimate-only"])
    assert result.exit_code == 0, result.output
    assert "Decision as of Friday 2026-11-13 08:20 New York time" in result.output
    assert "Estimated cost of a full run" in result.output


def test_dry_run_prints_a_full_decision(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seeded(project)
    fake = FakeClaude()
    busy_market(fake)

    def fake_client(api_key: str) -> AsyncAnthropic:
        assert api_key == "sk-ant-test"
        transport = httpx2.MockTransport(fake.handler)
        return AsyncAnthropic(api_key=api_key, max_retries=0,
                              http_client=DefaultAsyncHttpxClient(transport=transport))  # fmt: skip

    monkeypatch.setattr("papertrader.llm.client.make_anthropic", fake_client)
    result = runner.invoke(app, ["dry-run", "--date", "2026-11-13", "--max-cost", "3"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "1. BUY AAPL" in out and "[WOULD TRADE]" in out and "A calm market." in out
    assert "Cost of this run: $" in out and "sk-ant-test" not in out
    assert "news_event" in out and "supporting reports:" in out


def test_dry_run_rejects_bad_input(project: Path) -> None:
    assert "unknown agent" in runner.invoke(app, ["dry-run", "--agents", "astrology"]).output
    assert (
        "could not read the date" in runner.invoke(app, ["dry-run", "--date", "13/11/2026"]).output
    )
    assert (
        "in the future"
        in runner.invoke(app, ["dry-run", "--date", "2999-01-01", "--estimate-only"]).output
    )
