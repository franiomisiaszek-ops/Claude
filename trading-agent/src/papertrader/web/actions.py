"""The few things the web app can change, each confirmed twice and written to a log.

* pause / resume a system (the same switch as /pause and /resume on Telegram)
* skip one of system B's planned intraday trades before it is placed (like /skip)

Every action, every login and every wrong password is appended to
`<data>/web_audit.jsonl`, and every action also sends you a Telegram message through
the hub, so nothing changes without a trace. Pages never write: only these functions
open a writable connection, for one action at a time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from papertrader.db.session import make_session_factory, make_system_engine
from papertrader.ingest.timeutil import NEW_YORK
from papertrader.notify.outbox import queue
from papertrader.systems import SYSTEMS
from papertrader.trading import control

log = logging.getLogger(__name__)

AUDIT_FILE = "web_audit.jsonl"


@dataclass(frozen=True)
class Action:
    kind: str  # pause | resume | skip
    system: str
    ticker: str = ""

    def describe(self) -> str:
        if self.kind == "pause":
            return f"Pause system {self.system}: no new orders; open positions keep their stops and targets."
        if self.kind == "resume":
            return f"Resume system {self.system}: new orders allowed again (also clears its kill switch)."
        return f"Skip system B's planned trade in {self.ticker}: it will not be bought today."

    def warning(self) -> str:
        if self.system == "A" and self.kind in ("pause", "resume"):
            return "A is the frozen comparison: pausing it changes its results. Only do this in an emergency."
        return ""


class ActionError(ValueError):
    pass


def parse(kind: str, system: str, ticker: str = "") -> Action:
    kind, system, ticker = kind.strip().lower(), system.strip().upper(), ticker.strip().upper()
    if kind not in ("pause", "resume", "skip"):
        raise ActionError("unknown action")
    if system not in SYSTEMS:
        raise ActionError("unknown system")
    if kind == "skip":
        if system != "B":
            raise ActionError("only system B's intraday trades can be skipped")
        if not ticker.isalnum() or not 1 <= len(ticker) <= 8 or not ticker.isascii():
            raise ActionError("that is not a ticker")
    return Action(kind, system, ticker if kind == "skip" else "")


def run(database_url: str, action: Action, now: datetime) -> str:
    """Carry out a confirmed action; returns the message shown (and sent to Telegram)."""
    engine = make_system_engine(database_url, action.system)
    try:
        sessions = make_session_factory(engine)
        if action.kind == "pause":
            control.pause(sessions, "paused from the web app", now)
            message = (
                f"System {action.system} paused from the web app. Open positions keep their stops."
            )
        elif action.kind == "resume":
            control.resume(sessions, now)
            message = f"System {action.system} resumed from the web app (kill switch cleared)."
        else:
            from papertrader.b.scanner import skip

            message = skip(sessions, action.ticker, now)
        with sessions() as session:
            queue(
                session, "alert", f"WEB APP · {message} ({now.astimezone(NEW_YORK):%H:%M} New York)"
            )
            session.commit()
        return message
    finally:
        engine.dispose()


def audit(data_dir: Path, now: datetime, event: str, **fields: Any) -> None:
    line = json.dumps({"at": now.isoformat(), "event": event, **fields}, default=str)
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        with (data_dir / AUDIT_FILE).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        log.exception("could not write the web audit log")
    log.info("web app event", extra={"event": event, **fields})


def recent(data_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    try:
        lines = (data_dir / AUDIT_FILE).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines[-limit * 3 :]):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out
