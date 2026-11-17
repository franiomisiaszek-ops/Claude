from __future__ import annotations

import shutil
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest
from alpaca.common.rest import RESTClient
from sqlalchemy import Engine, select
from typer.testing import CliRunner

from papertrader.cli import app
from papertrader.db import models as m
from papertrader.db.session import make_session_factory
from papertrader.ingest.timeutil import at_new_york
from papertrader.safety import LiveTradingForbiddenError
from papertrader.trading import broker as broker_module
from papertrader.trading.broker import AlpacaPaperBroker
from papertrader.trading.thesis import find_candidates, review

from ..conftest import PROJECT_ROOT
from ..ingest.helpers import shipped_config
from ..research.helpers import FakeClaude, make_claude
from .helpers import MONDAY, OPEN_TIME, FakeBroker, idea, make_engine, seed_decision

CFG = shipped_config()


def holding(engine: Engine, ticker: str = "AAPL", price: float = 200.0):  # type: ignore[no-untyped-def]
    trading, broker, clock = make_engine(engine)
    broker.prices[ticker] = price
    seed_decision(engine, [idea(ticker, price, rationale="New product cycle")],
                  reports=[(ticker, "news_event", m.Stance.SUPPORT)])  # fmt: skip
    trading.place_orders()
    broker.fill_entry(ticker, price)
    trading.sync()
    return trading, broker, clock


def add_news(
    engine: Engine, news_id: int, ticker: str, headline: str, minutes_after_open: int
) -> None:
    at = OPEN_TIME + timedelta(minutes=minutes_after_open)
    with make_session_factory(engine)() as session:
        session.add(m.NewsItem(id=news_id, source="alpaca", source_item_id=str(news_id), headline=headline,
                               published_at=at, available_at=at, content_hash=f"{news_id:064d}",
                               tickers=[m.NewsTicker(ticker=ticker)]))  # fmt: skip
        session.commit()


# ----------------------------------------------------------------------------- thesis break


def test_only_fresh_clearly_negative_news_on_a_held_stock_is_a_candidate(engine: Engine) -> None:
    _trading, _broker, clock = holding(engine)
    add_news(
        engine, 1, "AAPL", "Apple shares plunge after weak guidance and a lawsuit", -30
    )  # before entry
    add_news(engine, 2, "AAPL", "Apple holds its annual meeting", 30)  # neutral
    add_news(engine, 3, "MSFT", "Microsoft plunges after weak results miss", 30)  # not held
    clock[0] = OPEN_TIME + timedelta(hours=2)
    with make_session_factory(engine)() as session:
        assert find_candidates(session, CFG, clock[0]) == []
    add_news(engine, 4, "AAPL", "Apple shares plunge after weak results miss and a recall", 60)
    with make_session_factory(engine)() as session:
        (candidate,) = find_candidates(session, CFG, clock[0])
    assert candidate.ticker == "AAPL" and candidate.news_ids == [4]


async def test_a_confident_broken_thesis_sells_and_is_reviewed_only_once(engine: Engine) -> None:
    trading, broker, clock = holding(engine)
    add_news(engine, 4, "AAPL", "Apple shares plunge after weak results miss and a recall", 60)
    clock[0] = OPEN_TIME + timedelta(hours=2)
    fake = FakeClaude()
    fake.responses["news_event"] = {"reports": [{
        "ticker": "AAPL", "direction": "short", "confidence": 0.7, "horizon_days": 5, "event_type": "product",
        "thesis": "recall undermines the product cycle", "evidence": [{"claim": "recall", "source_id": "news:4"}],
        "risks": [], "catalysts": []}], "summary": "s"}  # fmt: skip
    fake.responses["thesis_review"] = {
        "exit": True,
        "confidence": 0.8,
        "reason": "The recall breaks the product thesis.",
    }
    claude = make_claude(engine, fake, now=lambda: clock[0])
    with make_session_factory(engine)() as session:
        (candidate,) = find_candidates(session, CFG, clock[0])

    result = await review(candidate, engine=trading, claude=claude, config=CFG, now=clock[0])
    assert result.exited and "exit order placed" in result.detail
    assert fake.labels() == ["news_event", "thesis_review"]
    judge_prompt = fake.call("thesis_review").user
    assert "New product cycle" in judge_prompt and "news_event: t" in judge_prompt
    assert (
        "recall undermines the product cycle" in judge_prompt and "bought at 200.00" in judge_prompt
    )
    assert fake.call("thesis_review").body["model"] == "claude-opus-5-5"
    with make_session_factory(engine)() as session:
        trade = session.scalars(select(m.Trade)).one()
        assert trade.exit_trigger == m.ExitTrigger.THESIS_BREAK
        cycle = session.scalars(
            select(m.DecisionCycle).where(m.DecisionCycle.kind == m.CycleKind.THESIS_BREAK)
        ).one()
        assert cycle.status == m.RunStatus.SUCCEEDED and cycle.notes["exit"] is True
        assert (
            find_candidates(session, CFG, clock[0] + timedelta(hours=1)) == []
        )  # cooldown, and exit underway
    broker.fill_market("AAPL", 195.0)
    trading.sync()
    with make_session_factory(engine)() as session:
        assert session.scalars(select(m.Trade)).one().status == m.TradeStatus.CLOSED


