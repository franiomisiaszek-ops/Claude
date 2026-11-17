"""Runs Telegram inside the long-running app: the outbox sender and the command bot."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig, Settings
from papertrader.notify.bot import CommandBot
from papertrader.notify.telegram import TelegramClient, TelegramError, flush_outbox
from papertrader.trading.broker import Broker

log = logging.getLogger(__name__)


def telegram_ready(settings: Settings, config: AppConfig) -> bool:
    return bool(
        config.telegram.enabled and settings.telegram_bot_token and settings.telegram_chat_id
    )


def make_client(settings: Settings) -> TelegramClient:
    assert settings.telegram_bot_token is not None
    return TelegramClient(settings.telegram_bot_token.get_secret_value())


def make_broker_factory(settings: Settings, config: AppConfig) -> Callable[[], Broker | None]:
    """The bot only needs the broker for /positions and /pnl; create it on first use."""
    cache: dict[str, Broker] = {}

    def factory() -> Broker | None:
        if "broker" not in cache:
            if not (settings.alpaca_api_key and settings.alpaca_secret_key):
                return None
            from papertrader.trading.broker import AlpacaPaperBroker

            cache["broker"] = AlpacaPaperBroker(settings.alpaca_api_key.get_secret_value(),
                                                settings.alpaca_secret_key.get_secret_value(),
                                                live_feed=config.trading.live_feed)  # fmt: skip
        return cache["broker"]

    return factory


def flush_job(sessions: sessionmaker[Session], settings: Settings, config: AppConfig) -> None:
    client = make_client(settings)
    try:
        report = flush_outbox(sessions, client, str(settings.telegram_chat_id), config.telegram)
        if report.setup_problem:
            log.warning("telegram cannot send; check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID",
                        extra={"error": report.setup_problem})  # fmt: skip
        if report.sent or report.failed or report.dropped:
            log.info(
                "telegram outbox",
                extra={"sent": report.sent, "failed": report.failed, "dropped": report.dropped},
            )
    finally:
        client.close()


class BotThread(threading.Thread):
    """Long-polls Telegram for commands until stopped. Errors back off, never crash."""

    def __init__(self, bot: CommandBot) -> None:
        super().__init__(name="telegram-bot", daemon=True)
        self.bot = bot
        self.stopping = threading.Event()

    def run(self) -> None:
        delay = 5.0
        while not self.stopping.is_set():
            try:
                self.bot.poll_once()
                delay = 5.0
            except TelegramError as exc:
                log.warning(
                    "telegram polling failed", extra={"error": str(exc), "retry_in_s": delay}
                )
                self.stopping.wait(delay)
                delay = min(delay * 2, 300.0)
            except Exception:
                log.exception("telegram bot error")
                self.stopping.wait(delay)
                delay = min(delay * 2, 300.0)

    def stop(self) -> None:
        self.stopping.set()
