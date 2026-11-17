from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from sqlalchemy import Engine, select

from papertrader.config import get_settings
from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.db.state import HEARTBEAT_KEY, set_state
from papertrader.ingest.pipeline import StepResult
from papertrader.ingest.timeutil import NEW_YORK, at_new_york
from papertrader.runtime import Runtime
from papertrader.scheduler.jobs import Jobs
from papertrader.scheduler.service import build_scheduler

from ..ingest.helpers import shipped_config
from ..research.helpers import FRIDAY, FakeClaude, seed_market
from ..research.test_cycle import busy_market
from ..trading.helpers import HALF_DAYS, HOLIDAYS, MONDAY, FakeBroker, idea, seed_decision

SATURDAY = FRIDAY + timedelta(days=1)
THANKSGIVING = next(iter(HOLIDAYS))
HALF_DAY = next(iter(HALF_DAYS))


@dataclass
class World:
    jobs: Jobs
    clock: list[datetime]
    broker: FakeBroker
    claude: FakeClaude

    def at(self, day: date, hh: int, mm: int) -> None:
        self.clock[0] = at_new_york(day, time(hh, mm))

    def notes(self) -> list[m.Notification]:
        with self.jobs.sessions() as session:
            return list(session.scalars(select(m.Notification).order_by(m.Notification.id)))

    def runs(self, name: str | None = None) -> list[m.JobRun]:
        with self.jobs.sessions() as session:
            query = select(m.JobRun).order_by(m.JobRun.id)
            if name:
                query = query.where(m.JobRun.job_name == name)
            return list(session.scalars(query))


@pytest.fixture
def world(engine: Engine) -> Iterator[World]:
    clock = [at_new_york(MONDAY, time(6, 30))]
    broker = FakeBroker(clock)
    broker.prices["SPY"] = 600.0
    fake = FakeClaude()
    runtime = Runtime(settings=get_settings(), config=shipped_config(), engine=engine,
                      session_factory=make_session_factory(engine))  # fmt: skip

    def anthropic() -> AsyncAnthropic:
        return AsyncAnthropic(api_key="test-key", max_retries=0,
                              http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handler)))  # fmt: skip

    jobs = Jobs(
        runtime,
        broker=lambda: broker,
        anthropic=anthropic,
        now=lambda: clock[0],
        sleep=lambda _s: None,
    )
    yield World(jobs, clock, broker, fake)


def test_a_run_is_recorded_and_each_slot_runs_once(world: World) -> None:
    world.at(MONDAY, 8, 45)
    outcome = world.jobs.run("morning_brief")
    assert (outcome.status, outcome.detail) == ("succeeded", "queued")
    (run,) = world.runs()
    assert (
        run.scheduled_for == at_new_york(MONDAY, time(8, 45))
        and run.status == m.RunStatus.SUCCEEDED
    )
    assert run.finished_at is not None and run.details["summary"] == "queued"
    assert "No decision today" in world.notes()[0].text  # no research ran, and the brief says so
    world.at(MONDAY, 8, 46)
    assert world.jobs.run("morning_brief").status == "duplicate"  # e.g. a second copy of the app
    assert world.jobs.run("morning_brief", manual=True).detail == "already sent today"


def test_a_failure_is_recorded_and_alerted(world: World) -> None:
    world.jobs._anthropic = lambda: None  # no ANTHROPIC_API_KEY
    world.at(MONDAY, 7, 30)
    outcome = world.jobs.run("research_agents")
    assert outcome.status == "failed" and "ANTHROPIC_API_KEY is not set" in outcome.detail
    (run,) = world.runs()
    assert run.status == m.RunStatus.FAILED and "ANTHROPIC_API_KEY" in (run.error or "")
    assert (
        "job 'research_agents' failed: JobFailed: ANTHROPIC_API_KEY is not set"
        in world.notes()[0].text
    )


def test_failed_ingest_steps_fail_the_job_but_keep_the_rest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_daily(_ctx: Any, *, only: Any = None) -> list[StepResult]:
        return [StepResult("bars", "ok", rows=500), StepResult("news", "failed", detail="HTTP 503")]

    monkeypatch.setattr("papertrader.ingest.pipeline.run_daily", fake_daily)
    world.jobs._anthropic = lambda: None
    outcome = world.jobs.run("ingest")
    assert (
        outcome.status == "failed" and "steps failed: news (the others finished)" in outcome.detail
    )
    assert world.runs()[0].details["steps"] == {"bars": "ok", "news": "failed"}
    assert world.runs()[0].details["text_labels"] == "off"


