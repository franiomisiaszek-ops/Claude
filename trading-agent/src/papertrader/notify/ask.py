"""X6: /ask <question> on Telegram, answered in plain words from the systems' own data.

The hub collects a short summary of what the systems know (positions with their stops
and targets, today's ideas and the judge's reasons, recent trades and how they ended,
orders waiting, B's intraday plans, C's weekly plan, recent alerts and headlines) and
asks a cheap model (Haiku) to answer from that text only. It runs on its own small
budget (`telegram.ask_monthly_usd`), recorded as purpose "ask" in system B's spending
and left out of B's budget and of the experiment's cost comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select

from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK, new_york_date
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError
from papertrader.notify.messages import money, signed_pct

ASK_PURPOSE = "ask"
MAX_CONTEXT = 16_000  # characters

ASK = SystemPrompt(
    """\
You answer the owner's questions about their paper-trading experiment (three systems, \
A, B and C, each on its own paper account; no real money). Use ONLY the data given: if it \
does not contain the answer, say so plainly and say what would show it. Write for someone \
who is not an expert, in plain words, at most 150 words. Do not give advice to buy or sell \
real securities.""",
    """\
AGENT: ask
Answer the question at the end. Quote numbers exactly as they appear in the data.""",
)


class AskOutput(BaseModel):
    answer: str


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def context(views: Mapping[Any, Any], now: datetime, system: str | None = None) -> str:
    """What the systems know, as compact text. `views`: the hub's SystemViews."""
    from papertrader.notify.reports import latest_daily_decision
    from papertrader.web import live as live_mod

    day = new_york_date(now)
    lines = [f"NOW: {now.astimezone(NEW_YORK):%a %d %b %Y %H:%M} New York"]
    for name, view in views.items():
        if system and name != system:
            continue
        lines.append(f"\n=== SYSTEM {name} ===")
        snap = live_mod.read(view.sessions)
        if snap.equity is not None and snap.at is not None:
            lines.append(f"Account {money(snap.equity)}, today {signed_pct(snap.day_change_pct, 2)} "
                         f"(live numbers from {snap.at.astimezone(NEW_YORK):%a %H:%M} New York)")  # fmt: skip
        with view.sessions() as session:
            trades = session.scalars(select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN,
                                                           m.Trade.is_test.is_(False))).all()  # fmt: skip
            for t in trades:
                pos = snap.positions.get(t.ticker)
                now_px = f", now {money(pos.price)} ({signed_pct(pos.pnl_pct)})" if pos else ""
                why = ""
                if (
                    t.trade_idea_id
                    and (idea := session.get(m.TradeIdea, t.trade_idea_id)) is not None
                ):
                    why = f"; bought because: {_clip(idea.rationale, 240)}"
                limit = (f", time limit {t.horizon_ends_at.astimezone(NEW_YORK):%a %d %b %H:%M}"
                         if t.horizon_ends_at else "")  # fmt: skip
                lines.append(f"Open: {t.direction.value} {t.qty:g} {t.ticker} at {money(t.entry_price)}{now_px}; "
                             f"target {money(t.target_price or 0)}, stop {money(t.stop_price or 0)}{limit}{why}")  # fmt: skip
            if not trades and snap.positions:
                lines += [f"Holding {p.qty:g} {p.symbol} at {money(p.avg_entry)}, now {money(p.price)} "
                          f"({signed_pct(p.pnl_pct)})" for p in snap.positions.values()]  # fmt: skip
            if not trades and not snap.positions:
                lines.append("Open positions: none (the account holds only cash)")
            decision = latest_daily_decision(session, day)
            if decision is not None:
                lines.append(f"Today's judge ({decision.as_of.astimezone(NEW_YORK):%H:%M}): market: "
                             f"{_clip(decision.market_summary, 300)}")  # fmt: skip
                for idea in sorted(decision.ideas, key=lambda i: i.rank)[:8]:
                    lines.append(f"Idea {idea.rank}: {idea.action.value} {idea.ticker}, status {idea.status.value}, "
                                 f"confidence {idea.final_confidence:.0%}: {_clip(idea.rationale, 300)}"
                                 + (f" Against: {_clip(idea.dissent, 160)}" if idea.dissent else ""))  # fmt: skip
                for item in (decision.payload or {}).get("exits", [])[:5]:
                    lines.append(
                        f"Judge says sell {item.get('ticker')}: {_clip(str(item.get('reason', '')), 200)}"
                    )
            for t in session.scalars(select(m.Trade).where(m.Trade.status == m.TradeStatus.CLOSED,
                                                           m.Trade.is_test.is_(False))
                                     .order_by(m.Trade.exited_at.desc()).limit(8)):  # fmt: skip
                lines.append(f"Closed {t.ticker} {t.exited_at.astimezone(NEW_YORK):%d %b}: "
                             f"{signed_pct(t.pnl_pct)} ({money(t.pnl_usd or 0)}), "
                             f"by {t.exit_trigger.value if t.exit_trigger else '?'}: {_clip(t.exit_reason, 150)}"
                             if t.exited_at else f"Closed {t.ticker}")  # fmt: skip
            for n in session.scalars(select(m.Notification).where(
                    m.Notification.kind == "alert", m.Notification.created_at >= now - timedelta(days=2))
                    .order_by(m.Notification.id.desc()).limit(5)):  # fmt: skip
                lines.append(
                    f"Alert {n.created_at.astimezone(NEW_YORK):%a %H:%M}: {_clip(n.text, 160)}"
                )
            if name in ("B", "C"):
                lines += _waiting(session)
            if name == "B":
                lines += _intraday(session, day)
            if name == "C":
                lines += _c_plan(session)
    held = sorted({t for v in views.values() for t in live_mod.read(v.sessions).positions})
    if held and views:
        first = next(iter(views.values()))
        with first.sessions() as session:
            for ticker in held[:12]:
                for n in session.scalars(select(m.NewsItem).join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
                                         .where(m.NewsTicker.ticker == ticker, m.NewsItem.duplicate_of_id.is_(None),
                                                m.NewsItem.available_at >= now - timedelta(days=2),
                                                m.NewsItem.available_at <= now)
                                         .order_by(m.NewsItem.published_at.desc()).limit(3)):  # fmt: skip
                    lines.append(
                        f"News {ticker} {n.published_at.astimezone(NEW_YORK):%d %b %H:%M}: {_clip(n.headline, 160)}"
                    )
    text = "\n".join(line for line in lines if line)
    return text[:MAX_CONTEXT]


