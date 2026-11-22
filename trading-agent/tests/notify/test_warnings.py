"""X6: heads-up before orders (Skip / Go now), the hub's buttons and taps, A's
information-only warnings, near stop/target, quiet hours, long messages, news bullets
and /ask. Telegram, the broker and Claude are all fakes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from papertrader.config import QuietHours, WarningsConfig, get_settings
from papertrader.db import models as m
from papertrader.db.provision import provision
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.hub import HubBot, SystemView, flush_all, tidy_buttons
from papertrader.ingest.timeutil import at_new_york
from papertrader.llm.governor import BudgetExceededError
from papertrader.notify import ahead, ask, messages, news
from papertrader.notify.outbox import queue
from papertrader.notify.telegram import MAX_LENGTH
from papertrader.notify.warnings import (
    GO,
    INFO_KIND,
    SENT,
    SKIPPED,
    WAITING,
    WARN_KIND,
    Gate,
    OrderWarning,
    group_rows,
    keyboard,
    tap,
)  # fmt: skip
from papertrader.trading.engine import TradingRefused
from papertrader.web import live

from ..ingest.helpers import shipped_config
from ..trading.helpers import MONDAY, idea, make_engine, seed_decision
from .helpers import CHAT, FakeTelegram

CFG = shipped_config()
AT_0932 = at_new_york(MONDAY, time(9, 32))
AT_0935 = at_new_york(MONDAY, time(9, 35))


@pytest.fixture
def b_engine(engine: Engine, database_url: str) -> Iterator[Engine]:
    b = make_system_engine(database_url, "B")
    provision(b, "B")
    yield b
    b.dispose()


def gated(b_engine: Engine, start: datetime) -> tuple[Any, Any, list[datetime]]:
    trading, broker, clock = make_engine(b_engine, start=start)
    trading.gate = Gate(trading.sessions, WarningsConfig(), now=lambda: clock[0])
    broker.prices.update({"AAPL": 200.0, "MSFT": 400.0})
    return trading, broker, clock


def warnings_in(engine: Engine) -> dict[str, OrderWarning]:
    with make_session_factory(engine)() as session:
        return {w.ref: w for w in session.scalars(select(OrderWarning))}


# --------------------------------------------------------------------------- buys at the open


def test_buys_wait_for_their_heads_up_and_skips_are_respected(b_engine: Engine) -> None:
    seed_decision(b_engine, [idea("AAPL", 200.0, dissent="Rates could rise."), idea("MSFT", 400.0)])
    trading, broker, clock = gated(b_engine, AT_0932)
    gate = trading.gate
    with trading.sessions() as session:
        items = [ahead.idea_pending(i, 100_000.0) for i in trading.tradable_ideas(session, None)]
    assert gate.warn(items, due=AT_0935, early=False) == [i.ref for i in items]
    assert gate.warn(items, due=AT_0935, early=False) == []  # never twice
    with trading.sessions() as session:
        note = session.scalars(select(m.Notification)).one()
    text = note.text
    assert note.kind == WARN_KIND and text.startswith("HEADS-UP · 2 order(s) at 09:35 New York")
    for part in ("BUY AAPL · 49 shares · zone $199.00-$201.00", "target $212.00", "stop $194.00",
                 "hold up to 5 trading days", "confidence 80%", "• strong reasons",
                 "Strongest argument against: Rates could rise.", "No answer = it goes ahead"):  # fmt: skip
        assert part in text, part

    clock[0] = at_new_york(MONDAY, time(9, 33))
    with pytest.raises(TradingRefused):  # the engine itself never trades before 09:35
        trading.place_orders()
    assert broker.orders == {}
    rows = warnings_in(b_engine)
    msft = next(r for r in rows.values() if r.ticker == "MSFT")
    with trading.sessions() as session:
        assert (
            tap(session, f"ow:{msft.id}:skip", clock[0])[0] == "Skipped: buy MSFT will not be sent."
        )
        answer, _key = tap(session, f"og:{msft.group_key}:go", clock[0])
        session.commit()
    assert "can't go earlier" in answer  # buys at the open never go before 09:35
    assert trading.gate.due() == []  # "Go now" did not make them due early

    clock[0] = AT_0935
    placed = {p.ticker: p.status for p in trading.place_orders()}
    assert placed == {"AAPL": "submitted", "MSFT": "skipped"}
    rows = warnings_in(b_engine)
    assert {r.ticker: r.status for r in rows.values()} == {"AAPL": SENT, "MSFT": SKIPPED}
    with trading.sessions() as session:
        skipped = session.scalars(select(m.TradeIdea).where(m.TradeIdea.ticker == "MSFT")).one()
    assert (
        skipped.status == m.IdeaStatus.EXPIRED
        and skipped.status_reason == "skipped by you on Telegram"
    )


def test_an_order_never_announced_is_announced_then_waits(b_engine: Engine) -> None:
    seed_decision(b_engine, [idea("AAPL", 200.0)])
    trading, _broker, clock = gated(b_engine, AT_0935)
    assert [p.status for p in trading.place_orders()] == [
        "waiting"
    ]  # the 09:32 heads-up was missed
    row = next(iter(warnings_in(b_engine).values()))
    assert row.status == WAITING and row.due_at == AT_0935 + timedelta(minutes=3)
    assert [w.ref for w in trading.gate.due()] == []
    clock[0] = AT_0935 + timedelta(minutes=3)
    assert [w.ticker for w in trading.gate.due()] == ["AAPL"]  # the one-minute check sends it now
    assert [p.status for p in trading.place_orders()] == ["submitted"]


# --------------------------------------------------------------------------- sells


def open_aapl(b_engine: Engine) -> tuple[Any, Any, list[datetime], int]:
    seed_decision(b_engine, [idea("AAPL", 200.0)])
    trading, broker, clock = make_engine(b_engine, start=at_new_york(MONDAY, time(10, 0)))
    broker.prices.update({"AAPL": 200.0})
    trading.place_orders()
    broker.fill_entry("AAPL", 200.0)
    trading.sync()
    with trading.sessions() as session:
        trade = session.scalars(select(m.Trade)).one()
        trade.horizon_ends_at = clock[0] - timedelta(minutes=1)
        session.commit()
        trade_id = trade.id
    trading.gate = Gate(trading.sessions, WarningsConfig(), now=lambda: clock[0])
    return trading, broker, clock, trade_id


def test_a_time_limit_sell_is_announced_then_sent(b_engine: Engine) -> None:
    trading, broker, clock, trade_id = open_aapl(b_engine)
    assert trading.time_stops() == []  # announced, not sold yet
    assert not [o for o in broker.orders.values() if o.order_type == "market"]
    with trading.sessions() as session:
        warning = (
            session.scalars(select(m.Notification).where(m.Notification.kind == WARN_KIND))
            .one()
            .text
        )
    assert warning.startswith("HEADS-UP · SELL AAPL") and "holding period over" in warning
    assert "the reason it was bought: strong reasons" in warning
    clock[0] += timedelta(minutes=3)
    due = trading.gate.due()
    assert [w.source for w in due] == ["time"] and due[0].details["trade_id"] == trade_id
    assert (
        trading.exit_trade(trade_id, m.ExitTrigger.TIME, due[0].details["reason"])
        == "exit order placed"
    )
    assert next(iter(warnings_in(b_engine).values())).status == SENT


def test_a_skipped_sell_holds_for_the_day_and_asks_again_tomorrow(b_engine: Engine) -> None:
    trading, _broker, clock, trade_id = open_aapl(b_engine)
    trading.time_stops()
    row = next(iter(warnings_in(b_engine).values()))
    with trading.sessions() as session:
        tap(session, f"ow:{row.id}:skip", clock[0])
        session.commit()
    clock[0] += timedelta(minutes=10)
    assert trading.time_stops() == []
    assert trading.exit_trade(trade_id, m.ExitTrigger.TIME, "x").startswith("skipped by you")
    with trading.sessions() as session:
        notes = session.scalars(select(m.ExitSignal).where(m.ExitSignal.acted_on.is_(False))).all()
    assert len(notes) == 1 and notes[0].reason.startswith("skipped by you on Telegram")
    assert len(warnings_in(b_engine)) == 1  # no second heads-up today
    clock[0] += timedelta(days=1)
    trading.time_stops()
    assert len(warnings_in(b_engine)) == 2  # a new day, a new heads-up
    assert (
        trading.exit_trade(trade_id, m.ExitTrigger.MANUAL, "by hand") == "exit order placed"
    )  # never gated


# --------------------------------------------------------------------------- the hub: buttons and taps


def views(engine: Engine, b_engine: Engine) -> dict[str, SystemView]:
    return {"A": SystemView("A", CFG, engine, make_session_factory(engine)),
            "B": SystemView("B", CFG, b_engine, make_session_factory(b_engine))}  # fmt: skip


def test_the_hub_sends_buttons_and_applies_taps(engine: Engine, b_engine: Engine) -> None:
    seed_decision(b_engine, [idea("AAPL", 200.0), idea("MSFT", 400.0)])
    trading, _broker, clock = gated(b_engine, AT_0932)
    with trading.sessions() as session:
        items = [ahead.idea_pending(i, None) for i in trading.tradable_ideas(session, None)]
    trading.gate.warn(items, due=AT_0935, early=False)
    with make_session_factory(engine)() as session:
        queue(session, INFO_KIND, "HEADS-UP · BUY XOM\nInformation only: system A")
        session.commit()
    fake = FakeTelegram()
    flush_all(views(engine, b_engine), fake.client(), CHAT, CFG.telegram, now=AT_0932)
    a_msg, b_msg = fake.sent
    assert "reply_markup" not in a_msg  # A's warnings: information only
    buttons = [b["callback_data"] for row in b_msg["reply_markup"]["inline_keyboard"] for b in row]
    rows = warnings_in(b_engine)
    assert buttons == [f"B|ow:{r.id}:skip" for r in sorted(rows.values(), key=lambda r: r.id)] + [
        f"B|og:{next(iter(rows.values())).group_key}:go"]  # fmt: skip
    message_id = 102
    assert all(r.message_id == str(message_id) and r.buttons_open for r in rows.values())

    msft = next(r for r in rows.values() if r.ticker == "MSFT")
    fake.tap(f"B|ow:{msft.id}:skip", message_id)
    fake.tap(f"B|ow:{msft.id}:skip", message_id, chat="999")  # a stranger: ignored
    bot = HubBot(views(engine, b_engine), fake.client(), CHAT, get_settings(), telegram=CFG.telegram,  # type: ignore[arg-type]
                 brokers={"A": lambda: None, "B": lambda: None}, now=lambda: clock[0])  # fmt: skip
    bot.poll_once(timeout=0)
    assert warnings_in(b_engine)[msft.ref].status == SKIPPED
    answers = [c["text"] for c in fake.method_calls("answerCallbackQuery")]
    assert answers == ["[B] Skipped: buy MSFT will not be sent.", "Not allowed."]
    edits = fake.method_calls("editMessageReplyMarkup")
    left = [b["callback_data"] for row in edits[-1]["reply_markup"]["inline_keyboard"] for b in row]
    assert f"B|ow:{msft.id}:skip" not in left and any(d.endswith(":go") for d in left)
    assert fake.texts()[-1] == "<b>[B] Skipped: buy MSFT will not be sent.</b>"

    trading.gate.mark_sent(r.ref for r in rows.values() if r.ticker == "AAPL")  # it went ahead
    assert tidy_buttons(views(engine, b_engine), fake.client(), CHAT) == 1
    assert fake.method_calls("editMessageReplyMarkup")[-1]["reply_markup"] == {
        "inline_keyboard": []
    }
    assert not any(r.buttons_open for r in warnings_in(b_engine).values())


def test_go_now_sends_an_intraday_style_order_early(b_engine: Engine) -> None:
    trading, _b, clock = gated(b_engine, AT_0935)
    item = ahead.idea_pending  # (any Pending works; an early one here)
    gate = trading.gate
    from papertrader.notify.warnings import Pending

    gate.warn([Pending(ref="intraday:1", side="BUY", ticker="NVDA", source="intraday")],
              due=clock[0] + timedelta(minutes=3), early=True)  # fmt: skip
    assert gate.due() == [] and item
    row = warnings_in(b_engine)["intraday:1"]
    with trading.sessions() as session:
        answer, key = tap(session, f"og:{row.group_key}:go", clock[0])
        session.commit()
        assert keyboard(group_rows(session, key or ""), "B") == [
            [{"text": "Skip", "callback_data": f"B|ow:{row.id}:skip"}]
        ]
    assert (
        answer == "Go: buy NVDA within a minute."
        and warnings_in(b_engine)["intraday:1"].status == GO
    )
    assert [w.ref for w in gate.due()] == ["intraday:1"]  # sent at the next minute's check


# --------------------------------------------------------------------------- quiet hours and long messages


def test_quiet_hours_hold_routine_messages_but_not_alerts(engine: Engine) -> None:
    quiet = QuietHours()
    night = datetime(2026, 11, 23, 23, 30, tzinfo=UTC)  # 00:30 in Warsaw
    morning = datetime(2026, 11, 24, 6, 30, tzinfo=UTC)  # 07:30 in Warsaw
    assert quiet.quiet(night) and not quiet.quiet(morning)
    with make_session_factory(engine)() as session:
        queue(session, "eod", "End of day summary")
        queue(session, "alert", "ALERT · something broke")
        for row in session.scalars(select(m.Notification)):
            row.created_at = night - timedelta(minutes=5)
        session.commit()
    fake = FakeTelegram()
    one = {"A": SystemView("A", CFG, engine, make_session_factory(engine))}
    flush_all(one, fake.client(), CHAT, CFG.telegram, now=night)  # type: ignore[arg-type]
    assert fake.texts() == ["<b>[A] ALERT · something broke</b>"]
    flush_all(one, fake.client(), CHAT, CFG.telegram, now=morning)  # type: ignore[arg-type]
    assert fake.texts()[-1] == "<b>[A] End of day summary</b>"


def test_long_messages_are_split_never_cut_and_link_to_the_app(engine: Engine) -> None:
    body = "\n".join(f"line {i}: " + "x" * 90 for i in range(100))  # about 9,800 characters
    with make_session_factory(engine)() as session:
        queue(session, "morning_brief", "Morning brief · Mon 23 Nov\n" + body)
        session.commit()
    fake = FakeTelegram()
    cfg = CFG.telegram.model_copy(update={"quiet_hours": QuietHours(enabled=False)})
    flush_all({"A": SystemView("A", CFG, engine, make_session_factory(engine))}, fake.client(), CHAT, cfg,  # type: ignore[arg-type]
              public_url="https://my-pc.tail1234.ts.net")  # fmt: skip
    texts = fake.texts()
    assert len(texts) == 3 and all(len(t) <= MAX_LENGTH for t in texts)
    assert texts[1].startswith("<i>(part 2 of 3)</i>") and texts[0].startswith(
        "<b>[A] Morning brief"
    )
    joined = "".join(texts)
    assert all(f"line {i}: " in joined for i in range(100))  # nothing lost
    assert texts[-1].endswith("The whole report in the app: https://my-pc.tail1234.ts.net/planned")
    long_reason = "Because " + "the news was strong and " * 20 + "end."
    assert long_reason in messages.trade_executed(side="buy", qty=1, ticker="X", price=1.0, confidence=None,
                                                 reason=long_reason)  # fmt: skip


# --------------------------------------------------------------------------- the hub's own warnings


def test_as_warnings_are_information_only(engine: Engine) -> None:
    seed_decision(
        engine, [idea("AAPL", 200.0), idea("MSFT", 400.0, status=m.IdeaStatus.BELOW_THRESHOLD)]
    )
    with make_session_factory(engine)() as session:
        assert ahead.a_morning(session, place_at=AT_0935, equity=100_000.0, now=AT_0932) == 1
        assert (
            ahead.a_morning(session, place_at=AT_0935, equity=100_000.0, now=AT_0932) == 0
        )  # once
        session.commit()
        note = session.scalars(select(m.Notification)).one()
    assert (
        note.kind == INFO_KIND and "BUY AAPL · 49 shares" in note.text and "MSFT" not in note.text
    )
    assert "Information only: system A is the frozen comparison" in note.text


def test_close_to_a_stop_or_target_warns_once_a_day(b_engine: Engine) -> None:
    trading, _broker, clock, _trade_id = open_aapl(b_engine)
    with trading.sessions() as session:
        trade = session.scalars(select(m.Trade)).one()
        target = trade.target_price
        assert ahead.near_levels(session, {"AAPL": target * 0.996}, WarningsConfig(), clock[0]) == 1
        assert ahead.near_levels(session, {"AAPL": target * 0.997}, WarningsConfig(), clock[0]) == 0
        assert (
            ahead.near_levels(session, {"AAPL": 205.0}, WarningsConfig(), clock[0]) == 0
        )  # far from both
        session.commit()
        text = (
            session.scalars(select(m.Notification).where(m.Notification.kind == ahead.PRICE_KIND))
            .one()
            .text
        )
    assert text.startswith("NEAR TAKE-PROFIT · AAPL") and "0.40% away" in text


# --------------------------------------------------------------------------- news bullets and /ask


def test_news_bullets_say_why_it_matters(engine: Engine) -> None:
    now = at_new_york(MONDAY, time(9, 0))
    seed_decision(engine, [idea("AAPL", 200.0)], reports=[("AAPL", "earnings", m.Stance.SUPPORT)])
    with make_session_factory(engine)() as session:
        items = []
        for i, url in enumerate(("https://example.com/a", "javascript:alert(1)")):
            item = m.NewsItem(source="alpaca", source_item_id=f"n{i}", headline=f"Apple story {i}", url=url,
                              published_at=now - timedelta(hours=1 + i), available_at=now - timedelta(hours=1 + i),
                              content_hash=f"h{i}")  # fmt: skip
            item.tickers.append(m.NewsTicker(ticker="AAPL"))
            session.add(item)
            items.append(item)
        session.flush()
        report = session.scalars(select(m.AgentReport)).one()
        report.evidence = [
            {"claim": "iPhone sales beat estimates", "source_id": f"news:{items[0].id}"}
        ]
        session.add(m.NewsFeature(news_id=items[1].id, version="v1", event_type="lawsuit", sentiment=-0.6,
                                  materiality=0.7))  # fmt: skip
        session.commit()
        lines = news.bullets(session, ["AAPL"], now)
    assert lines[0].startswith(
        "• AAPL: Apple story 0 — why it matters: earnings analyst: iPhone sales beat"
    )
    assert lines[0].endswith("\n  https://example.com/a")
    assert (
        "lawsuit news, bad for the company, likely to matter" in lines[1]
        and "javascript" not in lines[1]
    )


def test_ask_answers_from_the_systems_own_data(engine: Engine, b_engine: Engine) -> None:
    seed_decision(b_engine, [idea("NVDA", 900.0)])
    with make_session_factory(b_engine)() as session:
        from papertrader.db.state import set_state

        set_state(session, live.LIVE_KEY, {"at": AT_0935.isoformat(), "equity": 25_500.0, "last_equity": 25_000.0,
                                           "cash": 1.0, "positions": []})  # fmt: skip
        session.commit()
    asked: list[tuple[str, str]] = []

    def asker(question: str, data: str) -> str:
        asked.append((question, data))
        return "Because the earnings looked strong."

    fake = FakeTelegram()
    bot = HubBot(views(engine, b_engine), fake.client(), CHAT, get_settings(), telegram=CFG.telegram,  # type: ignore[arg-type]
                 brokers={"A": lambda: None, "B": lambda: None}, now=lambda: AT_0935, asker=asker)  # fmt: skip
    assert bot.handle("/ask B why NVDA?") == "Because the earnings looked strong."
    question, data = asked[0]
    assert question == "why NVDA?" and "=== SYSTEM B ===" in data and "=== SYSTEM A ===" not in data
    assert "Idea 1: buy NVDA, status proposed, confidence 80%: strong reasons" in data
    assert "Account $25,500.00, today +2.00%" in data
    assert bot.handle("/ask").startswith("Ask in plain words")
    bot.asker = None
    assert "needs ANTHROPIC_API_KEY" in bot.handle("/ask what now?")


async def test_ask_stays_inside_its_own_budget() -> None:
    class Broke:
        async def structured(self, **_kwargs: Any) -> Any:
            raise BudgetExceededError("over")

    text = await ask.answer("why?", "data", Broke(), "claude-haiku-5-5")  # type: ignore[arg-type]
    assert "budget is used up" in text


def test_the_one_minute_check_sends_due_orders(b_engine: Engine) -> None:
    from ..research.helpers import FakeClaude
    from ..systems.test_b_and_hub import b_jobs

    seed_decision(b_engine, [idea("AAPL", 200.0)])
    clock: list[Any] = [AT_0932]
    jobs = b_jobs(b_engine, FakeClaude(), clock)
    broker = jobs.broker()
    broker.prices.update({"AAPL": 200.0, "SPY": 600.0})
    assert jobs.job_warn_orders()["summary"] == "1 order(s) announced"
    assert jobs.orders_due() == {}  # nothing is due at 09:32
    clock[0] = AT_0935
    assert jobs.orders_due() == {"buys": ["AAPL submitted"]}
    assert jobs.orders_due() == {}  # sent once
