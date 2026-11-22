"""X2b: the intraday scanner, from cheap rules to a scored shadow plan or a placed trade."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from papertrader.b import scanner as sc
from papertrader.b.live import Candle, Quote
from papertrader.b.models import IntradayPlan
from papertrader.config import IntradaySwitch, get_settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.hub import HubBot, SystemView
from papertrader.ingest.timeutil import NEW_YORK
from papertrader.scheduler.jobs import specs_for
from papertrader.trading.broker import Account
from papertrader.trading.engine import TradingEngine

from ..notify.helpers import CHAT, FakeTelegram
from ..research.helpers import FakeClaude, make_claude, seed_market
from ..trading.helpers import FakeBroker
from .helpers import CFG, b_config

MONDAY = date(2026, 11, 16)
NOW = datetime(2026, 11, 16, 16, 0, tzinfo=UTC)  # 11:00 New York


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


class FakeLive:
    def __init__(self) -> None:
        self.prices: dict[str, Quote] = {}
        self.minutes: dict[str, list[Candle]] = {}
        self.asked: list[tuple[str, bool]] = []

    def quotes(self, tickers: Sequence[str]) -> dict[str, Quote]:
        return {t: q for t, q in self.prices.items() if t in tickers}

    def candles(self, tickers: Sequence[str], timeframe: str, start: datetime, end: datetime,
                *, full_market: bool = False) -> dict[str, list[Candle]]:  # fmt: skip
        self.asked.append((timeframe, full_market))
        return {t: [c for c in self.minutes.get(t, []) if start <= c.at < end] for t in tickers}

    def headlines(
        self, tickers: Sequence[str], since: datetime, until: datetime
    ) -> dict[str, list[str]]:
        return {"AAPL": ["2026-11-16 13:05 UTC: Apple wins a large government contract"]}


def quote(ticker: str, price: float, prev_close: float, *, volume: float = 1e6, prev_volume: float = 1e6,
          high: float | None = None, low: float | None = None, day: date = MONDAY) -> Quote:  # fmt: skip
    return Quote(ticker, price, NOW, day.isoformat(), prev_close, high or max(price, prev_close) + 0.1,
                 low or min(price, prev_close) - 0.1, volume, prev_close, prev_volume)  # fmt: skip


def facts(**changes: float) -> sc.DayFacts:
    base: dict[str, Any] = {"ticker": "AAPL", "sector": "Information Technology", "last_close": 100.0,
                            "atr_pct": 0.02, "high20": 101.0, "sma50": 95.0, "ret20": 0.05}  # fmt: skip
    return sc.DayFacts(**(base | changes))


# --------------------------------------------------------------------------- the pieces


def test_the_cheap_rules() -> None:
    cfg = IntradaySwitch()
    f = {"AAPL": facts(), "MSFT": facts(ticker="MSFT", high20=110.0), "XOM": facts(ticker="XOM"),
         "JPM": facts(ticker="JPM"),
         "PFE": facts(ticker="PFE", sma50=110.0, ret20=-0.05)}  # fmt: skip
    quotes = {
        "AAPL": quote(
            "AAPL", 102.0, 100.0, volume=1e6
        ),  # above the 20-day high on 3x yesterday's pace
        "MSFT": quote(
            "MSFT", 102.5, 100.0, high=102.6, low=100.0, volume=1e6
        ),  # big move, near the high
        "XOM": quote(
            "XOM", 97.0, 100.0, high=100.1, low=95.0, volume=2e5
        ),  # a dip in an uptrend, bouncing
        "JPM": quote("JPM", 100.2, 100.0, volume=1e6),  # nothing going on
        "PFE": quote("PFE", 96.5, 100.0, high=100.1, low=95.0),  # a dip, but in a downtrend
    }
    found = {c.ticker: c.rule for c in sc.find_candidates(quotes, f, NOW, cfg, exclude=set())}
    assert found == {"AAPL": "breakout", "MSFT": "momentum", "XOM": "pullback"}
    assert sc.find_candidates(quotes, f, NOW, cfg, exclude={"AAPL", "MSFT", "XOM"}) == []
    stale = {
        "AAPL": quote("AAPL", 102.0, 100.0, day=date(2026, 11, 13))
    }  # yesterday's bar: market not open yet
    assert sc.find_candidates(stale, f, NOW, cfg, exclude=set()) == []


def decision(price: float, **changes: Any) -> sc.DecideOutput:
    base = {"trade": True, "entry_low": price * 0.998, "entry_high": price * 1.001, "target_price": price + 6,
            "stop_price": price - 3, "expected_hold_minutes": 120, "confidence": 0.65, "why": "w",
            "invalidation": "i"}  # fmt: skip
    return sc.DecideOutput.model_validate(base | changes)


def test_the_plan_checks() -> None:
    cfg, f = IntradaySwitch(), facts()  # a typical daily range of $2 at $100
    assert sc.check_plan(decision(100.0), 100.0, f, cfg) is None
    assert "passed" in (sc.check_plan(decision(100.0, trade=False), 100.0, f, cfg) or "")
    assert "below 0.60" in (sc.check_plan(decision(100.0, confidence=0.55), 100.0, f, cfg) or "")
    assert "not close to the price" in (
        sc.check_plan(decision(100.0, entry_low=97.0), 100.0, f, cfg) or ""
    )
    assert "reward is only" in (
        sc.check_plan(decision(100.0, target_price=103.0), 100.0, f, cfg) or ""
    )
    assert "normal noise" in (
        sc.check_plan(decision(100.0, stop_price=99.6, target_price=101.5), 100.0, f, cfg) or ""
    )
    assert "out of range" in (
        sc.check_plan(decision(100.0, expected_hold_minutes=5), 100.0, f, cfg) or ""
    )


def test_holding_time_counts_market_minutes_only() -> None:
    friday_3pm = datetime(2026, 11, 20, 15, 0, tzinfo=NEW_YORK)
    assert sc.add_market_minutes(friday_3pm, 30, 15) == datetime(
        2026, 11, 20, 15, 30, tzinfo=NEW_YORK
    )
    # 45 minutes left on Friday (until 15:45), the rest on Monday from the open
    assert sc.add_market_minutes(friday_3pm, 60, 15) == datetime(
        2026, 11, 23, 9, 45, tzinfo=NEW_YORK
    )


def candle(minute: int, o: float, h: float, lo: float, c: float, *, day: date = MONDAY) -> Candle:
    return Candle(
        datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC) + timedelta(minutes=minute),
        o,
        h,
        lo,
        c,
        1000,
    )


def plan_at(**changes: Any) -> IntradayPlan:
    base = {"day": MONDAY, "created_at": NOW, "mode": "shadow", "status": "shadow", "ticker": "AAPL",
            "rule": "breakout", "price_at_decision": 100.0, "entry_low": 99.8, "entry_high": 100.1,
            "target_price": 106.0, "stop_price": 97.0, "size_pct": 0.1, "confidence": 0.65,
            "expected_hold_minutes": 120, "time_stop_at": NOW + timedelta(hours=3),
            "place_after": NOW + timedelta(minutes=5), "why": "w"}  # fmt: skip
    return IntradayPlan(**(base | changes))


def test_the_shadow_simulation() -> None:
    plan = plan_at()
    start = 95  # NOW + 5 minutes = 11:05 New York = minute 95 after 09:30
    hits_target = [candle(start, 100.0, 100.3, 99.9, 100.2), candle(start + 1, 100.2, 104.0, 100.1, 103.0),
                   candle(start + 2, 103.0, 106.5, 102.9, 106.2)]  # fmt: skip
    result = sc.simulate(plan, hits_target, cost_bps=10)
    assert (result.entry_price, result.exit_price, result.reason) == (100.0, 106.0, "target")
    assert result.net == pytest.approx(106 / 100 - 1 - 0.001)
    both = [
        hits_target[0],
        candle(start + 1, 100.0, 106.5, 96.0, 101.0),
    ]  # stop and target in one minute
    assert sc.simulate(plan, both, 10).reason == "stop"  # the cautious assumption
    moved = [candle(start, 101.5, 102.0, 101.0, 101.8)]  # the price left the zone while we waited
    assert sc.simulate(plan, moved, 10).reason == "no_fill"
    quiet = [hits_target[0], candle(start + 1, 100.2, 100.6, 100.0, 100.4)]
    timed = sc.simulate(plan, quiet, 10)
    assert timed.reason == "time" and timed.exit_price == 100.4


# --------------------------------------------------------------------------- a whole scan


def market_with_a_breakout(engine: Engine) -> tuple[FakeLive, float]:
    seed_market(engine)
    with make_session_factory(engine)() as session:
        f = sc.day_facts(session, CFG, MONDAY)["AAPL"]
    live = FakeLive()
    price = round(f.high20 * 1.01, 2)
    live.prices = {"AAPL": quote("AAPL", price, f.last_close, volume=3e6, prev_volume=1e6),
                   "MSFT": quote("MSFT", 160.0, 160.0), "SPY": quote("SPY", 400.0, 399.0)}  # fmt: skip
    return live, price


def answer_decide(price: float) -> Any:
    def answer(call: Any) -> dict[str, Any]:
        assert (
            "Why it was flagged: above its 20-day high" in call.user and "Apple wins" in call.user
        )
        return decision(
            price, target_price=round(price + 10, 2), stop_price=round(price - 4.5, 2)
        ).model_dump()

    return answer


def run_scan(
    b_engine: Engine, live: FakeLive, fake: FakeClaude, config: Any, at: datetime = NOW
) -> sc.ScanReport:
    claude = make_claude(b_engine, fake, config=config, now=lambda: at)
    scanner = sc.Scanner(
        config, make_session_factory(b_engine), live, lambda: claude, equity=lambda: 25_000.0
    )
    return asyncio.run(scanner.scan(at))


def test_a_shadow_scan_from_rules_to_alert_to_score(engine: Engine, b_engine: Engine) -> None:
    live, price = market_with_a_breakout(engine)
    fake = FakeClaude()
    fake.responses["intraday_screen"] = {
        "picks": [{"ticker": "AAPL", "look_closer": True, "reason": "news"}]
    }
    fake.responses["intraday_decide"] = answer_decide(price)
    config = b_config(intraday={"enabled": True})
    report = run_scan(b_engine, live, fake, config)
    assert report.candidates == 1 and report.planned == ["AAPL"], report.details()
    assert fake.labels() == ["intraday_screen", "intraday_decide"]
    sessions = make_session_factory(b_engine)
    with sessions() as session:
        plan = session.scalars(select(IntradayPlan)).one()
        message = session.scalars(select(m.Notification)).one().text
        purposes = sorted(u.purpose for u in session.scalars(select(m.ApiUsage)))
    assert plan.status == "shadow" and plan.mode == "shadow" and plan.rule == "breakout"
    assert plan.size_pct == pytest.approx(
        min(
            0.01
            * ((plan.entry_low + plan.entry_high) / 2)
            / ((plan.entry_low + plan.entry_high) / 2 - plan.stop_price),
            0.10,
        ),
        abs=1e-4,
    )
    assert message.startswith("SHADOW (no order) · WOULD BUY AAPL (intraday scanner)")
    assert "Expected hold: about 2 hours" in message and "Shadow mode: nothing is placed" in message
    assert purposes == ["intraday_decide", "intraday_screen"]
    assert (
        run_scan(b_engine, live, fake, config).candidates == 0
    )  # planned today: not looked at again

    # After the close: scored on the full market's minute bars, and the live-feed gap measured.
    start = int(
        (plan.place_after - datetime(2026, 11, 16, 14, 30, tzinfo=UTC)).total_seconds() // 60
    )
    live.minutes["AAPL"] = [candle(start - 5, price, price, price, price + 0.02),
                            candle(start, plan.entry_high, plan.entry_high + 0.1, plan.entry_low, plan.entry_high),
                            candle(start + 30, price + 2, plan.target_price + 0.5, price + 1, plan.target_price)]  # fmt: skip
    assert sc.score_plans(sessions, config, live, plan.time_stop_at + timedelta(minutes=20)) == 1
    with sessions() as session:
        done = session.scalars(select(IntradayPlan)).one()
        record = sc.results(session, MONDAY)
    assert (done.status, done.exit_reason) == ("done", "target") and (done.net_return_pct or 0) > 0
    assert done.feed_gap_bps == pytest.approx((price / (price + 0.02) - 1) * 10_000, abs=0.01)
    assert ("1Min", True) in live.asked
    assert record["shadow"]["wins"] == 1 and record["live"]["plans"] == 0


def test_the_limits_stop_the_scanner_before_it_spends(engine: Engine, b_engine: Engine) -> None:
    live, _ = market_with_a_breakout(engine)
    fake = FakeClaude()
    fake.responses["intraday_screen"] = {
        "picks": [{"ticker": "AAPL", "look_closer": False, "reason": "priced in"}]
    }
    report = run_scan(b_engine, live, fake, b_config(intraday={"enabled": True}))
    assert report.planned == [] and report.passed == {"AAPL": "screen: priced in"}
    assert fake.labels() == ["intraday_screen"]  # passed at the cheap screen: no decision paid for
    capped = run_scan(
        b_engine, live, fake, b_config(intraday={"enabled": True, "max_calls_per_day": 1})
    )
    assert capped.note == "today's Claude call limit is reached" and len(fake.calls) == 1
    early = run_scan(b_engine, live, fake, b_config(intraday={"enabled": True}),
                     at=datetime(2026, 11, 16, 14, 35, tzinfo=UTC))  # fmt: skip
    assert early.note == "outside the scanning window"


# --------------------------------------------------------------------------- live mode


def test_live_mode_waits_for_skip_then_places_and_time_stops(
    engine: Engine, b_engine: Engine
) -> None:
    live, price = market_with_a_breakout(engine)
    fake = FakeClaude()
    fake.responses["intraday_screen"] = {
        "picks": [{"ticker": "AAPL", "look_closer": True, "reason": "news"}]
    }
    fake.responses["intraday_decide"] = answer_decide(price)
    config = b_config(intraday={"enabled": True, "mode": "live"})
    run_scan(b_engine, live, fake, config)
    sessions = make_session_factory(b_engine)
    with sessions() as session:
        plan = session.scalars(select(IntradayPlan)).one()
        assert plan.status == "planned"
        note = session.scalars(select(m.Notification)).one()
        alert = note.text
    # X6: a heads-up with Skip / Go now buttons (the hub adds them), not a plain message
    assert note.kind == "order_warning" and alert.startswith("HEADS-UP · BUY AAPL at ")
    assert " shares · limit $" in alert and "No answer = it goes ahead" in alert

    clock = [NOW + timedelta(minutes=2)]
    broker = FakeBroker(clock, equity=25_000.0)
    broker.prices = {"AAPL": price, "SPY": 400.0}
    engine_b = TradingEngine(sessions, broker, config, now=lambda: clock[0], sleep=lambda _s: None)
    assert sc.place_due(sessions, config, engine_b, clock[0]) == []  # still inside the /skip window
    clock[0] = plan.place_after + timedelta(minutes=1)
    assert sc.place_due(sessions, config, engine_b, clock[0]) == ["AAPL: placed"]
    with sessions() as session:
        placed = session.scalars(select(IntradayPlan)).one()
        idea = session.get(m.TradeIdea, placed.idea_id)
        assert (
            placed.status == "placed"
            and idea is not None
            and idea.rationale.startswith("[intraday scanner]")
        )
    order = next(o for o in broker.orders.values() if o.legs)
    assert order.limit_price == pytest.approx(placed.entry_high) and order.qty == placed.qty

    broker.fill_entry("AAPL", placed.entry_high)
    engine_b.sync()
    clock[0] = placed.time_stop_at + timedelta(minutes=1)
    assert sc.manage_open(sessions, engine_b, clock[0])[0].startswith("AAPL: time stop")
    broker.fill_market("AAPL", placed.entry_high + 1)
    engine_b.sync()
    sc.manage_open(sessions, engine_b, clock[0])
    with sessions() as session:
        finished = session.scalars(select(IntradayPlan)).one()
    assert (
        finished.status == "done"
        and finished.exit_reason == "time"
        and (finished.net_return_pct or 0) > 0
    )


class DayTrader(FakeBroker):
    """An account under $25,000 that has used its three day trades."""

    def account(self) -> Account:
        return replace(super().account(), daytrade_count=3)


def test_skip_refusals_and_the_day_trading_rule(engine: Engine, b_engine: Engine) -> None:
    seed_market(engine)
    sessions = make_session_factory(b_engine)
    config = b_config(intraday={"enabled": True, "mode": "live"})
    with sessions() as session:
        for ticker in ("AAPL", "MSFT", "JPM"):
            session.add(plan_at(ticker=ticker, mode="live", status="planned", place_after=NOW))
        session.commit()
    views = {"B": SystemView("B", config, b_engine, sessions)}
    bot = HubBot(views, FakeTelegram().client(), CHAT, get_settings(), telegram=CFG.telegram,  # type: ignore[arg-type]
                 brokers={"B": lambda: None}, now=lambda: NOW)  # fmt: skip
    assert bot.handle("/skip aapl") == "[B] Skipped AAPL: it will not be bought."
    assert (
        bot.handle("/skip C") == "[B] There is no planned trade in C today."
    )  # C is a ticker here
    assert bot.handle("/skip AAPL") == "[B] Too late for AAPL: the plan is already skipped."
    assert bot.handle("/skip") == "Say which stock: /skip NVDA"

    clock = [NOW + timedelta(minutes=1)]
    broker = DayTrader(clock, equity=24_000.0)
    broker.prices = {"MSFT": 500.0, "JPM": 100.0}  # MSFT has run away from its zone
    engine_b = TradingEngine(sessions, broker, config, now=lambda: clock[0], sleep=lambda _s: None)
    notes = sc.place_due(sessions, config, engine_b, clock[0])
    assert notes == ["MSFT: the price (500.0) has left the entry zone",
                     "JPM: US day-trading rule: 3 day trades in 5 days on an account under $25,000"]  # fmt: skip
    assert not broker.orders
    with sessions() as session:
        texts = [n.text for n in session.scalars(select(m.Notification))]
    assert any(t.startswith("NOT PLACED · MSFT") for t in texts)


def test_the_scanner_job_runs_only_when_switched_on() -> None:
    assert "intraday" not in [s.name for s in specs_for("B", CFG)]
    on = b_config(intraday={"enabled": True})
    spec = next(s for s in specs_for("B", on) if s.name == "intraday")
    assert spec.at is None and spec.every is not None and spec.every(on) == 5
    assert "intraday" not in [s.name for s in specs_for("A")]


def test_alpaca_replies_are_read_or_left_out() -> None:
    from papertrader.b.live import parse_candles, parse_snapshot

    raw = {"latestTrade": {"t": "2026-11-16T16:00:01Z", "p": 101.5},
           "dailyBar": {"t": "2026-11-16T05:00:00Z", "o": 100.5, "h": 102.0, "l": 100.1, "c": 101.4, "v": 51234},
           "prevDailyBar": {"t": "2026-11-13T05:00:00Z", "o": 99, "h": 100.5, "l": 98.8, "c": 100.0, "v": 80000}}  # fmt: skip
    q = parse_snapshot("AAPL", raw)
    assert q is not None and (q.price, q.day, q.prev_close, q.volume) == (
        101.5,
        "2026-11-16",
        100.0,
        51234,
    )
    assert (
        parse_snapshot("AAPL", {"latestTrade": {"p": 1}}) is None
    )  # no bars yet: skipped, not guessed
    bars = parse_candles(
        [{"t": "2026-11-16T14:30:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}, {"o": 1}]
    )
    assert len(bars) == 1 and bars[0].close == 1.5
