"""The Telegram bot: answers /status, /positions, /pnl, /brief, /pause, /resume, /help.

Only messages from TELEGRAM_CHAT_ID are answered. Anything else is ignored without a
reply (so a stranger learns nothing), and you get one alert per unknown chat per day.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig
from papertrader.db.state import get_state, set_state
from papertrader.db.types import utcnow
from papertrader.ingest.timeutil import new_york_date
from papertrader.notify import messages, reports
from papertrader.notify.outbox import queue
from papertrader.notify.telegram import TelegramClient, TelegramError
from papertrader.trading import control
from papertrader.trading.broker import Broker

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram.offset"
STALE_SECONDS = 15 * 60  # commands older than this (app was off) are not carried out
HELP = """Commands
/status - is everything running, budget, last research
/positions - open positions
/pnl - profit and loss
/brief - today's morning brief again
/pause [reason] - stop new orders (positions keep their stops)
/resume - allow new orders again (also clears the kill switch)"""


class CommandBot:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: AppConfig,
        client: TelegramClient,
        chat_id: str,
        *,
        broker: Callable[[], Broker | None] = lambda: None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.client = client
        self.chat_id = str(chat_id).strip()
        self.broker = broker
        self.now = now

    # ------------------------------------------------------------------ polling

    def poll_once(self, timeout: int | None = None) -> int:
        """Fetch new messages and answer them. Returns how many commands were answered."""
        with self.sessions() as session:
            offset = (get_state(session, OFFSET_KEY) or {}).get("offset")
        updates = self.client.updates(
            offset, self.config.telegram.poll_timeout_seconds if timeout is None else timeout
        )
        answered = 0
        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip()
            chat = str((message.get("chat") or {}).get("id", ""))
            if not text:
                continue
            if chat != self.chat_id:
                self._unknown_chat(chat)
                continue
            sent = message.get("date")
            age = self.now().timestamp() - float(sent) if sent else 0.0
            if not text.startswith("/"):
                reply = "Send /help for the list of commands."
            elif age > STALE_SECONDS:
                reply = f"Skipped {text.split()[0]}: it arrived {age / 60:.0f} minutes late. Send it again if you still want it."
            else:
                reply = self.handle(text)
            try:
                self.client.send(self.chat_id, reply)
                answered += 1
            except TelegramError as exc:
                log.warning("could not answer a command", extra={"error": str(exc)})
        if updates:
            with self.sessions() as session:
                set_state(session, OFFSET_KEY, {"offset": offset})
                session.commit()
        return answered

    def _unknown_chat(self, chat: str) -> None:
        log.warning("ignored a Telegram message from an unknown chat", extra={"chat": chat})
        with self.sessions() as session:
            queue(session, "alert", messages.alert(f"Ignored a message to the bot from an unknown Telegram chat (id {chat})."),
                  dedup_key=f"tg-unknown:{chat}:{new_york_date(self.now())}")  # fmt: skip
            session.commit()

    # ------------------------------------------------------------------ commands

    def handle(self, text: str) -> str:
        command, _, rest = text.partition(" ")
        command = command.split("@")[0].lower()
        handler: Callable[[str], str] | None = {
            "/start": lambda _: HELP, "/help": lambda _: HELP, "/status": self._status,
            "/positions": self._positions, "/pnl": self._pnl, "/brief": self._brief,
            "/pause": self._pause, "/resume": self._resume,
        }.get(command)  # fmt: skip
        if handler is None:
            return f"Unknown command {command}.\n\n{HELP}"
        try:
            return handler(rest.strip())
        except Exception as exc:  # a broken command must not kill the bot
            log.exception("command failed", extra={"command": command})
            return f"{command} failed: {type(exc).__name__}"

    def _broker(self) -> Broker | None:
        try:
            return self.broker()
        except Exception:
            log.exception("broker unavailable for a bot command")
            return None

    def _status(self, _rest: str) -> str:
        broker = self._broker()
        market: bool | None = None
        if broker is not None:
            try:
                market = broker.clock().is_open
            except Exception:
                market = None
        with self.sessions() as session:
            return reports.build_status(session, self.config, self.now(), market_open=market)

    def _positions(self, _rest: str) -> str:
        broker = self._broker()
        rows: list[tuple[str, float, float, float]] = []
        if broker is not None:
            try:
                rows = [
                    (p.symbol, p.qty, p.avg_entry_price, p.current_price)
                    for p in broker.positions()
                    if p.qty
                ]
            except Exception:
                rows = []
                broker = None
        with self.sessions() as session:
            fallback = reports.open_trade_labels(session) if broker is None else []
        return reports.build_positions(rows, fallback)

    def _pnl(self, _rest: str) -> str:
        broker = self._broker()
        equity = change = spy = None
        if broker is not None:
            try:
                account = broker.account()
                equity, change = account.equity, account.day_change_pct
                spy = broker.latest_prices([self.config.universe.benchmark]).get(
                    self.config.universe.benchmark
                )
            except Exception:
                equity = change = spy = None
        with self.sessions() as session:
            return reports.build_pnl(session, equity=equity, day_change=change, spy_now=spy)

    def _brief(self, _rest: str) -> str:
        prices: dict[str, Any] = {}
        broker = self._broker()
        with self.sessions() as session:
            if broker is not None:
                try:
                    tickers = [label.split()[0] for label in reports.open_trade_labels(session)]
                    prices = broker.latest_prices(tickers) if tickers else {}
                except Exception:
                    prices = {}
            brief = reports.build_brief(session, self.config, self.now(), prices)
        return brief or "No research run today yet, so there is no brief."

    def _pause(self, rest: str) -> str:
        control.pause(self.sessions, rest or "paused from Telegram", self.now())
        return "Trading paused. Open positions keep their stops and targets. /resume to continue."

    def _resume(self, _rest: str) -> str:
        control.resume(self.sessions, self.now())
        return "Trading resumed (kill switch cleared)."
