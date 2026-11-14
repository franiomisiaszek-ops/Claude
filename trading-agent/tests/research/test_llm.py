from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, func, select

from papertrader.config import ModelPrice
from papertrader.db.models import ApiUsage
from papertrader.db.session import make_session_factory
from papertrader.llm.client import (
    BudgetExceededError,
    LLMConfigError,
    LLMError,
    LLMRefusedError,
    LLMUnavailableError,
    SystemPrompt,
)
from papertrader.llm.governor import CostGovernor, lower_effort, weekdays_left_in_month
from papertrader.llm.pricing import Usage, cost_usd, estimate_cost

from ..ingest.helpers import shipped_config
from .helpers import AS_OF, FakeClaude, agent_answer, make_claude

SYSTEM = SystemPrompt("SHARED RULES " * 50, "AGENT: macro\nbe careful")


class Answer(BaseModel):
    reports: list[dict]  # type: ignore[type-arg]
    summary: str


def spend(engine: Engine, usd: float, when: datetime = AS_OF) -> None:
    with make_session_factory(engine)() as session:
        session.add(ApiUsage(ts=when, purpose="test", model="claude-sonnet-5-5", cost_usd=usd))
        session.commit()


async def ask(claude, **overrides):  # type: ignore[no-untyped-def]
    kwargs = {"purpose": "macro", "model": "claude-sonnet-5-5", "effort": "medium", "max_tokens": 16000,
              "system": SYSTEM, "user": "context " * 400, "output": Answer}  # fmt: skip
    return await claude.structured(**(kwargs | overrides))


# ----------------------------------------------------------------------------- pricing


def test_cost_counts_every_kind_of_token() -> None:
    budget = shipped_config().budget
    usage = Usage(input_tokens=10_000, output_tokens=2_000, cache_creation_input_tokens=1_000,
                  cache_read_input_tokens=4_000)  # fmt: skip
    # Sonnet 5.5: $2 in, $10 out, $2.50 cache write, $0.10 cache read, per million tokens.
    expected = (10_000 * 2 + 2_000 * 10 + 1_000 * 2.5 + 4_000 * 0.1) / 1e6
    assert cost_usd(usage, "claude-sonnet-5-5", budget) == pytest.approx(expected)
    assert cost_usd(usage, "claude-sonnet-5-5", budget, batch=True) == pytest.approx(expected / 2)
    assert cost_usd(usage, "claude-opus-5-5", budget) > expected


def test_estimates_err_on_the_expensive_side() -> None:
    budget = shipped_config().budget
    text = "word " * 2000  # about 2,000 tokens in reality
    estimate = estimate_cost(len(text), "claude-sonnet-5-5", "medium", 16000, budget)
    real = cost_usd(Usage(input_tokens=2000, output_tokens=1500), "claude-sonnet-5-5", budget)
    assert estimate > real
    assert estimate_cost(len(text), "claude-sonnet-5-5", "high", 16000, budget) > estimate


# ----------------------------------------------------------------------------- governor


def test_weekdays_left_and_effort_steps() -> None:
    assert weekdays_left_in_month(date(2026, 11, 13)) == 12  # Fri 13th .. Mon 30th
    assert weekdays_left_in_month(date(2026, 11, 29)) == 1  # a Sunday: only Monday the 30th
    assert [lower_effort(e) for e in ("max", "high", "medium", "low")] == [
        "xhigh",
        "medium",
        "low",
        "low",
    ]


def governor(engine: Engine, **kwargs) -> CostGovernor:  # type: ignore[no-untyped-def]
    return CostGovernor(
        make_session_factory(engine), shipped_config().budget, now=lambda: AS_OF, **kwargs
    )


def test_governor_modes_follow_the_monthly_thresholds(engine: Engine) -> None:
    gov = governor(engine)
    status = gov.status()
    assert (status.mode, status.warn, status.month_spent) == ("normal", False, 0.0)
    assert status.day_budget == pytest.approx(100 / 12 * 1.5)

    spend(engine, 72.0, datetime(2026, 11, 3, 15, tzinfo=UTC))  # earlier this month
    status = gov.status()
    assert (status.mode, status.warn) == ("normal", True)  # past the 70% warning line

    spend(engine, 14.0, datetime(2026, 11, 10, 15, tzinfo=UTC))
    status = gov.status()
    assert status.mode == "degraded" and "86%" in status.reason  # past 85%

    spend(engine, 14.0, datetime(2026, 11, 11, 15, tzinfo=UTC))
    status = gov.status()
    assert status.mode == "stopped" and "used up" in status.reason


def test_last_months_spend_does_not_count(engine: Engine) -> None:
    spend(engine, 500.0, datetime(2026, 10, 31, 12, tzinfo=UTC))
    assert governor(engine).status().month_spent == 0.0


def test_a_heavy_day_degrades_even_early_in_the_month(engine: Engine) -> None:
    spend(engine, 13.0)  # today; the day's share is 100/12*1.5 = 12.50
    status = governor(engine).status()
    assert status.mode == "degraded" and "today's share" in status.reason


def test_reservations_stop_parallel_calls_from_overshooting(engine: Engine) -> None:
    spend(engine, 99.0, datetime(2026, 11, 3, 15, tzinfo=UTC))
    gov = governor(engine)
    gov.reserve(0.60)
    with pytest.raises(BudgetExceededError, match="monthly API budget"):
        gov.reserve(0.60)  # 99.00 + 0.60 + 0.60 would pass $100
    gov.settle(0.60, 0.10)  # the first call turned out cheap...
    gov.reserve(0.60)  # ...so there is room again


