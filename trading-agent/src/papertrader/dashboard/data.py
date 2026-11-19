"""Queries behind the dashboard. Plain functions so they can be tested without Streamlit.

Everything here only reads. Times are shown in New York time, like the schedule.
Percentages are returned already multiplied by 100 and rounded, ready to display.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.base import Base
from papertrader.db.state import HEARTBEAT_KEY, KILL_SWITCH_KEY, TRADING_PAUSED_KEY, get_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date


@dataclass(frozen=True)
class SystemStatus:
    schema_ready: bool
    heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    table_rows: dict[str, int]

    @property
    def app_alive(self) -> bool:
        return self.heartbeat_age_seconds is not None and self.heartbeat_age_seconds < 180


def load_status(engine: Engine) -> SystemStatus:
    existing = set(inspect(engine).get_table_names())
    expected = set(Base.metadata.tables)
    if not expected <= existing:
        return SystemStatus(False, None, None, {})

    with Session(engine) as session:
        counts = {
            name: session.scalar(select(func.count()).select_from(table)) or 0
            for name, table in sorted(Base.metadata.tables.items())
        }
        heartbeat = get_state(session, HEARTBEAT_KEY)

    at = datetime.fromisoformat(heartbeat["at"]) if heartbeat else None
    age = (utcnow() - at).total_seconds() if at else None
    return SystemStatus(True, at, age, counts)


# --------------------------------------------------------------------------- helpers


def ny(moment: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return moment.astimezone(NEW_YORK).strftime(fmt) if moment else ""


def pct(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value * 100, digits)


def _month_start(now: datetime) -> datetime:
    return at_new_york(new_york_date(now).replace(day=1), time(0))


def _spend_group(purpose: str) -> str:
    if purpose in ("judge", "thesis:judge", "lessons"):
        return "judge"
    if purpose == "text_features":
        return "headline labels"
    if purpose.startswith("thesis"):
        return "thesis checks"
    return "research agents"


# --------------------------------------------------------------------------- overview


@dataclass
class Overview:
    equity: float | None = None
    day_pnl_pct: float | None = None
    total_pct: float | None = None
    spy_total_pct: float | None = None
    since: str = ""
    open_trades: int = 0
    closed_trades: int = 0
    win_rate: float | None = None
    realised_usd: float = 0.0
    month_spend: float = 0.0
    budget: float = 0.0
    paused: str | None = None
    kill_switch: bool = False
    last_research: str = "none"
    active_model: str = "none"


def overview(engine: Engine, config: AppConfig, now: datetime | None = None) -> Overview:
    now = now or utcnow()
    out = Overview(budget=config.budget.monthly_usd)
    with Session(engine) as session:
        first = session.scalars(select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts)).first()
        last = session.scalars(
            select(m.EquitySnapshot).order_by(m.EquitySnapshot.ts.desc())
        ).first()
        if last is not None:
            out.equity, out.day_pnl_pct = last.equity, pct(last.day_pnl_pct)
        if first is not None and last is not None and first.equity:
            out.total_pct = pct(last.equity / first.equity - 1)
            out.since = ny(first.ts, "%d %b %Y")
            if first.benchmark_close and last.benchmark_close:
                out.spy_total_pct = pct(last.benchmark_close / first.benchmark_close - 1)
        out.open_trades = session.scalar(select(func.count()).select_from(m.Trade).where(
            m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False))) or 0  # fmt: skip
        closed = session.scalars(select(m.Trade).where(
            m.Trade.status == m.TradeStatus.CLOSED, m.Trade.is_test.is_(False))).all()  # fmt: skip
        out.closed_trades = len(closed)
        out.realised_usd = sum(t.pnl_usd or 0.0 for t in closed)
        if closed:
            out.win_rate = pct(sum(1 for t in closed if (t.pnl_usd or 0) > 0) / len(closed), 0)
        out.month_spend = float(session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0))
                                               .where(m.ApiUsage.ts >= _month_start(now))) or 0.0)  # fmt: skip
        paused = get_state(session, TRADING_PAUSED_KEY) or {}
        out.paused = str(paused.get("reason", "")) if paused.get("paused") else None
        out.kill_switch = bool((get_state(session, KILL_SWITCH_KEY) or {}).get("active"))
        cycle = session.scalars(select(m.DecisionCycle).where(m.DecisionCycle.kind == m.CycleKind.DAILY)
                                .order_by(m.DecisionCycle.id.desc())).first()  # fmt: skip
        if cycle is not None:
            out.last_research = f"{ny(cycle.as_of)} ({cycle.status.value})"
        model = session.scalars(select(m.ModelVersion).where(m.ModelVersion.is_active)).first()
        out.active_model = model.version if model else "none (the judge decides without it)"
    return out


# --------------------------------------------------------------------------- equity


def equity_curve(engine: Engine) -> pd.DataFrame:
    """One row per trading day (closing snapshot), plus the latest snapshot of today.

    Columns: day, equity, papertrader_pct, spy_pct, drawdown_pct.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(m.EquitySnapshot.ts, m.EquitySnapshot.kind, m.EquitySnapshot.equity,
                   m.EquitySnapshot.benchmark_close).order_by(m.EquitySnapshot.ts)
        ).all()  # fmt: skip
    columns = ["day", "equity", "papertrader_pct", "spy_pct", "drawdown_pct"]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows, columns=["ts", "kind", "equity", "benchmark"])
    frame["day"] = [new_york_date(ts) for ts in frame["ts"]]
    # The last snapshot of each day: the closing one when it exists, else the latest.
    frame["is_eod"] = frame["kind"] == "eod"
    frame = frame.sort_values(["day", "is_eod", "ts"]).groupby("day").tail(1).sort_values("day")
    first_equity = frame["equity"].iloc[0]
    known = frame["benchmark"].dropna()
    first_spy = known.iloc[0] if len(known) else None
    out = pd.DataFrame({"day": pd.to_datetime(frame["day"]), "equity": frame["equity"].round(2)})
    out["papertrader_pct"] = ((frame["equity"] / first_equity - 1) * 100).round(2).to_numpy()
    out["spy_pct"] = (
        ((frame["benchmark"] / first_spy - 1) * 100).round(2).to_numpy() if first_spy else None
    )
    peak = frame["equity"].cummax()
    out["drawdown_pct"] = ((frame["equity"] / peak - 1) * 100).round(2).to_numpy()
    return out.reset_index(drop=True)[columns]