@pytest.mark.parametrize("answer", [{"exit": True, "confidence": 0.5, "reason": "maybe"},
                                    {"exit": False, "confidence": 0.9, "reason": "noise"}])  # fmt: skip
async def test_an_unconvinced_judge_keeps_the_position(
    engine: Engine, answer: dict[str, Any]
) -> None:
    trading, broker, clock = holding(engine)
    add_news(engine, 4, "AAPL", "Apple shares plunge after weak results miss", 60)
    clock[0] = OPEN_TIME + timedelta(hours=2)
    fake = FakeClaude()
    fake.responses["thesis_review"] = answer
    with make_session_factory(engine)() as session:
        (candidate,) = find_candidates(session, CFG, clock[0])
    result = await review(candidate, engine=trading, claude=make_claude(engine, fake, now=lambda: clock[0]),
                          config=CFG, now=clock[0])  # fmt: skip
    assert not result.exited and result.detail.startswith("keep")
    assert not [o for o in broker.orders.values() if o.order_type == "market"]
    with make_session_factory(engine)() as session:
        signal = session.scalars(select(m.ExitSignal)).one()
        assert not signal.acted_on and signal.reason.startswith("kept:")
        # Reviewed: the same news does not trigger again within the cooldown...
        assert find_candidates(session, CFG, clock[0] + timedelta(hours=5)) == []
    # ...but new bad news after the cooldown does.
    add_news(engine, 5, "AAPL", "Apple plunges again after a weak outlook and a lawsuit", 60 * 8)
    with make_session_factory(engine)() as session:
        (again,) = find_candidates(session, CFG, clock[0] + timedelta(hours=7))
    assert again.news_ids == [5]


async def test_a_failed_review_never_sells(engine: Engine) -> None:
    trading, broker, clock = holding(engine)
    add_news(engine, 4, "AAPL", "Apple shares plunge after weak results miss", 60)
    clock[0] = OPEN_TIME + timedelta(hours=2)
    fake = FakeClaude()
    fake.responses["thesis_review"] = 529
    with make_session_factory(engine)() as session:
        (candidate,) = find_candidates(session, CFG, clock[0])
    result = await review(candidate, engine=trading, claude=make_claude(engine, fake, now=lambda: clock[0]),
                          config=CFG, now=clock[0])  # fmt: skip
    assert not result.exited and "review not possible" in result.detail
    assert not [o for o in broker.orders.values() if o.order_type == "market"]


# ----------------------------------------------------------------------------- the real Alpaca adapter


