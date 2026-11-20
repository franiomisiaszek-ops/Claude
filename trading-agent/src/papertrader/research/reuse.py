"""System B reuses system A's research instead of paying for the same agent calls twice.

At the judge's time B copies the agent reports of A's daily run into its own tables,
as a cycle waiting for a judge, and then runs its own judge on them. A and B therefore
start every day from the same reports and differ only in what B does with them.

Reuse is only fair while B would have asked the agents exactly what A asked: same
models, effort, limits and prompts. `agents_fingerprint` captures those; it must equal
the fingerprint A was frozen with, or B refuses to reuse.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, time, timedelta
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.provision import a_tables
from papertrader.ingest.timeutil import at_new_york
from papertrader.research.cycle import AGENTS_DONE

PROMPTS_FILE = Path(__file__).resolve().parents[1] / "agents" / "prompts.py"

# A's agent settings and prompts at the start of the experiment (see test_reuse.py).
FROZEN_A_FINGERPRINT = "06212f954b36fcce"


class ReuseRefused(RuntimeError):
    pass


def agents_fingerprint(config: AppConfig) -> str:
    """A short hash of everything that shapes an agent's answer."""
    payload = json.dumps(config.agents.model_dump(mode="json"), sort_keys=True)
    digest = hashlib.sha256(payload.encode() + PROMPTS_FILE.read_bytes())
    return digest.hexdigest()[:16]


def a_daily_cycle(engine: Engine, day: date) -> tuple[m.DecisionCycle, list[m.AgentReport]] | None:
    """A's daily run of `day` whose agents have finished (its judge may or may not have)."""
    with engine.connect() as conn:
        view = conn.execution_options(schema_translate_map=a_tables(engine))
        with Session(bind=view) as session:
            cycles = session.scalars(
                select(m.DecisionCycle)
                .where(m.DecisionCycle.kind == m.CycleKind.DAILY,
                       m.DecisionCycle.as_of >= at_new_york(day, time(0)),
                       m.DecisionCycle.as_of < at_new_york(day + timedelta(days=1), time(0)))
                .order_by(m.DecisionCycle.id.desc())
            ).all()  # fmt: skip
            cycle = next(
                (c for c in cycles if (c.notes or {}).get("stage") in (AGENTS_DONE, "done")), None
            )
            if cycle is None:
                return None
            reports = list(session.scalars(
                select(m.AgentReport).where(m.AgentReport.cycle_id == cycle.id).order_by(m.AgentReport.id)
            ))  # fmt: skip
            session.expunge_all()
            return cycle, reports


_REPORT_FIELDS = ("agent_name", "sector", "event_type", "as_of", "ticker", "direction", "confidence",
                  "horizon_days", "thesis", "evidence", "risks", "catalysts", "dropped_claims", "model")  # fmt: skip


def copy_a_research(
    engine: Engine, sessions: sessionmaker[Session], config: AppConfig, day: date
) -> tuple[int, int] | None:
    """Copy A's reports of `day` into B as a cycle waiting for B's judge.

    Returns (B's cycle id, number of reports), or None when A has no finished run yet.
    Copying twice is harmless: the second call finds the first copy.
    """
    if agents_fingerprint(config) != FROZEN_A_FINGERPRINT:
        raise ReuseRefused(
            "B's agent settings or prompts differ from A's frozen ones, so A's reports are not "
            "what B would have got. Undo the change, or give B agents of its own."
        )
    found = a_daily_cycle(engine, day)
    if found is None:
        return None
    a_cycle, reports = found
    with sessions() as session:
        existing = session.scalars(
            select(m.DecisionCycle).where(m.DecisionCycle.kind == m.CycleKind.DAILY,
                                          m.DecisionCycle.as_of == a_cycle.as_of)
        ).all()  # fmt: skip
        for cycle in existing:
            if (cycle.notes or {}).get("copied_from_a") == a_cycle.id:
                n = len(
                    session.scalars(
                        select(m.AgentReport.id).where(m.AgentReport.cycle_id == cycle.id)
                    ).all()
                )
                return cycle.id, n
        notes = {
            "stage": AGENTS_DONE,
            "copied_from_a": a_cycle.id,
            "notes": ["research reused from system A"],
        }
        if (a_cycle.notes or {}).get("fresh_from"):
            notes["fresh_from"] = a_cycle.notes["fresh_from"]
        cycle = m.DecisionCycle(
            kind=m.CycleKind.DAILY, as_of=a_cycle.as_of, status=m.RunStatus.RUNNING, notes=notes
        )
        session.add(cycle)
        session.flush()
        for report in reports:
            session.add(
                m.AgentReport(cycle_id=cycle.id, **{f: getattr(report, f) for f in _REPORT_FIELDS})
            )
        session.commit()
        return cycle.id, len(reports)
