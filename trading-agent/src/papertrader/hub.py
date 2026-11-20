"""The hub: one Telegram bot and one message sender for all three systems.

A Telegram bot can only be read by one program, so the systems do not each run their
own: they queue messages in their own outbox, and the hub sends all of them, tagged
[A], [B] or [C], and answers the commands for all three. It only reads the systems'
data, apart from marking messages as sent and the pause / resume switches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig, Settings, TelegramConfig, load_config
from papertrader.db.provision import SchemaNotReadyError, verify
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.db.state import get_state, set_state
from papertrader.db.types import utcnow
from papertrader.notify.bot import HELP, STALE_SECONDS, CommandBot
from papertrader.notify.service import make_broker_factory
from papertrader.notify.telegram import FlushReport, TelegramClient, TelegramError, flush_outbox
from papertrader.systems import SYSTEMS, SystemName, tag

log = logging.getLogger(__name__)

OFFSET_KEY = "hub.telegram.offset"  # kept in system A's state table
READ_COMMANDS = ("/status", "/positions", "/pnl", "/brief")
SWITCH_COMMANDS = ("/pause", "/resume")
HUB_HELP = (
    HELP.replace("Commands", "Commands (add A, B or C for one system)", 1)
    .replace("/pause [reason]", "/pause A|B|C [reason]", 1)
    .replace("/resume -", "/resume A|B|C -", 1)
    + "\nWithout a system, /status /positions /pnl /brief answer for all three."
    + "\n/skip TICKER - cancel system B's planned intraday trade before it is placed"
)


@dataclass
class SystemView:
    name: SystemName
    config: AppConfig
    engine: Engine
    sessions: sessionmaker[Session]


def config_path(name: SystemName, fallback: Path, folder: Path = Path("config")) -> Path:
    """Each system's settings: config/a.yaml, config/b.yaml, config/c.yaml."""
    path = folder / f"{name.lower()}.yaml"
    return path if path.exists() else fallback


def open_views(
    settings: Settings, folder: Path = Path("config"), skip: Collection[str] = ()
) -> dict[SystemName, SystemView]:
    """All systems whose tables exist. B and C are left out until they are provisioned."""
    views: dict[SystemName, SystemView] = {}
    for name in SYSTEMS:
        if name in skip:
            continue
        engine = make_system_engine(settings.database_url, name)
        try:
            verify(engine, name)
        except SchemaNotReadyError as exc:
            log.info(
                "system not set up yet; the hub leaves it out",
                extra={"system": name, "why": str(exc)},
            )
            engine.dispose()
            continue
        config = load_config(config_path(name, settings.papertrader_config, folder))
        views[name] = SystemView(name, config, engine, make_session_factory(engine))
    return views


def flush_all(
    views: Mapping[SystemName, SystemView],
    client: TelegramClient,
    chat_id: str,
    cfg: TelegramConfig,
) -> dict[SystemName, FlushReport]:
    """Send every system's waiting messages, each tagged with its system."""
    reports: dict[SystemName, FlushReport] = {}
    for name, view in views.items():
        reports[name] = flush_outbox(view.sessions, client, chat_id, cfg, prefix=f"{tag(name)} ")
        if reports[name].setup_problem:
            break  # wrong token or chat id: the other systems would fail the same way
    return reports


class HubBot:
    def __init__(
        self,
        views: Mapping[SystemName, SystemView],
        client: TelegramClient,
        chat_id: str,
        settings: Settings,
        *,
        telegram: TelegramConfig,
        brokers: Mapping[SystemName, Callable[[], object]] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.views = dict(views)
        self.client = client
        self.chat_id = str(chat_id).strip()
        self.telegram = telegram
        self.now = now
        self.bots = {
            name: CommandBot(view.sessions, view.config, client, self.chat_id, now=now,
                             broker=(brokers or {}).get(name)  # type: ignore[arg-type]
                             or make_broker_factory(settings, view.config, name))
            for name, view in self.views.items()
        }  # fmt: skip
        self.home = self.views.get("A") or next(iter(self.views.values()))

    def poll_once(self, timeout: int | None = None) -> int:
        with self.home.sessions() as session:
            offset = (get_state(session, OFFSET_KEY) or {}).get("offset")
        updates = self.client.updates(
            offset, self.telegram.poll_timeout_seconds if timeout is None else timeout
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
                self.bots[self.home.name]._unknown_chat(chat)
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
            with self.home.sessions() as session:
                set_state(session, OFFSET_KEY, {"offset": offset})
                session.commit()
        return answered

    def handle(self, text: str) -> str:
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        if command == "/skip":  # before the system is read: A and C are also tickers
            return self.skip(args)
        system = args[0].upper() if args and args[0].upper() in SYSTEMS else None
        rest = " ".join(args[1:] if system else args)
        if command in ("/start", "/help"):
            return HUB_HELP
        if system is not None and system not in self.bots:
            return f"System {system} is not set up yet."
        if command in SWITCH_COMMANDS:
            if system is None:
                return f"Say which system: {command} A, {command} B or {command} C."
            return f"{tag(system)} " + self.bots[system].handle(f"{command} {rest}".strip())
        if command in READ_COMMANDS:
            names = [system] if system else list(self.bots)
            return "\n\n".join(f"{tag(n)} " + self.bots[n].handle(command) for n in names)
        return f"Unknown command {command}.\n\n{HUB_HELP}"

    def skip(self, args: list[str]) -> str:
        from papertrader.b.scanner import skip

        if args and len(args) == 2 and args[0].upper() == "B":
            args = args[1:]
        if len(args) != 1:
            return "Say which stock: /skip NVDA"
        if "B" not in self.views:
            return "System B is not set up yet."
        return f"{tag('B')} " + skip(self.views["B"].sessions, args[0], self.now())
