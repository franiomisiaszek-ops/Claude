from __future__ import annotations

import logging
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.db.types import utcnow
from papertrader.logging_setup import install_url_redaction, redact_url_secrets
from papertrader.notify.outbox import queue
from papertrader.notify.telegram import TelegramError, flush_outbox, split_message, to_html

from ..ingest.helpers import shipped_config
from .helpers import CHAT, TOKEN, FakeTelegram, error

CFG = shipped_config().telegram


def test_html_is_escaped_and_the_first_line_is_the_title() -> None:
    assert (
        to_html("Trade <closed>\nP&L +$5 & <b>")
        == "<b>Trade &lt;closed&gt;</b>\nP&amp;L +$5 &amp; &lt;b&gt;"
    )
    assert to_html("one line") == "<b>one line</b>"


def test_long_messages_are_split_on_line_breaks() -> None:
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 1
    assert "\n".join(parts) == text
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_send_uses_html_without_link_previews_and_returns_ids() -> None:
    fake = FakeTelegram()
    ids = fake.client().send(CHAT, "Title\n" + "\n".join("z" * 90 for _ in range(100)))
    assert len(ids) == 3 and len(fake.sent) == 3
    first = fake.sent[0]
    assert first["chat_id"] == CHAT and first["parse_mode"] == "HTML"
    assert first["disable_web_page_preview"] is True and first["text"].startswith("<b>Title</b>")


@pytest.mark.parametrize(
    ("response", "permanent", "retry_after"),
    [(error(429, "Too Many Requests: retry after 7", 7), False, 7),
     (error(401, "Unauthorized"), True, None),
     (error(400, "Bad Request: chat not found"), True, None),
     (error(502, "Bad Gateway"), False, None),
     (httpx.Response(500, text="<html>oops</html>"), False, None)],
)  # fmt: skip
def test_errors_say_whether_retrying_helps_and_never_contain_the_token(
    response: httpx.Response, permanent: bool, retry_after: int | None
) -> None:
    fake = FakeTelegram()
    fake.fail = lambda *_: response
    with pytest.raises(TelegramError) as caught:
        fake.client().send(CHAT, "hi")
    assert caught.value.permanent is permanent and caught.value.retry_after == retry_after
    assert TOKEN not in str(caught.value) and "123456" not in repr(caught.value)


def test_a_network_failure_is_temporary_and_hides_the_url() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed to connect to {request.url}")

    from papertrader.notify.telegram import TelegramClient

    client = TelegramClient(TOKEN, http=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(TelegramError) as caught:
        client.send(CHAT, "hi")
    assert not caught.value.permanent and TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_the_bot_token_is_masked_in_http_library_logs(caplog: pytest.LogCaptureFixture) -> None:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert (
        redact_url_secrets(f'POST {url} "200 OK"')
        == 'POST https://api.telegram.org/bot***/sendMessage "200 OK"'
    )
    install_url_redaction()
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info("HTTP Request: %s %s", "POST", url)
    assert TOKEN not in caplog.text and "/bot***/sendMessage" in caplog.text


# ----------------------------------------------------------------------------- outbox


def _queue(engine: Engine, *texts: str) -> None:
    with make_session_factory(engine)() as session:
        for text in texts:
            queue(session, "alert", text)
        session.commit()


def _rows(engine: Engine) -> list[m.Notification]:
    with make_session_factory(engine)() as session:
        return list(session.scalars(select(m.Notification).order_by(m.Notification.id)))


def test_the_outbox_is_sent_in_order_and_marked_sent(engine: Engine) -> None:
    _queue(engine, "first", "second", "third")
    fake = FakeTelegram()
    report = flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG)
    assert (report.sent, report.failed, report.waiting) == (3, 0, False)
    assert fake.texts() == ["<b>first</b>", "<b>second</b>", "<b>third</b>"]
    rows = _rows(engine)
    assert {r.status for r in rows} == {m.NotificationStatus.SENT}
    assert all(r.sent_at and r.telegram_message_id for r in rows)
    assert (
        flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG).sent == 0
    )  # nothing twice


def test_a_temporary_problem_stops_the_run_and_keeps_the_order(engine: Engine) -> None:
    _queue(engine, "first", "second")
    fake = FakeTelegram()
    fake.fail = lambda *_: error(429, "Too Many Requests", 5)
    report = flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG)
    assert (report.sent, report.waiting) == (0, True)
    first, second = _rows(engine)
    assert first.status == second.status == m.NotificationStatus.PENDING
    assert (first.attempts, second.attempts) == (1, 0)  # the second was not tried before the first

    fake.fail = lambda *_: None
    assert flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG).sent == 2
    assert fake.texts() == ["<b>first</b>", "<b>second</b>"]


def test_a_message_that_keeps_failing_is_given_up_after_max_attempts(engine: Engine) -> None:
    _queue(engine, "stuck", "next")
    fake = FakeTelegram()
    fake.fail = lambda _method, payload: (
        error(502, "Bad Gateway") if "stuck" in payload["text"] else None
    )
    sessions = make_session_factory(engine)
    for _ in range(CFG.max_attempts - 1):
        assert flush_outbox(sessions, fake.client(), CHAT, CFG).waiting
    report = flush_outbox(sessions, fake.client(), CHAT, CFG)
    assert (report.failed, report.sent) == (1, 1)
    stuck, nxt = _rows(engine)
    assert stuck.status == m.NotificationStatus.FAILED and stuck.attempts == CFG.max_attempts
    assert "Bad Gateway" in (stuck.error or "") and nxt.status == m.NotificationStatus.SENT


def test_a_message_telegram_rejects_is_skipped_but_the_rest_go(engine: Engine) -> None:
    _queue(engine, "broken", "fine")
    fake = FakeTelegram()
    fake.fail = lambda _m, payload: (
        error(400, "Bad Request: can't parse entities") if "broken" in payload["text"] else None
    )
    report = flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG)
    assert (report.failed, report.sent) == (1, 1)
    assert [r.status for r in _rows(engine)] == [
        m.NotificationStatus.FAILED,
        m.NotificationStatus.SENT,
    ]


@pytest.mark.parametrize("problem", [error(401, "Unauthorized"), error(400, "Bad Request: chat not found"),
                                     error(403, "Forbidden: bot was blocked by the user")])  # fmt: skip
def test_a_wrong_token_or_chat_id_keeps_every_message_queued(
    engine: Engine, problem: httpx.Response
) -> None:
    _queue(engine, "first", "second")
    fake = FakeTelegram()
    fake.fail = lambda *_: problem
    report = flush_outbox(make_session_factory(engine), fake.client(), CHAT, CFG)
    assert report.setup_problem and report.waiting and report.failed == 0
    assert all(r.status == m.NotificationStatus.PENDING and r.attempts == 0 for r in _rows(engine))


def test_messages_older_than_max_age_are_dropped_not_sent_late(engine: Engine) -> None:
    sessions = make_session_factory(engine)
    with sessions() as session:
        session.add(m.Notification(kind="alert", text="yesterday's news",
                                   created_at=utcnow() - timedelta(hours=CFG.max_age_hours + 1)))  # fmt: skip
        queue(session, "alert", "fresh")
        session.commit()
    fake = FakeTelegram()
    report = flush_outbox(sessions, fake.client(), CHAT, CFG)
    assert (report.dropped, report.sent) == (1, 1) and fake.texts() == ["<b>fresh</b>"]
    old, _fresh = _rows(engine)
    assert old.status == m.NotificationStatus.FAILED and old.error == "too old to send"