def test_the_adapter_refuses_live_keys_and_live_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(LiveTradingForbiddenError, match="LIVE"):
        AlpacaPaperBroker("AKLIVEKEY", "secret")
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(LiveTradingForbiddenError):
        AlpacaPaperBroker("PKPAPER", "secret")
    monkeypatch.delenv("APCA_API_BASE_URL")

    original = broker_module.TradingClient.__init__

    def live_client(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        self._base_url = "https://api.alpaca.markets"  # as if the SDK picked the live host

    monkeypatch.setattr(broker_module.TradingClient, "__init__", live_client)
    with pytest.raises(LiveTradingForbiddenError, match="not the paper endpoint"):
        AlpacaPaperBroker("PKPAPER", "secret")


def test_the_adapter_sends_paper_bracket_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    order = {"id": "o-1", "client_order_id": "pt-i7-entry", "symbol": "AAPL", "side": "buy", "type": "limit",
             "order_class": "bracket", "time_in_force": "gtc", "qty": "49", "status": "accepted",
             "filled_qty": "0", "limit_price": "200.6", "submitted_at": "2026-11-16T14:36:00Z",
             "legs": [{"id": "o-2", "client_order_id": "x", "symbol": "AAPL", "side": "sell", "type": "limit",
                       "order_class": "bracket", "time_in_force": "gtc", "qty": "49", "status": "held",
                       "limit_price": "212"},
                      {"id": "o-3", "client_order_id": "y", "symbol": "AAPL", "side": "sell", "type": "stop",
                       "order_class": "bracket", "time_in_force": "gtc", "qty": "49", "status": "held",
                       "stop_price": "194"}]}  # fmt: skip

    def fake_request(
        self: RESTClient, method: str, url: str, opts: dict[str, Any], retry: int
    ) -> Any:
        calls.append((method, url, opts.get("json") or opts.get("params") or {}))
        if url.endswith("/v2/orders") and method == "POST":
            return order
        if url.endswith("/v2/calendar"):
            return [{"date": "2026-11-27", "open": "09:30", "close": "13:00", "session_open": "0400",
                     "session_close": "1700", "settlement_date": "2026-12-01"}]  # fmt: skip
        if url.endswith("/trades/latest"):
            return {"trades": {"AAPL": {"p": 200.1, "s": 10, "t": "2026-11-16T14:36:00Z"}}}
        if url.endswith("/v2/account"):
            return {"equity": "100000", "last_equity": "101000", "cash": "90000", "status": "ACTIVE",
                    "trading_blocked": False}  # fmt: skip
        raise AssertionError(url)

    monkeypatch.setattr(RESTClient, "_one_request", fake_request)
    broker = AlpacaPaperBroker("PKPAPER", "secret")
    placed = broker.submit_bracket(symbol="AAPL", qty=49, side="buy", client_order_id="pt-i7-entry",
                                   limit_price=200.6, take_profit=212.004, stop_loss=193.996, time_in_force="gtc")  # fmt: skip
    method, url, body = calls[0]
    assert (method, url) == ("POST", "https://paper-api.alpaca.markets/v2/orders")
    assert (
        body["order_class"] == "bracket"
        and body["time_in_force"] == "gtc"
        and body["type"] == "limit"
    )
    assert body["take_profit"] == {"limit_price": 212.0} and body["stop_loss"] == {
        "stop_price": 194.0
    }
    assert (
        body["client_order_id"] == "pt-i7-entry"
        and body["qty"] == 49
        and body["limit_price"] == 200.6
    )
    assert [leg.order_type for leg in placed.legs] == ["limit", "stop"] and placed.qty == 49

    (half_day,) = broker.calendar(MONDAY, MONDAY + timedelta(days=20))
    assert half_day.close == at_new_york(half_day.day, time(13, 0))
    assert broker.latest_prices(["AAPL"]) == {"AAPL": 200.1}
    _m, url, params = calls[-1]
    assert url == "https://data.alpaca.markets/v2/stocks/trades/latest" and params["feed"] == "iex"
    account = broker.account()
    assert account.day_change_pct == pytest.approx(100_000 / 101_000 - 1)
    assert all("paper-api.alpaca.markets" in u or "data.alpaca.markets" in u for _m, u, _b in calls)


# ----------------------------------------------------------------------------- commands

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, FakeBroker]:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    url = f"sqlite:///{tmp_path / 'data' / 'app.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("ALPACA_API_KEY", "PKTEST")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "secret")
    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    clock = [OPEN_TIME]
    fake = FakeBroker(clock)
    fake.prices.update({"SPY": 600.0, "AAPL": 200.0})
    monkeypatch.setattr("papertrader.trading.broker.AlpacaPaperBroker", lambda *a, **k: fake)
    monkeypatch.setattr("papertrader.db.types.utcnow", lambda: clock[0])
    monkeypatch.setattr("time.sleep", lambda _s: None)

    async def no_news(*_args: Any, **_kwargs: Any) -> None:  # no network in tests
        return None

    monkeypatch.setattr("papertrader.ingest.news.ingest_alpaca_news", no_news)
    return tmp_path, fake


