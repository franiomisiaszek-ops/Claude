"""C's two new order types build valid requests in the real Alpaca SDK (network replaced)."""

from __future__ import annotations

from typing import Any

from papertrader.trading.broker import AlpacaPaperBroker


def test_limit_and_stop_orders_use_the_sdks_own_request_types() -> None:
    broker = AlpacaPaperBroker("PKTESTKEY000000000001", "secret")
    sent: list[Any] = []

    def submit(request: Any) -> dict[str, Any]:
        sent.append(request)
        return {"id": f"o{len(sent)}", "client_order_id": request.client_order_id, "symbol": request.symbol,
                "side": request.side.value, "type": request.type.value, "order_class": "simple",
                "time_in_force": request.time_in_force.value, "qty": str(request.qty), "status": "new",
                "limit_price": str(getattr(request, "limit_price", "") or ""),
                "stop_price": str(getattr(request, "stop_price", "") or ""), "legs": None}  # fmt: skip

    broker._client.submit_order = submit  # type: ignore[method-assign]
    limit = broker.submit_limit(
        symbol="GLD", qty=11, side="buy", limit_price=187.123, client_order_id="c-p1-1-GLD"
    )
    stop = broker.submit_stop(
        symbol="GLD", qty=11, stop_price=150.0, client_order_id="c-stop-GLD-1"
    )
    first, second = sent
    assert (first.type.value, first.time_in_force.value, float(first.limit_price)) == (
        "limit",
        "day",
        187.12,
    )
    assert (second.type.value, second.time_in_force.value, second.side.value) == (
        "stop",
        "gtc",
        "sell",
    )
    assert limit.status == "new" and stop.client_order_id == "c-stop-GLD-1"
