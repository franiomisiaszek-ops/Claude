"""Message texts. Plain, short, emoji-light, readable on a phone."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from papertrader.ingest.timeutil import NEW_YORK


def money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def signed_pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def trade_executed(*, side: str, qty: float, ticker: str, price: float, confidence: float | None,
                   reason: str, test: bool = False) -> str:  # fmt: skip
    verb = "BOUGHT" if side == "buy" else "SOLD SHORT"
    parts = [f"{verb} {qty:g} {ticker} @ {money(price)}"]
    if confidence is not None:
        parts.append(f"conf {confidence:.0%}")
    parts.append(reason[:160])
    text = " · ".join(parts)
    return f"[TEST] {text}" if test else text


def sell_signal(*, ticker: str, trigger: str, reason: str) -> str:
    words = {"target": "take-profit reached", "stop": "stop-loss hit", "time": "holding period over",
             "thesis_break": "thesis broken", "kill_switch": "daily loss limit",
             "manual": "closed by hand"}  # fmt: skip
    return f"SELL {ticker} · {words.get(trigger, trigger)} · {reason[:200]}"


def trade_closed(*, ticker: str, exit_price: float, pnl_usd: float, pnl_pct: float,
                 excess_pct: float | None, opened: datetime, closed: datetime, trigger: str,
                 right: Sequence[str], wrong: Sequence[str], test: bool = False) -> str:  # fmt: skip
    days = (closed - opened).total_seconds() / 86400
    held = f"{days:.1f} days" if days >= 1 else f"{days * 24:.1f} hours"
    lines = [
        f"CLOSED {ticker} @ {money(exit_price)} ({trigger.replace('_', ' ')})",
        f"P&L {money(pnl_usd)} ({signed_pct(pnl_pct)}), vs SPY {signed_pct(excess_pct)} · held {held}",
    ]
    if right or wrong:
        lines.append(f"Right: {', '.join(right) or 'none'} · Wrong: {', '.join(wrong) or 'none'}")
    text = "\n".join(lines)
    return f"[TEST] {text}" if test else text


def alert(text: str) -> str:
    return f"ALERT · {text}"


def eod_summary(*, day: datetime, equity: float, day_pnl: float, day_pnl_pct: float,
                total_pct: float | None, spy_total_pct: float | None, win_rate: float | None,
                closed_trades: int, positions: Sequence[tuple[str, float, float]]) -> str:  # fmt: skip
    lines = [
        f"End of day {day.astimezone(NEW_YORK):%a %d %b}",
        f"Day P&L {money(day_pnl)} ({signed_pct(day_pnl_pct, 2)}) · equity {money(equity)}",
        f"Since start {signed_pct(total_pct)} vs SPY {signed_pct(spy_total_pct)}",
        f"Win rate {'n/a' if win_rate is None else f'{win_rate:.0%}'} over {closed_trades} closed trade(s)",
    ]
    if positions:
        lines.append("Open: " + ", ".join(f"{t} {signed_pct(p)}" for t, _v, p in positions))
    else:
        lines.append("No open positions")
    return "\n".join(lines)
