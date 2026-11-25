"""One Telegram message after the morning briefs: each system's top 3 ideas, with the
Avanza equivalent for your own money. Information only: nothing here places an order.

A and B: the judge's top 3 ideas today (also those below the bar, marked), with entry,
target, stop and confidence. The Avanza amount is the idea's suggested share of the
account applied to your Avanza money. Avanza can't short a share, so shorts say so.
C: its 3 largest trades this week from the "Match on Avanza" page. C follows fixed
rules, so it has no confidence number.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from papertrader.ingest.timeutil import new_york_date
from papertrader.notify.reports import ACTION_WORDS, NOT_TRADED, latest_daily_decision

TOP = 3


def _idea_lines(idea: Any, capital_sek: float) -> list[str]:
    action = ACTION_WORDS.get(idea.action.value, idea.action.value.upper())
    at = (f"{idea.entry_low:.2f}-{idea.entry_high:.2f}" if idea.entry_low and idea.entry_high
          else "market price")  # fmt: skip
    note = f" ({NOT_TRADED[idea.status.value]})" if idea.status.value in NOT_TRADED else ""
    lines = [f"{idea.rank}. {action} {idea.ticker} at {at} · {idea.final_confidence:.0%} confident · "
             f"target {idea.target_price:.2f} · stop {idea.stop_price:.2f}{note}"]  # fmt: skip
    if idea.action.value == "sell_short":
        lines.append("   Avanza: not possible (Avanza can't short a share); skip it")
    else:
        sek = round(idea.size_pct * capital_sek, -1)
        lines.append(f"   Avanza: buy {idea.ticker} (US share) for about {sek:,.0f} SEK, "
                     f"sell at {idea.target_price:.2f} or {idea.stop_price:.2f}")  # fmt: skip
    return lines


def _ab_block(view: Any, now: datetime, capital_sek: float) -> list[str]:
    with view.sessions() as session:
        decision = latest_daily_decision(session, new_york_date(now))
        if decision is None:
            return ["No ideas today (no decision yet)."]
        ideas = sorted(decision.ideas, key=lambda i: i.rank)[:TOP]
        if not ideas:
            return ["No ideas today."]
        return [line for i in ideas for line in _idea_lines(i, capital_sek)]


def _c_block(view: Any, now: datetime) -> list[str]:
    from papertrader.web.avanza import match

    view_c = match(view, None, now)
    if not view_c.has_plan:
        return ["No weekly plan yet."]
    trades = sorted(view_c.trades, key=lambda t: -t.sek)[:TOP]
    if not trades:
        return ["Nothing to trade this week; keep what you hold."]
    lines = []
    for n, t in enumerate(trades, 1):
        amount = "all you hold" if t.all_of_it else f"about {t.sek:,.0f} SEK"
        lines.append(
            f"{n}. {t.action} {t.fund} → {t.target_weight:.0%} of the account · {t.status}"
        )
        lines.append(f"   Avanza: {t.action.lower()} {t.product}, {amount}")
    return lines


def top_ideas_text(views: Mapping[Any, Any], now: datetime) -> str | None:
    """None when no system has anything to show (weekends, holidays)."""
    capital_sek = 10_500.0
    if "C" in views:
        cfg = views["C"].config.portfolio
        capital_sek = cfg.mirror_capital_usd * cfg.sek_per_usd
    lines = [f"Top {TOP} ideas · {new_york_date(now):%a %d %b} · information only",
             f"Avanza amounts are for {capital_sek:,.0f} SEK of your money."]  # fmt: skip
    found = False
    for name in ("A", "B", "C"):
        view = views.get(name)
        if view is None:
            continue
        try:
            block = _c_block(view, now) if name == "C" else _ab_block(view, now, capital_sek)
        except Exception as exc:  # one system's problem must not hide the others
            block = [f"Could not read its ideas ({type(exc).__name__})."]
        found = found or not block[0].startswith(("No ", "Nothing", "Could"))
        title = f"{name} (weekly rules, no confidence %)" if name == "C" else name
        lines += ["", title, *block]
    return "\n".join(lines) if found else None