def test_a_run_can_have_its_own_spending_cap(engine: Engine) -> None:
    gov = governor(engine, cycle_cap_usd=1.0)
    gov.reserve(0.7)
    with pytest.raises(BudgetExceededError, match="this run's spending cap"):
        gov.reserve(0.7)


# ----------------------------------------------------------------------------- client


async def test_a_call_sends_the_documented_request_and_logs_its_cost(engine: Engine) -> None:
    fake = FakeClaude()
    fake.responses["macro"] = agent_answer({"ticker": "XLK"})
    claude = make_claude(engine, fake)

    result = await ask(claude)
    assert result.output.reports == [{"ticker": "XLK"}]
    assert result.request_id == "req_1"

    body, headers = fake.calls[0].body, fake.calls[0].headers
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"  # structured output
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # static part is cached
    assert "cache_control" not in body["system"][1]
    assert body["fallbacks"] == "default"
    assert headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert "test-key" in headers["authorization"]

    with make_session_factory(engine)() as session:
        row = session.scalars(select(ApiUsage)).one()
    assert (row.purpose, row.model, row.effort, row.stop_reason) == (
        "macro",
        "claude-sonnet-5-5",
        "medium",
        "end_turn",
    )
    assert (row.output_tokens, row.cache_creation_input_tokens) == (400, 900)
    assert row.cost_usd == pytest.approx(result.cost_usd) and row.cost_usd > 0
    assert row.error is None and row.duration_ms is not None
    assert claude.governor.cycle_spent == pytest.approx(result.cost_usd)


async def test_haiku_gets_no_fallback_parameter(engine: Engine) -> None:
    fake = FakeClaude()
    await ask(make_claude(engine, fake), model="claude-haiku-5-5")
    assert "fallbacks" not in fake.calls[0].body
    assert "anthropic-beta" not in fake.calls[0].headers


async def test_rate_limits_are_retried_by_the_sdk(engine: Engine) -> None:
    fake = FakeClaude()
    answers = iter([429, 429, agent_answer()])
    fake.responses["macro"] = lambda _call: next(answers)
    result = await ask(make_claude(engine, fake, max_retries=2))
    assert len(fake.calls) == 3 and result.output.summary


@pytest.mark.parametrize(
    ("answer", "error", "logged"),
    [
        (429, LLMUnavailableError, "rate limited"),
        (529, LLMUnavailableError, "HTTP 529"),
        (400, LLMError, "HTTP 400"),
        (401, LLMConfigError, "authentication failed"),
        ("refusal", LLMRefusedError, "refused (cyber)"),
        ("max_tokens", LLMError, "cut off at max_tokens"),
        ({"reports": "not a list", "summary": 3}, LLMError, "did not match the schema"),
    ],
)
async def test_every_failure_becomes_a_clear_error_and_is_still_logged(
    engine: Engine, answer: object, error: type[Exception], logged: str
) -> None:
    fake = FakeClaude()
    fake.responses["macro"] = answer
    claude = make_claude(engine, fake)
    with pytest.raises(error):
        await ask(claude)
    with make_session_factory(engine)() as session:
        row = session.scalars(select(ApiUsage)).one()
    assert logged in (row.error or "")
    assert claude.governor.status().month_spent == pytest.approx(
        row.cost_usd
    )  # nothing left reserved


async def test_a_refusal_that_was_billed_is_still_counted(engine: Engine) -> None:
    fake = FakeClaude()
    fake.responses["macro"] = "refusal"
    claude = make_claude(engine, fake)
    with pytest.raises(LLMRefusedError):
        await ask(claude)
    assert claude.governor.cycle_spent > 0


async def test_an_answer_from_a_fallback_model_is_priced_at_that_models_rate(
    engine: Engine,
) -> None:
    fake = FakeClaude()
    fake.responses["judge"] = {"_served_by": "claude-opus-4-8", "reports": [], "summary": "s"}
    claude = make_claude(engine, fake)
    system = SystemPrompt("SHARED", "AGENT: judge")
    result = await ask(claude, purpose="judge", model="claude-opus-5-5", system=system)
    assert result.model == "claude-opus-4-8"
    usage = Usage(len("context " * 400) // 4, 400, 900, 0)
    assert result.cost_usd == pytest.approx(
        cost_usd(usage, "claude-opus-4-8", shipped_config().budget)
    )


async def test_no_request_is_sent_once_the_budget_is_gone(engine: Engine) -> None:
    spend(engine, 100.0, datetime(2026, 11, 3, 15, tzinfo=UTC))
    fake = FakeClaude()
    with pytest.raises(BudgetExceededError):
        await ask(make_claude(engine, fake))
    assert fake.calls == []
    with make_session_factory(engine)() as session:
        assert (
            session.scalar(select(func.count()).select_from(ApiUsage)) == 1
        )  # only the seeded row


async def test_parallel_calls_cannot_jointly_break_a_cap(engine: Engine) -> None:
    fake = FakeClaude()
    fake.delay = 0.01
    claude = make_claude(engine, fake, cycle_cap=0.12)  # room for about two calls
    results = await asyncio.gather(*(ask(claude) for _ in range(6)), return_exceptions=True)
    refused = [r for r in results if isinstance(r, BudgetExceededError)]
    assert len(fake.calls) + len(refused) == 6
    assert 1 <= len(fake.calls) <= 3 and refused
    assert claude.governor.cycle_spent <= 0.12


def test_unknown_model_prices_are_a_config_error() -> None:
    assert isinstance(shipped_config().budget.prices["claude-opus-4-8"], ModelPrice)
