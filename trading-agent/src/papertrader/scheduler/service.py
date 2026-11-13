"""The long-running process started by `papertrader run`.

Phase 1: starts APScheduler in New York time with a single heartbeat job, which
proves the app container, the database and the dashboard are wired together.
The trading-day jobs from the spec's schedule are registered in Phase 7.
"""

from __future__ import annotations

import logging
import signal
from types import FrameType
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy.orm import Session, sessionmaker

from papertrader import __version__
from papertrader.db.session import session_scope
from papertrader.db.state import HEARTBEAT_KEY, set_state
from papertrader.db.types import utcnow
from papertrader.runtime import Runtime

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60


def beat(session_factory: sessionmaker[Session]) -> None:
    """Record that the app process is alive."""
    now = utcnow()
    with session_scope(session_factory) as session:
        set_state(session, HEARTBEAT_KEY, {"at": now.isoformat(), "version": __version__})
    log.info("heartbeat", extra={"at": now.isoformat()})


def build_scheduler(runtime: Runtime) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=ZoneInfo(runtime.config.schedule.timezone))
    scheduler.add_job(
        beat,
        "interval",
        seconds=HEARTBEAT_SECONDS,
        args=[runtime.session_factory],
        id="heartbeat",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def run_forever(runtime: Runtime) -> None:
    scheduler = build_scheduler(runtime)

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown requested", extra={"signal": signal.Signals(signum).name})
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    beat(runtime.session_factory)  # one immediately, so status is visible right away
    log.info("scheduler started", extra={"jobs": [job.id for job in scheduler.get_jobs()]})
    scheduler.start()
    log.info("scheduler stopped")
