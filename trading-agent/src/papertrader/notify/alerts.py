"""Alerts that several parts of the system raise."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from papertrader.ingest.timeutil import NEW_YORK, new_york_date
from papertrader.llm.governor import BudgetStatus
from papertrader.notify import messages
from papertrader.notify.outbox import queue


def budget_alerts(session: Session, status: BudgetStatus, now: datetime) -> None:
    """At most one warning per month, and one degraded/stopped alert per day."""
    day, month = new_york_date(now), now.astimezone(NEW_YORK).strftime("%Y-%m")
    spent = f"${status.month_spent:.2f} of ${status.month_budget:.2f}"
    if status.mode == "stopped":
        queue(session, "alert", messages.alert(f"API budget used up ({spent}): no research until next month."),
              dedup_key=f"budget-stop:{day}")  # fmt: skip
    elif status.mode == "degraded":
        queue(session, "alert", messages.alert(f"Running in budget-saving mode: {status.reason}."),
              dedup_key=f"budget-degraded:{day}")  # fmt: skip
    if status.warn:
        queue(session, "alert", messages.alert(f"API spend has reached {status.month_fraction:.0%} of the monthly budget ({spent})."),
              dedup_key=f"budget-warn:{month}")  # fmt: skip


def job_failed(session: Session, job: str, error: str, now: datetime) -> None:
    queue(session, "alert", messages.alert(f"job '{job}' failed: {error[:300]}"),
          dedup_key=f"job-failed:{job}:{now:%Y-%m-%dT%H}")  # fmt: skip


def job_missed(session: Session, job: str, due: datetime) -> None:
    queue(session, "alert", messages.alert(f"job '{job}' did not run (due {due.astimezone(NEW_YORK):%a %H:%M} New York)"),
          dedup_key=f"job-missed:{job}:{due.isoformat()}")  # fmt: skip
