from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.db.state import KILL_SWITCH_KEY, get_state, set_state
from papertrader.notify.bot import OFFSET_KEY, CommandBot
from papertrader.trading import control

from ..ingest.helpers import shipped_config
from ..trading.helpers import OPEN_TIME, FakeBroker, Holding, idea, seed_decision
from .helpers import CHAT, FakeTelegram


def make_bot(engine: Engine, broker: FakeBroker | None = None) -> tuple[CommandBot, FakeTelegram]:
    fake = FakeTelegram()
    bot = CommandBot(make_session_factory(engine), shipped_config(), fake.client(), CHAT,
                     broker=lambda: broker, now=lambda: OPEN_TIME)  # fmt: skip
    return bot, fake


def ask(bot: CommandBot, fake: FakeTelegram, text: str) -> str:
    fake.message(text, date=OPEN_TIME.timestamp())
    before = len(fake.sent)
    bot.poll_once(timeout=0)
    assert len(fake.sent) == before + 1, "expected exactly one reply"
    return str(fake.sent[-1]["text"])


def test_help_and_unknown_commands(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    assert "/positions" in ask(bot, fake, "/start") and "/pause" in ask(bot, fake, "/help")
    assert "Unknown command /sell" in ask(bot, fake, "/sell NVDA")
    assert "Send /help" in ask(bot, fake, "hello")
    assert "/status" in ask(bot, fake, "/help@my_papertrader_bot")  # group-style command


def test_the_offset_is_remembered_so_nothing_is_answered_twice(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    ask(bot, fake, "/help")
    bot.poll_once(timeout=0)
    assert len(fake.sent) == 1
    with make_session_factory(engine)() as session:
        assert get_state(session, OFFSET_KEY) == {"offset": 2}
    assert fake.calls[-1][1]["offset"] == 2


def test_strangers_get_no_reply_and_one_alert_a_day(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    fake.message("/positions", chat="999", date=OPEN_TIME.timestamp())
    fake.message("/pause", chat="999", date=OPEN_TIME.timestamp())
    assert bot.poll_once(timeout=0) == 0 and fake.sent == []
    assert control.paused(make_session_factory(engine)) is None
    with make_session_factory(engine)() as session:
        alerts = session.scalars(select(m.Notification)).all()
    assert len(alerts) == 1 and "unknown Telegram chat (id 999)" in alerts[0].text


def test_late_commands_are_not_carried_out(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    fake.message("/pause", date=(OPEN_TIME - timedelta(hours=3)).timestamp())
    bot.poll_once(timeout=0)
    assert "Skipped /pause" in fake.texts()[-1] and "180 minutes late" in fake.texts()[-1]
    assert control.paused(make_session_factory(engine)) is None


def test_pause_and_resume(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        set_state(session, KILL_SWITCH_KEY, {"active": True})
        session.commit()
    assert "Trading paused" in ask(bot, fake, "/pause earnings week")
    assert (control.paused(sessions) or {})["reason"] == "earnings week"
    assert "PAUSED (earnings week)" in ask(bot, fake, "/status")
    assert "Trading resumed" in ask(bot, fake, "/resume")
    assert control.paused(sessions) is None
    with sessions() as session:
        assert (get_state(session, KILL_SWITCH_KEY) or {})["active"] is False


def test_status_positions_and_pnl_from_the_paper_account(engine: Engine) -> None:
    broker = FakeBroker([OPEN_TIME], equity=101_000.0)
    broker.last_equity = 100_000.0
    broker.prices.update({"AAPL": 210.0, "SPY": 606.0})
    broker.holdings["AAPL"] = Holding(10, 200.0)
    with make_session_factory(engine)() as session:
        session.add(m.EquitySnapshot(ts=OPEN_TIME - timedelta(days=5), equity=100_000.0, cash=100_000.0,
                                     positions_value=0.0, benchmark_close=600.0))  # fmt: skip
        session.commit()
    bot, fake = make_bot(engine, broker)
    status = ask(bot, fake, "/status")
    assert "PAPER" in status and "Market: open" in status and "Kill switch: off" in status
    assert "AAPL · 10 @ 200.00 · now 210.00 (+5.0%)" in ask(bot, fake, "/positions")
    pnl = ask(bot, fake, "/pnl")
    assert "Equity $101,000.00 · today +1.00%" in pnl and "+1.0% vs SPY +1.0%" in pnl


def test_without_alpaca_the_bot_still_answers_from_the_database(engine: Engine) -> None:
    bot, fake = make_bot(engine, broker=None)
    with make_session_factory(engine)() as session:
        session.add(m.Trade(ticker="NVDA", direction=m.Direction.LONG, qty=5, entry_price=140.0,
                            entered_at=OPEN_TIME, status=m.TradeStatus.OPEN))  # fmt: skip
        session.commit()
    assert "from the database" in ask(bot, fake, "/positions") and "NVDA" in fake.texts()[-1]
    assert "Account figures unavailable" in ask(bot, fake, "/pnl")
    assert "Market: unknown" in ask(bot, fake, "/status")


def test_brief_command(engine: Engine) -> None:
    bot, fake = make_bot(engine)
    assert "No research run today" in ask(bot, fake, "/brief")
    seed_decision(engine, [idea("NVDA", 140.0)])
    reply = ask(bot, fake, "/brief")
    assert "Morning brief" in reply and "BUY NVDA" in reply


def test_a_crashing_command_answers_with_an_error_and_the_bot_lives_on(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot, fake = make_bot(engine)

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("database gone")

    monkeypatch.setattr("papertrader.notify.reports.build_status", boom)
    assert ask(bot, fake, "/status") == "<b>/status failed: RuntimeError</b>"
    assert "/pnl" in ask(bot, fake, "/help")
