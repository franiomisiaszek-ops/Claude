"""Weekly lessons: the judge reads what really happened to recent calls and trades and
writes down what worked and what failed. The judge then sees the latest lessons as
context in every daily decision.

These are outcomes the system itself observed after its own decisions, so there is no
hindsight leakage in the material. Small samples are noise, and the prompt says so: a
lesson from five calls is a hunch, not a pattern.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.context import clip
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
from papertrader.llm.client import ClaudeClient, SystemPrompt
from papertrader.trading.engine import agents_verdict

LESSONS_RULES = """\
You are the judge of a team of research agents in a paper-trading experiment on US large-cap \
stocks. No real money is involved. Once a week you look back at what really happened to the \
team's recent calls and trades, and write short lessons that you will read before every \
decision next week.

You are given: accuracy per analyst, per event type and per sector over the period; a list of \
individual resolved calls (direction, confidence, horizon, the stock's return against SPY, and \
whether the call was right); the closed trades with their profit and which analysts were right; \
and last week's lessons.

Rules:
- Base every lesson on the figures given. Quote the numbers (e.g. "earnings: 14 of 20 long \
calls right"). Do not use anything you know about these companies or markets from elsewhere.
- Small samples are noise. Below about 20 calls, say the evidence is thin and keep the lesson \
tentative. Do not invent patterns to fill the lists; empty lists are fine.
- Compare with last week's lessons: keep what still holds, drop what the new figures contradict.
- Advice must be something you can apply when judging: e.g. "discount technicals' long calls \
on stocks that already gapped up", not "be careful".
- Calibration matters: if high-confidence calls were right no more often than low-confidence \
ones, say so.
"""

LESSONS_TASK = "AGENT: lessons\n\nWrite this week's lessons from the material below."


class LessonsOutput(BaseModel):
    summary: str = Field(description="Three to five sentences: the main lessons, with numbers.")
    worked: list[str] = Field(description="Patterns that worked, each one line with its numbers.")
    failed: list[str] = Field(description="Patterns that failed, each one line with its numbers.")
    advice: list[str] = Field(description="Concrete adjustments for judging next week.")


@dataclass(frozen=True)
class LessonsResult:
    status: str  # written | skipped | failed
    detail: str
    lesson_id: int | None = None
    cost_usd: float = 0.0


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def _rate(values: list[bool]) -> str:
    return (
        f"{sum(values)} of {len(values)} right ({sum(values) / len(values):.0%})"
        if values
        else "none"
    )


def gather(session: Session, config: AppConfig, start: date, end: date) -> tuple[str, int]:
    """The material for the lessons prompt, and the number of resolved directional calls."""
    sectors = dict(
        session.execute(select(m.UniverseMember.ticker, m.UniverseMember.gics_sector)).all()
    )
    rows = session.execute(
        select(m.AgentReport, m.PredictionOutcome)
        .join(m.PredictionOutcome, m.PredictionOutcome.agent_report_id == m.AgentReport.id)
        .where(m.AgentReport.as_of >= at_new_york(start, time(0)),
               m.AgentReport.as_of < at_new_york(end + timedelta(days=1), time(0)),
               m.PredictionOutcome.hit.is_not(None))
        .order_by(m.AgentReport.as_of)
    ).all()  # fmt: skip
    by_agent: dict[str, list[bool]] = defaultdict(list)
    by_event: dict[str, list[bool]] = defaultdict(list)
    by_sector: dict[str, list[bool]] = defaultdict(list)
    by_confidence: dict[str, list[bool]] = defaultdict(list)
    for report, outcome in rows:
        hit = bool(outcome.hit)
        by_agent[report.agent_name].append(hit)
        by_event[report.event_type or "unknown"].append(hit)
        by_sector[sectors.get(report.ticker) or report.sector or "unknown"].append(hit)
        band = (
            "0.75 and above"
            if report.confidence >= 0.75
            else "0.60-0.74"
            if report.confidence >= 0.6
            else "below 0.60"
        )
        by_confidence[band].append(hit)

    lines = [f"PERIOD: calls made {start:%d %b %Y} to {end:%d %b %Y}, resolved since.", "",
             f"## All directional calls: {_rate([bool(o.hit) for _r, o in rows])}"]  # fmt: skip
    for title, groups in (("By analyst", by_agent), ("By event type", by_event),
                          ("By sector", by_sector), ("By stated confidence", by_confidence)):  # fmt: skip
        lines += ["", f"## {title}"]
        lines += [
            f"- {name}: {_rate(values)}"
            for name, values in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ]

    # The biggest misses and the best calls say most; cap the list to keep the prompt small.
    ranked = sorted(rows, key=lambda ro: abs(ro[1].excess_return_pct), reverse=True)[:60]
    lines += ["", "## Individual calls (largest moves first)"]
    for report, outcome in ranked:
        lines.append(
            f"- {report.as_of.astimezone(NEW_YORK):%d %b} {report.agent_name} | {report.direction.value.upper()} "
            f"{report.ticker} ({sectors.get(report.ticker) or report.sector or '?'}) | {report.event_type or '-'} | "
            f"confidence {report.confidence:.2f} | {report.horizon_days}d | vs SPY {_pct(outcome.excess_return_pct)} | "
            f"{'RIGHT' if outcome.hit else 'WRONG'} | thesis: {clip(report.thesis, 160)}"
        )  # fmt: skip

    trades = session.scalars(
        select(m.Trade).where(m.Trade.status == m.TradeStatus.CLOSED, m.Trade.is_test.is_(False),
                              m.Trade.exited_at >= at_new_york(start, time(0)))
        .order_by(m.Trade.exited_at)
    ).all()  # fmt: skip
    lines += ["", "## Closed trades"]
    for trade in trades:
        right, wrong = agents_verdict(session, trade)
        lines.append(
            f"- {trade.ticker}: P&L {_pct(trade.pnl_pct)}, vs SPY {_pct(trade.excess_return_pct)}, exit "
            f"{trade.exit_trigger.value if trade.exit_trigger else '?'}; right: {', '.join(right) or '-'}; "
            f"wrong: {', '.join(wrong) or '-'}"
        )  # fmt: skip
    if not trades:
        lines.append("- none")

    previous = session.scalars(select(m.Lesson).order_by(m.Lesson.id.desc())).first()
    lines += ["", "## Last week's lessons", previous.text if previous else "none yet"]
    return "\n".join(lines), len(rows)


def render(output: LessonsOutput, start: date, end: date, n: int) -> str:
    lines = [f"Lessons from {n} resolved calls ({start:%d %b} to {end:%d %b}):", output.summary]
    for title, items in (
        ("Worked", output.worked),
        ("Failed", output.failed),
        ("Advice", output.advice),
    ):
        if items:
            lines += [f"{title}:"] + [f"- {item}" for item in items]
    return "\n".join(lines)


async def write_lessons(
    sessions: sessionmaker[Session], config: AppConfig, claude: ClaudeClient, now: datetime
) -> LessonsResult:
    cfg = config.learning
    end = new_york_date(now)
    start = end - timedelta(days=cfg.lessons_lookback_days)
    with sessions() as session:
        material, n = gather(session, config, start, end)
    if n < cfg.lessons_min_outcomes:
        return LessonsResult(
            "skipped", f"only {n} resolved calls (need {cfg.lessons_min_outcomes})"
        )
    model = cfg.lessons_model or config.judge.model
    result = await claude.structured(
        purpose="lessons", model=model, effort=cfg.lessons_effort, max_tokens=cfg.lessons_max_tokens,
        system=SystemPrompt(LESSONS_RULES, LESSONS_TASK), user=material, output=LessonsOutput,
    )  # fmt: skip
    text = render(result.output, start, end, n)
    with sessions() as session:
        row = m.Lesson(period_start=start, period_end=end, text=text, model=result.model, created_at=now,
                       payload=result.output.model_dump() | {"n": n, "cost_usd": result.cost_usd})  # fmt: skip
        session.add(row)
        session.commit()
        return LessonsResult("written", f"from {n} resolved calls", row.id, result.cost_usd)


def latest_lessons(session: Session, config: AppConfig, as_of: datetime) -> str | None:
    """The newest lessons written before `as_of`, unless they are too old to trust."""
    row = session.scalars(
        select(m.Lesson).where(m.Lesson.created_at <= as_of).order_by(m.Lesson.id.desc())
    ).first()
    if row is None or row.created_at < as_of - timedelta(days=config.learning.lessons_max_age_days):
        return None
    return row.text
