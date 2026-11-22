"""The long-running process started by `papertrader run`.

APScheduler in New York time runs the trading-day schedule from config.yaml (see
scheduler/jobs.py), a heartbeat every minute, a watchdog every five minutes that
alerts about jobs that did not run, the Telegram sender, and the Telegram bot.
"""

from __future__ import annotations

import logging
import signal
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from papertrader import __version__
from papertrader.db.session import session_scope
from papertrader.db.state import HEARTBEAT_KEY, set_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import at_new_york, new_york_date
from papertrader.notify.bot import CommandBot
from papertrader.notify.service import (
    BotThread,
    flush_job,
    make_broker_factory,
    make_client,
    telegram_ready,
)
from papertrader.runtime import Runtime
from papertrader.scheduler.jobs import Jobs

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
    for spec in jobs.specs:
        if spec.at is None:  # repeats every N minutes; the job checks the market hours itself
            every = spec.every(runtime.config) if spec.every else cfg.monitor_interval_minutes
            offset = cfg.place_orders.minute % every
            trigger = CronTrigger(day_of_week="mon-fri", hour="9-15",
                                  minute=f"{offset}-59/{every}", timezone=tz)  # fmt: skip
        else:
            at = spec.at(runtime.config)
            days = (
                spec.weekday(runtime.config)
                if spec.weekday
                else cfg.weekly_day
                if spec.weekly
                else "mon-fri"
            )
            trigger = CronTrigger(day_of_week=days, hour=at.hour, minute=at.minute, timezone=tz)
        scheduler.add_job(jobs.run, trigger, args=[spec.name], id=spec.name, max_instances=1,
                          coalesce=True, misfire_grace_time=grace)  # fmt: skip
    if jobs.system in ("B", "C") and runtime.config.telegram.warnings.enabled:
        # X6: send announced orders once their heads-up time has come (or after Go now).
        scheduler.add_job(jobs.orders_due, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*", timezone=tz),
                          id="orders_due", max_instances=1, coalesce=True, misfire_grace_time=50)  # fmt: skip
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
        if runtime.system != "A" or not runtime.config.telegram.enabled:
            log.info("telegram is off in this system's config: the hub sends its messages")
        else:
            log.info("telegram not configured: no messages will be sent")
        return None
    bot = CommandBot(
        runtime.session_factory,
        runtime.config,
        make_client(runtime.settings),
        str(runtime.settings.telegram_chat_id),
        broker=make_broker_factory(runtime.settings, runtime.config, runtime.system),
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


HUB_REFRESH_MINUTES = 10


def run_hub(runtime: Runtime) -> None:
    """Telegram for all three systems: send every outbox (tagged) and answer commands.

    It also stores each paper account's live numbers for the web app every 2 minutes
    while the market is open (papertrader.web.live), with or without Telegram.
    """
    from papertrader.hub import HubBot, flush_all, open_views, tidy_buttons
    from papertrader.notify.service import make_broker_factory
    from papertrader.web import live

    settings = runtime.settings
    chat = str(settings.telegram_chat_id or "")
    telegram_on = settings.telegram_bot_token is not None and bool(chat)
    if not telegram_on:
        log.warning("telegram not configured: the hub only stores live numbers for the web app. Add "
                    "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env, then: docker compose restart hub")  # fmt: skip
    views = open_views(settings)
    log.info("hub started", extra={"systems": sorted(views), "telegram": telegram_on})
    telegram = runtime.config.telegram
    brokers = {
        name: make_broker_factory(settings, view.config, name) for name, view in views.items()
    }

    def snapshot() -> None:
        now = utcnow()
        try:
            live.write_all(dict(views), dict(brokers), now)
        except Exception:  # the web app's numbers must never take the hub down
            log.exception("live snapshot failed")
        try:
            ahead_checks(dict(views), runtime.config.telegram.warnings, now)
        except Exception:
            log.exception("price warnings failed")

    def a_ahead() -> None:
        """X6: A's buys and the judge's sells, as information, before A's order run."""
        from papertrader.notify.ahead import a_morning

        view = views.get("A")
        if view is None:
            return
        now = utcnow()
        try:
            with view.sessions() as session:
                a_morning(session, place_at=at_new_york(new_york_date(now), view.config.schedule.place_orders),
                          equity=live.read(view.sessions).equity, now=now)  # fmt: skip
                session.commit()
        except Exception:
            log.exception("A's order heads-up failed")

    def send() -> None:
        client = make_client(settings)
        try:
            for name, report in flush_all(dict(views), client, chat, telegram,
                                          public_url=runtime.config.web.public_url).items():  # fmt: skip
                if report.sent or report.failed or report.dropped or report.setup_problem:
                    log.info("telegram outbox", extra={"system": name, "sent": report.sent, "failed": report.failed,
                                                       "dropped": report.dropped, "problem": report.setup_problem})  # fmt: skip
            tidy_buttons(dict(views), client, chat)
        finally:
            client.close()

    def refresh() -> None:
        """Pick up B or C once they have been set up, without a restart."""
        for name, view in open_views(settings, skip=set(views)).items():
            # Replace, never change, the dicts: the sender and the bot may be reading them.
            if bot is not None:
                bot.bots = {**bot.bots, **HubBot({name: view}, bot.client, chat, settings,
                                                 telegram=telegram).bots}  # fmt: skip
            brokers[name] = make_broker_factory(settings, view.config, name)
            views[name] = view
            log.info("hub now serves another system", extra={"system": name})

    def comparison() -> None:
        """The weekly A vs B vs C message (docs/PREREGISTRATION.md)."""
        from papertrader.experiment.weekly import weekly_text

        client = make_client(settings)
        try:
            text = weekly_text({n: v.engine for n, v in views.items()}, runtime.config, utcnow())
            client.send(chat, text)
        except Exception:  # a report must never take the hub down
            log.exception("weekly comparison failed")
        finally:
            client.close()

    bot = (
        HubBot(views, make_client(settings), chat, settings, telegram=telegram,
               asker=make_asker(settings, runtime.config, views))
        if telegram_on
        else None
    )  # fmt: skip
    new_york = ZoneInfo("America/New_York")
    scheduler = BlockingScheduler(timezone=ZoneInfo(runtime.config.schedule.timezone))
    scheduler.add_job(refresh, "interval", minutes=HUB_REFRESH_MINUTES, id="refresh_systems",
                      max_instances=1, coalesce=True)  # fmt: skip
    scheduler.add_job(snapshot, CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/2", timezone=new_york),
                      id="web_live", max_instances=1, coalesce=True, misfire_grace_time=60)  # fmt: skip
    scheduler.add_job(snapshot, id="web_live_start", max_instances=1)  # once at start
    a_place = (
        views["A"].config.schedule.place_orders
        if "A" in views
        else runtime.config.schedule.place_orders
    )
    a_warn = (
        datetime.combine(date(2000, 1, 3), a_place) - timedelta(minutes=telegram.warnings.minutes)
    ).time()
    scheduler.add_job(a_ahead, CronTrigger(day_of_week="mon-fri", hour=a_warn.hour, minute=a_warn.minute,
                                           timezone=new_york), id="a_ahead", max_instances=1, coalesce=True,
                      misfire_grace_time=120)  # fmt: skip
    thread = BotThread(bot) if bot is not None else None  # type: ignore[arg-type]
    if telegram_on:
        scheduler.add_job(send, "interval", seconds=telegram.outbox_interval_seconds, id="telegram_outbox",
                          max_instances=1, coalesce=True)  # fmt: skip
        exp = runtime.config.experiment
        scheduler.add_job(comparison, CronTrigger(day_of_week=exp.weekly_day, hour=exp.weekly_time.hour,
                                                  minute=exp.weekly_time.minute,
                                                  timezone=ZoneInfo(runtime.config.schedule.timezone)),
                          id="weekly_comparison", max_instances=1, coalesce=True, misfire_grace_time=3600)  # fmt: skip

    def _stop(signum: int, _frame: FrameType | None) -> None:
        log.info("hub shutdown requested", extra={"signal": signal.Signals(signum).name})
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if thread is not None:
        thread.start()
    scheduler.start()
    if thread is not None:
        thread.stop()
    log.info("hub stopped")


def ahead_checks(views: Mapping[Any, Any], cfg: Any, now: datetime) -> int:
    """X6, after each live snapshot: close-to-stop/target warnings for every system, and
    A's time-limit sells announced before A's next check (information only)."""
    from sqlalchemy import select

    from papertrader.notify.ahead import a_time_limits, near_levels
    from papertrader.web import live

    count = 0
    for name, view in views.items():
        snap = live.read(view.sessions)
        if not snap.fresh(now):
            continue
        prices = {t: p.price for t, p in snap.positions.items()}
        with view.sessions() as session:
            extra: dict[str, float] = {}
            if name == "C":
                from papertrader.c.models import Order

                extra = {o.ticker: o.stop_price for o in session.scalars(select(Order).where(
                    Order.kind == "stop", Order.status.in_(("new", "accepted", "held", "pending_new"))))
                    if o.stop_price}  # fmt: skip
            count += near_levels(session, prices, cfg, now, extra)
            if name == "A":
                count += a_time_limits(session, check_minutes=view.config.schedule.monitor_interval_minutes,
                                       prices=prices, now=now)  # fmt: skip
            session.commit()
    return count


def make_asker(settings: Any, config: Any, views: Mapping[Any, Any]) -> Any:
    """X6: /ask on a cheap model, on its own small budget, recorded in B's spending
    (purpose "ask", outside B's budget and the experiment's costs)."""
    key = settings.anthropic_api_key
    home = views.get("B") or views.get("C")
    if key is None or home is None or config.telegram.ask_monthly_usd <= 0:
        return None
    import asyncio

    from papertrader.llm.client import ClaudeClient, make_anthropic
    from papertrader.llm.governor import CostGovernor
    from papertrader.notify.ask import ASK_PURPOSE, answer

    budget = config.budget.model_copy(update={"monthly_usd": config.telegram.ask_monthly_usd})

    def ask(question: str, data: str) -> str:
        api = make_anthropic(key.get_secret_value())
        governor = CostGovernor(home.sessions, budget, only=(ASK_PURPOSE,))
        claude = ClaudeClient(api, home.sessions, budget, governor)
        return asyncio.run(answer(question, data, claude, config.telegram.ask_model))

    return ask
