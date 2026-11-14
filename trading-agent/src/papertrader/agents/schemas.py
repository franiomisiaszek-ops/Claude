"""What agents return. Two layers:

* `AgentOutput`  - what Claude is asked to produce (structured output).
* `AgentReport`  - what the rest of the system uses, after the program has checked
                   every citation and filled in the fields Claude is not trusted with
                   (agent name, timestamp, URLs).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict

DirectionName = Literal["long", "short", "neutral"]
EventType = Literal[
    "earnings", "guidance", "mna", "fda_regulatory", "lawsuit_legal", "product",
    "management_change", "analyst_rating", "capital_return", "insider_activity",
    "technical", "macro", "other",
]  # fmt: skip

MIN_HORIZON, MAX_HORIZON = 1, 20


def _clamp_unit(value: object) -> object:
    return min(max(value, 0.0), 1.0) if isinstance(value, int | float) else value


def _clamp_horizon(value: object) -> object:
    if isinstance(value, int | float):
        return int(min(max(value, MIN_HORIZON), MAX_HORIZON))
    return value


# Out-of-range numbers are pulled into range rather than failing the whole answer.
Confidence = Annotated[float, BeforeValidator(_clamp_unit)]
Horizon = Annotated[int, BeforeValidator(_clamp_horizon)]


class Evidence(BaseModel):
    claim: str
    source_id: str


class ReportDraft(BaseModel):
    ticker: str
    direction: DirectionName
    confidence: Confidence
    horizon_days: Horizon
    event_type: EventType
    thesis: str
    evidence: list[Evidence]
    risks: list[str]
    catalysts: list[str]


class AgentOutput(BaseModel):
    reports: list[ReportDraft]
    summary: str


class CitedEvidence(BaseModel):
    claim: str
    source_id: str
    url: str | None = None


class AgentReport(BaseModel):
    """The spec's AgentReport, plus sector / event type for later accuracy breakdowns."""

    model_config = ConfigDict(frozen=True)

    agent_name: str
    as_of: datetime
    ticker: str
    direction: DirectionName
    confidence: float
    horizon_days: int
    thesis: str
    evidence: list[CitedEvidence]
    risks: list[str]
    catalysts: list[str]
    sector: str | None = None
    event_type: str | None = None
    dropped_claims: int = 0
    report_id: int | None = None  # database id, set once stored
