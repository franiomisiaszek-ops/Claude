"""System B's own daily jobs: the 08:00 signals and the Fable test."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents import prompts
from papertrader.agents.roster import build, specs
from papertrader.agents.schemas import AgentOutput
from papertrader.agents.snapshot import build_snapshot
from papertrader.agents.validate import finalise
from papertrader.b import paid
from papertrader.b.models import ShadowReport, Signal
from papertrader.b.outcomes import IdeaSample, resolve
from papertrader.b.signals import insider_clusters
from papertrader.config import AppConfig, Settings
from papertrader.db import models as m
from papertrader.ingest.timeutil import at_new_york, new_york_date
from papertrader.llm.client import ClaudeClient, LLMError, SystemPrompt
from papertrader.llm.governor import BudgetExceededError
from papertrader.research.reuse import a_daily_cycle

log = logging.getLogger(__name__)


def _store(session: Session, day: date, ticker: str, kind: str, value: float | None, summary: str,
           details: dict[str, Any], now: datetime) -> bool:  # fmt: skip
    exists = session.scalar(
        select(Signal.id).where(Signal.day == day, Signal.ticker == ticker, Signal.kind == kind)
    )
    if exists:
        return False
    session.add(Signal(day=day, ticker=ticker, kind=kind, value=value, summary=summary, details=details,
                       available_at=now))  # fmt: skip
    return True


def watch_list(engine: Engine, session: Session, now: datetime, limit: int) -> list[str]:
    """The stocks worth paying for data on: today's research (yesterday's if today's is not
    done yet), most-discussed first, and every open position."""
    from collections import Counter

    funds = {"SPY", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLK", "XLB", "XLRE", "XLU"}
    counts: Counter[str] = Counter()
    day = new_york_date(now)
    for back in range(0, 5):
        found = a_daily_cycle(engine, day - timedelta(days=back))
        if found is not None:
            counts.update(r.ticker for r in found[1] if r.ticker not in funds)
            break
    held = session.scalars(select(m.Trade.ticker).where(m.Trade.status == m.TradeStatus.OPEN)).all()
    ordered = list(dict.fromkeys([*held, *(t for t, _ in counts.most_common())]))
    return ordered[: max(limit, len(held))]


def _last_close(session: Session, ticker: str, now: datetime) -> float | None:
    value = session.scalar(
        select(m.PriceBar.close).where(m.PriceBar.ticker == ticker, m.PriceBar.timeframe == "1Day",
                                       m.PriceBar.available_at <= now)
        .order_by(m.PriceBar.ts.desc()).limit(1)
    )  # fmt: skip
    return float(value) if value is not None else None


def _month_ago_eps(session: Session, ticker: str, day: date) -> float | None:
    row = session.scalars(
        select(Signal).where(Signal.ticker == ticker, Signal.kind == "analysts",
                             Signal.day <= day - timedelta(days=25), Signal.day >= day - timedelta(days=40))
        .order_by(Signal.day.desc()).limit(1)
    ).first()  # fmt: skip
    value = (row.details or {}).get("eps_estimate") if row else None
    return float(value) if value else None


def run_signals(engine: Engine, sessions: sessionmaker[Session], config: AppConfig, settings: Settings,
                now: datetime, *, http: httpx.Client | None = None) -> dict[str, Any]:  # fmt: skip
    """08:00. Insider clusters are always recorded, even with switch 5 off, so the rule can
    be judged on what it would have done; the paid signals only when switched on."""
    cfg = config.decision
    day = new_york_date(now)
    details: dict[str, Any] = {"insider_clusters": 0, "options": 0, "analysts": 0, "problems": []}
    client = http or httpx.Client()
    try:
        with sessions() as session:
            for ticker, cluster in insider_clusters(session, now, cfg.insiders).items():
                details["insider_clusters"] += _store(
                    session, day, ticker, "insider_cluster", float(len(cluster.buyers)),
                    cluster.summary(cfg.insiders.window_days),
                    {"buyers": len(cluster.buyers), "bought_usd": round(cluster.bought_usd),
                     "sold_usd": round(cluster.sold_usd), "roles": sorted(cluster.roles)}, now,
                )  # fmt: skip
            session.commit()

            for enabled, secret, name in ((cfg.options.enabled, settings.marketdata_api_key, "MARKETDATA_API_KEY"),
                                          (cfg.analysts.enabled, settings.fmp_api_key, "FMP_API_KEY")):  # fmt: skip
                if enabled and secret is None:
                    details["problems"].append(f"switch on but {name} is not set in .env")
            if cfg.options.enabled and settings.marketdata_api_key is not None:
                md_key = settings.marketdata_api_key.get_secret_value()
                for ticker in watch_list(engine, session, now, cfg.options.max_tickers):
                    try:
                        view = paid.fetch_options(client, md_key, ticker)
                    except paid.PaidDataError as exc:
                        details["problems"].append(f"options {ticker}: {exc}")
                        break  # the same problem would repeat for every stock
                    if view.atm_iv is None and view.call_put_spread is None:
                        continue
                    details["options"] += _store(session, day, ticker, "options", view.call_put_spread,
                                                 view.summary(), view.details(), now)  # fmt: skip
                session.commit()
            if cfg.analysts.enabled and settings.fmp_api_key is not None:
                fmp_key = settings.fmp_api_key.get_secret_value()
                for ticker in watch_list(engine, session, now, cfg.analysts.max_tickers):
                    try:
                        grades = paid.fetch_analysts(client, fmp_key, ticker, day, price=_last_close(session, ticker, now),
                                                     month_ago_eps=_month_ago_eps(session, ticker, day))  # fmt: skip
                    except paid.PaidDataError as exc:
                        details["problems"].append(f"analysts {ticker}: {exc}")
                        break
                    details["analysts"] += _store(session, day, ticker, "analysts", float(grades.net_grades),
                                                  grades.summary(), grades.details(), now)  # fmt: skip
                session.commit()
    finally:
        if http is None:
            client.close()
    return details


# --------------------------------------------------------------------------- the Fable test


def todays_copy(session: Session, day: date) -> m.DecisionCycle | None:
    """B's copy of A's research of `day`: the Fable test runs on exactly those inputs."""
    cycles = session.scalars(
        select(m.DecisionCycle).where(m.DecisionCycle.kind == m.CycleKind.DAILY,
                                      m.DecisionCycle.as_of >= at_new_york(day, time(0)),
                                      m.DecisionCycle.as_of < at_new_york(day + timedelta(days=1), time(0)))
        .order_by(m.DecisionCycle.id.desc())
    ).all()  # fmt: skip
    return next((c for c in cycles if (c.notes or {}).get("copied_from_a")), None)


async def run_fable(sessions: sessionmaker[Session], config: AppConfig, claude: ClaudeClient,
                    now: datetime) -> dict[str, Any]:  # fmt: skip
    """The chosen agents again, on Claude Fable, on the same snapshot as A's real run."""
    fable = config.decision.fable_shadow
    day = new_york_date(now)
    with sessions() as session:
        done = session.scalar(select(func.count(ShadowReport.id)).where(ShadowReport.day == day))
        cycle = todays_copy(session, day)
        if cycle is None:
            return {"skipped": "B has no copy of today's research yet"}
        if done:
            return {"skipped": "already ran today", "reports": done}
        stored_from = (cycle.notes or {}).get("fresh_from")
        fresh_from = datetime.fromisoformat(stored_from) if stored_from else None
        snap = build_snapshot(session, config, cycle.as_of, fresh_from=fresh_from)
        cycle_id, as_of = cycle.id, cycle.as_of
    limit = config.agents.max_reports_per_agent
    out: dict[str, Any] = {"agents": {}, "cost_usd": 0.0}
    for spec in specs():
        if spec.name not in fable.agents:
            continue
        pack = build(spec, snap, config.agents)
        if pack is None or pack.is_empty():
            out["agents"][spec.label] = "nothing new to look at"
            continue
        try:
            result = await claude.structured(
                purpose="fable_shadow", model=fable.model, effort=fable.effort, max_tokens=fable.max_tokens,
                system=SystemPrompt(prompts.SHARED_RULES, spec.prompt), user=pack.render(limit),
                output=AgentOutput, cycle_id=cycle_id,
            )  # fmt: skip
        except BudgetExceededError as exc:
            out["agents"][spec.label] = f"stopped: {exc}"
            break
        except LLMError as exc:
            out["agents"][spec.label] = f"failed: {exc}"
            continue
        checked = finalise(agent_name=spec.name, sector=spec.sector, as_of=as_of, output=result.output,
                           pack=pack, max_reports=limit)  # fmt: skip
        share = result.cost_usd / max(len(checked.reports), 1)
        with sessions() as session:
            for report in checked.reports:
                session.add(ShadowReport(day=day, as_of=as_of, agent_name=spec.name, model=result.model,
                                         ticker=report.ticker, direction=report.direction,
                                         confidence=report.confidence, horizon_days=report.horizon_days,
                                         thesis=report.thesis, cost_usd=share))  # fmt: skip
            session.commit()
        out["agents"][spec.label] = f"{len(checked.reports)} reports"
        out["cost_usd"] += result.cost_usd
    out["cost_usd"] = round(out["cost_usd"], 4)
    return out


