from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.db.state import KILL_SWITCH_KEY, get_state
from papertrader.ingest.timeutil import at_new_york
from papertrader.trading.broker import TradingDay
from papertrader.trading.engine import TradingRefused, entry_client_id, horizon_end

from ..ingest.helpers import shipped_config
from .helpers import MONDAY, OPEN_TIME, idea, make_engine, seed_decision


def rows(engine: Engine, model: type, *where: object) -> list:  # type: ignore[type-arg]
    with make_session_factory(engine)() as session:
        return list(session.scalars(select(model).where(*where)))  # type: ignore[arg-type]


def notes(engine: Engine) -> list[str]:
    return [n.text for n in rows(engine, m.Notification)]


# ----------------------------------------------------------------------------- the time stop date


def test_time_stop_counts_trading_days_and_respects_holidays_and_half_days() -> None:
    from .helpers import FakeBroker

    days = FakeBroker([OPEN_TIME]).calendar(MONDAY, MONDAY + timedelta(days=30))
    entered = at_new_york(MONDAY, time(9, 36))
    # Five trading days: Mon 16 .. Fri 20, 15 minutes before the close.
    assert horizon_end(days, entered, 5, 15) == at_new_york(
        MONDAY + timedelta(days=4), time(15, 45)
    )
    # From Tue 24: Tue, Wed, (Thanksgiving skipped), Fri 27 is a half day closing at 13:00.
    tuesday = at_new_york(MONDAY + timedelta(days=8), time(10, 0))
    assert horizon_end(days, tuesday, 3, 15) == at_new_york(
        MONDAY + timedelta(days=11), time(12, 45)
    )
    # A fill after the close counts from the next session.
    late = at_new_york(MONDAY, time(16, 30))
    assert horizon_end(days, late, 1, 15) == at_new_york(MONDAY + timedelta(days=1), time(15, 45))
    assert horizon_end([], entered, 2, 15) == entered + timedelta(days=2)


# ----------------------------------------------------------------------------- placing


def test_orders_are_placed_as_brackets_and_never_twice(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices.update({"AAPL": 200.0, "JPM": 150.0})
    ids = seed_decision(engine, [idea("AAPL", 200.0), idea("JPM", 150.0)])

    results = trading.place_orders()
    assert [(r.ticker, r.status, r.qty) for r in results] == [
        ("AAPL", "submitted", 49),
        ("JPM", "submitted", 66),
    ]
    parent = next(o for o in broker.orders.values() if o.client_order_id == entry_client_id(ids[0]))
    assert (parent.order_class, parent.time_in_force, parent.limit_price) == (
        "bracket",
        "gtc",
        200.6,
    )
    assert [(leg.order_type, leg.limit_price or leg.stop_price) for leg in parent.legs] == [
        ("limit", 212.0),
        ("stop", 194.0),
    ]

    orders = rows(engine, m.Order)
    assert sorted((o.ticker, o.leg) for o in orders) == sorted(
        [
            (t, leg)
            for t in ("AAPL", "JPM")
            for leg in (m.OrderLeg.ENTRY, m.OrderLeg.TAKE_PROFIT, m.OrderLeg.STOP_LOSS)
        ]
    )
    assert {i.status for i in rows(engine, m.TradeIdea)} == {m.IdeaStatus.SUBMITTED}

    assert trading.place_orders() == []  # nothing left to place, and nothing doubled
    assert len([o for o in broker.orders.values() if o.legs]) == 2


def test_dry_runs_and_old_decisions_are_never_traded(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices["AAPL"] = 200.0
    seed_decision(engine, [idea("AAPL", 200.0)], kind=m.CycleKind.DRY_RUN)
    seed_decision(engine, [idea("AAPL", 200.0)], as_of=OPEN_TIME - timedelta(days=3))
    assert trading.place_orders() == []
    assert broker.orders == {}


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        (lambda t, b, c: setattr(b, "is_open", False), "market is closed"),
        (
            lambda t, b, c: c.__setitem__(0, at_new_york(MONDAY, time(9, 32))),
            "only placed from 09:35",
        ),
        (lambda t, b, c: t.pause("testing"), r"paused \(testing\)"),
    ],
)
def test_orders_wait_for_the_market_and_for_a_resume(engine: Engine, setup, message: str) -> None:  # type: ignore[no-untyped-def]
    trading, broker, clock = make_engine(engine)
    broker.prices["AAPL"] = 200.0
    seed_decision(engine, [idea("AAPL", 200.0)])
    setup(trading, broker, clock)
    with pytest.raises(TradingRefused, match=message):
        trading.place_orders()
    assert broker.orders == {}


