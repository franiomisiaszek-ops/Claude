"""The documented formula for a trade's final confidence.

    final = w_judge * judge + w_model * model + w_agree * agreement

* judge      - the judge's own probability for the trade (0-1).
* model      - the pattern model's calibrated probability (0-1). Until that model
               exists (Phase 4) it is left out and the other two weights are scaled
               up to sum to 1, so nothing is silently treated as 0 or 0.5.
* agreement  - how far the research agents back the trade (0-1), defined below.

Weights come from `judge.blend` in config.yaml (default 0.5 / 0.3 / 0.2). Only trades
with `final >= judge.confidence_threshold` (default 0.65) are executed.

Agreement, for the reports on one ticker (sector-level macro reports are left out):

    agreement = FOR / (FOR + AGAINST + 0.5 * NEUTRAL + 0.5)

where each term is the sum of (agent reliability weight x report confidence) over the
reports on that side. The constant 0.5 is a dose of built-in doubt: one agent alone at
confidence 0.8 gives 0.62, three such agents give 0.83, and any opposing report pulls
the figure down. This is where disagreement between agents is penalised.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from papertrader.agents.schemas import AgentReport
from papertrader.config import BlendWeights

DOUBT = 0.5


def agreement(reports: Iterable[AgentReport], action: str, weights: Mapping[str, float]) -> float:
    wanted = "long" if action == "buy" else "short"
    in_favour = against = neutral = 0.0
    for report in reports:
        strength = weights.get(report.agent_name, 1.0) * report.confidence
        if report.direction == wanted:
            in_favour += strength
        elif report.direction == "neutral":
            neutral += strength
        else:
            against += strength
    return in_favour / (in_favour + against + 0.5 * neutral + DOUBT)


def final_confidence(judge: float, model: float | None, agree: float, blend: BlendWeights) -> float:
    if model is None:
        total = blend.judge + blend.agreement
        if total <= 0:
            return judge
        return (blend.judge * judge + blend.agreement * agree) / total
    return blend.judge * judge + blend.model * model + blend.agreement * agree