def score_shadow(session: Session, config: AppConfig, now: datetime) -> int:
    """Score finished Fable reports exactly like the agents' own calls (excess vs SPY)."""
    pending = session.scalars(select(ShadowReport).where(ShadowReport.resolved_at.is_(None))).all()
    pairs = [
        (row, IdeaSample(row.as_of, row.ticker, "buy" if row.direction == "long" else "sell_short",
                         row.horizon_days, row.confidence, "fable"))
        for row in pending if row.direction != "neutral"
    ]  # fmt: skip
    by_sample = {id(sample): row for row, sample in pairs}
    scored = 0
    for item in resolve(session, config, [s for _, s in pairs], now, sector_relative=False):
        row = by_sample[id(item.sample)]
        row.resolved_at, row.excess_return_pct, row.hit = now, item.excess, item.worked
        row.brier = (row.confidence - (1.0 if item.worked else 0.0)) ** 2
        scored += 1
    for row in pending:
        if row.direction == "neutral" and now - row.as_of > timedelta(days=60):
            row.resolved_at = now  # neutral calls have no right or wrong
    return scored


def _summary(hits: list[bool], briers: list[float]) -> dict[str, Any]:
    if not hits:
        return {"scored": 0}
    return {"scored": len(hits), "hit_rate": round(sum(hits) / len(hits), 3),
            "brier": round(sum(briers) / len(briers), 4)}  # fmt: skip


