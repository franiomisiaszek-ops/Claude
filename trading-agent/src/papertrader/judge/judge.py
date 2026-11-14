"""The judge: weighs all agent reports and decides what is worth trading.

Claude proposes; the program disposes. After the judge answers, code here checks
that every trade is backed by real reports, obeys the risk limits it can check at
this stage, has sane price levels, and then computes the final confidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.agents import prompts
from papertrader.agents.context import clip
from papertrader.agents.roster import price_item
from papertrader.agents.schemas import AgentReport
from papertrader.agents.snapshot import SECTOR_ETF, Snapshot
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.judge.blend import agreement, final_confidence
from papertrader.judge.schemas import JudgeDecision, JudgeOutput, JudgeTrade, RankedTrade
from papertrader.llm.client import SystemPrompt

JUDGE_SYSTEM = SystemPrompt(prompts.JUDGE_SHARED, prompts.JUDGE_SPECIFIC)
# A proposed entry further than this from the last close is treated as a made-up price.
MAX_ENTRY_DISTANCE = 0.05
_FUNDS = frozenset(SECTOR_ETF.values())


def agent_weights(session: Session, as_of: datetime) -> dict[str, float]:
    """Latest reliability weight per agent. Agents without a track record count as 1.0."""
    rows = session.execute(
        select(m.AgentWeight.agent_name, m.AgentWeight.weight)
        .where(m.AgentWeight.effective_from <= as_of)
        .order_by(m.AgentWeight.effective_from)
    )
    return dict(rows.all())  # later rows overwrite earlier ones


def render_context(
    *,
    as_of_text: str,
    reports: Sequence[AgentReport],
    snap: Snapshot,
    config: AppConfig,
    weights: Mapping[str, float],
    model_scores: Mapping[str, float],
    positions: Sequence[str],
    lessons: str | None,
) -> str:
    risk = config.risk
    lines = [
        f"AS OF: {as_of_text}",
        "",
        "## Limits",
        f"- at most {risk.max_position_pct:.0%} of the account in one position",
        f"- at most {risk.max_open_positions} open positions, {risk.max_sector_pct:.0%} per sector",
        f"- shorts are {'ALLOWED' if risk.allow_shorts else 'NOT allowed: buy ideas only'}",
        f"- at most {config.judge.max_trades_per_day} new trades today",
        "",
        "## Analyst reliability (1.0 = no track record yet)",
    ]
    names = sorted({r.agent_name for r in reports})
    lines += [f"- {name}: {weights.get(name, 1.0):.2f}" for name in names]

    lines += ["", "## Open positions"]
    lines += [f"- {p}" for p in positions] or ["- none"]

    stock_reports = [r for r in reports if r.ticker not in _FUNDS and r.ticker != snap.benchmark]
    sector_reports = [r for r in reports if r not in stock_reports]

    def describe(report: AgentReport) -> str:
        who = report.agent_name + (f" ({report.sector})" if report.sector else "")
        claims = "; ".join(f"{clip(e.claim, 220)} <{e.source_id}>" for e in report.evidence)
        text = (
            f"[report:{report.report_id}] {who} | {report.direction.upper()} {report.ticker} | "
            f"confidence {report.confidence:.2f} | {report.horizon_days} days | "
            f"{report.event_type} | thesis: {report.thesis} | evidence: {claims}"
        )
        if report.risks:
            text += " | risks: " + "; ".join(report.risks)
        return text

    lines += ["", "## Sector views (macro analyst)"]
    lines += [describe(r) for r in sector_reports] or ["- none"]

    lines += ["", "## Stock reports, grouped by ticker"]
    tickers = sorted({r.ticker for r in stock_reports})
    for ticker in tickers:
        lines.append(f"\n### {ticker}")
        price = price_item(snap, ticker)
        if price is not None:
            lines.append(f"price: {price.text}")
        if ticker in model_scores:
            lines.append(f"pattern model probability of beating SPY: {model_scores[ticker]:.2f}")
        lines += [describe(r) for r in stock_reports if r.ticker == ticker]
    if not tickers:
        lines.append("- none")

    if not model_scores:
        lines += ["", "NOTE: the pattern model is not available yet; there are no model scores."]
    if lessons:
        lines += ["", "## Lessons from past outcomes", lessons]
    return "\n".join(lines)


def _ids(raw: Sequence[str], valid: Mapping[int, AgentReport], ticker: str) -> list[int]:
    found = []
    for text in raw:
        digits = "".join(ch for ch in text.rsplit(":", 1)[-1] if ch.isdigit())
        if digits and int(digits) in valid and valid[int(digits)].ticker == ticker:
            found.append(int(digits))
    return list(dict.fromkeys(found))


def _price_problem(trade: JudgeTrade, last_close: float) -> str | None:
    low, high = sorted((trade.entry_low, trade.entry_high))
    if min(low, trade.target_price, trade.stop_price) <= 0:
        return "a price level is zero or negative"
    if (
        abs(low / last_close - 1) > MAX_ENTRY_DISTANCE
        or abs(high / last_close - 1) > MAX_ENTRY_DISTANCE
    ):
        return f"entry zone {low:.2f}-{high:.2f} is far from the last close {last_close:.2f}"
    if trade.action == "buy" and not trade.stop_price < low <= high < trade.target_price:
        return "for a buy the stop must be below the entry and the target above it"
    if trade.action == "sell_short" and not trade.target_price < low <= high < trade.stop_price:
        return "for a short the target must be below the entry and the stop above it"
    return None


def decide(
    *,
    output: JudgeOutput,
    reports: Sequence[AgentReport],
    snap: Snapshot,
    config: AppConfig,
    weights: Mapping[str, float],
    model_scores: Mapping[str, float],
) -> JudgeDecision:
    """Check, score and rank the judge's trades."""
    by_id = {r.report_id: r for r in reports if r.report_id is not None}
    threshold = config.judge.confidence_threshold
    covered = set(snap.covered)
    wanted_direction = {"buy": "long", "sell_short": "short"}
    scored: list[RankedTrade] = []
    seen: set[str] = set()

    for trade in output.trades:
        ticker = trade.ticker.strip().upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        ticker_reports = [r for r in reports if r.ticker == ticker]
        supporting = [
            i for i in _ids(trade.supporting_reports, by_id, ticker)
            if by_id[i].direction == wanted_direction[trade.action]
        ]  # fmt: skip
        opposing = _ids(trade.opposing_reports, by_id, ticker)
        agree = agreement(ticker_reports, trade.action, weights)
        model = model_scores.get(ticker)
        final = final_confidence(trade.confidence, model, agree, config.judge.blend)
        low, high = sorted((trade.entry_low, trade.entry_high))

        problem: str | None = None
        if ticker not in covered:
            problem = "not a stock in the universe with current prices"
        elif trade.action == "sell_short" and not config.risk.allow_shorts:
            problem = "short selling is disabled"
        elif not supporting:
            problem = "no valid supporting report from an analyst"
        else:
            problem = _price_problem(trade, snap.indicators[ticker].last_close)

        status: Literal["proposed", "below_threshold", "rejected_by_risk"]
        reason: str | None
        if problem is not None:
            status, reason = "rejected_by_risk", problem
        elif final < threshold:
            status = "below_threshold"
            reason = f"final confidence {final:.2f} is below {threshold:.2f}"
        else:
            status, reason = "proposed", None

        scored.append(
            RankedTrade(
                rank=0, ticker=ticker, action=trade.action, judge_confidence=trade.confidence,
                model_probability=model, agent_agreement=agree, final_confidence=final,
                entry_low=low, entry_high=high, target_price=trade.target_price,
                stop_price=trade.stop_price,
                size_pct=min(trade.size_pct, config.risk.max_position_pct),
                horizon_days=trade.horizon_days, rationale=clip(trade.rationale, 700),
                dissent=clip(trade.dissent, 500), supporting=supporting, opposing=opposing,
                status=status, status_reason=reason,
            )
        )  # fmt: skip

    scored.sort(key=lambda t: (t.status != "proposed", -t.final_confidence))
    limit = config.judge.max_trades_per_day
    ranked = []
    for index, entry in enumerate(scored, start=1):
        update: dict[str, object] = {"rank": index}
        if entry.status == "proposed" and index > limit:
            update["status"] = "below_threshold"
            update["status_reason"] = f"ranked below today's maximum of {limit} trades"
        ranked.append(entry.model_copy(update=update))

    return JudgeDecision(
        as_of=snap.as_of, market_summary=clip(output.market_summary, 1500), trades=ranked,
        exits=output.exits, rejected=output.rejected, threshold=threshold,
    )  # fmt: skip


