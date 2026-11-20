"""The daily schedule's jobs, and the runner that records and guards every run.

Each job is a plain method here, so the scheduler and `papertrader jobs run NAME` run
exactly the same code. Every run is recorded in `job_runs` (started, finished, status,
details); a failure queues a Telegram alert; a slot can only run once (the database
refuses a second row for the same job and time). The watchdog compares what should
have run today with what did, and alerts about the gaps.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.state import HEARTBEAT_KEY, get_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import NEW_YORK, at_new_york, new_york_date
from papertrader.llm.client import BudgetExceededError, ClaudeClient, make_anthropic
from papertrader.llm.governor import CostGovernor
from papertrader.notify import messages
from papertrader.notify.alerts import job_failed
from papertrader.notify.outbox import queue
from papertrader.runtime import Runtime
from papertrader.trading.broker import Broker, TradingDay

log = logging.getLogger(__name__)

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FABLE_PURPOSE = "fable_shadow"  # system B's Fable test: its own budget, never traded


class JobSkipped(Exception):
    def __init__(self, reason: str, *, alert: bool = False) -> None:
        super().__init__(reason)
        self.alert = alert


class JobFailed(RuntimeError):
    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.details = details or {}


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    at: Callable[[AppConfig], time] | None  # None: the monitor, which repeats
    weekly: bool = False


SPECS: tuple[JobSpec, ...] = (
    JobSpec("ingest", "load overnight news, filings, prices and macro data", lambda c: c.schedule.ingest),
    JobSpec("research_agents", "the research agents write their reports", lambda c: c.schedule.research_agents),
    JobSpec("b_signals", "system B's extra signals: insider clusters, options, analysts", lambda c: c.schedule.b_signals),
    JobSpec("judge", "pattern-model scores and the judge's decision", lambda c: c.schedule.score_and_judge),
    JobSpec("morning_brief", "Telegram morning brief", lambda c: c.schedule.morning_brief),
    JobSpec("place_orders", "sell what the judge dropped, then place today's orders", lambda c: c.schedule.place_orders),
    JobSpec("monitor", "fills, time stops, kill switch, bad news on held stocks", None),
    JobSpec("fable_shadow", "system B's Fable test: some agents again on Claude Fable, never traded", lambda c: c.schedule.b_fable),
    JobSpec("reconcile_eod", "cancel unfilled orders, end-of-day summary", lambda c: c.schedule.reconcile_eod),
    JobSpec("resolve_outcomes", "score agent calls whose horizon has ended", lambda c: c.schedule.resolve_outcomes),
    JobSpec("weekly", "agent weights, retrain the model, lessons, weekly summary", lambda c: c.schedule.weekly_time, weekly=True),
)  # fmt: skip
BY_NAME = {spec.name: spec for spec in SPECS}

# B reuses A's research and does not load market data; C gets its own jobs in Phase X4.
B_ONLY = ("b_signals", "fable_shadow")
SYSTEM_JOBS: dict[str, tuple[str, ...]] = {
    "A": tuple(spec.name for spec in SPECS if spec.name not in B_ONLY),
    "B": (
        "b_signals",
        "judge",
        "morning_brief",
        "place_orders",
        "monitor",
        "fable_shadow",
        "reconcile_eod",
        "resolve_outcomes",
        "weekly",
    ),
    "C": (),
}


def specs_for(system: str, config: AppConfig | None = None) -> tuple[JobSpec, ...]:
    names = set(SYSTEM_JOBS[system])
    if config is not None and not config.decision.fable_shadow.enabled:
        names.discard("fable_shadow")  # only scheduled while the test is switched on
    return tuple(spec for spec in SPECS if spec.name in names)


@dataclass(frozen=True)
class RunOutcome:
    name: str
    status: str  # succeeded | failed | skipped | not_due | duplicate
    detail: str
    details: dict[str, Any]


class Jobs:
    def __init__(
        self,
        runtime: Runtime,
        *,
        broker: Callable[[], Broker | None] | None = None,
        anthropic: Callable[[], AsyncAnthropic | None] | None = None,
        now: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        from papertrader.notify.service import make_broker_factory

        self.runtime = runtime
        self.config = runtime.config
        self.sessions = runtime.session_factory
        self._broker = broker or make_broker_factory(
            runtime.settings, runtime.config, runtime.system
        )
        self._anthropic = anthropic or self._default_anthropic
        self.now = now
        self.sleep = sleep
        self.trading_lock = threading.Lock()  # orders, monitor and reconcile never overlap
        self._days: dict[date, TradingDay | None] = {}
        self._clock_checked: datetime | None = None
        self.system = runtime.system
        self.specs = specs_for(runtime.system, runtime.config)
        # Only A loads market data; B and C read it and never write to it.
        self.writes_market_data = runtime.system == "A"

    # ------------------------------------------------------------------ resources

    def _default_anthropic(self) -> AsyncAnthropic | None:
        key = self.runtime.settings.anthropic_api_key
        return make_anthropic(key.get_secret_value()) if key else None

    def broker(self) -> Broker:
        broker = self._broker()
        if broker is None:
            raise JobFailed(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY (paper account) are not set in .env"
            )
        return broker

    def claude(self) -> ClaudeClient | None:
        """A fresh client for each job: each job runs its own event loop."""
        api = self._anthropic()
        if api is None:
            return None
        governor = CostGovernor(self.sessions, self.config.budget, exclude=(FABLE_PURPOSE,))
        return ClaudeClient(api, self.sessions, self.config.budget, governor)

    def fable_claude(self) -> ClaudeClient:
        """System B's Fable test, on its own small budget (decision.fable_shadow.monthly_usd)."""
        api = self._anthropic()
        if api is None:
            raise JobFailed("ANTHROPIC_API_KEY is not set in .env")
        fable = self.config.decision.fable_shadow
        budget = self.config.budget.model_copy(update={"monthly_usd": fable.monthly_usd})
        governor = CostGovernor(self.sessions, budget, only=(FABLE_PURPOSE,))
        return ClaudeClient(api, self.sessions, budget, governor)

    def _need_claude(self) -> ClaudeClient:
        client = self.claude()
        if client is None:
            raise JobFailed("ANTHROPIC_API_KEY is not set in .env")
        return client

    def _engine(self) -> Any:
        from papertrader.trading.engine import TradingEngine

        return TradingEngine(
            self.sessions, self.broker(), self.config, now=self.now, sleep=self.sleep
        )

    def trading_day(self, day: date) -> TradingDay | None:
        """Today's market hours from the Alpaca calendar (holidays and half days included).

        Without Alpaca, Monday to Friday 09:30-16:00 is assumed.
        """
        if day not in self._days:
            try:
                broker = self._broker()
                days = (
                    broker.calendar(day - timedelta(days=10), day + timedelta(days=10))
                    if broker
                    else None
                )
            except Exception:
                log.warning("market calendar unavailable; assuming a normal weekday")
                days = None
            if days is None:
                self._days[day] = (
                    TradingDay(day, at_new_york(day, time(9, 30)), at_new_york(day, time(16, 0)))
                    if day.weekday() < 5
                    else None
                )
            else:
                known = {item.day: item for item in days}
                for offset in range(-10, 11):  # days the calendar does not list are closed
                    when = day + timedelta(days=offset)
                    self._days.setdefault(when, known.get(when))
        return self._days[day]

    def previous_trading_day(self, day: date) -> date | None:
        for back in range(1, 11):
            candidate = day - timedelta(days=back)
            if self.trading_day(candidate) is not None:
                return candidate
        return None

    # ------------------------------------------------------------------ the runner

    def slot(self, name: str, now: datetime) -> datetime:
        """The scheduled time this run belongs to (minutes for the monitor)."""
        spec = BY_NAME[name]
        if spec.at is None:
            return now.replace(second=0, microsecond=0)
        return at_new_york(new_york_date(now), spec.at(self.config))

    def run(self, name: str, *, manual: bool = False) -> RunOutcome:
        """Run one job now, recording it. `manual` runs are never skipped as not due."""
        if name not in {spec.name for spec in self.specs}:
            raise KeyError(name)
        now = self.now()
        day = new_york_date(now)
        spec = BY_NAME[name]
        trading = self.trading_day(day)
        if not manual and name == "monitor" and not self._monitor_due(now, trading):
            return RunOutcome(name, "not_due", "outside market hours", {})
        scheduled_for = now.replace(microsecond=0) if manual else self.slot(name, now)

        with self.sessions() as session:
            row = m.JobRun(job_name=name, scheduled_for=scheduled_for, started_at=now,
                           status=m.RunStatus.RUNNING, details={"manual": manual})  # fmt: skip
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return RunOutcome(name, "duplicate", "this slot has already run", {})
            row_id = row.id

        status, detail = m.RunStatus.SUCCEEDED, ""
        details: dict[str, Any] = {}
        try:
            if not spec.weekly and trading is None and not manual:
                raise JobSkipped("the market is closed today")
            details = getattr(self, "job_" + name)() or {}
            detail = str(details.get("summary", ""))
        except JobSkipped as exc:
            status, detail = m.RunStatus.SKIPPED, str(exc)
            if exc.alert:
                with self.sessions() as session:
                    queue(session, "alert", messages.alert(f"{name}: {exc}"),
                          dedup_key=f"job-skipped:{name}:{day}")  # fmt: skip
                    session.commit()
        except Exception as exc:  # a job must never take the scheduler down
            status, detail = m.RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
            details = getattr(exc, "details", {})
            log.exception("job failed", extra={"job": name})
            with self.sessions() as session:
                job_failed(session, name, detail, now)
                session.commit()

        with self.sessions() as session:
            done = session.get(m.JobRun, row_id)
            assert done is not None
            done.status, done.finished_at = status, utcnow()
            done.error = detail[:2000] if status == m.RunStatus.FAILED else None
            done.details = {"manual": manual, "summary": detail[:500]} | _jsonable(details)
            session.commit()
        log.info(
            "job finished", extra={"job": name, "status": status.value, "detail": detail[:300]}
        )
        return RunOutcome(name, status.value, detail, details)

    def _monitor_due(self, now: datetime, trading: TradingDay | None) -> bool:
        if trading is None:
            return False
        start = max(trading.open, at_new_york(trading.day, self.config.schedule.place_orders))
        return start <= now < trading.close

    # ------------------------------------------------------------------ jobs

    def job_ingest(self) -> dict[str, Any]:
        from papertrader.ingest.context import open_context
        from papertrader.ingest.pipeline import run_daily

        async def go() -> list[Any]:
            async with open_context(self.config, self.runtime.settings, self.sessions) as ctx:
                return await run_daily(ctx)

        results = asyncio.run(go())
        steps = {r.name: r.status for r in results}
        details: dict[str, Any] = {"steps": steps, "text_labels": self._submit_text_labels()}
        failed = [r.name for r in results if r.status == "failed"]
        details["summary"] = f"{sum(r.rows for r in results):,} rows new or changed"
        if failed:
            raise JobFailed(f"steps failed: {', '.join(failed)} (the others finished)", details)
        return details

    def _submit_text_labels(self) -> str:
        """New headlines get the same Claude labels the model was trained with (half-price batch)."""
        from papertrader.model.llm_text import submit

        limit = self.config.learning.text_batch_limit
        api = self._anthropic() if limit and self.writes_market_data else None
        if api is None:
            return "off"
        governor = CostGovernor(self.sessions, self.config.budget)
        if governor.status().mode != "normal":
            return "skipped to save budget"
        try:
            batch, items, _cost = asyncio.run(
                submit(api, self.sessions, self.config, governor, limit=limit)
            )
        except BudgetExceededError as exc:
            return f"not sent: {exc}"
        except Exception as exc:  # optional: never fail the ingest for it
            return f"not sent ({type(exc).__name__})"
        return f"{items} headlines sent ({batch})" if batch else "nothing new"

    def _collect_text_labels(self) -> str:
        from papertrader.model.llm_text import collect

        api = self._anthropic() if self.writes_market_data else None
        if api is None:
            return "off"
        try:
            summary = asyncio.run(collect(api, self.sessions, self.config))
        except Exception as exc:
            return f"not collected ({type(exc).__name__})"
        return (
            f"{summary['stored']} stored, {len(summary['still_running'])} batch(es) still running"
        )

    def job_research_agents(self) -> dict[str, Any]:
        from papertrader.agents.snapshot import fresh_since
        from papertrader.ingest.context import open_context
        from papertrader.research.cycle import CycleResult, run_cycle

        claude = self._need_claude()
        now = self.now()
        # "Fresh" news reaches back to the previous trading day's run, so nothing published
        # over a weekend or a market holiday is missed (plus two hours of overlap).
        fresh_from = fresh_since(now, self.config.agents.lookback_hours)
        previous = self.previous_trading_day(new_york_date(now))
        if previous is not None:
            fresh_from = min(
                fresh_from,
                at_new_york(previous, self.config.schedule.research_agents) - timedelta(hours=2),
            )

        async def go() -> CycleResult:
            async with open_context(self.config, self.runtime.settings, self.sessions) as ctx:
                return await run_cycle(config=self.config, sessions=self.sessions, claude=claude,
                                       as_of=now, kind=m.CycleKind.DAILY, sec=ctx.sec,
                                       stage="agents", fresh_from=fresh_from)  # fmt: skip

        result = asyncio.run(go())
        details = {"cycle_id": result.cycle_id, "reports": len(result.reports),
                   "cost_usd": round(result.cost_usd, 4), "notes": result.notes,
                   "summary": f"{len(result.reports)} reports, ${result.cost_usd:.2f}"}  # fmt: skip
        if result.status == "failed":
            raise JobFailed("; ".join(result.notes) or "the research run failed", details)
        if result.status == "skipped":
            raise JobSkipped("; ".join(result.notes), alert=result.budget.mode != "stopped")
        return details

    def job_judge(self) -> dict[str, Any]:
        from papertrader.judge.judge import open_positions
        from papertrader.learning.lessons import latest_lessons
        from papertrader.research.cycle import run_judge_stage

        claude = self._need_claude()
        now = self.now()
        copied = self._copy_research_from_a(now) if self.system == "B" else None
        labels = self._collect_text_labels()  # today's headlines, before the model scores them
        with self.sessions() as session:
            positions = open_positions(session)
            lessons = latest_lessons(session, self.config, now)
        layer = None
        if self.system == "B":
            from papertrader.b.layer import DecisionLayer

            layer = DecisionLayer(self.config, self.sessions, self.runtime.engine, now)
        result = asyncio.run(run_judge_stage(config=self.config, sessions=self.sessions, claude=claude,
                                             now=now, positions=positions, lessons=lessons,
                                             layer=layer))  # fmt: skip
        trades = result.decision.trades if result.decision else []
        proposed = [t.ticker for t in trades if t.status == "proposed"]
        details = {"cycle_id": result.cycle_id, "trades": len(trades), "to_trade": proposed,
                   "lessons": bool(lessons), "text_labels": labels, "notes": result.notes,
                   "reports_from_a": copied,
                   "cost_usd": round(result.cost_usd, 4),
                   "summary": f"{len(trades)} ideas, {len(proposed)} above the bar"}  # fmt: skip
        if result.status == "skipped":
            raise JobSkipped("; ".join(result.notes), alert=result.budget.mode != "stopped")
        if result.status == "failed" or result.decision is None:
            raise JobFailed("; ".join(result.notes) or "the judge did not decide", details)
        return details

    def _copy_research_from_a(self, now: datetime) -> int:
        """B starts from A's agent reports of today (see research/reuse.py)."""
        from papertrader.research.reuse import ReuseRefused, copy_a_research

        try:
            copied = copy_a_research(
                self.runtime.engine, self.sessions, self.config, new_york_date(now)
            )
        except ReuseRefused as exc:
            raise JobFailed(str(exc)) from exc
        if copied is None:
            raise JobSkipped("system A has not finished today's research yet", alert=True)
        return copied[1]

    def job_morning_brief(self) -> dict[str, Any]:
        from papertrader.notify.reports import build_brief, open_trade_labels

        now = self.now()
        prices: dict[str, float] = {}
        try:
            with self.sessions() as session:
                held = [label.split()[0] for label in open_trade_labels(session)]
            broker = self._broker()
            if held and broker is not None:
                prices = broker.latest_prices(held)
        except Exception:
            prices = {}  # the brief is still useful without today's prices
        with self.sessions() as session:
            text = build_brief(session, self.config, now, prices)
            if text is None:
                text = (f"Morning brief · {new_york_date(now):%a %d %b}\n"
                        "No decision today: the research or the judge did not finish (see the alerts).")  # fmt: skip
            added = queue(session, "morning_brief", text, dedup_key=f"brief:{new_york_date(now)}")
            session.commit()
        return {"summary": "queued" if added else "already sent today"}

    def job_place_orders(self) -> dict[str, Any]:
        from papertrader.notify.reports import latest_daily_decision
        from papertrader.trading.engine import TradingRefused

        now = self.now()
        with self.trading_lock:
            engine = self._engine()
            sold = []
            with self.sessions() as session:
                decision = latest_daily_decision(session, new_york_date(now))
                exits = list((decision.payload or {}).get("exits", [])) if decision else []
                held = set(session.scalars(select(m.Trade.ticker).where(
                    m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False))))  # fmt: skip
            for item in exits:  # the judge's "positions to exit", before any new buying
                ticker = str(item.get("ticker", "")).upper()
                if ticker in held:
                    outcome = engine.exit_ticker(ticker, m.ExitTrigger.THESIS_BREAK,
                                                 "judge: " + str(item.get("reason", ""))[:300])  # fmt: skip
                    sold.append(f"{ticker}: {outcome}")
            try:
                placements = engine.place_orders()
            except TradingRefused as exc:
                raise JobSkipped(str(exc)) from exc
        rows = [f"{p.ticker} {p.status}: {p.detail}" for p in placements]
        submitted = sum(1 for p in placements if p.status == "submitted")
        return {"exits": sold, "placements": rows,
                "summary": f"{submitted} order(s) placed" + (f", {len(sold)} exit(s)" if sold else "")}  # fmt: skip

    def job_monitor(self) -> dict[str, Any]:
        from papertrader.trading.thesis import check_held_positions

        with self.trading_lock:
            engine = self._engine()
            report = engine.monitor()
            notes = asyncio.run(check_held_positions(
                config=self.config, settings=self.runtime.settings, sessions=self.sessions,
                engine=engine, claude=self.claude, now=self.now(), refresh_news=self.writes_market_data,
            ))  # fmt: skip
        return {"equity": report.equity, "day_change_pct": report.day_change_pct,
                "opened": report.sync.opened, "closed": report.sync.closed,
                "time_stops": report.time_stops, "kill_switch": report.kill_switch,
                "warnings": report.warnings, "notes": notes,
                "summary": f"equity ${report.equity:,.0f} ({report.day_change_pct:+.2%})"}  # fmt: skip

    def job_reconcile_eod(self) -> dict[str, Any]:
        with self.trading_lock:
            summary = self._engine().end_of_day()
        expired = list(summary.get("expired", []))
        return {"expired": expired, "summary": f"{len(expired)} unfilled order(s) cancelled"}

    def _resolve(self, *, refresh_prices: bool) -> dict[str, Any]:
        from papertrader.learning.outcomes import resolve_outcomes
        from papertrader.learning.stats import compute_stats

        details: dict[str, Any] = {}
        if (
            refresh_prices and self.writes_market_data
        ):  # today's closes, so today's horizons can be scored
            from papertrader.ingest.context import open_context
            from papertrader.ingest.pipeline import run_daily

            async def go() -> list[Any]:
                async with open_context(self.config, self.runtime.settings, self.sessions) as ctx:
                    return await run_daily(ctx, only=["bars"])

            try:
                details["prices"] = {r.name: r.status for r in asyncio.run(go())}
            except Exception as exc:
                details["prices"] = f"not refreshed ({type(exc).__name__})"
        now = self.now()
        with self.sessions() as session:
            report = resolve_outcomes(session, self.config, now)
            rows = compute_stats(session, self.config, new_york_date(now))
            session.commit()
        details |= {"resolved": report.resolved, "waiting": report.waiting, "no_prices": report.no_prices,
                    "replays_ignored": report.replays, "stat_rows": rows}  # fmt: skip
        details["summary"] = f"{report.resolved} calls scored, {report.waiting} still running"
        if self.system == "B":
            from papertrader.b.daily import score_shadow

            with self.sessions() as session:
                details["fable_scored"] = score_shadow(session, self.config, now)
                session.commit()
        return details

    def job_b_signals(self) -> dict[str, Any]:
        from papertrader.b.daily import run_signals

        details = run_signals(self.runtime.engine, self.sessions, self.config, self.runtime.settings,
                              self.now())  # fmt: skip
        details["summary"] = (f"{details['insider_clusters']} insider clusters, {details['options']} options, "
                              f"{details['analysts']} analyst signals")  # fmt: skip
        if details["problems"]:
            details["summary"] += "; problems: " + "; ".join(details["problems"])
        return details

    def job_fable_shadow(self) -> dict[str, Any]:
        from papertrader.b.daily import run_fable

        details = asyncio.run(
            run_fable(self.sessions, self.config, self.fable_claude(), self.now())
        )
        if "skipped" in details:
            raise JobSkipped(details["skipped"])
        details["summary"] = (
            ", ".join(f"{a}: {v}" for a, v in details["agents"].items()) or "no agents ran"
        )
        return details

    def job_resolve_outcomes(self) -> dict[str, Any]:
        details = self._resolve(refresh_prices=True)
        details["text_labels"] = self._collect_text_labels()
        return details

    def job_weekly(self) -> dict[str, Any]:
        from papertrader.learning.lessons import write_lessons
        from papertrader.learning.weights import update_weights
        from papertrader.model.pipeline import NotEnoughDataError, run_training
        from papertrader.notify.reports import build_weekly

        now = self.now()
        details: dict[str, Any] = {}
        problems: list[str] = []
        extra: list[str] = []

        def step(name: str, body: Callable[[], Any]) -> Any:
            try:
                return body()
            except Exception as exc:  # one step failing must not stop the others
                log.exception("weekly step failed", extra={"step": name})
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
                return None

        details["text_labels"] = self._collect_text_labels()
        details["outcomes"] = step("outcomes", lambda: self._resolve(refresh_prices=False))

        def weights() -> list[Any]:
            with self.sessions() as session:
                changes = update_weights(session, self.config, now)
                session.commit()
            return changes

        changes = step("weights", weights) or []
        details["weights"] = [f"{c.agent} {c.old:.2f}->{c.new:.2f} (n={c.n})" for c in changes]
        moved = [
            f"{c.agent} {c.old:.2f}->{c.new:.2f}" for c in changes if abs(c.new - c.old) >= 0.005
        ]
        extra.append("Agent weights: " + (", ".join(moved) if moved else "unchanged"))

        def retrain() -> str:
            if self.system != "A":
                return "skipped: only system A uses the pattern model"
            try:
                result = run_training(
                    self.sessions, self.config, as_of=now, data_dir=self.runtime.settings.data_dir
                )
            except NotEnoughDataError as exc:
                return f"not retrained: {exc}"
            return f"{result.version}: {result.report['promotion']}"

        details["retrain"] = step("retrain", retrain)
        extra.append(f"Retrain: {details['retrain'] or 'failed'}")

        def lessons() -> str:
            claude = self.claude()
            if claude is None:
                return "skipped: ANTHROPIC_API_KEY is not set"
            result = asyncio.run(write_lessons(self.sessions, self.config, claude, now))
            if result.status == "written":
                with self.sessions() as session:
                    row = session.get(m.Lesson, result.lesson_id)
                    summary = (row.payload or {}).get("summary", "") if row else ""
                extra.append(f"Lessons: {summary}")
            return f"{result.status}: {result.detail}"

        details["lessons"] = step("lessons", lessons)
        if problems:
            extra.append("Problems: " + "; ".join(problems))
        with self.sessions() as session:
            queue(session, "weekly", build_weekly(session, self.config, now, extra=extra),
                  dedup_key=f"weekly:{new_york_date(now)}")  # fmt: skip
            session.commit()
        details["summary"] = f"retrain: {details['retrain']}; lessons: {details['lessons']}"
        if problems:
            raise JobFailed("; ".join(problems), details)
        return details

    # ------------------------------------------------------------------ watchdog

    def expected_today(self, now: datetime) -> list[tuple[str, datetime]]:
        """Jobs that should have started by now today (past their time plus the grace period)."""
        day = new_york_date(now)
        grace = timedelta(minutes=self.config.schedule.heartbeat_grace_minutes)
        trading = self.trading_day(day)
        due = []
        for spec in self.specs:
            if spec.at is None:
                continue
            if spec.weekly:
                if WEEKDAYS[day.weekday()] != self.config.schedule.weekly_day:
                    continue
            elif trading is None:
                continue
            when = at_new_york(day, spec.at(self.config))
            if when + grace <= now:
                due.append((spec.name, when))
        return due

    def watchdog(self) -> list[str]:
        """Record and alert jobs that did not run today. Returns their names."""
        now = self.now()
        day = new_york_date(now)
        start, end = at_new_york(day, time(0)), at_new_york(day + timedelta(days=1), time(0))
        missed: list[tuple[str, datetime]] = []
        with self.sessions() as session:
            ran = set(session.scalars(select(m.JobRun.job_name).where(
                m.JobRun.scheduled_for >= start, m.JobRun.scheduled_for < end)))  # fmt: skip
            for name, when in self.expected_today(now):
                if name not in ran:
                    session.add(m.JobRun(job_name=name, scheduled_for=when, status=m.RunStatus.MISSED,
                                         error="did not run (app off, asleep or busy)"))  # fmt: skip
                    missed.append((name, when))
            stalled = self._monitor_stalled(session, now)
            backup = self._backup_problem(session, now)
            clock = self._clock_problem(now)
            for problem, key in ((backup, "backup"), (clock, "clock")):
                if problem:
                    queue(session, "alert", messages.alert(problem), dedup_key=f"{key}:{day}")
            if missed:
                listed = ", ".join(
                    f"{name} ({when.astimezone(NEW_YORK):%H:%M})" for name, when in missed
                )
                queue(session, "alert", messages.alert(
                    f"These jobs did not run today: {listed}. Run one by hand with: "
                    "docker compose run --rm app papertrader jobs run NAME"),
                    dedup_key=f"jobs-missed:{day}:{','.join(n for n, _ in missed)}")  # fmt: skip
            if stalled:
                queue(session, "alert", messages.alert(stalled),
                      dedup_key=f"monitor-stalled:{now.astimezone(NEW_YORK):%Y-%m-%dT%H}")  # fmt: skip
            session.commit()
        flags = [("monitor", stalled), ("backup", backup), ("clock", clock)]
        return [name for name, _ in missed] + [name for name, problem in flags if problem]

    BACKUP_MAX_AGE = timedelta(hours=36)

    def _backup_problem(self, session: Any, now: datetime) -> str | None:
        """The backup service writes one database dump a day; say so if they stopped."""
        folder = self.runtime.settings.backups_dir
        if folder is None or not folder.is_dir():
            return None
        newest = max((p.stat().st_mtime for p in folder.glob("papertrader-*.dump")), default=None)
        if newest is None:
            first = session.scalar(select(func.min(m.JobRun.started_at)))
            if first is None or now - first < timedelta(hours=48):
                return None  # a new installation: the first backup comes at night
            return "No database backup has been made yet. Check the backup service: docker compose logs backup"
        age = now - datetime.fromtimestamp(newest, tz=UTC)
        if age <= self.BACKUP_MAX_AGE:
            return None
        return (f"The newest database backup is {age.total_seconds() / 3600:.0f} hours old. "
                "Check the backup service: docker compose logs backup")  # fmt: skip

    CLOCK_TOLERANCE_SECONDS = 60.0

    def clock_offset(self) -> float | None:
        """Seconds this computer's clock is ahead (+) or behind (-) Alpaca's; None if unknown."""
        try:
            broker = self._broker()
            return (
                None if broker is None else (self.now() - broker.clock().timestamp).total_seconds()
            )
        except Exception:
            return None

    def _clock_problem(self, now: datetime) -> str | None:
        """At most once an hour: a wrong clock means jobs and time stops at the wrong time."""
        if self._clock_checked is not None and now - self._clock_checked < timedelta(hours=1):
            return None
        self._clock_checked = now
        offset = self.clock_offset()
        if offset is None or abs(offset) <= self.CLOCK_TOLERANCE_SECONDS:
            return None
        return (f"This computer's clock is {abs(offset):.0f} seconds {'ahead of' if offset > 0 else 'behind'} "
                "the market's. Jobs and time stops will run at the wrong moment. Restart Docker Desktop "
                "(after sleep its clock can drift) and check Windows: Settings > Time > Sync now.")  # fmt: skip

    def _monitor_stalled(self, session: Any, now: datetime) -> str | None:
        trading = self.trading_day(new_york_date(now))
        if not self._monitor_due(now, trading):
            return None
        assert trading is not None
        interval = timedelta(minutes=self.config.schedule.monitor_interval_minutes)
        grace = timedelta(minutes=self.config.schedule.heartbeat_grace_minutes)
        first = max(trading.open, at_new_york(trading.day, self.config.schedule.place_orders))
        if now < first + interval + grace:
            return None
        last = session.scalar(select(m.JobRun.started_at).where(m.JobRun.job_name == "monitor")
                              .order_by(m.JobRun.started_at.desc()).limit(1))  # fmt: skip
        if last is not None and now - last <= 2 * interval + grace:
            return None
        since = f"since {last.astimezone(NEW_YORK):%H:%M}" if last else "today"
        return (f"The position monitor has not run {since}. Stops and targets still work at "
                "Alpaca; time stops and thesis checks are waiting.")  # fmt: skip

    def startup_check(self) -> str | None:
        """When the app starts: say how long it was off, if it was off for a while."""
        now = self.now()
        with self.sessions() as session:
            beat = get_state(session, HEARTBEAT_KEY) or {}
            if not beat.get("at"):
                return None
            last = datetime.fromisoformat(beat["at"])
            gap = now - last
            if gap < timedelta(minutes=self.config.schedule.heartbeat_grace_minutes):
                return None
            text = (f"The app was not running from {last.astimezone(NEW_YORK):%a %d %b %H:%M} to "
                    f"{now.astimezone(NEW_YORK):%a %H:%M} New York time ({_duration(gap)}).")  # fmt: skip
            queue(session, "alert", messages.alert(text), dedup_key=f"app-down:{last.isoformat()}")
            session.commit()
        return text


def _duration(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
