"""Switch 3: a second model reads the trades and may only remove them or make them smaller.

The model's answer is checked in code: a verdict for a ticker that is not on the list is
ignored, and nothing it says can add a trade, raise a size or move a price. If the call
fails, every trade is kept as it was (the switch fails "open", never inventing a veto).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from papertrader.agents.context import clip
from papertrader.agents.schemas import AgentReport
from papertrader.agents.snapshot import Snapshot
from papertrader.b.prompts import VETO_SHARED, VETO_SPECIFIC
from papertrader.config import VetoSwitch
from papertrader.judge.schemas import RankedTrade
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError

SYSTEM = SystemPrompt(VETO_SHARED, VETO_SPECIFIC)


class Review(BaseModel):
    ticker: str
    verdict: Literal["keep", "shrink", "veto"]
    reason: str


class VetoOutput(BaseModel):
    reviews: list[Review]


@dataclass(frozen=True)
class Verdict:
    verdict: str  # keep | shrink | veto | failed
    reason: str


def render(trades: Sequence[RankedTrade], reports: Sequence[AgentReport], snap: Snapshot,
           extra: dict[str, list[str]]) -> str:  # fmt: skip
    by_id = {r.report_id: r for r in reports}
    lines = [f"AS OF: {snap.as_of:%Y-%m-%d %H:%M} UTC", ""]
    for t in trades:
        ind = snap.indicators.get(t.ticker)
        lines.append(f"## {t.ticker}: {t.action}, {t.size_pct:.1%} of the account, hold up to "
                     f"{t.horizon_days} trading days")  # fmt: skip
        lines.append(f"entry {t.entry_low:.2f}-{t.entry_high:.2f}, stop {t.stop_price:.2f}, "
                     f"target {t.target_price:.2f}, confidence {t.final_confidence:.2f}")  # fmt: skip
        if ind is not None:
            atr = f", typical daily range {ind.atr_pct:.1%}" if ind.atr_pct else ""
            moves = ", ".join(f"{label} {value:+.1%}" for label, value in
                              (("1d", ind.ret_1d), ("5d", ind.ret_5d), ("20d", ind.ret_20d))
                              if value is not None)  # fmt: skip
            lines.append(f"price {ind.last_close:.2f} ({moves}{atr})")
        upcoming = [e for e in snap.earnings if e.ticker == t.ticker and e.released_at is None]
        if upcoming:
            lines.append(f"earnings scheduled: {upcoming[0].report_date:%d %b}")
        lines.append(f"judge's case: {clip(t.rationale, 600)}")
        if t.dissent:
            lines.append(f"judge's doubts: {clip(t.dissent, 400)}")
        for label, ids in (("supporting", t.supporting), ("opposing", t.opposing)):
            for rid in ids:
                report = by_id.get(rid)
                if report is not None:
                    lines.append(f"{label} [{report.agent_name}, {report.confidence:.2f}]: "
                                 f"{clip(report.thesis, 300)}")  # fmt: skip
        lines += extra.get(t.ticker, [])
        lines.append("")
    return "\n".join(lines)


async def review(
    trades: Sequence[RankedTrade],
    reports: Sequence[AgentReport],
    snap: Snapshot,
    claude: ClaudeClient,
    cfg: VetoSwitch,
    cycle_id: int,
    extra: dict[str, list[str]] | None = None,
) -> dict[str, Verdict]:
    if not trades:
        return {}
    tickers = {t.ticker for t in trades}
    try:
        answer = await claude.structured(
            purpose="veto", model=cfg.model, effort=cfg.effort, max_tokens=cfg.max_tokens,
            system=SYSTEM, user=render(trades, reports, snap, extra or {}), output=VetoOutput,
            cycle_id=cycle_id,
        )  # fmt: skip
    except (LLMError, BudgetExceededError) as exc:
        return {
            t: Verdict("failed", f"the review did not run ({exc}); kept as it was") for t in tickers
        }
    verdicts = {t: Verdict("keep", "no verdict given") for t in tickers}
    for item in answer.output.reviews:
        ticker = item.ticker.strip().upper()
        if ticker in verdicts:
            verdicts[ticker] = Verdict(item.verdict, clip(item.reason, 400))
    return verdicts
