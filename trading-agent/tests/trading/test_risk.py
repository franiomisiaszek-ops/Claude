from __future__ import annotations

import pytest

from papertrader.trading.risk import Book, Proposal, check_entry

from ..ingest.helpers import shipped_config

CFG = shipped_config()
RISK, TRADING = CFG.risk, CFG.trading
SECTORS = {"AAPL": "Tech", "MSFT": "Tech", "NVDA": "Tech", "JPM": "Fin", "XOM": "Energy"}


def book(**kwargs: object) -> Book:
    fields: dict[str, object] = {
        "equity": 100_000.0,
        "cash": 100_000.0,
        "sectors": SECTORS,
    } | kwargs
    return Book(**fields)  # type: ignore[arg-type]


def buy(ticker: str = "AAPL", **kwargs: object) -> Proposal:
    fields: dict[str, object] = {"ticker": ticker, "action": "buy", "entry_low": 99.5, "entry_high": 100.5,
                                 "target_price": 106.0, "stop_price": 97.0, "size_pct": 0.10} | kwargs  # fmt: skip
    return Proposal(**fields)  # type: ignore[arg-type]


def test_a_normal_buy_is_sized_to_ten_percent_with_a_capped_limit() -> None:
    v = check_entry(buy(), 100.0, book(), RISK, TRADING)
    assert v.ok and v.side == "buy"
    assert v.limit_price == pytest.approx(100.3)  # last trade + 0.3%, inside the entry zone
    assert v.qty == 99  # floor(10,000 / 100.3)
    assert (v.take_profit, v.stop_loss) == (106.0, 97.0)
    assert v.notional <= 10_000


def test_the_judge_cannot_ask_for_more_than_the_position_limit() -> None:
    assert check_entry(buy(size_pct=0.5), 100.0, book(), RISK, TRADING).qty == 99
    assert check_entry(buy(size_pct=0.05), 100.0, book(), RISK, TRADING).qty == 49


@pytest.mark.parametrize(
    ("proposal", "price", "reason"),
    [
        (buy(action="sell_short"), 100.0, "short selling is disabled"),
        (buy(), 101.0, "run above the entry zone"),
        (buy(), 96.5, "at or below the stop"),
        (buy(), None, "no current price"),
        (buy(target_price=100.4), 100.0, "too close"),
        (buy(stop_price=100.2), 100.0, "at or below the stop"),
    ],
)
def test_bad_entries_are_refused(proposal: Proposal, price: float | None, reason: str) -> None:
    verdict = check_entry(proposal, price, book(), RISK, TRADING)
    assert not verdict.ok and reason in verdict.reason


def test_no_second_position_in_the_same_stock_and_a_cap_on_positions() -> None:
    assert (
        "already held"
        in check_entry(buy(), 100.0, book(held={"AAPL": 5000.0}), RISK, TRADING).reason
    )
    assert (
        "already held"
        in check_entry(buy(), 100.0, book(pending={"AAPL": 5000.0}), RISK, TRADING).reason
    )
    full = book(held={f"T{i}": 1000.0 for i in range(10)})
    assert "maximum of 10 positions" in check_entry(buy(), 100.0, full, RISK, TRADING).reason


def test_the_sector_limit_shrinks_or_refuses_the_order() -> None:
    nearly_full = book(held={"MSFT": 10_000.0, "NVDA": 15_000.0})  # 25% in Tech already
    v = check_entry(buy(), 100.0, nearly_full, RISK, TRADING)
    assert v.ok and v.qty == 49  # only 5% of equity left in the sector
    full = book(held={"MSFT": 10_000.0, "NVDA": 20_000.0})
    refused = check_entry(buy(), 100.0, full, RISK, TRADING)
    assert not refused.ok and "Tech is already at its 30% limit" in refused.reason
    assert check_entry(buy("JPM"), 100.0, full, RISK, TRADING).ok  # other sectors are fine


def test_no_leverage_only_cash_not_yet_committed() -> None:
    v = check_entry(buy(), 100.0, book(cash=3_000.0, held={"XOM": 97_000.0}), RISK, TRADING)
    assert v.ok and v.qty == 29  # 3,000 of cash left
    broke = check_entry(buy(), 100.0, book(cash=50.0), RISK, TRADING)
    assert not broke.ok and "not enough room" in broke.reason


def test_a_test_order_can_ask_for_fewer_shares_never_more() -> None:
    assert check_entry(buy(qty_override=1), 100.0, book(), RISK, TRADING).qty == 1
    assert check_entry(buy(qty_override=10_000), 100.0, book(), RISK, TRADING).qty == 99


def test_book_accounts_for_orders_placed_in_the_same_run() -> None:
    b = book()
    b.add("MSFT", 10_000.0)
    b.add("NVDA", 10_000.0)
    v = check_entry(buy(), 100.0, b, RISK, TRADING)
    assert v.ok and v.qty == 99  # 30% sector limit: exactly 10% left
    b.add("AAPL", v.notional)
    assert check_entry(buy("JPM"), 100.0, b, RISK, TRADING).ok  # a different sector still has room


def test_market_orders_have_no_limit_price() -> None:
    market = TRADING.model_copy(update={"order_type": "market"})
    v = check_entry(buy(), 100.0, book(), RISK, market)
    assert v.ok and v.limit_price is None and v.notional == pytest.approx(v.qty * 100.3)


def test_shorts_when_allowed_are_mirrored() -> None:
    allowed = RISK.model_copy(update={"allow_shorts": True})
    short = buy(action="sell_short", target_price=94.0, stop_price=103.0)
    v = check_entry(short, 100.0, book(), allowed, TRADING)
    assert v.ok and v.side == "sell" and v.limit_price == pytest.approx(99.7)
    assert "below the entry zone" in check_entry(short, 98.0, book(), allowed, TRADING).reason
