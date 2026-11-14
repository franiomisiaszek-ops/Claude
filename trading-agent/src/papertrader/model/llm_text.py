"""Text features from Claude for historical headlines, through the Batches API.

Two safeguards from the spec meet here:

* Leakage: Claude only ever sees MASKED text (company names, tickers and dates
  replaced by placeholders) and is asked what the text says, never what happened next.
* Cost: this is bulk, non-urgent work, so it goes through the Message Batches API at
  half price, on the cheapest model. It is submitted, then collected later.

The features are optional. The pattern model trains without them (it has keyword and
word-list features of its own); the backtest report states how many rows had them.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Annotated, Any, Literal, cast

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, BeforeValidator, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from papertrader.agents.schemas import Confidence
from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.state import get_state, set_state
from papertrader.db.types import utcnow
from papertrader.ingest.store import upsert
from papertrader.llm.governor import CostGovernor
from papertrader.llm.pricing import Usage, cost_usd
from papertrader.model.dataset import MAX_TICKERS_PER_STORY
from papertrader.model.text import Masker

log = logging.getLogger(__name__)

VERSION = "masked-v1"
STATE_KEY = "model.text_batches"
PURPOSE = "text_features"
MAX_TOKENS = 8000

SYSTEM = """\
You label short financial news items for a research dataset. Each item has been \
anonymised: the company the item is about appears as [COMPANY], other companies as [OTHER], \
and dates, years and quarters as [DATE].

Judge only what the text itself says. Do not try to work out which company or date is behind \
a placeholder, and do not use any memory of how a stock moved. If you think you recognise an \
event, ignore that and label the text as written.