def test_trade_commands_need_paper_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copy(PROJECT_ROOT / "config.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    result = runner.invoke(app, ["trade", "status"])
    assert result.exit_code == 1 and "ALPACA_API_KEY and ALPACA_SECRET_KEY" in result.output


def test_test_order_then_status_close_and_reconcile(project: tuple[Path, FakeBroker]) -> None:
    _path, fake = project
    result = runner.invoke(app, ["trade", "test-order", "AAPL"])
    assert result.exit_code == 0, result.output
    assert "AAPL   submitted  1 shares limit 200.60" in result.output

    fake.fill_entry("AAPL", 200.3)
    result = runner.invoke(app, ["trade", "monitor", "--no-thesis"])
    assert result.exit_code == 0, result.output
    assert "opened ['AAPL']" in result.output and "[TEST] BOUGHT 1 AAPL @ $200.30" in result.output

    status = runner.invoke(app, ["trade", "status"])
    assert "Market: OPEN" in status.output and "Trading: active" in status.output
    assert "AAPL   1 @ 200.30 [TEST]" in status.output

    original = fake.submit_market

    def submit_and_fill(**kwargs: Any) -> Any:
        placed = original(**kwargs)
        fake.fill_market(kwargs["symbol"], 201.0)
        return fake.order(placed.id)

    fake.submit_market = submit_and_fill  # type: ignore[method-assign]
    closed = runner.invoke(app, ["trade", "close", "AAPL"])
    assert closed.exit_code == 0, closed.output
    assert "exit order placed" in closed.output and "Closed: AAPL" in closed.output
    assert "[TEST] CLOSED AAPL @ $201.00 (manual)" in closed.output

    eod = runner.invoke(app, ["trade", "reconcile"])
    assert eod.exit_code == 0 and "End of day Mon 16 Nov" in eod.output

    assert "paused" in runner.invoke(app, ["trade", "pause"]).output
    assert "Trading: PAUSED" in runner.invoke(app, ["trade", "status"]).output
    refused = runner.invoke(app, ["trade", "place"])
    assert refused.exit_code == 1 and "trading is paused" in refused.output
    assert "resumed" in runner.invoke(app, ["trade", "resume"]).output


def test_a_compressed_trading_day(project: tuple[Path, FakeBroker]) -> None:
    _path, fake = project
    original = fake.submit_bracket

    def fill_at_once(**kwargs: Any) -> Any:  # the paper account fills the test share immediately
        placed = original(**kwargs)
        fake.fill_entry(kwargs["symbol"], 600.2)
        return placed

    fake.submit_bracket = fill_at_once  # type: ignore[method-assign]
    original_market = fake.submit_market

    def market_and_fill(**kwargs: Any) -> Any:
        placed = original_market(**kwargs)
        fake.fill_market(kwargs["symbol"], 600.4)
        return placed

    fake.submit_market = market_and_fill  # type: ignore[method-assign]
    result = runner.invoke(app, ["trade", "day", "--minutes", "0"])
    assert result.exit_code == 0, result.output
    out = result.output
    for step in ("1. Research skipped", "2. Orders", "SPY    submitted  1 shares", "3. Monitoring",
                 "opened ['SPY']", "4. Selling the test share of SPY", "exit order placed", "5. End of day",
                 "[TEST] BOUGHT 1 SPY", "[TEST] CLOSED SPY @ $600.40 (manual)", "End of day Mon 16 Nov"):  # fmt: skip
        assert step in out, step
    assert not fake.holdings  # the account is left as it was


def test_the_day_command_refuses_a_closed_market(project: tuple[Path, FakeBroker]) -> None:
    _path, fake = project
    fake.is_open = False
    result = runner.invoke(app, ["trade", "day"])
    assert result.exit_code == 1 and "market is closed" in result.output
