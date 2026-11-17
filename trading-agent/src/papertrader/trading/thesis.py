"""Thesis break: fresh bad news on a stock we hold triggers a re-check.

1. Detection (no API cost): news about a held stock, published since the position was
   opened or last reviewed, with a clearly negative word-list score.
2. Review (two Claude calls, budget-governed): the news analyst writes a fresh report on
   the new items; then the judge sees the original thesis, the analysts' reports behind
   it, the position and the new report, and decides whether to sell now.
3. Action: only a confident "sell" exits the position, with a market order.

Each position is reviewed at most once per cooldown period, so a stream of headlines
cannot run up the API bill.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from papertrader.agents import prompts
from papertrader.agents.context import ContextItem, ContextPack, clip, stamp
from papertrader.agents.schemas import AgentOutput, Confidence
from papertrader.agents.validate import finalise
from papertrader.config import AppConfig, Settings
from papertrader.db import models as m
from papertrader.db.state import get_state, set_state
from papertrader.db.types import utcnow
from papertrader.llm.client import BudgetExceededError, ClaudeClient, LLMError, SystemPrompt
from papertrader.model.text import lexicon_sentiment
from papertrader.research.cycle import _store_reports
from papertrader.trading.engine import TradingEngine

log = logging.getLogger(__name__)

STATE_PREFIX = "thesis.reviewed."


class ThesisVerdict(BaseModel):
    exit: bool
    confidence: Confidence
    reason: str


@dataclass
class Candidate:
    trade_id: int
    ticker: str
    news_ids: list[int] = field(default_factory=list)


@dataclass
class ReviewResult:
    ticker: str
    exited: bool
    detail: str


def find_candidates(session: Session, config: AppConfig, now: datetime) -> list[Candidate]:
    cooldown = timedelta(hours=config.trading.thesis_review_cooldown_hours)
    threshold = config.trading.thesis_break_sentiment
    found = []
    trades = session.scalars(
        select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN, m.Trade.exit_trigger.is_(None))
    ).all()
    for trade in trades:
        state = get_state(session, f"{STATE_PREFIX}{trade.id}") or {}
        last = datetime.fromisoformat(state["at"]) if state.get("at") else None
        if last is not None and now - last < cooldown:
            continue
        since = max(trade.entered_at, last) if last else trade.entered_at
        items = session.scalars(
            select(m.NewsItem)
            .join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
            .where(m.NewsTicker.ticker == trade.ticker, m.NewsItem.available_at > since,
                   m.NewsItem.available_at <= now, m.NewsItem.duplicate_of_id.is_(None))
            .options(selectinload(m.NewsItem.tickers))
            .order_by(m.NewsItem.available_at)
        ).all()  # fmt: skip
        bad = [
            n for n in items if lexicon_sentiment(f"{n.headline}. {n.summary or ''}") <= threshold
        ]
        if bad:
            found.append(Candidate(trade.id, trade.ticker, [n.id for n in bad]))
    return found


def _context(
    trade: m.Trade, news: list[m.NewsItem], price: float | None, now: datetime
) -> ContextPack:
    pack = ContextPack(now, frozenset({trade.ticker}))
    move = (
        f", now {price:.2f} ({price / trade.entry_price - 1:+.1%})"
        if price and trade.entry_price
        else ""
    )
    pack.add("Position", [ContextItem(
        f"pos:{trade.ticker}",
        f"{trade.ticker} held since {stamp(trade.entered_at)} | bought at {trade.entry_price:.2f}{move} | "
        f"stop {trade.stop_price} | target {trade.target_price}",
        now, tickers=frozenset({trade.ticker}),
    )])  # fmt: skip
    pack.add("New news", [
        ContextItem(f"news:{n.id}", f"{stamp(n.available_at)} | {clip(n.headline, 200)}"
                    + (f" - {clip(n.summary, 300)}" if n.summary else ""),
                    n.available_at, n.url, frozenset(t.ticker for t in n.tickers))
        for n in news
    ])  # fmt: skip
    return pack


async def review(
    candidate: Candidate,
    *,
    engine: TradingEngine,
    claude: ClaudeClient,
    config: AppConfig,
    now: datetime | None = None,
) -> ReviewResult:
    now = now or utcnow()
    sessions: sessionmaker[Session] = engine.sessions
    price = engine.broker.latest_prices([candidate.ticker]).get(candidate.ticker)
    with sessions() as session:
        trade = session.get(m.Trade, candidate.trade_id)
        assert trade is not None
        news = list(
            session.scalars(
                select(m.NewsItem)
                .where(m.NewsItem.id.in_(candidate.news_ids))
                .options(selectinload(m.NewsItem.tickers))
                .order_by(m.NewsItem.available_at)
            )
        )
        pack = _context(trade, news, price, now)
        idea = session.get(m.TradeIdea, trade.trade_idea_id) if trade.trade_idea_id else None
        original = idea.rationale if idea else "no recorded rationale (a manual or test trade)"
        backers = session.scalars(
            select(m.AgentReport)
            .join(m.TradeIdeaReport, m.TradeIdeaReport.agent_report_id == m.AgentReport.id)
            .where(m.TradeIdeaReport.trade_idea_id == trade.trade_idea_id,
                   m.TradeIdeaReport.stance == m.Stance.SUPPORT)
        ).all() if trade.trade_idea_id else []  # fmt: skip
        backing = [f"{r.agent_name}: {r.thesis}" for r in backers]
        cycle = m.DecisionCycle(kind=m.CycleKind.THESIS_BREAK, as_of=now, status=m.RunStatus.RUNNING,
                                notes={"trade_id": trade.id, "ticker": trade.ticker})  # fmt: skip
        session.add(cycle)
        set_state(session, f"{STATE_PREFIX}{trade.id}", {"at": now.isoformat()})
        session.commit()
        cycle_id = cycle.id

    notes: dict[str, object] = {"trade_id": candidate.trade_id, "ticker": candidate.ticker}
    try:
        agent = await claude.structured(
            purpose="thesis:news_event", model=config.agents.for_agent("news_event").model,
            effort=config.agents.for_agent("news_event").effort,
            max_tokens=config.agents.for_agent("news_event").max_tokens,
            system=SystemPrompt(prompts.SHARED_RULES, prompts.NEWS_EVENT), user=pack.render(1),
            output=AgentOutput, cycle_id=cycle_id,
        )  # fmt: skip
        checked = finalise(agent_name="news_event", sector=None, as_of=now, output=agent.output, pack=pack,
                           max_reports=1)  # fmt: skip
        stored = _store_reports(sessions, cycle_id, agent.model, checked.reports)
        fresh = (f"{r.direction.upper()} confidence {r.confidence:.2f}: {r.thesis}" for r in stored)
        user = "\n".join([
            f"AS OF: {now:%Y-%m-%d %H:%M} UTC",
            f"\n## Why the position was opened\n{original}",
            "\n## Analyst reports behind it", *(backing or ["- none recorded"]),
            "\n## The position and the new news", pack.render(1),
            "\n## News analyst's fresh view", *(list(fresh) or ["- no report (no usable view on the news)"]),
        ])  # fmt: skip
        verdict = await claude.structured(
            purpose="thesis:judge", model=config.judge.model, effort=config.judge.effort,
            max_tokens=config.judge.max_tokens, system=SystemPrompt(prompts.THESIS_SHARED, prompts.THESIS_SPECIFIC),
            user=user, output=ThesisVerdict, cycle_id=cycle_id,
        )  # fmt: skip
    except (BudgetExceededError, LLMError) as exc:
        notes["error"] = str(exc)
        _finish(sessions, cycle_id, m.RunStatus.FAILED, notes)
        return ReviewResult(candidate.ticker, False, f"review not possible: {exc}")

    decision = verdict.output
    notes |= {"exit": decision.exit, "confidence": decision.confidence, "reason": decision.reason}
    sell = decision.exit and decision.confidence >= config.trading.thesis_exit_confidence
    detail = f"{'sell' if sell else 'keep'} ({decision.confidence:.0%}): {decision.reason}"
    if sell:
        detail += f" -> {engine.exit_trade(candidate.trade_id, m.ExitTrigger.THESIS_BREAK, decision.reason)}"
    else:
        with sessions() as session:
            session.add(m.ExitSignal(trade_id=candidate.trade_id, ticker=candidate.ticker,
                                     trigger=m.ExitTrigger.THESIS_BREAK, reason=f"kept: {decision.reason}"[:2000],
                                     acted_on=False))  # fmt: skip
            session.commit()
    _finish(sessions, cycle_id, m.RunStatus.SUCCEEDED, notes)
    return ReviewResult(candidate.ticker, sell, detail)


def _finish(
    sessions: sessionmaker[Session], cycle_id: int, status: m.RunStatus, notes: dict[str, object]
) -> None:
    with sessions() as session:
        cycle = session.get(m.DecisionCycle, cycle_id)
        if cycle is not None:
            cycle.status, cycle.finished_at, cycle.notes = status, utcnow(), notes
            session.commit()


async def check_held_positions(
    *,
    config: AppConfig,
    settings: Settings,
    sessions: sessionmaker[Session],
    engine: TradingEngine,
    claude: Callable[[], ClaudeClient | None],
    now: datetime,
) -> list[str]:
    """Fetch fresh news for held stocks, then review any with bad news (needs Claude).

    Shared by `trade monitor` and the scheduled monitor. Returns lines for the log.
    """
    from papertrader.ingest.context import open_context
    from papertrader.ingest.news import dedupe_news, ingest_alpaca_news

    with sessions() as session:
        held = sorted(
            set(session.scalars(select(m.Trade.ticker).where(m.Trade.status == m.TradeStatus.OPEN)))
        )
    if not held:
        return []
    notes: list[str] = []
    async with open_context(config, settings, sessions) as ctx:
        if ctx.alpaca is not None:
            try:  # a news outage must never stop the monitor
                await ingest_alpaca_news(ctx, held, start=now - timedelta(days=3))
                with sessions() as session:
                    dedupe_news(session, since=now - timedelta(days=3),
                                window_hours=config.ingest.news_dedupe_window_hours)  # fmt: skip
                    session.commit()
            except Exception as exc:
                notes.append(
                    f"news refresh failed ({type(exc).__name__}); checked stored news only"
                )
    with sessions() as session:
        candidates = find_candidates(session, config, now)
    if not candidates:
        return notes
    client = claude()
    if client is None:
        notes.append("bad news on " + ", ".join(c.ticker for c in candidates)
                     + " but ANTHROPIC_API_KEY is not set, so it was not reviewed")  # fmt: skip
        return notes
    for candidate in candidates:
        result = await review(candidate, engine=engine, claude=client, config=config, now=now)
        notes.append(f"thesis check {result.ticker}: {result.detail}")
    return notes
