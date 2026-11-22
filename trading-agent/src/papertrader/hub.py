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
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import AppConfig, Settings, TelegramConfig, load_config
from papertrader.db.models import Notification
from papertrader.db.provision import SchemaNotReadyError, verify
from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.db.state import get_state, set_state
from papertrader.db.types import utcnow
from papertrader.notify.bot import HELP, STALE_SECONDS, CommandBot
from papertrader.notify.service import make_broker_factory
from papertrader.notify.telegram import (
    Buttons,
    FlushReport,
    TelegramClient,
    TelegramError,
    flush_outbox,
)
from papertrader.notify.warnings import (
    GO,
    SENT,
    SKIPPED,
    WAITING,
    WARN_KIND,
    OrderWarning,
    group_rows,
    keyboard,
    tap,
)
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
    + '\n/why TICKER - system B\'s latest "why did it move?" post-mortem for a stock'
    + "\n/ask QUESTION - plain answers from the systems' own data (add A, B or C for one)"
    + "\nOrder warnings come with Skip and Go now buttons (B and C; A's are information only)."
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
    settings: Settings,
    folder: Path = Path("config"),
    skip: Collection[str] = (),
    *,
    read_only: bool = False,
) -> dict[SystemName, SystemView]:
    """All systems whose tables exist. B and C are left out until they are provisioned.

    `read_only=True` (the web app's pages): the database refuses every write.
    """
    views: dict[SystemName, SystemView] = {}
    for name in SYSTEMS:
        if name in skip:
            continue
        engine = make_system_engine(settings.database_url, name, read_only=read_only)
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


# Long reports end with a link to the matching page of the web app (web.public_url).
LINK_PAGES = {"morning_brief": "/planned", "c_plan": "/c", "c_orders": "/c", "weekly": "/compare",
              "digest": "/postmortems", "eod": "/", "intraday": "/planned"}  # fmt: skip
LONG_TEXT = 1500


def flush_all(
    views: Mapping[SystemName, SystemView],
    client: TelegramClient,
    chat_id: str,
    cfg: TelegramConfig,
    *,
    public_url: str = "",
    now: datetime | None = None,
) -> dict[SystemName, FlushReport]:
    """Send every system's waiting messages, each tagged with its system.

    X6: order warnings get their Skip / Go now buttons (B and C; A's are information
    only), routine messages wait during the quiet hours, long reports link to the app."""
    now = now or utcnow()
    quiet = cfg.quiet_hours.quiet(now)
    always = set(cfg.quiet_hours.always)

    def hold(row: Notification) -> bool:
        return quiet and row.kind not in always

    def link(row: Notification) -> str | None:
        page = LINK_PAGES.get(row.kind)
        return f"{public_url}{page}" if public_url and page and len(row.text) > LONG_TEXT else None

    reports: dict[SystemName, FlushReport] = {}
    for name, view in views.items():
        extras: dict[str, Any] = {}
        if name != "A":
            extras = {"buttons": partial(_buttons, name), "sent": _sent}
        reports[name] = flush_outbox(view.sessions, client, chat_id, cfg, prefix=f"{tag(name)} ", now=now,
                                     hold=hold, link=link, **extras)  # fmt: skip
        if reports[name].setup_problem:
            break  # wrong token or chat id: the other systems would fail the same way
    return reports


def _group(row: Notification) -> str | None:
    key = row.dedup_key or ""
    return key.removeprefix("warn:") if row.kind == WARN_KIND and key.startswith("warn:") else None


def _buttons(system: str, session: Session, row: Notification) -> Buttons | None:
    key = _group(row)
    return keyboard(group_rows(session, key), system) if key else None


def _sent(session: Session, row: Notification, ids: list[int]) -> None:
    key = _group(row)
    if key:
        for warning in group_rows(session, key):
            warning.message_id = str(ids[-1])  # the buttons sit under the last part
            warning.buttons_open = warning.status in (WAITING, GO)