def store_decision(
    session: Session, cycle_id: int, decision: JudgeDecision, model: str
) -> m.JudgeDecision:
    """Save the decision with links from each trade idea to the reports behind it."""
    row = m.JudgeDecision(
        cycle_id=cycle_id, as_of=decision.as_of, market_summary=decision.market_summary,
        model=model, payload=decision.model_dump(mode="json"),
    )  # fmt: skip
    for trade in decision.trades:
        idea = m.TradeIdea(
            rank=trade.rank, ticker=trade.ticker,
            action=m.TradeAction.BUY if trade.action == "buy" else m.TradeAction.SELL_SHORT,
            judge_confidence=trade.judge_confidence, model_probability=trade.model_probability,
            agent_agreement=trade.agent_agreement, final_confidence=trade.final_confidence,
            entry_low=trade.entry_low, entry_high=trade.entry_high,
            target_price=trade.target_price, stop_price=trade.stop_price,
            size_pct=trade.size_pct, horizon_days=trade.horizon_days, rationale=trade.rationale,
            dissent=trade.dissent or None, status=m.IdeaStatus(trade.status),
            status_reason=trade.status_reason,
        )  # fmt: skip
        idea.report_links = [
            m.TradeIdeaReport(agent_report_id=i, stance=m.Stance.SUPPORT) for i in trade.supporting
        ] + [
            m.TradeIdeaReport(agent_report_id=i, stance=m.Stance.OPPOSE)
            for i in trade.opposing
            if i not in trade.supporting
        ]
        row.ideas.append(idea)
    session.add(row)
    return row
