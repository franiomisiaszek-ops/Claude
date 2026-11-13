"""Paper-trading guard.

This system must only ever talk to Alpaca's PAPER trading endpoint. The paper
URL is hard-coded here and is not configurable. `assert_paper_only()` runs at
startup (and again before any trading client is created) and refuses to
continue if anything in the environment points at a different trading host.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

# The one and only trading endpoint this program is allowed to use.
ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"

# Environment variables that Alpaca SDKs / tools read to pick a trading host.
# If any of these is set to something other than the paper URL we stop.
_TRADING_URL_ENV_VARS = ("APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL")

# Alpaca paper API keys start with "PK"; live keys start with "AK".
_LIVE_KEY_PREFIX = "AK"


class LiveTradingForbiddenError(RuntimeError):
    """Raised when something tries to point the system away from paper trading."""


def _normalise(url: str) -> tuple[str, str]:
    parts = urlsplit(url.strip())
    return parts.scheme.lower(), (parts.hostname or "").lower()


def is_paper_url(url: str) -> bool:
    """True only for https://paper-api.alpaca.markets (any path, default port)."""
    scheme, host = _normalise(url)
    paper_scheme, paper_host = _normalise(ALPACA_PAPER_BASE_URL)
    try:
        port = urlsplit(url.strip()).port
    except ValueError:
        return False
    return scheme == paper_scheme and host == paper_host and port in (None, 443)


def assert_paper_only(
    base_url: str = ALPACA_PAPER_BASE_URL,
    *,
    api_key: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Refuse to run unless everything points at the Alpaca paper endpoint.

    Checks, in order:
    1. the base URL about to be used is the paper URL;
    2. no Alpaca base-URL environment variable overrides it with another host;
    3. the API key (if given) does not look like a live-account key.
    """
    env = os.environ if environ is None else environ

    if not is_paper_url(base_url):
        raise LiveTradingForbiddenError(
            f"Refusing to run: trading base URL {base_url!r} is not the Alpaca paper "
            f"endpoint ({ALPACA_PAPER_BASE_URL}). This system is paper-trading only."
        )

    for var in _TRADING_URL_ENV_VARS:
        value = env.get(var)
        if value and not is_paper_url(value):
            raise LiveTradingForbiddenError(
                f"Refusing to run: environment variable {var}={value!r} points away from "
                f"the Alpaca paper endpoint ({ALPACA_PAPER_BASE_URL}). Remove it."
            )

    if api_key and api_key.strip().upper().startswith(_LIVE_KEY_PREFIX):
        raise LiveTradingForbiddenError(
            "Refusing to run: ALPACA_API_KEY looks like a LIVE account key (starts with "
            "'AK'). Use the keys from your Alpaca PAPER account (they start with 'PK')."
        )