# --------------------------------------------------------------------------- trades


def trades(
    engine: Engine, *, open_only: bool, include_tests: bool = False, limit: int = 500
) -> pd.DataFrame:
    query = select(m.Trade).order_by(m.Trade.entered_at.desc()).limit(limit)
    query = query.where(
        m.Trade.status == (m.TradeStatus.OPEN if open_only else m.TradeStatus.CLOSED)
    )
    if not include_tests:
        query = query.where(m.Trade.is_test.is_(False))
    with Session(engine) as session:
        rows = session.scalars(query).all()
        verdicts = {}
        if not open_only:
            from papertrader.trading.engine import agents_verdict

            verdicts = {t.id: agents_verdict(session, t) for t in rows}
    records = []
    for t in rows:
        row: dict[str, Any] = {
            "ticker": t.ticker, "sector": t.sector or "", "side": t.direction.value, "shares": t.qty,
            "entry": t.entry_price, "opened": ny(t.entered_at), "target": t.target_price, "stop": t.stop_price,
        }  # fmt: skip
        if open_only:
            row["time limit"] = ny(t.horizon_ends_at)
        else:
            right, wrong = verdicts.get(t.id, ([], []))
            held = (t.exited_at - t.entered_at).total_seconds() / 86400 if t.exited_at else None
            row |= {"exit": t.exit_price, "closed": ny(t.exited_at), "days held": round(held, 1) if held else None,
                    "P&L $": round(t.pnl_usd or 0.0, 2), "P&L %": pct(t.pnl_pct), "vs SPY %": pct(t.excess_return_pct),
                    "exit by": t.exit_trigger.value if t.exit_trigger else "", "agents right": ", ".join(right),
                    "agents wrong": ", ".join(wrong), "reason": t.exit_reason or ""}  # fmt: skip
        if include_tests:
            row["test"] = t.is_test
        records.append(row)
    return pd.DataFrame(records)


