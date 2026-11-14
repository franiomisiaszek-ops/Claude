"""Runs one full research cycle as of a fixed moment.

    snapshot (point-in-time data)
      -> research agents, in parallel
      -> contrarian agent, challenging their best ideas
      -> judge
      -> decision, stored with links back to every report

The same function serves the daily run, a thesis-break re-check and the `dry-run`
command. One agent failing never stops the cycle; an exhausted budget ends it cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents import prompts
from papertrader.agents.context import ContextPack
from papertrader.agents.roster import CONTRARIAN, AgentSpec, build, build_contrarian, specs
from papertrader.agents.schemas import AgentOutput, AgentReport
from papertrader.agents.snapshot import Snapshot, build_snapshot, fresh_since
from papertrader.agents.validate import finalise
from papertrader.config import AgentSettings, AppConfig
from papertrader.db import models as m
from papertrader.db.types import utcnow
from papertrader.ingest.filing_text import fetch_filing_excerpts
from papertrader.ingest.http import Fetcher
from papertrader.ingest.timeutil import NEW_YORK
from papertrader.judge.judge import (
    JUDGE_SYSTEM,
    agent_weights,
    decide,
    render_context,
    store_decision,
)
from papertrader.judge.schemas import JudgeDecision, JudgeOutput
from papertrader.llm.client import (
    BudgetExceededError,
    ClaudeClient,
    LLMConfigError,
    LLMError,
    SystemPrompt,
)
from papertrader.llm.governor import BudgetStatus, lower_effort
from papertrader.llm.pricing import estimate_cost

log = logging.getLogger(__name__)

# When money is tight only these run: the eleven sector analysts and the slow-moving
# insider/13F agent are the first to go.
DEGRADED_AGENTS = frozenset(
    {"macro", "earnings", "sec_filings", "news_event", "technicals", "contrarian"}
)
# Room kept free for the judge so the agents cannot spend the whole allowance.
JUDGE_PROMPT_CHARS_GUESS = 45_000


@dataclass
class AgentRun:
    label: str
    status: str  # ok | skipped | failed | estimated
    reports: int = 0
    dropped_claims: int = 0
    dropped_reports: int = 0
    cost_usd: float = 0.0
    detail: str = ""
    cache_read_tokens: int = 0  # input tokens served from Claude's prompt cache


@dataclass
class CycleResult:
    as_of: datetime
    kind: str
    status: str  # succeeded | skipped | failed | estimated
    budget: BudgetStatus
    cycle_id: int | None = None
    runs: list[AgentRun] = field(default_factory=list)
    reports: list[AgentReport] = field(default_factory=list)
    decision: JudgeDecision | None = None
    cost_usd: float = 0.0
    notes: list[str] = field(default_factory=list)
    prices_through: date | None = None
    covered: int = 0


def _settings(config: AppConfig, name: str, degraded: bool) -> AgentSettings:
    settings = config.agents.for_agent(name)
    if degraded:
        settings = settings.model_copy(update={"effort": lower_effort(settings.effort)})
    return settings


def _store_reports(
    sessions: sessionmaker[Session], cycle_id: int, model: str, reports: Sequence[AgentReport]
) -> list[AgentReport]:
    """Save reports and hand them back with their database ids (the judge cites those)."""
    if not reports:
        return []
    with sessions() as session:
        rows = [
            m.AgentReport(
                cycle_id=cycle_id, agent_name=r.agent_name, sector=r.sector,
                event_type=r.event_type, as_of=r.as_of, ticker=r.ticker,
                direction=m.Direction(r.direction), confidence=r.confidence,
                horizon_days=r.horizon_days, thesis=r.thesis,
                evidence=[e.model_dump() for e in r.evidence], risks=list(r.risks),
                catalysts=list(r.catalysts), dropped_claims=r.dropped_claims, model=model,
            )
            for r in reports
        ]  # fmt: skip
        session.add_all(rows)
        session.commit()
        return [
            r.model_copy(update={"report_id": row.id}) for r, row in zip(reports, rows, strict=True)
        ]


async def _run_agent(
    *,
    spec: AgentSpec,
    pack: ContextPack,
    settings: AgentSettings,
    config: AppConfig,
    claude: ClaudeClient,
    sessions: sessionmaker[Session],
    cycle_id: int,
    as_of: datetime,
) -> tuple[AgentRun, list[AgentReport]]:
    limit = config.agents.max_reports_per_agent
    try:
        result = await claude.structured(
            purpose=spec.label,
            model=settings.model,
            effort=settings.effort,
            max_tokens=settings.max_tokens,
            system=SystemPrompt(prompts.SHARED_RULES, spec.prompt),
            user=pack.render(limit),
            output=AgentOutput,
            cycle_id=cycle_id,
        )
    except BudgetExceededError as exc:
        return AgentRun(spec.label, "skipped", detail=str(exc)), []
    except LLMConfigError:
        raise  # a bad API key affects everything: stop the cycle
    except LLMError as exc:
        log.warning("agent failed", extra={"agent": spec.label, "error": str(exc)})
        return AgentRun(spec.label, "failed", detail=str(exc)), []

    checked = finalise(
        agent_name=spec.name, sector=spec.sector, as_of=as_of, output=result.output,
        pack=pack, max_reports=limit,
    )  # fmt: skip
    stored = _store_reports(sessions, cycle_id, result.model, checked.reports)
    run = AgentRun(
        spec.label, "ok", len(stored), checked.dropped_claims, checked.dropped_reports,
        result.cost_usd, result.output.summary[:300], result.usage.cache_read_input_tokens,
    )  # fmt: skip
    log.info(
        "agent done",
        extra={"agent": spec.label, "reports": len(stored), "dropped_claims": checked.dropped_claims,
               "cost_usd": round(result.cost_usd, 4)},
    )  # fmt: skip
    return run, stored


def _finish(sessions: sessionmaker[Session], result: CycleResult, status: m.RunStatus) -> None:
    if result.cycle_id is None:
        return
    with sessions() as session:
        cycle = session.get(m.DecisionCycle, result.cycle_id)
        assert cycle is not None
        cycle.status = status
        cycle.finished_at = utcnow()
        cycle.notes = {
            "budget_mode": result.budget.mode,
            "cost_usd": round(result.cost_usd, 4),
            "notes": result.notes,
            "agents": [
                {"agent": r.label, "status": r.status, "reports": r.reports,
                 "dropped_claims": r.dropped_claims, "detail": r.detail[:200]}
                for r in result.runs
            ],
        }  # fmt: skip
        session.commit()


async def run_cycle(
    *,
    config: AppConfig,
    sessions: sessionmaker[Session],
    claude: ClaudeClient,
    as_of: datetime,
    kind: m.CycleKind = m.CycleKind.DAILY,
    only: Sequence[str] | None = None,
    sec: Fetcher | None = None,
    estimate_only: bool = False,
    positions: Sequence[str] = (),
    model_scores: Mapping[str, float] | None = None,
    lessons: str | None = None,
) -> CycleResult:
    governor = claude.governor
    budget = governor.status()
    result = CycleResult(as_of=as_of, kind=kind.value, status="failed", budget=budget)
    degraded = budget.mode == "degraded"

    if budget.mode == "stopped" and not estimate_only:
        result.status = "skipped"
        result.notes.append(f"No research today: {budget.reason}.")
        return result
    if degraded:
        result.notes.append(f"Reduced run ({budget.reason}): fewer agents, lower effort.")

    # ------------------------------------------------------------------ data
    if sec is not None and not estimate_only:
        since = fresh_since(as_of, config.agents.lookback_hours)
        try:
            await fetch_filing_excerpts(sec, sessions, since=since, as_of=as_of)
        except Exception as exc:  # optional extra; never block the cycle on it
            result.notes.append(f"Filing texts could not be read ({type(exc).__name__}).")
    with sessions() as session:
        snap: Snapshot = build_snapshot(session, config, as_of)
        weights = agent_weights(session, as_of)
    result.prices_through = snap.prices_through
    result.covered = len(snap.covered)
    if not snap.covered:
        result.status = "skipped"
        result.notes.append("No stocks have recent prices. Run `papertrader ingest daily` first.")
        return result

    # ------------------------------------------------------------------ who runs
    runnable: list[tuple[AgentSpec, ContextPack, AgentSettings]] = []
    for spec in specs():
        settings = _settings(config, spec.name, degraded)
        if only and spec.name not in only:
            continue
        if not settings.enabled:
            result.runs.append(AgentRun(spec.label, "skipped", detail="disabled in config.yaml"))
        elif degraded and spec.name not in DEGRADED_AGENTS:
            result.runs.append(AgentRun(spec.label, "skipped", detail="skipped to save budget"))
        elif (pack := build(spec, snap, config.agents)) is None or pack.is_empty():
            result.runs.append(AgentRun(spec.label, "skipped", detail="nothing new to look at"))
        else:
            runnable.append((spec, pack, settings))

    limit = config.agents.max_reports_per_agent
    if estimate_only:
        for spec, pack, settings in runnable:
            chars = len(prompts.SHARED_RULES) + len(spec.prompt) + len(pack.render(limit))
            cost = estimate_cost(chars, settings.model, settings.effort, settings.max_tokens,
                                 config.budget)  # fmt: skip
            result.runs.append(AgentRun(spec.label, "estimated", cost_usd=cost,
                                        detail=f"about {chars // 3:,} input tokens"))  # fmt: skip
        judge_cfg = config.judge
        judge_cost = estimate_cost(JUDGE_PROMPT_CHARS_GUESS, judge_cfg.model, judge_cfg.effort,
                                   judge_cfg.max_tokens, config.budget)  # fmt: skip
        result.runs.append(AgentRun("contrarian", "estimated", cost_usd=judge_cost / 4,
                                    detail="depends on the other agents' ideas"))  # fmt: skip
        result.runs.append(AgentRun("judge", "estimated", cost_usd=judge_cost,
                                    detail="depends on the number of reports"))  # fmt: skip
        result.cost_usd = sum(r.cost_usd for r in result.runs)
        result.status = "estimated"
        return result

    # ------------------------------------------------------------------ run
    judge_effort = lower_effort(config.judge.effort) if degraded else config.judge.effort
    judge_hold = estimate_cost(JUDGE_PROMPT_CHARS_GUESS, config.judge.model, judge_effort,
                               config.judge.max_tokens, config.budget)  # fmt: skip
    try:
        governor.reserve(judge_hold)
    except BudgetExceededError as exc:
        result.status = "skipped"
        result.notes.append(f"No research: {exc}.")
        return result

    with sessions() as session:
        cycle = m.DecisionCycle(kind=kind, as_of=as_of, status=m.RunStatus.RUNNING)
        session.add(cycle)
        session.commit()
        result.cycle_id = cycle.id
    assert result.cycle_id is not None
    cycle_id = result.cycle_id

    try:
        gate = asyncio.Semaphore(config.agents.concurrency)

        async def guarded(spec: AgentSpec, pack: ContextPack, settings: AgentSettings):  # type: ignore[no-untyped-def]
            async with gate:
                return await _run_agent(
                    spec=spec, pack=pack, settings=settings, config=config, claude=claude,
                    sessions=sessions, cycle_id=cycle_id, as_of=as_of,
                )  # fmt: skip

        outcomes = []
        if runnable:
            # The first agent goes alone: it writes the shared rules to Claude's prompt
            # cache, and the others, started afterwards, read them from it at a discount.
            outcomes.append(await guarded(*runnable[0]))
            outcomes += await asyncio.gather(*(guarded(*item) for item in runnable[1:]))
        for run, reports in outcomes:
            result.runs.append(run)
            result.reports += reports

        contrarian = _settings(config, CONTRARIAN.name, degraded)
        if contrarian.enabled and (not only or CONTRARIAN.name in only):
            pack = build_contrarian(snap, config.agents, result.reports)
            if pack is None:
                result.runs.append(
                    AgentRun("contrarian", "skipped", detail="no ideas to challenge")
                )
            else:
                run, reports = await guarded(CONTRARIAN, pack, contrarian)
                result.runs.append(run)
                result.reports += reports

        # ---------------------------------------------------------------- judge
        governor.settle(judge_hold, 0.0)  # release the room that was kept for the judge
        if not result.reports:
            result.notes.append("No analyst produced a report, so there was nothing to judge.")
            result.decision = JudgeDecision(
                as_of=as_of, market_summary="No analyst produced a report today.", trades=[],
                exits=[], rejected=[], threshold=config.judge.confidence_threshold,
            )  # fmt: skip
        else:
            scores = dict(model_scores or {})
            user = render_context(
                as_of_text=as_of.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M New York time"),
                reports=result.reports, snap=snap, config=config, weights=weights,
                model_scores=scores, positions=positions, lessons=lessons,
            )  # fmt: skip
            try:
                judged = await claude.structured(
                    purpose="judge", model=config.judge.model, effort=judge_effort,
                    max_tokens=config.judge.max_tokens, system=JUDGE_SYSTEM, user=user,
                    output=JudgeOutput, cycle_id=cycle_id,
                )  # fmt: skip
            except (BudgetExceededError, LLMError) as exc:
                if isinstance(exc, LLMConfigError):
                    raise
                result.runs.append(AgentRun("judge", "failed", detail=str(exc)))
                result.notes.append(f"The judge did not run ({exc}); no trades were proposed.")
            else:
                result.decision = decide(
                    output=judged.output, reports=result.reports, snap=snap, config=config,
                    weights=weights, model_scores=scores,
                )  # fmt: skip
                result.runs.append(
                    AgentRun("judge", "ok", len(result.decision.trades), cost_usd=judged.cost_usd)
                )
                with sessions() as session:
                    store_decision(session, cycle_id, result.decision, judged.model)
                    session.commit()
    except LLMConfigError as exc:
        result.notes.append(str(exc))
        result.cost_usd = governor.cycle_spent
        _finish(sessions, result, m.RunStatus.FAILED)
        return result

    result.cost_usd = governor.cycle_spent
    result.status = "succeeded"
    _finish(sessions, result, m.RunStatus.SUCCEEDED)
    return result
