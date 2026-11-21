"""Claude's only two roles in system C; everything else is fixed rules.

Weekly review (Sonnet): before Monday's orders, it reads the planned buys with the
reasons the rules gave, the latest macro readings and recent headlines, and may answer
keep, reduce (buy only half the increase) or skip (no increase) for each BUY. It cannot
add a fund, raise a weight or stop a sale: it can only lower risk. Its effect is measured
against a "shadow C" that ignores it (the plan keeps both target portfolios).

Daily watch (Haiku): each morning it reads the night's headlines and the VIX, and sends
an alert when something could hurt C's holdings badly. It never trades.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.agents.context import clip
from papertrader.config import CVeto
from papertrader.db import models as m
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError

REVIEW = SystemPrompt(
    """\
You review the weekly plan of a simple, rules-based ETF portfolio (paper trading) the \
evening before its orders go in. The rules buy funds that have been rising for months and \
size them by their volatility. You see the planned BUYS with the rules' reasons, recent \
macro readings and headlines.

For each buy answer keep, reduce (buy only half of the increase) or skip (no increase \
this week), with one concrete reason. Reduce or skip only for a specific, scheduled or \
already-visible risk the rules cannot see: for example a central-bank decision or a key \
data release in the next days that the fund is very sensitive to, a fund-specific \
problem (closure, a big change in what it holds), or a market already in turmoil. Trend \
following works by staying with trends; "it has risen a lot" is not a reason. Most \
weeks, keep everything.""",
    """\
AGENT: c_review
Answer once for every fund listed, using its ticker exactly as written.""",
)

WATCH = SystemPrompt(
    """\
You watch the news each morning for a rules-based ETF portfolio (paper trading). Given \
its holdings, the latest headlines and the VIX, say whether anything could hurt these \
holdings badly in the next days: severity none, low or high, and one plain sentence. \
"High" only for something serious and specific (a crash under way, a default, a war \
escalating, an emergency central-bank move). Most days are "none".""",
    """\
AGENT: c_watch
One sentence. No advice to trade.""",
)


class Verdict(BaseModel):
    ticker: str
    verdict: Literal["keep", "reduce", "skip"]
    reason: str


class ReviewOutput(BaseModel):
    verdicts: list[Verdict]


class WatchOutput(BaseModel):
    severity: Literal["none", "low", "high"]
    note: str


@dataclass(frozen=True)
class Reviewed:
    targets: dict[str, float]
    verdicts: dict[str, dict[str, str]]
    note: str = ""


def macro_lines(session: Session) -> list[str]:
    names = {"VIXCLS": "VIX", "DGS10": "10-year Treasury yield", "T10Y2Y": "yield curve 10y-2y",
             "BAMLH0A0HYM2": "high-yield credit spread", "DFF": "Fed funds rate"}  # fmt: skip
    lines = []
    for series_id, label in names.items():
        row = session.scalars(select(m.MacroObservation).where(m.MacroObservation.series_id == series_id)
                              .order_by(m.MacroObservation.observation_date.desc()).limit(1)).first()  # fmt: skip
        if row is not None and row.value is not None:
            lines.append(f"{label}: {row.value:g} ({row.observation_date:%d %b})")
    return lines


def headlines(session: Session, since: datetime, limit: int = 15) -> list[str]:
    rows = session.scalars(select(m.NewsItem).where(m.NewsItem.available_at >= since,
                                                    m.NewsItem.duplicate_of_id.is_(None))
                           .order_by(m.NewsItem.available_at.desc()).limit(limit)).all()  # fmt: skip
    return [f"{n.published_at:%d %b %H:%M} UTC: {clip(n.headline, 160)}" for n in rows]


async def review(session: Session, claude: ClaudeClient, cfg: CVeto, now: datetime, *,
                 targets: Mapping[str, float], current: Mapping[str, float],
                 reasons: Mapping[str, str]) -> Reviewed:  # fmt: skip
    buys = {t: w for t, w in targets.items() if w > current.get(t, 0.0) + 1e-6}
    if not buys:
        return Reviewed(dict(targets), {}, "no buys to review")
    lines = [f"AS OF: {now:%Y-%m-%d %H:%M} UTC", "", "Planned buys (weight now -> target):"]
    lines += [
        f"- {t}: {current.get(t, 0.0):.0%} -> {w:.0%}; rules: {reasons.get(t, '')}"
        for t, w in buys.items()
    ]
    lines += ["", "Macro:", *(macro_lines(session) or ["none stored"])]
    lines += [
        "",
        "Recent headlines:",
        *(headlines(session, now - timedelta(days=3)) or ["none stored"]),
    ]
    try:
        answer = await claude.structured(purpose="c_review", model=cfg.model, effort=cfg.effort, max_tokens=8000,
                                         system=REVIEW, user="\n".join(lines), output=ReviewOutput)  # fmt: skip
    except (LLMError, BudgetExceededError) as exc:
        return Reviewed(dict(targets), {}, f"the review did not run ({exc}); the plan is unchanged")
    out, verdicts = dict(targets), {}
    for v in answer.output.verdicts:
        t = v.ticker.strip().upper()
        if t not in buys:
            continue  # only buys can be touched
        verdicts[t] = {"verdict": v.verdict, "reason": clip(v.reason, 300)}
        now_w = current.get(t, 0.0)
        if v.verdict == "reduce":
            out[t] = round(now_w + (buys[t] - now_w) / 2, 4)
        elif v.verdict == "skip":
            out[t] = round(now_w, 4)
            if out[t] <= 0:
                out.pop(t)
    return Reviewed(out, verdicts)


async def watch(
    session: Session, claude: ClaudeClient, cfg: CVeto, now: datetime, holdings: Sequence[str]
) -> WatchOutput | None:
    news = headlines(session, now - timedelta(hours=24), limit=25)
    lines = [f"Holdings: {', '.join(holdings) or 'none'}", "", "Macro:", *(macro_lines(session) or ["none stored"]),
             "", "Headlines (last 24 hours):", *(news or ["none"])]  # fmt: skip
    try:
        answer = await claude.structured(purpose="c_watch", model=cfg.watch_model, effort="low", max_tokens=2000,
                                         system=WATCH, user="\n".join(lines), output=WatchOutput)  # fmt: skip
    except (LLMError, BudgetExceededError):
        return None
    return answer.output
