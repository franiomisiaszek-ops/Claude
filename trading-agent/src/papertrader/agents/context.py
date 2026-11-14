"""The material shown to an agent, with a citation id on every item.

An agent may only make claims it can tie to one of these ids. After Claude answers,
`validate.py` checks each cited id against the pack that was really sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from papertrader.ingest.timeutil import NEW_YORK


@dataclass(frozen=True)
class ContextItem:
    source_id: str  # e.g. "news:123", "filing:45", "px:AAPL", "macro:DGS10"
    text: str
    available_at: datetime
    url: str | None = None
    tickers: frozenset[str] = frozenset()  # empty = about the market in general


@dataclass
class ContextPack:
    as_of: datetime
    allowed_tickers: frozenset[str]
    sections: list[tuple[str, list[ContextItem]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, title: str, items: list[ContextItem]) -> None:
        if items:
            self.sections.append((title, items))

    @property
    def items(self) -> dict[str, ContextItem]:
        return {item.source_id: item for _title, items in self.sections for item in items}

    def is_empty(self) -> bool:
        return not self.sections

    def render(self, max_reports: int) -> str:
        """The user message. Everything in it is data that existed at `as_of`."""
        when = self.as_of.astimezone(NEW_YORK).strftime("%Y-%m-%d %H:%M New York time")
        lines = [
            f"AS OF: {when}",
            f"MAXIMUM REPORTS: {max_reports}",
            "ALLOWED TICKERS: " + ", ".join(sorted(self.allowed_tickers)),
        ]
        lines += [f"NOTE: {note}" for note in self.notes]
        for title, items in self.sections:
            lines.append(f"\n## {title}")
            lines += [f"[{item.source_id}] {item.text}" for item in items]
        return "\n".join(lines)


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def stamp(moment: datetime) -> str:
    return moment.astimezone(NEW_YORK).strftime("%m-%d %H:%M")


def clip(text: str | None, limit: int) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"