def test_holidays_weekends_and_half_days(world: World) -> None:
    world.at(THANKSGIVING, 6, 30)
    assert world.jobs.run("ingest").status == "skipped"
    assert world.runs("ingest")[0].details["summary"] == "the market is closed today"
    world.at(THANKSGIVING, 10, 5)
    assert world.jobs.run("monitor").status == "not_due" and world.runs("monitor") == []
    world.at(HALF_DAY, 12, 55)
    assert world.jobs.run("monitor").status == "succeeded"
    world.at(HALF_DAY, 13, 5)  # half day: the market closed at 13:00
    assert world.jobs.run("monitor").status == "not_due"
    world.at(MONDAY, 9, 25)  # before the first order time
    assert world.jobs.run("monitor").status == "not_due"
    world.at(MONDAY + timedelta(days=5), 9, 0)  # Saturday: the weekly job runs anyway
    world.jobs._anthropic = lambda: None
    assert world.jobs.run("weekly").status in ("succeeded", "failed")
    assert world.runs("weekly")[0].status != m.RunStatus.SKIPPED


def test_the_watchdog_reports_jobs_that_did_not_run(world: World) -> None:
    world.at(MONDAY, 8, 45)
    world.jobs.run("morning_brief")
    world.at(MONDAY, 9, 1)
    missed = world.jobs.watchdog()
    assert missed == ["ingest", "research_agents", "judge"]
    alert = world.notes()[-1].text
    assert (
        "These jobs did not run today: ingest (06:30), research_agents (07:30), judge (08:20)"
        in alert
    )
    assert "papertrader jobs run NAME" in alert
    assert {r.job_name: r.status for r in world.runs()}["judge"] == m.RunStatus.MISSED
    count = len(world.notes())
    world.at(MONDAY, 9, 30)
    assert world.jobs.watchdog() == [] and len(world.notes()) == count  # said once
    world.at(THANKSGIVING, 12, 0)
    assert world.jobs.watchdog() == []
    world.at(SATURDAY, 9, 10)
    assert world.jobs.watchdog() == []  # within the grace period
    world.at(SATURDAY, 9, 20)
    assert world.jobs.watchdog() == ["weekly"]


def test_the_watchdog_notices_a_stalled_monitor(world: World) -> None:
    world.at(MONDAY, 9, 35)
    world.jobs.run("monitor")
    world.at(MONDAY, 9, 55)
    assert "monitor" not in world.jobs.watchdog()
    world.at(MONDAY, 10, 40)
    assert "monitor" in world.jobs.watchdog()
    assert "The position monitor has not run since 09:35" in world.notes()[-1].text
    world.at(MONDAY, 10, 45)
    world.jobs.watchdog()
    assert sum("position monitor" in n.text for n in world.notes()) == 1  # once an hour at most


def test_the_app_says_how_long_it_was_off(world: World) -> None:
    with world.jobs.sessions() as session:
        set_state(session, HEARTBEAT_KEY, {"at": at_new_york(MONDAY, time(7, 0)).isoformat()})
        session.commit()
    world.at(MONDAY, 7, 5)
    assert world.jobs.startup_check() is None  # a quick restart is not news
    world.at(MONDAY, 10, 0)
    text = world.jobs.startup_check()
    assert (
        text
        == "The app was not running from Mon 16 Nov 07:00 to Mon 10:00 New York time (3 hours)."
    )
    assert world.notes()[-1].text.endswith(text)


def test_orders_follow_the_judge_including_its_exits(world: World, engine: Engine) -> None:
    world.broker.prices.update({"AAPL": 200.0, "MSFT": 400.0})
    world.at(MONDAY - timedelta(days=3), 9, 40)  # Friday: buy AAPL
    seed_decision(engine, [idea("AAPL", 200.0)], as_of=world.clock[0] - timedelta(hours=2))
    assert world.jobs.run("place_orders").detail == "1 order(s) placed"
    world.broker.fill_entry("AAPL", 200.0)
    world.at(MONDAY - timedelta(days=3), 9, 45)
    world.jobs.run("monitor")

    world.at(MONDAY, 9, 35)  # Monday: the judge wants out of AAPL and into MSFT
    seed_decision(engine, [idea("MSFT", 400.0)], as_of=at_new_york(MONDAY, time(7, 30)))
    with world.jobs.sessions() as session:
        decision = session.scalars(
            select(m.JudgeDecision).order_by(m.JudgeDecision.id.desc())
        ).first()
        assert decision is not None
        decision.payload = {
            "exits": [
                {"ticker": "AAPL", "reason": "guidance cut"},
                {"ticker": "TSLA", "reason": "not held"},
            ]
        }
        session.commit()
    outcome = world.jobs.run("place_orders")
    assert outcome.status == "succeeded", outcome.detail
    assert outcome.details["exits"] == ["AAPL: exit order placed"]
    assert any(p.startswith("MSFT submitted") for p in outcome.details["placements"])
    with world.jobs.sessions() as session:
        trade = session.scalars(select(m.Trade).where(m.Trade.ticker == "AAPL")).one()
        assert trade.exit_trigger == m.ExitTrigger.THESIS_BREAK and "judge: guidance cut" in (
            trade.exit_reason or ""
        )


