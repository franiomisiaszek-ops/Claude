"""Masked text features through the Batches API, against a fake API behind the real SDK."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from sqlalchemy import Engine, select

from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.llm.client import BudgetExceededError
from papertrader.llm.governor import CostGovernor
from papertrader.model.dataset import build_dataset
from papertrader.model.llm_text import (
    SYSTEM,
    VERSION,
    build_requests,
    collect,
    pending_items,
    submit,
)

from ..ingest.helpers import shipped_config
from ..research.helpers import AS_OF, seed_market

CFG = shipped_config()
CFG = CFG.model_copy(
    update={"pattern_model": CFG.pattern_model.model_copy(update={"text_items_per_request": 2})}
)


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.ended = False
        self.answers: dict[str, Any] = {}

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        batch = {"id": "msgbatch_1", "type": "message_batch", "created_at": "2026-11-13T12:00:00Z",
                 "expires_at": "2026-11-14T12:00:00Z", "ended_at": None, "archived_at": None,
                 "cancel_initiated_at": None, "results_url": None, "processing_status": "in_progress",
                 "request_counts": {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0}}  # fmt: skip
        if request.method == "POST" and path == "/v1/messages/batches":
            self.created = json.loads(request.content)["requests"]
            return httpx2.Response(200, json=batch)
        if path == "/v1/messages/batches/msgbatch_1":
            if self.ended:
                batch |= {"processing_status": "ended",
                          "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_1/results"}  # fmt: skip
            return httpx2.Response(200, json=batch)
        if path.endswith("/results"):
            lines = [json.dumps(self._result(req)) for req in self.created]
            return httpx2.Response(200, content="\n".join(lines).encode(),
                                   headers={"content-type": "application/x-jsonl"})  # fmt: skip
        return httpx2.Response(404)

    def _result(self, req: dict[str, Any]) -> dict[str, Any]:
        answer = self.answers.get(req["custom_id"])
        if answer == "errored":
            return {"custom_id": req["custom_id"], "result": {"type": "errored", "error": {
                "type": "error", "error": {"type": "api_error", "message": "x"}}}}  # fmt: skip
        ids = [
            int(line.split("]")[0][1:])
            for line in req["params"]["messages"][0]["content"].split("\n")
        ]
        items = answer or [
            {"id": i, "event_type": "product", "sentiment": 0.6, "materiality": 0.4} for i in ids
        ]
        message = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": req["params"]["model"],
            "stop_reason": "end_turn", "stop_sequence": None,
            "content": [{"type": "text", "text": json.dumps({"items": items})}],
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 500},
        }  # fmt: skip
        return {"custom_id": req["custom_id"], "result": {"type": "succeeded", "message": message}}


def client(fake: FakeBatches) -> AsyncAnthropic:
    return AsyncAnthropic(api_key="test", max_retries=0,
                          http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handler)))  # fmt: skip


def real_names(engine: Engine) -> None:
    names = {"AAPL": "Apple Inc.", "JPM": "JPMorgan Chase & Co.", "XOM": "Exxon Mobil Corporation"}
    with make_session_factory(engine)() as session:
        for member in session.scalars(select(m.UniverseMember)):
            member.name = names.get(member.ticker, member.name)
        session.commit()


def test_only_masked_text_ever_leaves_the_program(engine: Engine) -> None:
    seed_market(engine)
    real_names(engine)
    with make_session_factory(engine)() as session:
        items = dict(pending_items(session, CFG, 100))
    # Originals only (item 3 is a repost), for universe stocks.
    assert sorted(items) == [1, 2, 4, 5]
    assert items[1].startswith("[COMPANY] unveils a new product line")
    assert items[2].startswith("[COMPANY] to acquire a regional lender")
    assert items[4].startswith("Old [COMPANY] story")  # "Exxon Mobil Corporation" -> "Exxon"
    requests, chunks = build_requests(list(items.items()), CFG)
    assert [len(ids) for ids in chunks.values()] == [2, 2]
    sent = json.dumps(requests)
    for secret in ("AAPL", "JPM", "XOM", "Apple", "JPMorgan", "Exxon", "2026"):
        assert secret not in sent  # no ticker, company name or year in any request
    params = requests[0]["params"]
    assert params["model"] == "claude-haiku-5-5" and params["output_config"]["effort"] == "low"
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in params  # not allowed in batches
    assert "do not use any memory of how a stock moved" in SYSTEM


async def test_submit_then_collect_stores_features_and_costs(engine: Engine) -> None:
    seed_market(engine)
    sessions = make_session_factory(engine)
    fake = FakeBatches()
    api = client(fake)
    governor = CostGovernor(sessions, CFG.budget, now=lambda: AS_OF)

    batch_id, count, estimate = await submit(api, sessions, CFG, governor, limit=100)
    assert (batch_id, count) == ("msgbatch_1", 4) and 0 < estimate < 0.01
    assert await submit(api, sessions, CFG, governor, limit=100) == (None, 0, 0.0)  # all waiting

    waiting = await collect(api, sessions, CFG)
    assert waiting["still_running"] == ["msgbatch_1"] and waiting["stored"] == 0

    fake.ended = True
    first, second = (req["custom_id"] for req in fake.created)
    fake.answers[second] = "errored"
    ids = [1, 2]
    fake.answers[first] = [
        {
            "id": ids[0],
            "event_type": "product",
            "sentiment": 7.0,
            "materiality": 0.5,
        },  # out of range
        {
            "id": 424242,
            "event_type": "mna",
            "sentiment": 0.9,
            "materiality": 0.9,
        },  # an id never sent
    ]
    done = await collect(api, sessions, CFG)
    assert done["stored"] == 1 and done["failed_requests"] == 1 and done["still_running"] == []

    with sessions() as session:
        feature = session.scalars(select(m.NewsFeature)).one()
        assert (feature.news_id, feature.version, feature.sentiment) == (1, VERSION, 1.0)  # clamped
        usage = session.scalars(select(m.ApiUsage)).one()
        assert (
            usage.is_batch and usage.batch_id == "msgbatch_1" and usage.purpose == "text_features"
        )
        # Haiku 5.5 at half price: (1000 x 0.10 + 200 x 0.50 + 500 x 0.01) / 1e6 / 2
        assert usage.cost_usd == pytest.approx((1000 * 0.10 + 200 * 0.50 + 500 * 0.01) / 1e6 / 2)
        assert done["cost_usd"] == pytest.approx(usage.cost_usd)
        # The items that got no answer are eligible again; the stored one is not.
        assert [i for i, _t in pending_items(session, CFG, 100)] == [2, 4, 5]
        # ...and the stored features reach the dataset.
        today = build_dataset(session, CFG, as_of=AS_OF, live=True).set_index("ticker")
        assert today.loc["AAPL", "llm_count"] == 1.0 and today.loc["AAPL", "llm_sent_mean"] == 1.0
        assert today.loc["JPM", "llm_count"] == 0.0  # no features yet for that story


async def test_nothing_is_sent_when_the_budget_is_spent(engine: Engine) -> None:
    seed_market(engine)
    sessions = make_session_factory(engine)
    with sessions() as session:
        session.add(
            m.ApiUsage(ts=AS_OF, purpose="earlier", model="claude-haiku-5-5", cost_usd=100.0)
        )
        session.commit()
    fake = FakeBatches()
    with pytest.raises(BudgetExceededError):
        await submit(
            client(fake),
            sessions,
            CFG,
            CostGovernor(sessions, CFG.budget, now=lambda: AS_OF),
            limit=10,
        )
    assert fake.created == []