# --------------------------------------------------------------------------- decisions


@dataclass
class DecisionView:
    id: int
    as_of: str
    kind: str
    model: str
    summary: str
    ideas: pd.DataFrame
    rejected: pd.DataFrame
    exits: pd.DataFrame
    runs: pd.DataFrame
    reports: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    threshold: float | None = None


def decision_choices(engine: Engine, limit: int = 60) -> list[tuple[int, str]]:
    """(decision id, label), newest first. Daily runs, dry runs and thesis checks."""
    with Session(engine) as session:
        rows = session.execute(
            select(m.JudgeDecision.id, m.JudgeDecision.as_of, m.DecisionCycle.kind,
                   func.count(m.TradeIdea.id))
            .join(m.DecisionCycle, m.DecisionCycle.id == m.JudgeDecision.cycle_id)
            .outerjoin(m.TradeIdea, m.TradeIdea.decision_id == m.JudgeDecision.id)
            .group_by(m.JudgeDecision.id, m.JudgeDecision.as_of, m.DecisionCycle.kind)
            .order_by(m.JudgeDecision.id.desc()).limit(limit)
        ).all()  # fmt: skip
    return [
        (i, f"{ny(as_of)} · {kind.value.replace('_', ' ')} · {n} idea(s)")
        for i, as_of, kind, n in rows
    ]


def decision_view(engine: Engine, decision_id: int) -> DecisionView | None:
    with Session(engine) as session:
        decision = session.get(m.JudgeDecision, decision_id)
        if decision is None:
            return None
        cycle = session.get(m.DecisionCycle, decision.cycle_id)
        assert cycle is not None
        ideas = pd.DataFrame([{
            "rank": i.rank, "action": i.action.value, "ticker": i.ticker, "status": i.status.value,
            "final %": pct(i.final_confidence, 0), "judge %": pct(i.judge_confidence, 0),
            "model %": pct(i.model_probability, 0), "agreement %": pct(i.agent_agreement, 0),
            "entry": f"{i.entry_low:.2f}-{i.entry_high:.2f}" if i.entry_low and i.entry_high else "market",
            "target": i.target_price, "stop": i.stop_price, "size %": pct(i.size_pct, 1),
            "days": i.horizon_days, "why": i.rationale, "against": i.dissent or "",
            "note": i.status_reason or "",
        } for i in sorted(decision.ideas, key=lambda x: x.rank)])  # fmt: skip
        payload = decision.payload or {}
        notes = cycle.notes or {}
        reports = session.scalars(select(m.AgentReport).where(m.AgentReport.cycle_id == cycle.id)
                                  .order_by(m.AgentReport.ticker, m.AgentReport.agent_name)).all()  # fmt: skip
        outcomes = dict(session.execute(
            select(m.PredictionOutcome.agent_report_id, m.PredictionOutcome.excess_return_pct)
            .where(m.PredictionOutcome.agent_report_id.in_([r.id for r in reports] or [-1]))
        ).all())  # fmt: skip
        report_rows = [{
            "id": r.id, "agent": r.agent_name + (f" ({r.sector})" if r.sector else ""), "ticker": r.ticker,
            "direction": r.direction.value, "confidence": r.confidence, "days": r.horizon_days,
            "event": r.event_type or "", "thesis": r.thesis, "evidence": r.evidence or [], "risks": r.risks or [],
            "dropped claims": r.dropped_claims,
            "vs SPY so far": pct(outcomes[r.id]) if r.id in outcomes else None,
        } for r in reports]  # fmt: skip
        return DecisionView(
            id=decision.id, as_of=ny(decision.as_of, "%a %d %b %Y %H:%M"), kind=cycle.kind.value,
            model=decision.model, summary=decision.market_summary, ideas=ideas,
            rejected=pd.DataFrame(payload.get("rejected", []), columns=["ticker", "reason"]),
            exits=pd.DataFrame(payload.get("exits", []), columns=["ticker", "reason"]),
            runs=pd.DataFrame(notes.get("agents", []), columns=["agent", "status", "reports", "dropped_claims", "detail"]),
            reports=report_rows, cost_usd=float(notes.get("cost_usd", 0.0)), threshold=payload.get("threshold"),
        )  # fmt: skip


