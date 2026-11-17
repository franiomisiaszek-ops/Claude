"""The long-running process started by `papertrader run`.

APScheduler in New York time runs the trading-day schedule from config.yaml (see
scheduler/jobs.py), a heartbeat every minute, a watchdog every five minutes that
alerts about jobs that did not run, the Telegram sender, and the Telegram bot.
"""

from __future__ import annotations

import logging
import signal
from datetime import timedelta
from types import FrameType
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from papertrader import __version__
from papertrader.db.session import session_scope
from papertrader.db.state import HEARTBEAT_KEY, set_state
from papertrader.db.types import utcnow
from papertrader.notify.bot import CommandBot
from papertrader.notify.service import (
    BotThread,
    flush_job,
    make_broker_factory,
    make_client,
    telegram_ready,
)
from papertrader.runtime import Runtime
from papertrader.scheduler.jobs import SPECS, Jobs

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 60


def beat(session_factory: sessionmaker[Session]) -> None:
    """Record that the app process is alive."""
    now = utcnow()
    with session_scope(session_factory) as session:
        set_state(session, HEARTBEAT_KEY, {"at": now.isoformat(), "version": __version__})
    log.info("heartbeat", extra={"at": now.isoformat()})


WATCHDOG_MINUTES = 5


def add_schedule(scheduler: BlockingScheduler, runtime: Runtime, jobs: Jobs) -> None:
    """The trading-day jobs. Weekdays only; jobs check the market calendar themselves."""
    cfg = runtime.config.schedule
    tz = ZoneInfo(cfg.timezone)
    grace = cfg.heartbeat_grace_minutes * 60
    for spec in SPECS:
        if spec.at is None:  # the monitor: every N minutes; it checks the market hours itself
            offset = cfg.place_orders.minute % cfg.monitor_interval_minutes
            trigger = CronTrigger(day_of_week="mon-fri", hour="9-15",
                                  minute=f"{offset}-59/{cfg.monitor_interval_minutes}", timezone=tz)  # fmt: skip
        else:
            at = spec.at(runtime.config)
            days = cfg.weekly_day if spec.weekly else "mon-fri"
            trigger = CronTrigger(day_of_week=days, hour=at.hour, minute=at.minute, timezone=tz)
        scheduler.add_job(jobs.run, trigger, args=[spec.name], id=spec.name, max_instances=1,
                          coalesce=True, misfire_grace_time=grace)  # fmt: skip
    scheduler.add_job(jobs.watchdog, "interval", minutes=WATCHDOG_MINUTES, id="watchdog",
                      max_instances=1, coalesce=True,
                      next_run_time=utcnow() + timedelta(seconds=60))  # fmt: skip


def build_scheduler(runtime: Runtime, jobs: Jobs | None = None) -> BlockingScheduler:
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
    if telegram_ready(runtime.settings, runtime.config):
        scheduler.add_job(
            flush_job,
            "interval",
            seconds=runtime.config.telegram.outbox_interval_seconds,
            args=[runtime.session_factory, runtime.settings, runtime.config],
            id="telegram_outbox",
            max_instances=1,
            coalesce=True,
        )
    if jobs is not None:
        add_schedule(scheduler, runtime, jobs)
    return scheduler


def start_bot(runtime: Runtime) -> BotThread | None:
    if not telegram_ready(runtime.settings, runtime.config):
        log.info("telegram not configured: no messages will be sent")
        return None
    bot = CommandBot(
        runtime.session_factory,
        runtime.config,
        make_client(runtime.settings),
        str(runtime.settings.telegram_chat_id),
        broker=make_broker_factory(runtime.settings, runtime.config),
    )
    thread = BotThread(bot)
    thread.start()
    return thread


def run_forever(runtime: Runtime) -> None:
    jobs = Jobs(runtime)
    scheduler = build_scheduler(runtime, jobs)

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("shutdown requested", extra={"signal": signal.Signals(signum).name})
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    bot = start_bot(runtime)

    try:
        down = jobs.startup_check()  # was the app off for a while? (before the first beat)
        if down:
            log.warning(down)
    except Exception:
        log.exception("startup check failed")
    beat(runtime.session_factory)  # one immediately, so status is visible right away
    log.info("scheduler started", extra={"jobs": [job.id for job in scheduler.get_jobs()]})
    scheduler.start()
    if bot is not None:
        bot.stop()
    log.info("scheduler stopped")
