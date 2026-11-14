"""A fake Claude API behind the real Anthropic SDK, and a small seeded market.

The SDK itself is not mocked: requests go through its real code and are answered by
an in-memory transport, so request shapes, retries and response parsing are exercised.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import httpx2
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient
from sqlalchemy import Engine

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.store import upsert
from papertrader.ingest.timeutil import at_new_york, daily_bar_available_at, new_york_date
from papertrader.llm.client import ClaudeClient
from papertrader.llm.governor import CostGovernor

from ..ingest.helpers import daily_bars, shipped_config

AS_OF = datetime(2026, 11, 13, 13, 20, tzinfo=UTC)  # Friday 08:20 New York
THURSDAY, FRIDAY = date(2026, 11, 12), date(2026, 11, 13)
FUNDS = ["SPY", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLK", "XLB", "XLRE", "XLU"]
STOCKS = {
    "AAPL": "Information Technology", "MSFT": "Information Technology",
    "JPM": "Financials", "XOM": "Energy", "PFE": "Health Care",
}  # fmt: skip


@dataclass
class Call:
    label: str
    body: dict[str, Any]
    headers: dict[str, str]

    @property
    def user(self) -> str:
        return str(self.body["messages"][0]["content"])


def report(ticker: str, direction: str = "long", confidence: float = 0.7, *sources: str,
           event_type: str = "other", horizon: int = 5) -> dict[str, Any]:  # fmt: skip
    return {
        "ticker": ticker, "direction": direction, "confidence": confidence,
        "horizon_days": horizon, "event_type": event_type, "thesis": f"{direction} {ticker} thesis",
        "evidence": [{"claim": f"claim from {s}", "source_id": s} for s in sources],
        "risks": ["a risk"], "catalysts": [],
    }  # fmt: skip


def agent_answer(*reports: dict[str, Any]) -> dict[str, Any]:
    return {"reports": list(reports), "summary": "looked at the context"}


class FakeClaude:
    """Answers each request according to which agent sent it."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[Call] = []
        self.events: list[tuple[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = 0.0
        self._cached: set[tuple[str, str]] = set()

    @staticmethod
    def label_of(body: dict[str, Any]) -> str:
        specific = body["system"][-1]["text"]
        name = specific.split("AGENT:", 1)[1].split()[0]
        sector = re.search(r"Your sector: (.+?)\. Sector fund", str(body["messages"][0]["content"]))
        return f"{name}:{sector.group(1)}" if sector and name == "sector_analyst" else name

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        label = self.label_of(body)
        call = Call(label, body, dict(request.headers))
        self.calls.append(call)
        self.events.append(("start", label))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._respond(call)
        finally:
            self.in_flight -= 1
            self.events.append(("end", label))

    def _respond(self, call: Call) -> httpx2.Response:
        answer = self.responses.get(call.label, self.responses.get(call.label.split(":")[0]))
        if callable(answer):
            answer = answer(call)
        if isinstance(answer, int):
            kind = {401: "authentication_error", 429: "rate_limit_error"}.get(answer, "api_error")
            return httpx2.Response(
                answer, headers={"retry-after": "0"},
                json={"type": "error", "error": {"type": kind, "message": f"fake {answer}"}},
            )  # fmt: skip
        if answer is None:
            answer = {"market_summary": "quiet", "trades": [], "exits": [], "rejected": []} \
                if call.label == "judge" else agent_answer()  # fmt: skip

        model = call.body["model"]
        shared = call.body["system"][0]["text"]
        cached = (model, shared) in self._cached
        self._cached.add((model, shared))
        prompt_tokens = len(call.user) // 4
        usage = {
            "input_tokens": prompt_tokens, "output_tokens": 400,
            "cache_creation_input_tokens": 0 if cached else 900,
            "cache_read_input_tokens": 900 if cached else 0,
        }  # fmt: skip
        message: dict[str, Any] = {
            "id": f"msg_{len(self.calls)}", "type": "message", "role": "assistant", "model": model,
            "stop_reason": "end_turn", "stop_sequence": None, "stop_details": None, "usage": usage,
            "content": [{"type": "thinking", "thinking": "", "signature": "s"},
                        {"type": "text", "text": json.dumps(answer)}],
        }  # fmt: skip
        if answer == "refusal":
            message |= {"stop_reason": "refusal", "content": [],
                        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"}}  # fmt: skip
        elif answer == "max_tokens":
            message |= {
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": '{"repor'}],
            }
        elif isinstance(answer, dict) and "_served_by" in answer:
            message["model"] = answer.pop("_served_by")
            message["content"][1]["text"] = json.dumps(answer)
        return httpx2.Response(200, headers={"request-id": f"req_{len(self.calls)}"}, json=message)

    def labels(self) -> list[str]:
        return [call.label for call in self.calls]

    def call(self, label: str) -> Call:
        return next(c for c in self.calls if c.label == label)


def make_claude(
    engine: Engine,
    fake: FakeClaude,
    *,
    config: AppConfig | None = None,
    cycle_cap: float | None = None,
    now: Callable[[], datetime] = lambda: AS_OF,
    max_retries: int = 0,
) -> ClaudeClient:
    config = config or shipped_config()
    sessions = make_session_factory(engine)
    api = AsyncAnthropic(
        api_key="test-key", max_retries=max_retries,
        http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(fake.handler)),
    )  # fmt: skip
    governor = CostGovernor(sessions, config.budget, now=now, cycle_cap_usd=cycle_cap)
    return ClaudeClient(api, sessions, config.budget, governor)


def judge_buys(*tickers: str, confidence: float = 0.8) -> Callable[[Call], dict[str, Any]]:
    """A judge that buys the given tickers, citing the real report ids it was shown."""

    def answer(call: Call) -> dict[str, Any]:
        trades = []
        for ticker in tickers:
            ids = re.findall(rf"\[report:(\d+)\] [^|]+\| (LONG|SHORT|NEUTRAL) {ticker} ", call.user)
            found = re.search(rf"price: {ticker} \([^)]*\) \| close ([\d.]+)", call.user)
            close = float(found.group(1)) if found else 100.0  # no report on it: no price shown
            trades.append({
                "ticker": ticker, "action": "buy", "confidence": confidence,
                "entry_low": round(close * 0.995, 2), "entry_high": round(close * 1.005, 2),
                "target_price": round(close * 1.06, 2), "stop_price": round(close * 0.97, 2),
                "size_pct": 0.08, "horizon_days": 5, "rationale": f"buy {ticker}", "dissent": "",
                "supporting_reports": [f"report:{i}" for i, d in ids if d == "LONG"],
                "opposing_reports": [f"report:{i}" for i, d in ids if d != "LONG"],
            })  # fmt: skip
        return {"market_summary": "A calm market.", "trades": trades, "exits": [],
                "rejected": [{"ticker": "XOM", "reason": "thin evidence"}]}  # fmt: skip

    return answer


def seed_market(engine: Engine, *, with_events: bool = True) -> None:
    """Five stocks in four sectors, the sector funds, and one of each kind of event."""
    sessions = make_session_factory(engine)
    with sessions() as session:
        for index, (ticker, sector) in enumerate(STOCKS.items()):
            session.add(m.UniverseMember(index_name="sp500", ticker=ticker, name=f"{ticker} Corp",
                                         gics_sector=sector, cik=f"{index + 1:010d}", is_active=True))  # fmt: skip
        bars = []
        for index, ticker in enumerate([*STOCKS, *FUNDS]):
            for bar in daily_bars(date(2025, 10, 1), FRIDAY, price=50 + 20 * index):
                day = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
                bars.append({
                    "ticker": ticker, "timeframe": "1Day", "ts": day, "open": bar["o"],
                    "high": bar["h"], "low": bar["l"], "close": bar["c"], "volume": bar["v"],
                    "vwap": bar["vw"], "trade_count": bar["n"],
                    "available_at": daily_bar_available_at(new_york_date(day)),
                })  # fmt: skip
        upsert(session, m.PriceBar, bars, conflict=("ticker", "timeframe", "ts"))
        if with_events:
            _seed_events(session)
        session.commit()


def _news(session: Any, item_id: int, headline: str, tickers: list[str], at: datetime,
          duplicate_of: int | None = None) -> None:  # fmt: skip
    session.add(m.NewsItem(
        id=item_id, source="alpaca", source_item_id=str(item_id), headline=headline,
        summary="Details of the story.", url=f"https://news.example/{item_id}", published_at=at,
        available_at=at, content_hash=f"{item_id:064d}", duplicate_of_id=duplicate_of,
        tickers=[m.NewsTicker(ticker=t) for t in tickers],
    ))  # fmt: skip


def _seed_events(session: Any) -> None:
    hour = timedelta(hours=1)
    _news(session, 1, "Apple unveils a new product line", ["AAPL"], AS_OF - 3 * hour)
    _news(session, 2, "JPMorgan to acquire a regional lender", ["JPM"], AS_OF - 5 * hour)
    _news(session, 3, "Apple unveils a new product line (repost)", ["AAPL"], AS_OF - 2 * hour, 1)
    _news(session, 4, "Old Exxon story from last week", ["XOM"], AS_OF - timedelta(days=6))
    _news(session, 5, "Published AFTER the decision time", ["AAPL"], AS_OF + hour)

    def filing(fid: int, ticker: str, cik: str, form: str, at: datetime, items: str | None = None,
               excerpt: str | None = None) -> None:  # fmt: skip
        session.add(m.Filing(id=fid, accession_no=f"0000-26-{fid:06d}", cik=cik, ticker=ticker,
                             form_type=form, items=items, url=f"https://sec.example/{fid}.htm",
                             published_at=at, available_at=at, text_excerpt=excerpt))  # fmt: skip

    filing(1, "PFE", "0000000005", "8-K", AS_OF - 4 * hour, "5.02",
           "Item 5.02 The Chief Financial Officer resigned effective immediately.")  # fmt: skip
    filing(2, "PFE", "0000000005", "8-K", AS_OF + 2 * hour, "1.01")  # accepted after the decision
    filing(3, "AAPL", "0000000001", "4", AS_OF - 30 * hour)

    # MSFT reported last night; JPM reports next week; XOM "reported" but only after as_of.
    session.add(m.EarningsEvent(source="finnhub", ticker="MSFT", report_date=THURSDAY,
                                time_of_day="amc", eps_estimate=3.0, eps_actual=3.3,
                                revenue_estimate=70e9, revenue_actual=72e9,
                                actual_available_at=at_new_york(THURSDAY, time(20, 0)),
                                available_at=at_new_york(THURSDAY, time(20, 0))))  # fmt: skip
    session.add(m.EarningsEvent(source="finnhub", ticker="JPM", report_date=date(2026, 11, 17),
                                time_of_day="bmo", eps_estimate=4.5, available_at=AS_OF - 48 * hour))  # fmt: skip
    session.add(m.EarningsEvent(source="finnhub", ticker="XOM", report_date=FRIDAY,
                                time_of_day="amc", eps_estimate=2.0, eps_actual=9.9,
                                actual_available_at=at_new_york(FRIDAY, time(20, 0)),
                                available_at=AS_OF - 48 * hour))  # fmt: skip

    def insider(line: int, ticker: str, name: str, code: str, shares: float, price: float,
                at: datetime, plan: bool | None = None) -> None:  # fmt: skip
        session.add(m.InsiderTransaction(
            filing_id=3, line_no=line, ticker=ticker, insider_name=name, insider_title="Director",
            transaction_date=at.date(), transaction_code=code, acquired=code == "P", shares=shares,
            price=price, under_10b5_1_plan=plan, available_at=at,
        ))  # fmt: skip

    insider(0, "XOM", "Alice", "P", 1000, 100.0, AS_OF - 72 * hour)
    insider(1, "XOM", "Bob", "P", 500, 100.0, AS_OF - 48 * hour)
    insider(2, "AAPL", "Carol", "S", 2000, 200.0, AS_OF - 24 * hour, True)
    insider(3, "AAPL", "Dan", "A", 9999, 0.0, AS_OF - 24 * hour)  # a grant, not a trade
    insider(4, "JPM", "Eve", "P", 5000, 150.0, AS_OF + hour)  # filed after the decision

    filing(4, "", "0001067983", "13F-HR", AS_OF - 30 * 24 * hour)
    filing(5, "", "0001067983", "13F-HR", AS_OF - 24 * hour)
    for fid, period, shares, at in ((4, date(2026, 3, 31), 1000.0, AS_OF - 120 * 24 * hour),
                                    (5, date(2026, 9, 30), 1500.0, AS_OF - 24 * hour)):  # fmt: skip
        session.add(m.InstitutionalHolding(
            filing_id=fid, line_no=0, manager_cik="0001067983", manager_name="Big Fund",
            ticker="XOM", cusip="30231G102", report_period=period, shares=shares,
            value_usd=shares * 100, available_at=at,
        ))  # fmt: skip

    def macro(series: str, day: date, published: date, value: float, at: datetime) -> None:
        session.add(m.MacroObservation(series_id=series, observation_date=day,
                                       realtime_start=published, value=value, available_at=at))  # fmt: skip

    macro("DGS10", date(2026, 11, 10), date(2026, 11, 10), 4.20, AS_OF - 60 * hour)
    macro("DGS10", date(2026, 10, 1), date(2026, 10, 1), 4.00, AS_OF - 40 * 24 * hour)
    # CPI for September: first published in October, then revised AFTER the decision time.
    macro("CPIAUCSL", date(2026, 9, 1), date(2026, 10, 10), 330.0, AS_OF - 30 * 24 * hour)
    macro("CPIAUCSL", date(2026, 9, 1), date(2026, 11, 14), 331.5, AS_OF + 24 * hour)