For every item return:
- id: the number shown in square brackets before the item, unchanged.
- event_type: the main kind of event, one of earnings, guidance, mna, fda_regulatory, \
lawsuit_legal, product, management_change, analyst_rating, capital_return, other.
- sentiment: from -1 (clearly bad news for [COMPANY]) to 1 (clearly good news for \
[COMPANY]); 0 for neutral or unclear. Judge the news for [COMPANY], not for [OTHER].
- materiality: from 0 (routine noise: roundups, opinion, minor updates) to 1 (an event that \
could change the company's prospects). Most items are below 0.3.

Return one entry per item, in the order given, and nothing else.
"""

TextEvent = Literal[
    "earnings", "guidance", "mna", "fda_regulatory", "lawsuit_legal", "product",
    "management_change", "analyst_rating", "capital_return", "other",
]  # fmt: skip


def _clamp_signed(value: object) -> object:
    return min(max(value, -1.0), 1.0) if isinstance(value, int | float) else value


class ItemFeature(BaseModel):
    id: int
    event_type: TextEvent
    sentiment: Annotated[float, BeforeValidator(_clamp_signed)]
    materiality: Confidence


class TextFeatures(BaseModel):
    items: list[ItemFeature]


def pending_items(session: Session, config: AppConfig, limit: int) -> list[tuple[int, str]]:
    """(news id, masked text) for stories about universe stocks that have no features yet."""
    companies = session.execute(
        select(m.UniverseMember.ticker, m.UniverseMember.name).where(
            m.UniverseMember.index_name == config.universe.index
        )
    ).all()
    masker = Masker([(ticker, name) for ticker, name in companies])
    universe = {ticker for ticker, _name in companies}

    sizes = (
        select(m.NewsTicker.news_id, func.count().label("n"))
        .group_by(m.NewsTicker.news_id)
        .subquery()
    )
    rows = session.execute(
        select(m.NewsItem.id, m.NewsItem.headline, m.NewsItem.summary)
        .join(sizes, sizes.c.news_id == m.NewsItem.id)
        .outerjoin(m.NewsFeature, m.NewsFeature.news_id == m.NewsItem.id)
        .where(
            m.NewsItem.duplicate_of_id.is_(None),
            m.NewsFeature.news_id.is_(None),
            sizes.c.n <= MAX_TICKERS_PER_STORY,
            m.NewsItem.id.in_(
                select(m.NewsTicker.news_id).where(m.NewsTicker.ticker.in_(universe))
            ),
        )
        .order_by(m.NewsItem.id)
        .limit(limit)
    ).all()
    ids = [row[0] for row in rows]
    own: dict[int, list[str]] = {}
    for start in range(0, len(ids), 500):
        links = session.execute(
            select(m.NewsTicker.news_id, m.NewsTicker.ticker).where(
                m.NewsTicker.news_id.in_(ids[start : start + 500])
            )
        )
        for news_id, ticker in links:
            own.setdefault(news_id, []).append(ticker)

    items = []
    for news_id, headline, summary in rows:
        text = (
            headline
            if not summary or summary.strip() == headline.strip()
            else f"{headline} - {summary}"
        )
        items.append((news_id, masker.mask(text[:600], own.get(news_id, []))))
    return items


def build_requests(
    items: Sequence[tuple[int, str]], config: AppConfig
) -> tuple[list[dict[str, Any]], dict[str, list[int]]]:
    """Batch requests of `text_items_per_request` items each, plus which ids each holds."""
    cfg = config.pattern_model
    schema = anthropic.transform_schema(TextFeatures)
    requests, chunks = [], {}
    for index in range(0, len(items), cfg.text_items_per_request):
        part = items[index : index + cfg.text_items_per_request]
        custom_id = f"tf-{part[0][0]}-{len(part)}"
        chunks[custom_id] = [news_id for news_id, _text in part]
        requests.append(
            {
                "custom_id": custom_id,
                "params": {
                    "model": cfg.text_model,
                    "max_tokens": MAX_TOKENS,
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": cfg.text_effort,
                                      "format": {"type": "json_schema", "schema": schema}},
                    # The instructions are identical in every request: cache them.
                    "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user",
                                  "content": "\n".join(f"[{news_id}] {text}" for news_id, text in part)}],
                },
            }
        )  # fmt: skip
    return requests, chunks


def estimate_batch_cost(items: Sequence[tuple[int, str]], config: AppConfig) -> float:
    """Upper-end guess: every item's text in, about 60 output tokens per item, half price."""
    cfg = config.pattern_model
    requests = max(1, -(-len(items) // cfg.text_items_per_request))
    usage = Usage(
        input_tokens=sum(len(text) for _id, text in items) // 3
        + requests * (len(SYSTEM) // 3 + 50),
        output_tokens=len(items) * 60 + requests * 300,
    )
    return cost_usd(usage, cfg.text_model, config.budget, batch=True)


async def submit(
    client: AsyncAnthropic,
    sessions: sessionmaker[Session],
    config: AppConfig,
    governor: CostGovernor,
    *,
    limit: int,
) -> tuple[str | None, int, float]:
    """Send one batch. Returns (batch id, number of items, estimated cost)."""
    with sessions() as session:
        waiting = {news_id for batch in (get_state(session, STATE_KEY) or {}).values()
                   for ids in batch["chunks"].values() for news_id in ids}  # fmt: skip
        items = [item for item in pending_items(session, config, limit + len(waiting))
                 if item[0] not in waiting][:limit]  # fmt: skip
    if not items:
        return None, 0, 0.0
    estimate = estimate_batch_cost(items, config)
    governor.reserve(estimate)  # raises BudgetExceededError before anything is sent
    governor.settle(estimate, 0.0)  # the real cost is recorded when results are collected

    requests, chunks = build_requests(items, config)
    batch = await client.messages.batches.create(requests=cast(Any, requests))
    with sessions() as session:
        state = get_state(session, STATE_KEY) or {}
        state[batch.id] = {
            "chunks": chunks,
            "submitted_at": utcnow().isoformat(),
            "items": len(items),
        }
        set_state(session, STATE_KEY, state)
        session.commit()
    log.info("text-feature batch submitted", extra={"batch": batch.id, "items": len(items)})
    return batch.id, len(items), estimate


async def collect(
    client: AsyncAnthropic, sessions: sessionmaker[Session], config: AppConfig
) -> dict[str, Any]:
    """Fetch finished batches, store the features and record what they cost."""
    summary: dict[str, Any] = {
        "stored": 0,
        "cost_usd": 0.0,
        "still_running": [],
        "failed_requests": 0,
    }
    with sessions() as session:
        state = get_state(session, STATE_KEY) or {}
    for batch_id, info in list(state.items()):
        batch = await client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            summary["still_running"].append(batch_id)
            continue
        rows: list[dict[str, Any]] = []
        usage_rows: list[m.ApiUsage] = []
        async for entry in await client.messages.batches.results(batch_id):
            wanted = set(info["chunks"].get(entry.custom_id, []))
            if entry.result.type != "succeeded":
                summary["failed_requests"] += 1
                continue
            message = entry.result.message
            usage = Usage(
                message.usage.input_tokens or 0, message.usage.output_tokens or 0,
                message.usage.cache_creation_input_tokens or 0, message.usage.cache_read_input_tokens or 0,
            )  # fmt: skip
            model = (
                message.model
                if message.model in config.budget.prices
                else config.pattern_model.text_model
            )
            cost = cost_usd(usage, model, config.budget, batch=True)
            summary["cost_usd"] += cost
            usage_rows.append(m.ApiUsage(
                ts=utcnow(), purpose=PURPOSE, model=model, effort=config.pattern_model.text_effort,
                batch_id=batch_id, is_batch=True, input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens, cost_usd=cost,
                stop_reason=message.stop_reason,
            ))  # fmt: skip
            # Stop reason first, then the content.
            if message.stop_reason != "end_turn":
                summary["failed_requests"] += 1
                continue
            text = next((block.text for block in message.content if block.type == "text"), "")
            try:
                parsed = TextFeatures.model_validate_json(text)
            except ValidationError:
                summary["failed_requests"] += 1
                continue
            for item in parsed.items:
                if item.id in wanted:  # ids the model made up are ignored
                    wanted.discard(item.id)
                    rows.append({"news_id": item.id, "version": VERSION, "event_type": item.event_type,
                                 "sentiment": item.sentiment, "materiality": item.materiality,
                                 "created_at": utcnow()})  # fmt: skip
        with sessions() as session:
            summary["stored"] += upsert(session, m.NewsFeature, rows, conflict=("news_id",))
            session.add_all(usage_rows)
            remaining = get_state(session, STATE_KEY) or {}
            remaining.pop(batch_id, None)
            set_state(session, STATE_KEY, remaining)
            session.commit()
    return summary
