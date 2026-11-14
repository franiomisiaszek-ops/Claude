"""Reads the opening text of newly filed 8-K reports, for the SEC filings agent.

A filing never changes after it is accepted, so fetching its text later is still
point-in-time safe: the text is exactly what was public at the acceptance time.
"""

from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.db.models import Filing
from papertrader.ingest.http import Fetcher, FetchError

EXCERPT_CHARS = 4000
_ITEM = re.compile(r"\bItem\s+\d\.\d\d\b", re.IGNORECASE)


class _Text(HTMLParser):
    """Visible text only: scripts, styles and hidden inline-XBRL headers are skipped."""

    _SKIP = frozenset({"script", "style", "head", "ix:header", "ix:hidden"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def extract_excerpt(html: str, limit: int = EXCERPT_CHARS) -> str:
    """Plain text of an 8-K, starting at its first "Item x.xx" heading when there is one."""
    parser = _Text()
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())
    # The cover page lists item numbers in a table of check boxes; the body starts later.
    matches = list(_ITEM.finditer(text))
    body = [match for match in matches if match.start() > 600] or matches
    start = body[0].start() if body else 0
    return text[start : start + limit]


async def fetch_filing_excerpts(
    sec: Fetcher,
    session_factory: sessionmaker[Session],
    *,
    since: datetime,
    as_of: datetime,
    limit: int = 80,
) -> int:
    """Download the text of 8-Ks accepted in the window that have none stored yet."""
    with session_factory() as session:
        pending = session.scalars(
            select(Filing)
            .where(
                Filing.form_type.in_(("8-K", "8-K/A")),
                Filing.available_at > since,
                Filing.available_at <= as_of,
                Filing.text_excerpt.is_(None),
                Filing.url.is_not(None),
            )
            .order_by(Filing.available_at.desc())
            .limit(limit)
        ).all()
        stored = 0
        for filing in pending:
            url = filing.url or ""
            if not url.lower().endswith((".htm", ".html", ".txt")):
                continue
            try:
                filing.text_excerpt = extract_excerpt(await sec.get_text(url)) or None
            except FetchError:
                continue  # the agent then works from the item codes alone
            stored += filing.text_excerpt is not None
        session.commit()
    return stored