def fable_comparison(engine: Engine, session: Session) -> dict[str, Any]:
    """Fable against the real (Sonnet) agents on the same days, for the weekly report.

    A fair comparison: the same agents, the same days, the same inputs, scored the same way.
    """
    rows = session.scalars(select(ShadowReport).where(ShadowReport.hit.is_not(None))).all()
    if not rows:
        return {"fable": {"scored": 0}, "sonnet": {"scored": 0}}
    agents = sorted({r.agent_name for r in rows})
    days = sorted({r.day for r in rows})
    from papertrader.db.provision import a_tables

    with engine.connect() as conn:
        view = conn.execution_options(schema_translate_map=a_tables(engine))
        with Session(bind=view) as a_session:
            theirs = a_session.execute(
                select(m.AgentReport.as_of, m.PredictionOutcome.hit, m.PredictionOutcome.brier)
                .join(m.PredictionOutcome, m.PredictionOutcome.agent_report_id == m.AgentReport.id)
                .join(m.DecisionCycle, m.DecisionCycle.id == m.AgentReport.cycle_id)
                .where(m.AgentReport.agent_name.in_(agents), m.DecisionCycle.kind == m.CycleKind.DAILY,
                       m.PredictionOutcome.hit.is_not(None))
            ).all()  # fmt: skip
    wanted = set(days)
    same_days = [(hit, brier) for as_of, hit, brier in theirs if new_york_date(as_of) in wanted]
    return {
        "fable": _summary([bool(r.hit) for r in rows], [r.brier or 0.0 for r in rows])
        | {"cost_usd": round(sum(r.cost_usd for r in rows), 2)},
        "sonnet": _summary([bool(h) for h, _ in same_days], [b or 0.0 for _, b in same_days]),
        "agents": agents,
        "days": len(days),
    }
