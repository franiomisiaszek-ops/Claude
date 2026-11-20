"""The monthly switch review for system B (docs/PREREGISTRATION.md).

For every judged idea, B's decision log says what A's rules would have done and what B
did. Where a switch removed a trade A would have placed, or added one A would not have,
the idea is scored at 5 days against its sector fund. A switch whose removed trades would
have made money, or whose added trades lost money, over at least 20 changed ideas, is
recommended off. (Size changes are listed but not scored: their effect is in the money
comparison. Agent pruning changes the reports, not the decisions, so it is not here.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from papertrader.b.models import DecisionLog
from papertrader.b.outcomes import IdeaSample, resolve
from papertrader.config import AppConfig
from papertrader.ingest.timeutil import at_new_york

MIN_CHANGED = 20

# Which switch made a trade go away, read from B's reason (first match wins).
REASONS = (
    ("abstained", "1 confidence (abstain)"),
    ("confidence", "1 confidence (calibration / bar)"),
    ("200-day average", "2 risk (SPY filter)"),
    ("vetoed by the review", "3 review"),
    ("does not clear", "7 cost band"),
    ("meta-label", "10 meta-labelling"),
    ("past cases lowered", "X2c lessons"),
    ("ranked below today's maximum", "daily maximum"),
)


def switch_for(reason: str | None) -> str:
    text = (reason or "").lower()
    return next((name for key, name in REASONS if key.lower() in text), "other")


@dataclass
class Line:
    switch: str
    removed: list[float] = field(
        default_factory=list
    )  # 5-day gain the removed trades would have had
    added: list[float] = field(default_factory=list)  # 5-day gain of the trades it added
    resized: int = 0

    @property
    def advice(self) -> str:
        n = len(self.removed) + len(self.added)
        if n < MIN_CHANGED:
            return f"too few changes to judge ({n} of {MIN_CHANGED})"
        if self.removed and sum(self.removed) / len(self.removed) > 0:
            return "recommend OFF: the trades it removed would have made money"
        if self.added and sum(self.added) / len(self.added) < 0:
            return "recommend OFF: the trades it added lost money"
        return "keep: its changes helped"


def review(b_engine: Engine, config: AppConfig, start: date, now: datetime) -> list[Line]:
    with Session(b_engine) as session:
        rows = session.scalars(
            select(DecisionLog).where(DecisionLog.as_of >= at_new_york(start, time(0)))
        ).all()
        changed = []
        lines: dict[str, Line] = {}
        for row in rows:
            a_on, b_on = row.a_status == "proposed", row.b_status == "proposed"
            if a_on and not b_on:
                changed.append((row, switch_for(row.b_reason), "removed"))
            elif b_on and not a_on:
                changed.append((row, "1 confidence (calibration / bar)", "added"))
            elif a_on and b_on and abs(row.a_size - row.b_size) > 1e-9:
                lines.setdefault(
                    "sizing (2 risk, 3 review)", Line("sizing (2 risk, 3 review)")
                ).resized += 1
        pairs = [(row, name, kind, IdeaSample(row.as_of, row.ticker, row.action, 5, row.raw_confidence, "B"))
                 for row, name, kind in changed]  # fmt: skip
        by_sample = {id(s): (row, name, kind) for row, name, kind, s in pairs}
        for result in resolve(session, config, [s for *_, s in pairs], now, sector_relative=True):
            row, name, kind = by_sample[id(result.sample)]
            gain = result.excess if row.action == "buy" else -result.excess
            line = lines.setdefault(name, Line(name))
            (line.removed if kind == "removed" else line.added).append(gain)
    return sorted(lines.values(), key=lambda line: line.switch)


def review_text(lines: list[Line]) -> str:
    if not lines:
        return (
            "SWITCH REVIEW (system B): no decision changed by B's switches has a 5-day result yet."
        )
    out = [
        "SWITCH REVIEW (system B, monthly): ideas each switch changed, scored at 5 days vs the sector"
    ]
    for line in lines:
        parts = []
        if line.removed:
            parts.append(
                f"removed {len(line.removed)} (they would have made {sum(line.removed) / len(line.removed):+.2%} on average)"
            )
        if line.added:
            parts.append(
                f"added {len(line.added)} (they made {sum(line.added) / len(line.added):+.2%} on average)"
            )
        if line.resized:
            parts.append(f"resized {line.resized}")
        advice = (
            line.advice
            if (line.removed or line.added)
            else "sizes only: judged in the money comparison"
        )
        out.append(f"- {line.switch}: {', '.join(parts)} -> {advice}")
    return "\n".join(out)