def refresh_buttons(
    session: Session, system: str, key: str, client: TelegramClient, chat_id: str
) -> None:
    """Show only the buttons that still mean something (or none)."""
    rows = group_rows(session, key)
    message = next((r.message_id for r in rows if r.message_id), None)
    if not message or not message.isdigit():
        return
    try:
        client.set_buttons(chat_id, int(message), keyboard(rows, system))
    except TelegramError as exc:  # e.g. "message is not modified": nothing to do
        log.info("could not update the buttons", extra={"error": str(exc)})
    for row in rows:
        row.buttons_open = row.status in (WAITING, GO)


def tidy_buttons(
    views: Mapping[SystemName, SystemView], client: TelegramClient, chat_id: str
) -> int:
    """Remove the buttons of orders that went ahead (no answer) or were decided elsewhere."""
    done = 0
    for name, view in views.items():
        if name == "A":
            continue
        with view.sessions() as session:
            stale = session.scalars(select(OrderWarning.group_key).where(
                OrderWarning.buttons_open.is_(True), OrderWarning.status.in_((SENT, SKIPPED)))).all()  # fmt: skip
            for key in sorted(set(stale)):
                refresh_buttons(session, name, key, client, chat_id)
                done += 1
            session.commit()
    return done


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
        asker: Callable[[str, str], str] | None = None,
    ) -> None:
        self.views = dict(views)
        self.client = client
        self.chat_id = str(chat_id).strip()
        self.telegram = telegram
        self.now = now
        self.asker = asker  # (question, context) -> answer; None without an Anthropic key
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
            if update.get("callback_query"):
                self.on_button(update["callback_query"])
                continue
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
        if command == "/why":
            return self.why(args)
        if command == "/ask":
            return self.ask(args)
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

    def why(self, args: list[str]) -> str:
        from papertrader.b.postmortem import latest_why

        if len(args) == 2 and args[0].upper() == "B":
            args = args[1:]
        if len(args) != 1:
            return "Say which stock: /why NVDA"
        if "B" not in self.views:
            return "System B is not set up yet."
        return f"{tag('B')} " + latest_why(self.views["B"].sessions, args[0])

    # ------------------------------------------------------------------ X6: buttons and /ask

    def on_button(self, callback: dict[str, Any]) -> None:
        """A tap on Skip or Go now under an order warning."""
        message = callback.get("message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        data = str(callback.get("data") or "")
        reply = "That button is not understood."
        system, _, rest = data.partition("|")
        if chat != self.chat_id:
            self.bots[self.home.name]._unknown_chat(chat)
            reply = "Not allowed."
        elif system not in self.views or system == "A":
            reply = "That system is not set up here."
        else:
            view = self.views[system]
            with view.sessions() as session:
                reply, key = tap(session, rest, self.now())
                session.commit()
                if key:
                    refresh_buttons(session, system, key, self.client, self.chat_id)
                    session.commit()
            reply = f"{tag(system)} {reply}"
        try:
            self.client.answer_button(str(callback.get("id", "")), reply)
            if chat == self.chat_id:
                self.client.send(self.chat_id, reply)
        except TelegramError as exc:
            log.warning("could not answer a button", extra={"error": str(exc)})

    def ask(self, args: list[str]) -> str:
        from papertrader.notify.ask import context

        system = args[0].upper() if args and args[0].upper() in SYSTEMS and len(args) > 1 else None
        question = " ".join(args[1:] if system else args).strip()[:500]
        if not question:
            return "Ask in plain words, e.g. /ask why did you buy NVDA?  or  /ask B what are you watching today?"
        if self.asker is None:
            return "/ask needs ANTHROPIC_API_KEY in .env (then: docker compose restart hub)."
        if system is not None and system not in self.views:
            return f"System {system} is not set up yet."
        data = context(self.views, self.now(), system)
        return self.asker(question, data)
