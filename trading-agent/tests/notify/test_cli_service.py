from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from papertrader import config as config_module
from papertrader.cli import app
from papertrader.db import models as m
from papertrader.notify.outbox import queue
from papertrader.notify.service import BotThread
from papertrader.notify.telegram import TelegramError
from papertrader.runtime import bootstrap
from papertrader.scheduler.service import build_scheduler

from ..conftest import PROJECT_ROOT
from .helpers import CHAT, TOKEN, FakeTelegram, error

runner = CliRunner()


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeTelegram:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    telegram = FakeTelegram()
    monkeypatch.setattr(
        "papertrader.notify.service.make_client", lambda _settings: telegram.client()
    )
    return telegram


def test_commands_explain_missing_keys(fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    result = runner.invoke(app, ["telegram", "test"])
    assert result.exit_code == 1 and "papertrader telegram setup" in result.output
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    config_module.get_settings.cache_clear()
    result = runner.invoke(app, ["telegram", "setup"])
    assert result.exit_code == 1 and "TELEGRAM_BOT_TOKEN is not set" in result.output


def test_setup_checks_the_token_and_finds_the_chat_id(
    fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    result = runner.invoke(app, ["telegram", "setup"])
    assert result.exit_code == 0, result.output
    assert "@my_papertrader_bot" in result.output and "press Start" in result.output
    fake.message("hi")
    result = runner.invoke(app, ["telegram", "setup"])
    assert f"Chat Fran: TELEGRAM_CHAT_ID={CHAT}" in result.output
    assert TOKEN not in result.output

    fake.fail = lambda *_: error(401, "Unauthorized")
    result = runner.invoke(app, ["telegram", "setup"])
    assert result.exit_code == 1 and "Unauthorized. Check TELEGRAM_BOT_TOKEN" in result.output


def test_test_sends_one_message_of_every_type(fake: FakeTelegram) -> None:
    result = runner.invoke(app, ["telegram", "test"])
    assert result.exit_code == 0, result.output
    assert "All 7 message types sent" in result.output
    texts = fake.texts()
    assert len(texts) == 7 and all(t.startswith("<b>[TEST] ") for t in texts)
    titles = [t.split("\n")[0] for t in texts]
    for word in (
        "Morning brief",
        "BUY",
        "NVDA",
        "closed",
        "End of day",
        "Weekly summary",
        "KILL SWITCH",
    ):
        assert any(word in t for t in texts), word
    assert len(set(titles)) == 7


def test_test_reports_a_failure(fake: FakeTelegram) -> None:
    fake.fail = lambda *_: error(400, "Bad Request: chat not found")
    result = runner.invoke(app, ["telegram", "test"])
    assert (
        result.exit_code == 1
        and "FAILED: Morning brief: Telegram refused: Bad Request: chat not found" in result.output
    )


def test_send_flushes_the_outbox_and_brief_and_weekly_send_now(fake: FakeTelegram) -> None:
    runtime = bootstrap(migrate=True)
    with runtime.session_factory() as session:
        queue(session, "alert", "ALERT: hello")
        session.commit()
    result = runner.invoke(app, ["telegram", "send"])
    assert result.exit_code == 0 and "Sent 1, failed 0" in result.output
    assert fake.texts() == ["<b>ALERT: hello</b>"]

    result = runner.invoke(app, ["telegram", "brief"])
    assert result.exit_code == 1 and "No daily research run today" in result.output
    result = runner.invoke(app, ["telegram", "weekly"])
    assert result.exit_code == 0 and fake.texts()[-1].startswith("<b>Weekly summary")
    with runtime.session_factory() as session:
        assert {n.status for n in session.scalars(select(m.Notification))} == {
            m.NotificationStatus.SENT
        }


def test_send_says_when_the_chat_id_is_wrong(fake: FakeTelegram) -> None:
    runtime = bootstrap(migrate=True)
    with runtime.session_factory() as session:
        queue(session, "alert", "ALERT: hello")
        session.commit()
    fake.fail = lambda *_: error(400, "Bad Request: chat not found")
    result = runner.invoke(app, ["telegram", "send"])
    assert (
        result.exit_code == 1 and "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID" in result.output
    )


def test_the_app_sends_messages_only_when_telegram_is_set_up(
    fake: FakeTelegram, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = [job.id for job in build_scheduler(bootstrap(migrate=True)).get_jobs()]
    assert jobs == ["heartbeat", "telegram_outbox"]
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    config_module.get_settings.cache_clear()
    assert [job.id for job in build_scheduler(bootstrap()).get_jobs()] == ["heartbeat"]


def test_the_bot_thread_backs_off_on_errors_and_stops_cleanly() -> None:
    calls: list[int] = []
    done = threading.Event()

    class FlakyBot:
        def poll_once(self) -> int:
            calls.append(1)
            if len(calls) >= 3:
                done.set()
            raise TelegramError("could not reach Telegram (ConnectError)")

    thread = BotThread(FlakyBot())  # type: ignore[arg-type]
    waits: list[float] = []
    thread.stopping.wait = lambda timeout=None: waits.append(timeout or 0) or False  # type: ignore[method-assign]
    thread.start()
    assert done.wait(5)
    thread.stop()
    thread.stopping.set()
    thread.join(5)
    assert not thread.is_alive()
    assert waits[:3] == [5.0, 10.0, 20.0]
