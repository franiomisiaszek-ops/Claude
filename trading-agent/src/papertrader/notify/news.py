"""X6: news as bullet points for Telegram, one per item:

    • What happened (the headline) — why it matters for the stock · source, time · link

"Why it matters" costs nothing extra: it is the claim a research agent made when it
cited that news item (its evidence points at "news:<id>"), else the headline labels
already stored (event type, good or bad for the company, how much it matters), else it
says it was not assessed. Only plain web links are shown.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.db import models as m
from papertrader.ingest.timeutil import NEW_YORK


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tone(sentiment: float) -> str:
    if sentiment >= 0.3:
        return "good for the company"
    if sentiment <= -0.3:
        return "bad for the company"
    return "mixed or neutral for the company"


def why_it_matters(session: Session, news: m.NewsItem, since: datetime) -> str:
    source = f"news:{news.id}"
    for report in session.scalars(select(m.AgentReport).where(m.AgentReport.ticker.in_([t.ticker for t in news.tickers]),
                                                              m.AgentReport.as_of >= since)
                                  .order_by(m.AgentReport.as_of.desc())):  # fmt: skip
        for item in report.evidence or []:
            if item.get("source_id") == source and item.get("claim"):
                return f"{report.agent_name.replace('_', ' ')} analyst: {_clip(str(item['claim']), 200)}"
    feature = session.get(m.NewsFeature, news.id)
    if feature is not None:
        weight = ("likely to matter" if feature.materiality >= 0.6 else
                  "may matter" if feature.materiality >= 0.3 else "probably noise")  # fmt: skip
        return f"{feature.event_type.replace('_', ' ')} news, {tone(feature.sentiment)}, {weight}"
    return "not assessed yet"


def bullets(session: Session, tickers: Sequence[str], now: datetime, *, hours: int = 24,
            per_ticker: int = 3, limit: int = 15) -> list[str]:  # fmt: skip
    """Bullet lines for the newest news on these stocks (shared news tables)."""
    since = now - timedelta(hours=hours)
    lines: list[str] = []
    for ticker in sorted(set(tickers)):
        items = session.scalars(
            select(m.NewsItem).join(m.NewsTicker, m.NewsTicker.news_id == m.NewsItem.id)
            .where(m.NewsTicker.ticker == ticker, m.NewsItem.duplicate_of_id.is_(None),
                   m.NewsItem.available_at >= since, m.NewsItem.available_at <= now)
            .order_by(m.NewsItem.published_at.desc()).limit(per_ticker)
        ).all()  # fmt: skip
        for news in items:
            url = news.url if (news.url or "").startswith(("https://", "http://")) else None
            line = (f"• {ticker}: {_clip(news.headline, 180)} — why it matters: {why_it_matters(session, news, since - timedelta(days=2))}"
                    f" · {news.source}, {news.published_at.astimezone(NEW_YORK):%a %H:%M} New York")  # fmt: skip
            lines.append(line + (f"\n  {url}" if url else ""))
            if len(lines) >= limit:
                return lines
    return lines