def test_risk_limits_apply_across_the_whole_list(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices.update({"AAPL": 200.0, "MSFT": 400.0, "NVDA": 100.0, "XOM": 110.0})
    seed_decision(engine, [idea("AAPL", 200.0), idea("MSFT", 400.0), idea("NVDA", 100.0),
                           idea("XOM", 110.0, action=m.TradeAction.SELL_SHORT)])  # fmt: skip
    broker.prices["NVDA"] = 120.0  # it ran away before the open
    results = {r.ticker: r for r in trading.place_orders()}
    assert results["AAPL"].status == results["MSFT"].status == "submitted"
    assert "run above the entry zone" in results["NVDA"].detail
    assert "short selling is disabled" in results["XOM"].detail
    status = {i.ticker: (i.status, i.status_reason) for i in rows(engine, m.TradeIdea)}
    assert status["NVDA"][0] == m.IdeaStatus.REJECTED_BY_RISK


def test_a_broker_refusal_is_recorded_not_fatal(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices.update({"AAPL": 200.0, "JPM": 150.0})
    ids = seed_decision(engine, [idea("AAPL", 200.0), idea("JPM", 150.0)])
    broker._new(client_order_id=entry_client_id(ids[0]), symbol="AAPL", side="buy", order_type="limit",
                order_class="bracket", time_in_force="gtc", qty=1, status="new")  # an old order with that id  # fmt: skip
    results = {r.ticker: r for r in trading.place_orders()}
    assert results["AAPL"].status == "failed" and "unique" in results["AAPL"].detail
    assert results["JPM"].status == "submitted"


# ----------------------------------------------------------------------------- fills and exits


def test_entry_fill_opens_a_trade_and_a_target_fill_closes_it(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices.update({"NVDA": 142.0})
    seed_decision(engine, [idea("NVDA", 142.0, final_confidence=0.82, rationale="Strong data-centre demand")],
                  reports=[("NVDA", "earnings", m.Stance.SUPPORT), ("NVDA", "contrarian", m.Stance.OPPOSE)])  # fmt: skip
    trading.place_orders()
    clock[0] += timedelta(minutes=1)
    broker.fill_entry("NVDA", 142.10)

    report = trading.sync()
    assert report.opened == ["NVDA"]
    (trade,) = rows(engine, m.Trade)
    assert (trade.qty, trade.entry_price, trade.status, trade.benchmark_entry_price) == (
        70,
        142.10,
        m.TradeStatus.OPEN,
        600.0,
    )
    assert trade.horizon_ends_at == at_new_york(MONDAY + timedelta(days=4), time(15, 45))
    assert trade.sector == "Information Technology" and not trade.is_test
    assert notes(engine)[-1] == "BOUGHT 70 NVDA @ $142.10 · conf 82% · Strong data-centre demand"
    assert rows(engine, m.TradeIdea)[0].status == m.IdeaStatus.EXECUTED
    assert trading.sync().opened == []  # a second sync changes nothing

    clock[0] += timedelta(days=2)
    broker.prices.update({"NVDA": 150.6, "SPY": 606.0})
    broker.hit("NVDA", "target", 150.6)
    assert trading.sync().closed == ["NVDA"]
    (trade,) = rows(engine, m.Trade)
    assert trade.status == m.TradeStatus.CLOSED and trade.exit_trigger == m.ExitTrigger.TARGET
    assert trade.pnl_usd == pytest.approx((150.6 - 142.10) * 70)
    assert trade.pnl_pct == pytest.approx(150.6 / 142.10 - 1)
    assert trade.benchmark_return_pct == pytest.approx(0.01)
    assert trade.excess_return_pct == pytest.approx(150.6 / 142.10 - 1 - 0.01)
    closing = notes(engine)[-1]
    assert closing.startswith("CLOSED NVDA @ $150.60 (target)")
    assert "P&L $595.00 (+6.0%), vs SPY +5.0% · held 2.0 days" in closing
    assert "Right: earnings · Wrong: contrarian" in closing
    fills = rows(engine, m.Fill)
    assert sorted((f.side, f.qty, f.price) for f in fills) == [
        (m.OrderSide.BUY, 70, 142.10),
        (m.OrderSide.SELL, 70, 150.6),
    ]
    stop_leg = next(o for o in rows(engine, m.Order) if o.leg == m.OrderLeg.STOP_LOSS)
    assert stop_leg.status == "canceled" and stop_leg.trade_id == trade.id


def test_a_stop_loss_fill_is_recorded_as_a_stop(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices["JPM"] = 150.0
    seed_decision(engine, [idea("JPM", 150.0)], reports=[("JPM", "news_event", m.Stance.SUPPORT)])
    trading.place_orders()
    broker.fill_entry("JPM", 150.0)
    trading.sync()
    broker.hit("JPM", "stop", 145.5)
    trading.sync()
    (trade,) = rows(engine, m.Trade)
    assert trade.exit_trigger == m.ExitTrigger.STOP and trade.pnl_usd < 0
    assert "Right: none · Wrong: news_event" in notes(engine)[-1]


def test_unfilled_entries_are_cancelled_at_the_end_of_the_day(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices.update({"AAPL": 200.0, "JPM": 150.0})
    seed_decision(engine, [idea("AAPL", 200.0), idea("JPM", 150.0)])
    trading.place_orders()
    broker.fill_entry("JPM", 150.0)
    clock[0] = at_new_york(MONDAY, time(16, 15))
    summary = trading.end_of_day()
    assert summary["expired"] == ["AAPL"]
    ideas = {i.ticker: i.status for i in rows(engine, m.TradeIdea)}
    assert ideas == {"AAPL": m.IdeaStatus.EXPIRED, "JPM": m.IdeaStatus.EXECUTED}
    assert [t.ticker for t in rows(engine, m.Trade)] == ["JPM"]  # the filled one is untouched
    assert any(text.startswith("End of day Mon 16 Nov") for text in notes(engine))
    snapshot = rows(engine, m.EquitySnapshot, m.EquitySnapshot.kind == "eod")
    assert len(snapshot) == 1 and snapshot[0].benchmark_close == 600.0


def test_a_partly_filled_entry_becomes_a_smaller_trade(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices["AAPL"] = 200.0
    seed_decision(engine, [idea("AAPL", 200.0)])
    trading.place_orders()
    broker.fill_entry("AAPL", 200.2, qty=20)
    assert trading.sync().opened == []  # still working
    clock[0] = at_new_york(MONDAY, time(16, 15))
    broker.holdings["AAPL"] = __import__("tests.trading.helpers", fromlist=["Holding"]).Holding(
        20, 200.2
    )
    trading.end_of_day()
    (trade,) = rows(engine, m.Trade)
    assert (trade.qty, trade.entry_price) == (20, 200.2)


def test_the_time_stop_sells_at_the_end_of_the_horizon(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices["XOM"] = 110.0
    seed_decision(engine, [idea("XOM", 110.0, horizon_days=2)])
    trading.place_orders()
    broker.fill_entry("XOM", 110.0)
    trading.monitor()
    (trade,) = rows(engine, m.Trade)
    assert trade.horizon_ends_at == at_new_york(MONDAY + timedelta(days=1), time(15, 45))

    clock[0] = at_new_york(MONDAY + timedelta(days=1), time(15, 40))
    assert trading.monitor().time_stops == []  # not yet
    clock[0] = at_new_york(MONDAY + timedelta(days=1), time(15, 50))
    broker.refuse_market = 1  # right after the cancel the shares are briefly still held
    report = trading.monitor()
    assert report.time_stops == ["XOM"]
    legs = [
        o for o in rows(engine, m.Order) if o.leg in (m.OrderLeg.TAKE_PROFIT, m.OrderLeg.STOP_LOSS)
    ]
    assert {o.status for o in legs} == {"canceled"}
    assert (
        notes(engine)[-1] == "SELL XOM · holding period over · the planned holding period is over"
    )

    broker.fill_market("XOM", 111.0)
    trading.sync()
    (trade,) = rows(engine, m.Trade)
    assert trade.status == m.TradeStatus.CLOSED and trade.exit_trigger == m.ExitTrigger.TIME
    assert trading.time_stops() == []  # never twice


def test_manual_close_and_its_safeguards(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices["AAPL"] = 200.0
    seed_decision(engine, [idea("AAPL", 200.0)])
    trading.place_orders()
    broker.fill_entry("AAPL", 200.0)
    trading.sync()
    assert trading.exit_ticker("MSFT", m.ExitTrigger.MANUAL, "x") == "no open trade in MSFT"
    assert trading.exit_ticker("aapl", m.ExitTrigger.MANUAL, "by hand") == "exit order placed"
    assert (
        trading.exit_ticker("AAPL", m.ExitTrigger.MANUAL, "again")
        == "an exit order is already working"
    )
    broker.fill_market("AAPL", 201.0)
    trading.sync()
    assert trading.exit_ticker("AAPL", m.ExitTrigger.MANUAL, "x") == "no open trade in AAPL"
    signal = rows(engine, m.ExitSignal)[0]
    assert (signal.trigger, signal.acted_on, signal.reason) == (
        m.ExitTrigger.MANUAL,
        True,
        "by hand",
    )


def test_an_exit_the_broker_keeps_refusing_raises_an_alert(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices["AAPL"] = 200.0
    seed_decision(engine, [idea("AAPL", 200.0)])
    trading.place_orders()
    broker.fill_entry("AAPL", 200.0)
    trading.sync()
    broker.refuse_market = 99
    assert trading.exit_ticker("AAPL", m.ExitTrigger.MANUAL, "x").startswith("failed")
    assert notes(engine)[-1].startswith("ALERT · could not close AAPL")


# ----------------------------------------------------------------------------- kill switch + reconcile


def test_the_kill_switch_pauses_trading_and_cancels_unfilled_entries(engine: Engine) -> None:
    trading, broker, _clock = make_engine(engine)
    broker.prices.update({"AAPL": 200.0, "JPM": 150.0})
    seed_decision(engine, [idea("AAPL", 200.0), idea("JPM", 150.0)])
    trading.place_orders()
    broker.fill_entry("JPM", 150.0)
    trading.sync()

    broker.equity = 97_100.0  # -2.9%: not yet
    assert not trading.monitor().kill_switch
    broker.equity = 96_900.0  # -3.1%
    assert trading.monitor().kill_switch
    assert trading.monitor().kill_switch  # stays on, one alert only
    alerts = [n for n in notes(engine) if n.startswith("ALERT · KILL SWITCH")]
    assert len(alerts) == 1 and "-3.1%" in alerts[0]
    entry = next(o for o in broker.orders.values() if o.symbol == "AAPL" and o.legs)
    assert entry.status == "canceled"
    jpm_stop = next(
        o for o in broker.orders.values() if o.symbol == "JPM" and o.order_type == "stop"
    )
    assert jpm_stop.status == "new"  # open positions keep their protection
    assert "kill switch" in (trading.paused() or {}).get("reason", "")

    seed_decision(engine, [idea("PFE", 30.0)])
    with pytest.raises(TradingRefused, match="kill switch"):
        trading.place_orders()
    trading.resume()
    assert trading.paused() is None
    with make_session_factory(engine)() as session:
        assert get_state(session, KILL_SWITCH_KEY)["active"] is False  # type: ignore[index]


def test_reconcile_flags_positions_the_system_does_not_know(engine: Engine) -> None:
    from .helpers import Holding

    trading, broker, _clock = make_engine(engine)
    broker.prices.update({"TSLA": 300.0, "AAPL": 200.0})
    broker.holdings["TSLA"] = Holding(5, 290.0)
    seed_decision(engine, [idea("AAPL", 200.0)])
    trading.place_orders()
    broker.fill_entry("AAPL", 200.0)
    trading.sync()
    broker.holdings.pop("AAPL")  # sold outside the system
    warnings = trading.reconcile_positions()
    assert warnings == ["TSLA is held in the paper account but not managed by this system",
                        "open trade in AAPL has no position at Alpaca (closed outside this system?)"]  # fmt: skip
    trading.reconcile_positions()
    assert len([n for n in notes(engine) if n.startswith("ALERT")]) == 2  # once per day each


# ----------------------------------------------------------------------------- test orders


def test_test_orders_are_small_marked_and_excluded_from_statistics(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices["SPY"] = 600.0
    decision_id, idea_id = trading.create_test_idea("spy")
    results = trading.place_orders(decision_id, qty_overrides={idea_id: 1})
    assert [(r.ticker, r.qty) for r in results] == [("SPY", 1)]
    broker.fill_entry("SPY", 600.1)
    trading.sync()
    (trade,) = rows(engine, m.Trade)
    assert trade.is_test and trade.qty == 1
    assert trade.horizon_ends_at == at_new_york(MONDAY, time(15, 45))  # sold at today's close
    assert notes(engine)[-1].startswith("[TEST] BOUGHT 1 SPY @ $600.10")
    trading.exit_ticker("SPY", m.ExitTrigger.MANUAL, "end of test")
    broker.fill_market("SPY", 600.5)
    trading.sync()
    clock[0] = at_new_york(MONDAY, time(16, 15))
    summary = trading.end_of_day()
    assert summary["closed_trades"] == 0 and summary["win_rate"] is None  # tests never count


def test_monitor_records_equity_snapshots(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    trading.monitor()
    clock[0] += timedelta(minutes=10)
    broker.equity = 100_500.0
    report = trading.monitor()
    assert report.equity == 100_500.0 and report.day_change_pct == pytest.approx(0.005)
    snaps = rows(engine, m.EquitySnapshot)
    assert [s.equity for s in snaps] == [100_000.0, 100_500.0]
    assert snaps[-1].day_pnl_usd == 500.0


def test_closed_market_monitor_does_not_place_time_stop_orders(engine: Engine) -> None:
    trading, broker, clock = make_engine(engine)
    broker.prices["XOM"] = 110.0
    seed_decision(engine, [idea("XOM", 110.0, horizon_days=1)])
    trading.place_orders()
    broker.fill_entry("XOM", 110.0)
    trading.sync()
    clock[0] = at_new_york(MONDAY, time(17, 0))
    broker.is_open = False
    assert trading.monitor().time_stops == []
    assert not [o for o in broker.orders.values() if o.order_type == "market"]


def test_config_defaults_keep_brackets_alive_overnight() -> None:
    assert shipped_config().trading.time_in_force == "gtc"
    assert TradingDay  # imported for the type
    assert datetime and UTC