# --------------------------------------------------------------------------- agents


@dataclass
class Leaderboard:
    as_of: date | None
    overall: pd.DataFrame
    by_sector: pd.DataFrame
    by_event: pd.DataFrame


def _stats_frame(stats: Sequence[m.AgentStat], key: str | None) -> pd.DataFrame:
    rows = [{
        "agent": s.agent_name, **({key: getattr(s, key)} if key else {}), "calls": s.n,
        "hit rate %": pct(s.hit_rate, 1), "Brier": round(s.brier, 4) if s.brier is not None else None,
        "avg vs SPY %": pct(s.avg_excess_return_pct),
    } for s in stats]  # fmt: skip
    frame = pd.DataFrame(rows)
    return (
        frame.sort_values(["hit rate %", "calls"], ascending=False).reset_index(drop=True)
        if len(frame)
        else frame
    )


def leaderboard(engine: Engine, window_days: int) -> Leaderboard:
    with Session(engine) as session:
        latest = session.scalar(select(func.max(m.AgentStat.as_of_date)))
        if latest is None:
            empty = pd.DataFrame()
            return Leaderboard(None, empty, empty, empty)
        stats = session.scalars(select(m.AgentStat).where(m.AgentStat.as_of_date == latest,
                                                          m.AgentStat.window_days == window_days)).all()  # fmt: skip
    overall = [s for s in stats if not s.sector and not s.event_type]
    sector = [s for s in stats if s.sector and not s.event_type]
    event = [s for s in stats if s.event_type and not s.sector]
    return Leaderboard(latest, _stats_frame(overall, None), _stats_frame(sector, "sector"),
                       _stats_frame(event, "event_type"))  # fmt: skip


def weights(engine: Engine) -> pd.DataFrame:
    with Session(engine) as session:
        rows = session.execute(select(m.AgentWeight.agent_name, m.AgentWeight.weight, m.AgentWeight.effective_from)
                               .order_by(m.AgentWeight.effective_from)).all()  # fmt: skip
    if not rows:
        return pd.DataFrame(columns=["week", "agent", "weight"])
    frame = pd.DataFrame(rows, columns=["agent", "weight", "from"])
    frame["week"] = pd.to_datetime([new_york_date(ts) for ts in frame["from"]])
    return frame[["week", "agent", "weight"]]


def latest_lesson(engine: Engine) -> tuple[str, str] | None:
    with Session(engine) as session:
        row = session.scalars(select(m.Lesson).order_by(m.Lesson.id.desc())).first()
    if row is None:
        return None
    return f"written {ny(row.created_at, '%a %d %b %Y')}, by {row.model or '?'}", row.text


# --------------------------------------------------------------------------- pattern model


def model_versions(engine: Engine) -> pd.DataFrame:
    with Session(engine) as session:
        rows = session.scalars(select(m.ModelVersion).order_by(m.ModelVersion.id.desc())).all()
    records = []
    for v in rows:
        metrics = v.metrics or {}
        backtest = metrics.get("backtest") or {}
        strategy = (backtest.get("strategy") or {}).get("total_return")
        spy = (backtest.get("benchmark") or {}).get("total_return")
        records.append({
            "version": v.version, "trained": ny(v.created_at, "%d %b %Y"), "active": v.is_active,
            "data from": str(v.trained_from or ""), "data to": str(v.trained_to or ""),
            "out-of-sample rows": metrics.get("rows"), "hit rate %": pct(metrics.get("hit_rate"), 1),
            "base rate %": pct(metrics.get("base_rate"), 1), "daily IC": metrics.get("ic_daily"),
            "latest window IC": metrics.get("latest_window_ic"), "Brier": metrics.get("brier"),
            "Brier naive": metrics.get("brier_naive"), "AUC": metrics.get("auc"),
            "backtest %": pct(strategy, 1), "SPY same period %": pct(spy, 1), "decision": v.notes or "",
        })  # fmt: skip
    return pd.DataFrame(records)


