"""What the judge returns (`JudgeOutput`, from Claude) and what the system keeps
(`JudgeDecision`, after the program has checked and scored every trade)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from papertrader.agents.schemas import Confidence, Horizon

Action = Literal["buy", "sell_short"]


class JudgeTrade(BaseModel):
    ticker: str
    action: Action
    confidence: Confidence
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    size_pct: Confidence
    horizon_days: Horizon
    rationale: str
    dissent: str
    supporting_reports: list[str]
    opposing_reports: list[str]


class JudgeExit(BaseModel):
    ticker: str
    reason: str


class RejectedIdea(BaseModel):
    ticker: str
    reason: str


class JudgeOutput(BaseModel):
    market_summary: str
    trades: list[JudgeTrade]
    exits: list[JudgeExit]
    rejected: list[RejectedIdea]


class RankedTrade(BaseModel):
    rank: int
    ticker: str
    action: Action
    judge_confidence: float
    model_probability: float | None
    agent_agreement: float
    final_confidence: float
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    size_pct: float
    horizon_days: int
    rationale: str
    dissent: str
    supporting: list[int]  # agent report ids
    opposing: list[int]
    status: Literal["proposed", "below_threshold", "rejected_by_risk"]
    status_reason: str | None = None

    @property
    def executable(self) -> bool:
        return self.status == "proposed"


class JudgeDecision(BaseModel):
    as_of: datetime
    market_summary: str
    trades: list[RankedTrade]
    exits: list[JudgeExit]
    rejected: list[RejectedIdea]
    threshold: float
