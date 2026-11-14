"""Text handling for the pattern model: masking, and features that need no LLM.

Why masking: Claude has read about most historical market events, so showing it a
real headline with the company name and date invites it to "remember" what the stock
did next. Before any historical text goes to Claude, company names, tickers, dates,
years and quarters are replaced by placeholders. This reduces that leak; it cannot
remove it (a famous product or a unique deal size can still give an event away), and
the backtest report says so.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

COMPANY, OTHER, DATE = "[COMPANY]", "[OTHER]", "[DATE]"

_SUFFIX = re.compile(
    r"\b(incorporated|inc|corporation|corp|company|co|ltd|limited|plc|holdings|holding|group|"
    r"the|class [abc]|n\.?v|s\.?a|llc|lp|trust)\b\.?",
    re.IGNORECASE,
)
_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_PATTERNS = [
    re.compile(
        rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{{2}})?\b", re.I
    ),
    re.compile(rf"\b\d{{1,2}}\s+(?:{_MONTHS})\.?(?:\s+(?:19|20)\d{{2}})?\b", re.I),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+(?:19|20)\d{{2}}\b", re.I),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b(?:Q[1-4]|[1-4]Q)\s*'?(?:(?:19|20)?\d{2})?\b", re.I),
    re.compile(r"\b(?:FY|fiscal(?: year)?)\s*'?(?:19|20)?\d{2}\b", re.I),
    re.compile(r"\b(?:19|20)\d{2}\b"),
    re.compile(r"\b(?:first|second|third|fourth)[- ]quarter\b", re.I),
]
_GENERIC_FIRST_WORDS = frozenset(
    [
        "american",
        "general",
        "united",
        "national",
        "international",
        "first",
        "global",
        "western",
        "southern",
        "northern",
        "pacific",
        "digital",
        "advanced",
        "applied",
        "universal",
        "standard",
        "royal",
        "capital",
        "energy",
        "health",
    ]
)
_TICKER_TAG = re.compile(r"\((?:NYSE|NASDAQ|AMEX|NYSEARCA|OTC)[^)]*\)", re.I)


def name_variants(name: str) -> list[str]:
    """'Apple Inc.' -> ['Apple Inc.', 'Apple']: the full name and its distinctive core."""
    core = " ".join(_SUFFIX.sub(" ", name).replace(",", " ").split())
    variants = {name.strip(), core}
    first = core.split()[0] if core else ""
    if len(first) >= 5 and first.lower() not in _GENERIC_FIRST_WORDS:
        variants.add(first)  # "Agilent Technologies" is usually just "Agilent" in a headline
    return sorted((v for v in variants if len(v) >= 3), key=len, reverse=True)


class Masker:
    """Masks a whole universe of companies in one pass per text.

    Built once from (ticker, company name) pairs. For each text, mentions of the
    companies the story is about become [COMPANY]; every other known company becomes
    [OTHER]; dates, years and quarters become [DATE].
    """

    def __init__(self, companies: Iterable[tuple[str, str]]) -> None:
        self._owners: dict[str, set[str]] = {}
        tickers: set[str] = set()
        for ticker, name in companies:
            for variant in name_variants(name):
                self._owners.setdefault(variant.lower(), set()).add(ticker)
            if len(ticker) >= 2:  # one-letter tickers would eat ordinary words
                tickers.add(ticker)
                self._owners.setdefault(ticker, set()).add(ticker)
        names = sorted((n for n in self._owners if n not in tickers), key=len, reverse=True)
        self._names = (
            re.compile(
                r"(?<![A-Za-z0-9])("
                + "|".join(re.escape(n) for n in names)
                + r")(?:'s|\u2019s)?(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if names
            else None
        )
        # Tickers are matched case-sensitively and whole, so "ALL" does not eat "all".
        ordered = sorted(tickers, key=len, reverse=True)
        self._tickers = (
            re.compile(
                r"(?<![A-Za-z0-9.])("
                + "|".join(re.escape(t) for t in ordered)
                + r")(?![A-Za-z0-9])"
            )
            if ordered
            else None
        )

    def mask(self, text: str, own: Iterable[str]) -> str:
        mine = set(own)

        def swap(key: str) -> str:
            return COMPANY if self._owners.get(key, set()) & mine else OTHER

        masked = _TICKER_TAG.sub("", text)
        if self._names is not None:
            masked = self._names.sub(lambda match: swap(match.group(1).lower()), masked)
        if self._tickers is not None:
            masked = self._tickers.sub(lambda match: swap(match.group(1)), masked)
        for pattern in _DATE_PATTERNS:
            masked = pattern.sub(DATE, masked)
        return " ".join(masked.split())


def mask_text(
    text: str,
    *,
    own: Iterable[tuple[str, str]],
    others: Iterable[tuple[str, str]] = (),
) -> str:
    """Hide who and when. `own` / `others` are (ticker, company name) pairs."""
    own = list(own)
    return Masker([*own, *others]).mask(text, [ticker for ticker, _name in own])


# --------------------------------------------------------------------------- no-LLM features

EVENT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        (
            "mna",
            r"\b(acqui\w+|merger|merge[sd]?|takeover|buyout|to buy|tender offer|divest\w*|spin[- ]?off)\b",
        ),
        (
            "fda_regulatory",
            r"\b(fda|approval|approved|clinical trial|phase (?:1|2|3|i|ii|iii)\b|regulator\w*|antitrust|recall)",
        ),
        (
            "lawsuit_legal",
            r"\b(lawsuit|sues?|sued|litigation|settle\w*|class action|investigation|subpoena|probe|fraud|fine[sd]?)\b",
        ),
        (
            "guidance",
            r"\b(guidance|outlook|forecast|raises? (?:its )?(?:full[- ]year|fy)|cuts? (?:its )?(?:full[- ]year|fy)|expects|reaffirm\w*)\b",
        ),
        (
            "earnings",
            r"\b(earnings|eps|quarterly results|revenue (?:beat|miss)|beats?|misses?|profit|results)\b",
        ),
        (
            "management_change",
            r"\b(ceo|cfo|chief executive|chief financial|resign\w*|steps? down|appoint\w*|names? new|retire\w*)\b",
        ),
        (
            "analyst_rating",
            r"\b(upgrade[sd]?|downgrade[sd]?|price target|initiat\w+ coverage|overweight|underweight|outperform|buy rating|sell rating)\b",
        ),
        ("capital_return", r"\b(dividend|buyback|repurchase|share repurchase)\b"),
        (
            "product",
            r"\b(launch\w*|unveil\w*|introduc\w+|new product|partnership|contract|deal with|collaborat\w+)\b",
        ),
    )
)
EVENT_TYPES: tuple[str, ...] = (*(name for name, _ in EVENT_RULES), "other")

_POSITIVE = frozenset(
    [
        "beat",
        "beats",
        "beating",
        "surge",
        "surges",
        "surged",
        "soar",
        "soars",
        "soared",
        "jump",
        "jumps",
        "jumped",
        "rally",
        "rallies",
        "rallied",
        "gain",
        "gains",
        "gained",
        "rise",
        "rises",
        "rose",
        "climb",
        "climbs",
        "climbed",
        "record",
        "strong",
        "stronger",
        "strongest",
        "growth",
        "grow",
        "grows",
        "grew",
        "upgrade",
        "upgrades",
        "upgraded",
        "outperform",
        "raise",
        "raises",
        "raised",
        "boost",
        "boosts",
        "boosted",
        "profit",
        "profitable",
        "approval",
        "approved",
        "win",
        "wins",
        "won",
        "expand",
        "expands",
        "expanded",
        "exceed",
        "exceeds",
        "exceeded",
        "bullish",
        "optimistic",
        "upbeat",
        "robust",
        "accelerate",
        "accelerates",
        "accelerated",
        "buyback",
        "dividend",
        "breakthrough",
        "tops",
        "topped",
        "higher",
        "improve",
        "improves",
        "improved",
        "positive",
        "momentum",
        "rebound",
        "rebounds",
        "rebounded",
    ]
)
_NEGATIVE = frozenset(
    [
        "miss",
        "misses",
        "missed",
        "plunge",
        "plunges",
        "plunged",
        "tumble",
        "tumbles",
        "tumbled",
        "slump",
        "slumps",
        "slumped",
        "fall",
        "falls",
        "fell",
        "drop",
        "drops",
        "dropped",
        "decline",
        "declines",
        "declined",
        "sink",
        "sinks",
        "sank",
        "weak",
        "weaker",
        "weakest",
        "loss",
        "losses",
        "downgrade",
        "downgrades",
        "downgraded",
        "underperform",
        "cut",
        "cuts",
        "cutting",
        "warn",
        "warns",
        "warned",
        "warning",
        "lawsuit",
        "sued",
        "probe",
        "investigation",
        "recall",
        "recalls",
        "recalled",
        "fraud",
        "bankruptcy",
        "default",
        "layoffs",
        "layoff",
        "resign",
        "resigns",
        "resigned",
        "delay",
        "delays",
        "delayed",
        "halt",
        "halts",
        "halted",
        "bearish",
        "pessimistic",
        "disappointing",
        "disappoint",
        "disappoints",
        "disappointed",
        "lower",
        "worse",
        "worst",
        "negative",
        "slowdown",
        "shortfall",
        "impairment",
        "penalty",
        "fine",
        "fined",
        "breach",
        "crash",
        "crashes",
        "crashed",
        "selloff",
        "slashed",
        "slash",
    ]
)
_WORD = re.compile(r"[a-z]+")


def rule_event_type(text: str) -> str:
    """First matching keyword family; 'other' when none matches. No model involved."""
    for name, pattern in EVENT_RULES:
        if pattern.search(text):
            return name
    return "other"


def lexicon_sentiment(text: str) -> float:
    """Word-list sentiment in [-1, 1]. Crude, but it cannot know the future."""
    words = _WORD.findall(text.lower())
    positive = sum(word in _POSITIVE for word in words)
    negative = sum(word in _NEGATIVE for word in words)
    return (positive - negative) / (positive + negative + 1)
