"""X6: turning orders into warnings, and the hub's own warnings.

* `idea_pending` / `exit_pending`: the warning text for a buy at the open or a sell,
  used by systems B and C before they send an order (papertrader.notify.warnings).
* System A's warnings, written by the hub because A's code is frozen: its buys and the
  judge's sells before A's 09:35 run, and its time-limit sells. Information only.
* "Close to a stop or target" warnings for every system: stops and targets fill at
  Alpaca and can't be announced, so the hub warns when a live price comes within
  `near_pct` (0.5%) of one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.config import WarningsConfig
from papertrader.db import models as m
from papertrader.ingest.timeutil import at_new_york, new_york_date
from papertrader.notify.messages import money
from papertrader.notify.outbox import queue
from papertrader.notify.warnings import INFO_KIND, Pending, render

PRICE_KIND = "price_alert"


def sentences(text: str | None, limit: int = 4) -> tuple[str, ...]:
    parts = [
        p.strip() for p in re.split(r"(?<=[.!?])\s+", " ".join((text or "").split())) if p.strip()
    ]
    return tuple(parts[:limit])


def idea_pending(idea: m.TradeIdea, equity: float | None) -> Pending:
    """A buy (or short) at the open, from the judge's idea."""
    side = "BUY" if idea.action == m.TradeAction.BUY else "SHORT"
    zone = (f"zone {money(idea.entry_low)}-{money(idea.entry_high)}" if idea.entry_low and idea.entry_high
            else "at the market")  # fmt: skip
    shares = None
    if equity and idea.entry_high and idea.size_pct:
        shares = float(math.floor(equity * idea.size_pct / idea.entry_high)) or None
    against = " ".join(sentences(idea.dissent, 2))
    return Pending(ref=f"idea:{idea.id}", side=side, ticker=idea.ticker, source="open", shares=shares, price=zone,
                   target=idea.target_price, stop=idea.stop_price,
                   hold=f"up to {idea.horizon_days} trading days" if idea.horizon_days else "",
                   confidence=idea.final_confidence, reasons=sentences(idea.rationale), against=against,
                   details={"idea_id": idea.id})  # fmt: skip


def exit_pending(session: Session, trade: m.Trade, source: str, reason: str, day: date,
                 price: float | None = None) -> Pending:  # fmt: skip
    """A sell (or a short's buy-back) at the market. A skip lasts for the rest of `day`."""
    side = "SELL" if trade.direction == m.Direction.LONG else "COVER"
    idea = session.get(m.TradeIdea, trade.trade_idea_id) if trade.trade_idea_id else None
    move = ""
    if price and trade.entry_price:
        change = (price / trade.entry_price - 1) * (
            1 if trade.direction == m.Direction.LONG else -1
        )
        move = f" (now {money(price)}, {change:+.1%} since the buy)"
    against = ""
    if idea is not None and idea.rationale:
        against = "the reason it was bought: " + " ".join(sentences(idea.rationale, 1))
    if trade.target_price:
        against += (
            "; " if against else ""
        ) + f"its target {money(trade.target_price)} is still open"
    return Pending(ref=f"exit:{trade.id}:{source}:{day.isoformat()}", side=side, ticker=trade.ticker, source=source,
                   shares=trade.qty, price="at the market" + move, stop=None, target=None,
                   reasons=(reason,), against=against,
                   details={"trade_id": trade.id, "source": source, "reason": reason})  # fmt: skip


# --------------------------------------------------------------------------- system A (hub)


def a_morning(session: Session, *, place_at: datetime, equity: float | None, now: datetime) -> int:
    """A's buys and the judge's sells for A's next order run, as information only."""
    from papertrader.notify.reports import latest_daily_decision

    day = new_york_date(now)
    decision = latest_daily_decision(session, day)
    if decision is None or now > place_at:
        return 0
    items = [idea_pending(i, equity) for i in sorted(decision.ideas, key=lambda i: i.rank)
             if i.status == m.IdeaStatus.PROPOSED]  # fmt: skip
    held = {t.ticker: t for t in session.scalars(select(m.Trade).where(
        m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False)))}  # fmt: skip
    for item in (decision.payload or {}).get("exits", []):
        trade = held.get(str(item.get("ticker", "")).upper())
        if trade is not None:
            items.insert(
                0,
                exit_pending(session, trade, "judge", "judge: " + str(item.get("reason", "")), day),
            )
    if not items:
        return 0
    added = queue(
        session, INFO_KIND, render(items, place_at, buttons=False), dedup_key=f"a-ahead:{day}"
    )
    return len(items) if added else 0


def a_time_limits(
    session: Session, *, check_minutes: int, prices: Mapping[str, float], now: datetime
) -> int:
    """A's positions whose holding period ends before its next check: they will be sold."""
    soon = now + timedelta(minutes=check_minutes)
    count = 0
    for trade in session.scalars(select(m.Trade).where(
            m.Trade.status == m.TradeStatus.OPEN, m.Trade.is_test.is_(False),
            m.Trade.horizon_ends_at.is_not(None), m.Trade.horizon_ends_at <= soon)):  # fmt: skip
        item = exit_pending(session, trade, "time", "the planned holding period is over", new_york_date(now),
                            prices.get(trade.ticker))  # fmt: skip
        due = max(trade.horizon_ends_at or now, now)
        text = render([item], due, buttons=False).replace(
            "HEADS-UP · ", f"HEADS-UP (at A's next check, every {check_minutes} minutes) · ", 1)  # fmt: skip
        count += queue(session, INFO_KIND, text, dedup_key=f"a-time:{trade.id}")
    return count


# --------------------------------------------------------------------------- near a stop or target


def near_levels(session: Session, prices: Mapping[str, float], cfg: WarningsConfig, now: datetime,
                extra_stops: Mapping[str, float] | None = None) -> int:  # fmt: skip
    """Warn once a day per position and level when the price is within `near_pct`."""
    day = new_york_date(now)
    count = 0
    levels: list[tuple[str, str, float, float, Any]] = []  # (key, ticker, price, level, kind)
    for trade in session.scalars(select(m.Trade).where(m.Trade.status == m.TradeStatus.OPEN)):
        price = prices.get(trade.ticker)
        if not price:
            continue
        for kind, level in (("target", trade.target_price), ("stop", trade.stop_price)):
            if level:
                levels.append((f"{trade.id}:{kind}", trade.ticker, price, level, kind))
    for ticker, stop in (extra_stops or {}).items():
        if prices.get(ticker):
            levels.append((f"c-{ticker}:stop", ticker, prices[ticker], stop, "emergency stop"))
    for key, ticker, price, level, kind in levels:
        gap = abs(price / level - 1)
        if gap > cfg.near_pct:
            continue
        what = "take-profit" if kind == "target" else kind
        text = (f"NEAR {what.upper()} · {ticker} at {money(price)}, {what} {money(level)} ({gap:.2%} away)\n"
                f"If it gets there, Alpaca {'sells' if kind != 'target' else 'takes the profit'} automatically. "
                "This can't be skipped or announced earlier.")  # fmt: skip
        count += queue(session, PRICE_KIND, text, dedup_key=f"near:{key}:{day}")
    return count


def place_time(day: date, at: Any) -> datetime:
    return at_new_york(day, at)