# --------------------------------------------------------------------------- API spend


@dataclass
class Spend:
    daily: pd.DataFrame  # day x group, dollars
    by_purpose: pd.DataFrame
    by_model: pd.DataFrame
    month_total: float
    today_total: float


def api_spend(engine: Engine, days: int = 30, now: datetime | None = None) -> Spend:
    now = now or utcnow()
    start = at_new_york(new_york_date(now) - timedelta(days=days - 1), time(0))
    with Session(engine) as session:
        rows = session.execute(select(m.ApiUsage.ts, m.ApiUsage.purpose, m.ApiUsage.model, m.ApiUsage.cost_usd,
                                      m.ApiUsage.error).where(m.ApiUsage.ts >= start)).all()  # fmt: skip
        month = float(session.scalar(select(func.coalesce(func.sum(m.ApiUsage.cost_usd), 0.0))
                                     .where(m.ApiUsage.ts >= _month_start(now))) or 0.0)  # fmt: skip
    frame = pd.DataFrame(rows, columns=["ts", "purpose", "model", "cost", "error"])
    if frame.empty:
        empty = pd.DataFrame()
        return Spend(empty, empty, empty, month, 0.0)
    frame["day"] = pd.to_datetime([new_york_date(ts) for ts in frame["ts"]])
    frame["group"] = frame["purpose"].map(_spend_group)
    daily = frame.pivot_table(
        index="day", columns="group", values="cost", aggfunc="sum", fill_value=0.0
    ).round(4)
    by_purpose = (frame.groupby("purpose").agg(calls=("cost", "size"), dollars=("cost", "sum"),
                                               errors=("error", lambda s: int(s.notna().sum())))
                  .sort_values("dollars", ascending=False).round(4).reset_index())  # fmt: skip
    by_model = (frame.groupby("model").agg(calls=("cost", "size"), dollars=("cost", "sum"))
                .sort_values("dollars", ascending=False).round(4).reset_index())  # fmt: skip
    today = float(frame.loc[frame["day"] == pd.Timestamp(new_york_date(now)), "cost"].sum())
    return Spend(daily, by_purpose, by_model, month, today)


def budget_status(engine: Engine, config: AppConfig) -> Any:
    from papertrader.llm.governor import CostGovernor

    return CostGovernor(sessionmaker(engine), config.budget).status()


# --------------------------------------------------------------------------- jobs and messages


def job_runs(engine: Engine, days: int = 3, *, include_monitor: bool = False) -> pd.DataFrame:
    query = select(m.JobRun).where(m.JobRun.scheduled_for >= utcnow() - timedelta(days=days))
    if not include_monitor:
        query = query.where(m.JobRun.job_name != "monitor")
    with Session(engine) as session:
        rows = session.scalars(query.order_by(m.JobRun.scheduled_for.desc())).all()
    return pd.DataFrame([{
        "when": ny(r.scheduled_for), "job": r.job_name, "result": r.status.value,
        "seconds": round((r.finished_at - r.started_at).total_seconds()) if r.finished_at and r.started_at else None,
        "summary": r.error or (r.details or {}).get("summary", ""),
    } for r in rows])  # fmt: skip


def notifications(engine: Engine, limit: int = 50) -> pd.DataFrame:
    with Session(engine) as session:
        rows = session.scalars(
            select(m.Notification).order_by(m.Notification.id.desc()).limit(limit)
        ).all()
    return pd.DataFrame([{
        "created": ny(n.created_at), "kind": n.kind, "status": n.status.value, "sent": ny(n.sent_at),
        "text": n.text, "problem": n.error or "",
    } for n in rows])  # fmt: skip
