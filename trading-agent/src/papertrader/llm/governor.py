"""The cost governor: keeps Claude API spending inside the monthly budget.

It reads what was spent from the `api_usage` table and decides how the system
should behave:

* normal    - everything runs as configured
* degraded  - fewer agents and lower effort (close to the monthly budget, or today
              has already used more than its fair share)
* stopped   - no API calls at all (monthly budget used up)

It degrades instead of failing: a cycle in degraded mode still produces a decision,
and a stopped cycle ends cleanly with "no decision" rather than an error.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import BudgetConfig, Effort
from papertrader.db.models import ApiUsage
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import NEW_YORK

Mode = Literal["normal", "degraded", "stopped"]
_EFFORT_ORDER: tuple[Effort, ...] = ("low", "medium", "high", "xhigh", "max")


class BudgetExceededError(RuntimeError):
    """The call was not made because it would go over a spending limit."""


@dataclass(frozen=True)
class BudgetStatus:
    mode: Mode
    reason: str
    month_spent: float
    month_budget: float
    day_spent: float
    day_budget: float
    warn: bool  # past the warning threshold (for a Telegram alert)

    @property
    def month_fraction(self) -> float:
        return self.month_spent / self.month_budget


def lower_effort(effort: Effort) -> Effort:
    """One step down, never below 'low'."""
    return _EFFORT_ORDER[max(0, _EFFORT_ORDER.index(effort) - 1)]


def weekdays_left_in_month(today: date) -> int:
    """Trading days still to pay for, today included (holidays ignored: slightly cautious)."""
    count, day = 0, today
    while day.month == today.month:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return max(count, 1)


class CostGovernor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        budget: BudgetConfig,
        *,
        now: Callable[[], datetime] = utcnow,
        cycle_cap_usd: float | None = None,
    ) -> None:
        self._sessions = session_factory
        self._budget = budget
        self._now = now
        self._lock = threading.Lock()
        self._reserved = 0.0  # calls in flight, not yet in the database
        self._cycle_cap = cycle_cap_usd
        self._cycle_spent = 0.0

    # ------------------------------------------------------------------ reading spend

    def _spent_since(self, start: datetime) -> float:
        with self._sessions() as session:
            total = session.scalar(
                select(func.coalesce(func.sum(ApiUsage.cost_usd), 0.0)).where(ApiUsage.ts >= start)
            )
        return float(total or 0.0)

    def status(self) -> BudgetStatus:
        local = self._now().astimezone(NEW_YORK)
        month_start = datetime.combine(local.date().replace(day=1), time(0), tzinfo=NEW_YORK)
        day_start = datetime.combine(local.date(), time(0), tzinfo=NEW_YORK)
        month_spent = self._spent_since(month_start) + self._reserved
        day_spent = self._spent_since(day_start) + self._reserved

        monthly = self._budget.monthly_usd
        remaining = max(monthly - (month_spent - day_spent), 0.0)  # what was left this morning
        day_budget = (
            remaining / weekdays_left_in_month(local.date()) * self._budget.daily_overspend_factor
        )

        if month_spent >= monthly * self._budget.hard_stop_at:
            mode: Mode = "stopped"
            reason = f"monthly budget used up (${month_spent:.2f} of ${monthly:.2f})"
        elif month_spent >= monthly * self._budget.degrade_at:
            mode = "degraded"
            reason = f"{month_spent / monthly:.0%} of the monthly budget is spent"
        elif day_spent >= day_budget:
            mode = "degraded"
            reason = f"today's share of the budget is spent (${day_spent:.2f} of ${day_budget:.2f})"
        else:
            mode, reason = "normal", "within budget"
        return BudgetStatus(
            mode, reason, month_spent, monthly, day_spent, day_budget,
            warn=month_spent >= monthly * self._budget.warn_at,
        )  # fmt: skip

    # ------------------------------------------------------------------ per-call control

    def reserve(self, estimate_usd: float) -> None:
        """Claim room for a call before making it. Raises if it would break a limit.

        Reservations matter because agents run at the same time: without them ten calls
        could each see "there is room" and together overshoot.
        """
        with self._lock:
            status = self.status()
            limit = status.month_budget * self._budget.hard_stop_at
            if status.month_spent + estimate_usd > limit:
                raise BudgetExceededError(
                    f"monthly API budget reached (${status.month_spent:.2f} of ${limit:.2f})"
                )
            if self._cycle_cap is not None:
                committed = self._cycle_spent + self._reserved
                if committed + estimate_usd > self._cycle_cap:
                    raise BudgetExceededError(
                        f"this run's spending cap reached "
                        f"(${committed:.2f} of ${self._cycle_cap:.2f})"
                    )
            self._reserved += estimate_usd

    def settle(self, estimate_usd: float, actual_usd: float) -> None:
        """Replace a reservation with what the call really cost (already written to the DB)."""
        with self._lock:
            self._reserved = max(self._reserved - estimate_usd, 0.0)
            self._cycle_spent += actual_usd

    @property
    def cycle_spent(self) -> float:
        return self._cycle_spent