def test_a_full_trading_day_on_the_schedule(world: World, engine: Engine) -> None:
    seed_market(engine)
    busy_market(world.claude)
    day = FRIDAY
    world.at(day, 7, 30)
    research = world.jobs.run("research_agents")
    assert research.status == "succeeded" and research.details["reports"] == 7
    world.at(day, 8, 20)
    judge = world.jobs.run("judge")
    assert judge.status == "succeeded" and judge.details["to_trade"] == ["AAPL"]
    world.at(day, 8, 45)
    world.jobs.run("morning_brief")
    brief = world.notes()[-1].text
    assert brief.startswith("Morning brief · Fri 13 Nov") and "1. BUY AAPL" in brief

    with world.jobs.sessions() as session:
        aapl = session.scalars(select(m.TradeIdea).where(m.TradeIdea.ticker == "AAPL")).one()
        price = round(((aapl.entry_low or 0) + (aapl.entry_high or 0)) / 2, 2)
    world.broker.prices["AAPL"] = price
    world.at(day, 9, 35)
    assert world.jobs.run("place_orders").detail == "1 order(s) placed"
    world.broker.fill_entry("AAPL", price)
    world.at(day, 9, 45)
    monitor = world.jobs.run("monitor")
    assert monitor.status == "succeeded" and monitor.details["opened"] == ["AAPL"]
    world.at(day, 16, 15)
    assert world.jobs.run("reconcile_eod").status == "succeeded"
    world.at(day, 17, 0)
    resolve = world.jobs.run("resolve_outcomes")
    assert (
        resolve.status == "succeeded" and resolve.details["resolved"] == 0
    )  # horizons still running

    kinds = [n.kind for n in world.notes()]
    assert kinds[:2] == ["morning_brief", "trade_executed"] and "eod" in kinds
    world.at(day, 17, 20)
    assert world.jobs.watchdog() == ["ingest"]  # the one job this test did not run

    world.at(SATURDAY, 9, 0)
    weekly = world.jobs.run("weekly")
    text = world.notes()[-1].text
    assert (
        text.startswith("Weekly summary · week to Sat 14 Nov")
        and "Agent weights: unchanged" in text
    )
    assert "Retrain: " in text and weekly.details["lessons"].startswith("skipped")


def test_the_schedule_in_new_york_time(world: World) -> None:
    scheduler = build_scheduler(world.jobs.runtime, world.jobs)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    assert {
        "heartbeat",
        "watchdog",
        "ingest",
        "research_agents",
        "judge",
        "morning_brief",
        "place_orders",
        "monitor",
        "reconcile_eod",
        "resolve_outcomes",
        "weekly",
    } <= set(jobs)

    def next_fire(name: str, after: datetime) -> datetime:
        fire = jobs[name].trigger.get_next_fire_time(None, after)
        assert fire is not None
        return fire.astimezone(NEW_YORK)

    sunday_night = at_new_york(MONDAY - timedelta(days=1), time(23, 0)).astimezone(NEW_YORK)
    assert next_fire("ingest", sunday_night).strftime("%a %H:%M") == "Mon 06:30"
    assert next_fire("place_orders", sunday_night).strftime("%a %H:%M") == "Mon 09:35"
    assert next_fire("weekly", sunday_night).strftime("%a %H:%M") == "Sat 09:00"
    after_open = at_new_york(MONDAY, time(9, 31)).astimezone(NEW_YORK)
    assert next_fire("monitor", after_open).strftime("%H:%M") == "09:35"
    assert next_fire("monitor", after_open + timedelta(minutes=5)).strftime("%H:%M") == "09:45"
    late = at_new_york(MONDAY, time(15, 56)).astimezone(NEW_YORK)
    assert next_fire("monitor", late).strftime("%a %H:%M") == "Tue 09:05"  # skipped: before orders
    assert jobs["ingest"].misfire_grace_time == 15 * 60


def test_after_a_holiday_fresh_news_reaches_back_to_the_last_trading_day(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from papertrader.research.cycle import CycleResult

    seen: list[datetime] = []

    async def fake_cycle(**kwargs: Any) -> CycleResult:
        seen.append(kwargs["fresh_from"])
        assert kwargs["stage"] == "agents"
        return CycleResult(as_of=kwargs["as_of"], kind="daily", status="succeeded", budget=None)  # type: ignore[arg-type]

    monkeypatch.setattr("papertrader.research.cycle.run_cycle", fake_cycle)
    wednesday = THANKSGIVING - timedelta(days=1)
    assert world.jobs.previous_trading_day(HALF_DAY) == wednesday
    assert world.jobs.previous_trading_day(MONDAY) == MONDAY - timedelta(days=3)
    world.at(HALF_DAY, 7, 30)  # the Friday after Thanksgiving
    assert world.jobs.run("research_agents").status == "succeeded"
    world.at(wednesday + timedelta(days=6), 7, 30)  # an ordinary Tuesday
    world.jobs.run("research_agents")
    assert [s.astimezone(NEW_YORK).strftime("%a %d %H:%M") for s in seen] == [
        "Wed 25 05:30",
        "Mon 30 05:30",
    ]
