"""System B's two optional PAID data sources. Nothing here runs without its key.

Switch 8, MarketData.app (MARKETDATA_API_KEY): the option chain about a month out, from
which three numbers are taken per stock:

    implied volatility at the money   how much the market expects the stock to move
    call-put spread                   call IV minus put IV at the same strikes; in the
                                      research (Cremers and Weinbaum 2010) stocks whose
                                      calls were relatively expensive did better
    put skew                          IV of 25-delta puts minus 25-delta calls; a steep
                                      skew (Xing, Zhang and Zhao 2010) preceded worse returns

Switch 9, Financial Modeling Prep (FMP_API_KEY): analyst grade changes of the last 30
days, the consensus price target, and the current-year EPS estimate. B stores the
estimate every day, so "revision" is measured against what B itself saw a month ago:
point-in-time by construction.

The reply formats below are written from the services' documentation and could not be
checked from the build machine. Every field is checked when it arrives; a reply that does
not match raises PaidDataError with what was missing, and `papertrader --system B
paid-data-check` makes one real call to verify both after you subscribe.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import httpx

MARKETDATA_CHAIN = "https://api.marketdata.app/v1/options/chain/{symbol}/"
FMP_BASE = "https://financialmodelingprep.com/stable/"
TIMEOUT = 20.0


class PaidDataError(RuntimeError):
    pass


def _get(
    client: httpx.Client, url: str, *, params: Mapping[str, Any], headers: Mapping[str, str]
) -> Any:
    try:
        response = client.get(url, params=dict(params), headers=dict(headers), timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise PaidDataError(
            f"could not reach {httpx.URL(url).host} ({type(exc).__name__})"
        ) from exc
    if response.status_code in (401, 402, 403):
        raise PaidDataError(f"{httpx.URL(url).host} refused the key (HTTP {response.status_code}): "
                            "check the key and that the subscription is active")  # fmt: skip
    if response.status_code == 429:
        raise PaidDataError(f"{httpx.URL(url).host}: too many requests (HTTP 429)")
    if response.status_code >= 400 and response.status_code != 404:
        raise PaidDataError(f"{httpx.URL(url).host} answered HTTP {response.status_code}")
    if response.status_code == 404:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise PaidDataError(f"{httpx.URL(url).host} did not answer with JSON") from exc


# --------------------------------------------------------------------------- options


@dataclass(frozen=True)
class OptionsView:
    ticker: str
    days: int | None
    atm_iv: float | None
    call_put_spread: float | None
    put_skew: float | None

    def summary(self) -> str:
        parts = []
        if self.atm_iv is not None:
            parts.append(
                f"implied volatility {self.atm_iv:.0%} a year"
                + (f" ({self.days} days out)" if self.days else "")
            )
        if self.call_put_spread is not None:
            parts.append(f"calls minus puts {self.call_put_spread * 100:+.1f} vol points")
        if self.put_skew is not None:
            parts.append(f"put skew {self.put_skew * 100:+.1f} points")
        return "; ".join(parts) or "no usable option prices"

    def details(self) -> dict[str, Any]:
        return {"days": self.days, "atm_iv": self.atm_iv, "call_put_spread": self.call_put_spread,
                "put_skew": self.put_skew}  # fmt: skip


def parse_chain(ticker: str, body: Any) -> OptionsView:
    if not isinstance(body, dict):
        raise PaidDataError("MarketData.app: the reply is not an object")
    status = body.get("s")
    if status == "no_data":
        return OptionsView(ticker, None, None, None, None)
    if status != "ok":
        raise PaidDataError(
            f"MarketData.app: status {status!r} ({body.get('errmsg', 'no message')})"
        )
    needed = ("side", "strike", "iv")
    missing = [k for k in needed if not isinstance(body.get(k), list)]
    if missing:
        raise PaidDataError(f"MarketData.app: the reply has no {', '.join(missing)} list")
    n = len(body["side"])
    if any(len(body[k]) != n for k in needed):
        raise PaidDataError("MarketData.app: the reply's lists have different lengths")
    deltas: Sequence[Any] = (
        body["delta"]
        if isinstance(body.get("delta"), list) and len(body["delta"]) == n
        else [None] * n
    )
    under = body.get("underlyingPrice")
    price = float(under[0]) if isinstance(under, list) and under and under[0] else None
    dte = body.get("dte")
    days = int(dte[0]) if isinstance(dte, list) and dte and dte[0] is not None else None

    calls: dict[float, float] = {}
    puts: dict[float, float] = {}
    otm_calls, otm_puts = [], []
    for side, strike, iv, delta in zip(
        body["side"], body["strike"], body["iv"], deltas, strict=True
    ):
        if iv is None or strike is None or not math.isfinite(float(iv)) or float(iv) <= 0:
            continue
        (calls if side == "call" else puts)[float(strike)] = float(iv)
        if delta is not None:
            if side == "call" and 0.15 <= float(delta) <= 0.35:
                otm_calls.append(float(iv))
            elif side == "put" and -0.35 <= float(delta) <= -0.15:
                otm_puts.append(float(iv))
    both = sorted(set(calls) & set(puts))
    spread = sum(calls[k] - puts[k] for k in both) / len(both) if both else None
    atm = None
    if both and price:
        nearest = min(both, key=lambda k: abs(k - price))
        atm = (calls[nearest] + puts[nearest]) / 2
    skew = (
        (sum(otm_puts) / len(otm_puts) - sum(otm_calls) / len(otm_calls))
        if otm_puts and otm_calls
        else None
    )
    return OptionsView(ticker, days, atm, spread, skew)


def fetch_options(client: httpx.Client, key: str, ticker: str, days: int = 30) -> OptionsView:
    body = _get(client, MARKETDATA_CHAIN.format(symbol=ticker),
                params={"dte": days, "strikeLimit": 12}, headers={"Authorization": f"Bearer {key}"})  # fmt: skip
    if body is None:
        return OptionsView(ticker, None, None, None, None)
    return parse_chain(ticker, body)


# --------------------------------------------------------------------------- analysts


@dataclass(frozen=True)
class AnalystView:
    ticker: str
    upgrades: int
    downgrades: int
    target: float | None
    eps_estimate: float | None
    eps_revision: float | None  # against what B stored about a month ago
    price: float | None

    @property
    def net_grades(self) -> int:
        return self.upgrades - self.downgrades

    def summary(self) -> str:
        parts = [f"last 30 days: {self.upgrades} upgrades, {self.downgrades} downgrades"]
        if self.target and self.price:
            parts.append(f"consensus target {self.target / self.price - 1:+.0%} from the price")
        if self.eps_revision is not None:
            parts.append(f"this year's EPS estimate {self.eps_revision:+.1%} vs a month ago")
        return "; ".join(parts)

    def details(self) -> dict[str, Any]:
        return {"upgrades": self.upgrades, "downgrades": self.downgrades, "target": self.target,
                "eps_estimate": self.eps_estimate, "eps_revision": self.eps_revision,
                "price": self.price}  # fmt: skip


def _rows(body: Any, what: str) -> list[dict[str, Any]]:
    if body is None:
        return []
    if isinstance(body, dict) and ("Error Message" in body or "error" in body):
        raise PaidDataError(f"FMP ({what}): {body.get('Error Message') or body.get('error')}")
    if not isinstance(body, list) or not all(isinstance(r, dict) for r in body):
        raise PaidDataError(f"FMP ({what}): the reply is not a list of records")
    return body


def _day(text: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(text)[:10]).date()
    except ValueError:
        return None


def parse_analysts(ticker: str, today: date, grades: Any, target: Any, estimates: Any,
                   *, price: float | None, month_ago_eps: float | None) -> AnalystView:  # fmt: skip
    since = today - timedelta(days=30)
    up = down = 0
    for row in _rows(grades, "grades"):
        day = _day(row.get("date"))
        if day is None or not since <= day <= today:
            continue
        action = str(row.get("action", "")).lower()
        up += action == "upgrade"
        down += action == "downgrade"
    targets = _rows(target, "price target consensus")
    consensus = targets[0].get("targetConsensus") if targets else None
    eps = None
    rows = _rows(estimates, "analyst estimates")
    this_year = [r for r in rows if (d := _day(r.get("date"))) is not None and d.year == today.year]
    if this_year:
        eps = this_year[0].get("epsAvg", this_year[0].get("estimatedEpsAvg"))
    revision = None
    if eps is not None and month_ago_eps:
        revision = float(eps) / float(month_ago_eps) - 1
    return AnalystView(ticker, up, down, float(consensus) if consensus else None,
                       float(eps) if eps is not None else None, revision, price)  # fmt: skip


def fetch_analysts(client: httpx.Client, key: str, ticker: str, today: date, *,
                   price: float | None, month_ago_eps: float | None) -> AnalystView:  # fmt: skip
    params = {"symbol": ticker, "apikey": key}
    grades = _get(client, FMP_BASE + "grades", params=params, headers={})
    target = _get(client, FMP_BASE + "price-target-consensus", params=params, headers={})
    estimates = _get(client, FMP_BASE + "analyst-estimates", params=params | {"period": "annual", "limit": 5},
                     headers={})  # fmt: skip
    return parse_analysts(
        ticker, today, grades, target, estimates, price=price, month_ago_eps=month_ago_eps
    )
