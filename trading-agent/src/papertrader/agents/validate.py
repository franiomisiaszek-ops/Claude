"""Enforces the citation rule after Claude answers.

The spec: every claim must cite a source id from the provided context; any claim
without one is dropped. Here that is done by code, not by trusting the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from papertrader.agents.context import ContextPack, clip
from papertrader.agents.schemas import AgentOutput, AgentReport, CitedEvidence


@dataclass
class Validated:
    reports: list[AgentReport]
    dropped_claims: int = 0
    dropped_reports: int = 0
    removed: list[str] = field(default_factory=list)  # what was deleted and why, for the log


def _clean_id(raw: str) -> str:
    return raw.strip().strip("[]").strip()


def finalise(
    *,
    agent_name: str,
    sector: str | None,
    as_of: datetime,
    output: AgentOutput,
    pack: ContextPack,
    max_reports: int,
) -> Validated:
    """Turn Claude's drafts into trusted reports, deleting whatever is not backed up."""
    items = pack.items
    result = Validated([])
    seen: set[str] = set()

    for draft in output.reports:
        ticker = draft.ticker.strip().upper()
        if ticker not in pack.allowed_tickers or ticker in seen:
            result.dropped_reports += 1  # outside this agent's remit, or a repeat
            result.removed.append(f"{ticker}: report not allowed (wrong ticker or a repeat)")
            continue

        evidence: list[CitedEvidence] = []
        dropped = 0
        for claim in draft.evidence:
            item = items.get(_clean_id(claim.source_id))
            # The source must exist in what was sent, and be about this company (or the
            # market in general) rather than borrowed from another one.
            if item is None or (item.tickers and ticker not in item.tickers):
                dropped += 1
                why = (
                    "source not in the context"
                    if item is None
                    else "source is about another company"
                )
                result.removed.append(
                    f"{ticker}: claim citing '{clip(claim.source_id, 40)}' ({why})"
                )
                continue
            evidence.append(
                CitedEvidence(claim=clip(claim.claim, 400), source_id=item.source_id, url=item.url)
            )
        result.dropped_claims += dropped
        if not evidence:
            result.dropped_reports += 1  # nothing left to stand on
            result.removed.append(f"{ticker}: report left with no valid evidence")
            continue

        seen.add(ticker)
        result.reports.append(
            AgentReport(
                agent_name=agent_name, as_of=as_of, ticker=ticker, direction=draft.direction,
                confidence=draft.confidence, horizon_days=draft.horizon_days,
                thesis=clip(draft.thesis, 700), evidence=evidence,
                risks=[clip(r, 240) for r in draft.risks[:3]],
                catalysts=[clip(c, 240) for c in draft.catalysts[:3]],
                sector=sector, event_type=draft.event_type, dropped_claims=dropped,
            )
        )  # fmt: skip

    # Keep the strongest if the model returned more than it was allowed.
    result.reports.sort(key=lambda r: r.confidence, reverse=True)
    result.dropped_reports += max(len(result.reports) - max_reports, 0)
    result.reports = result.reports[:max_reports]
    return result