def _waiting(session: Any) -> list[str]:
    from papertrader.notify.warnings import GO, WAITING, OrderWarning

    rows = session.scalars(select(OrderWarning).where(OrderWarning.status.in_((WAITING, GO)))).all()
    return [
        f"Order waiting: {r.side} {r.ticker} at {r.due_at.astimezone(NEW_YORK):%H:%M} ({r.source})"
        for r in rows
    ]


def _intraday(session: Any, day: Any) -> list[str]:
    from papertrader.b.models import IntradayPlan

    rows = session.scalars(
        select(IntradayPlan).where(IntradayPlan.day == day).order_by(IntradayPlan.id)
    ).all()
    return [f"Intraday plan {p.ticker} ({p.status}, {p.mode}): {_clip(p.why, 200)}" for p in rows]


def _c_plan(session: Any) -> list[str]:
    from papertrader.c.models import Plan

    plan = session.scalars(select(Plan).order_by(Plan.signal_day.desc()).limit(1)).first()
    if plan is None:
        return []
    targets = ", ".join(
        f"{t} {w:.0%}" for t, w in sorted(plan.targets.items(), key=lambda kv: -kv[1])
    )
    reasons = "; ".join(f"{t}: {_clip(str(r), 80)}" for t, r in list(plan.reasons.items())[:12])
    return [f"C's plan from {plan.signal_day:%d %b} ({plan.status}, state {plan.state}): {targets}",
            f"C's reasons: {reasons}"]  # fmt: skip


async def answer(question: str, data: str, claude: ClaudeClient, model: str) -> str:
    try:
        result = await claude.structured(purpose=ASK_PURPOSE, model=model, effort="low", max_tokens=1500, system=ASK,
                                         user=f"{data}\n\nQUESTION: {question}", output=AskOutput)  # fmt: skip
    except BudgetExceededError:
        return "This month's /ask budget is used up. It starts again next month (telegram.ask_monthly_usd)."
    except LLMError as exc:
        return f"/ask did not work this time ({type(exc).__name__}). Try again in a minute."
    return result.output.answer.strip() or "No answer came back."
