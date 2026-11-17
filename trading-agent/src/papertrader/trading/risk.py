"""Risk rules for new positions. Pure functions: no broker, no database.

Every rule here is a hard limit from config.yaml. A trade the judge likes is still
refused, or made smaller, if it would break one of them.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from papertrader.config import RiskConfig, TradingConfig

# A take-profit or stop this close to the entry is noise, and Alpaca requires a gap.
MIN_BRACKET_GAP = 0.002


@dataclass
class Book:
    """What the account holds and has committed, in dollars."""

    equity: float
    cash: float
    held: dict[str, float] = field(default_factory=dict)  # ticker -> market value (abs)
    pending: dict[str, float] = field(default_factory=dict)  # ticker -> notional of open entries
    sectors: Mapping[str, str | None] = field(default_factory=dict)

    def sector_value(self, sector: str | None) -> float:
        tickers = set(self.held) | set(self.pending)
        return sum(
            self.held.get(t, 0.0) + self.pending.get(t, 0.0)
            for t in tickers
            if self.sectors.get(t) == sector
        )

    @property
    def committed(self) -> float:
        return sum(self.held.values()) + sum(self.pending.values())

    def add(self, ticker: str, notional: float) -> None:
        self.pending[ticker] = self.pending.get(ticker, 0.0) + notional
        self.cash -= notional


@dataclass(frozen=True)
class Proposal:
    ticker: str
    action: str  # buy | sell_short
    entry_low: float
    entry_high: float
    target_price: float
    stop_price: float
    size_pct: float
    qty_override: int | None = None  # test orders


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str
    qty: int = 0
    side: str = "buy"
    limit_price: float | None = None
    take_profit: float = 0.0
    stop_loss: float = 0.0
    reference_price: float = 0.0  # the most one share can cost

    @property
    def notional(self) -> float:
        return self.qty * self.reference_price


def _cents(price: float) -> float:
    """US stocks above $1 are priced in whole cents (below $1, in hundredths of a cent)."""
    return round(price, 2) if price >= 1 else round(price, 4)


def refuse(reason: str) -> Verdict:
    return Verdict(False, reason)


def check_entry(
    proposal: Proposal, price: float | None, book: Book, risk: RiskConfig, trading: TradingConfig
) -> Verdict:
    p = proposal
    if p.action == "sell_short" and not risk.allow_shorts:
        return refuse("short selling is disabled")
    if p.ticker in book.held or p.ticker in book.pending:
        return refuse("already held or already ordered")
    if len(set(book.held) | set(book.pending)) >= risk.max_open_positions:
        return refuse(f"already at the maximum of {risk.max_open_positions} positions")
    if price is None or price <= 0:
        return refuse("no current price")
    if book.equity <= 0:
        return refuse("account equity is zero")

    buy = p.action == "buy"
    if buy:
        if price > p.entry_high * (1 + trading.limit_slippage_pct):
            return refuse(
                f"price {price:.2f} has run above the entry zone (up to {p.entry_high:.2f})"
            )
        if price <= p.stop_price:
            return refuse(f"price {price:.2f} is already at or below the stop {p.stop_price:.2f}")
        limit = _cents(min(p.entry_high, price * (1 + trading.limit_slippage_pct)))
    else:
        if price < p.entry_low * (1 - trading.limit_slippage_pct):
            return refuse(
                f"price {price:.2f} has fallen below the entry zone (from {p.entry_low:.2f})"
            )
        if price >= p.stop_price:
            return refuse(f"price {price:.2f} is already at or above the stop {p.stop_price:.2f}")
        limit = _cents(max(p.entry_low, price * (1 - trading.limit_slippage_pct)))
    reference = limit  # the most the entry can cost per share

    far_side = p.target_price - reference if buy else reference - p.target_price
    near_side = reference - p.stop_price if buy else p.stop_price - reference
    if far_side < reference * MIN_BRACKET_GAP or near_side < reference * MIN_BRACKET_GAP:
        return refuse("target or stop is too close to the entry price")

    # Size: the judge's suggestion, never above the per-position limit.
    budget = book.equity * min(p.size_pct, risk.max_position_pct)
    # Sector limit, counting what is held and what is already ordered.
    sector = book.sectors.get(p.ticker)
    budget = min(budget, risk.max_sector_pct * book.equity - book.sector_value(sector))
    # No leverage: only cash that is not already committed to open entries.
    budget = min(budget, book.cash, book.equity - book.committed)
    qty = math.floor(budget / reference) if budget > 0 else 0
    if p.qty_override is not None:
        qty = min(qty, p.qty_override)
    if qty < 1:
        if book.sector_value(sector) + reference > risk.max_sector_pct * book.equity:
            return refuse(f"sector {sector} is already at its {risk.max_sector_pct:.0%} limit")
        return refuse("not enough room for even one share within the limits")

    return Verdict(
        ok=True, reason="ok", qty=qty, side="buy" if buy else "sell",
        limit_price=limit if trading.order_type == "limit" else None,
        take_profit=p.target_price, stop_loss=p.stop_price, reference_price=reference,
    )  # fmt: skip
