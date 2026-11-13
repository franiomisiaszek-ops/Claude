"""The paper-only guard is the most important safety property of the system."""

from __future__ import annotations

import pytest

from papertrader.safety import (
    ALPACA_PAPER_BASE_URL,
    LiveTradingForbiddenError,
    assert_paper_only,
    is_paper_url,
)


def test_paper_url_constant_is_the_paper_endpoint() -> None:
    assert ALPACA_PAPER_BASE_URL == "https://paper-api.alpaca.markets"


@pytest.mark.parametrize(
    "url",
    [
        "https://paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets/",
        "https://paper-api.alpaca.markets/v2",
        "HTTPS://PAPER-API.ALPACA.MARKETS",
        "https://paper-api.alpaca.markets:443",
    ],
)
def test_paper_urls_are_accepted(url: str) -> None:
    assert is_paper_url(url)
    assert_paper_only(url, environ={})


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets",  # the LIVE endpoint
        "https://api.alpaca.markets/v2",
        "http://paper-api.alpaca.markets",  # not https
        "https://paper-api.alpaca.markets.evil.example",
        "https://evil.example/paper-api.alpaca.markets",
        "https://paper-api.alpaca.markets@api.alpaca.markets",
        "https://paper-api.alpaca.markets:8443",
        "https://paper-api.alpaca.markets:notaport",
        "",
        "paper-api.alpaca.markets",
    ],
)
def test_everything_else_is_refused(url: str) -> None:
    assert not is_paper_url(url)
    with pytest.raises(LiveTradingForbiddenError):
        assert_paper_only(url, environ={})


@pytest.mark.parametrize("var", ["APCA_API_BASE_URL", "ALPACA_BASE_URL", "ALPACA_API_BASE_URL"])
def test_environment_override_to_live_is_refused(var: str) -> None:
    with pytest.raises(LiveTradingForbiddenError, match=var):
        assert_paper_only(environ={var: "https://api.alpaca.markets"})


def test_environment_override_to_paper_is_fine() -> None:
    assert_paper_only(environ={"APCA_API_BASE_URL": "https://paper-api.alpaca.markets"})


def test_real_environment_is_checked_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(LiveTradingForbiddenError):
        assert_paper_only()


def test_live_looking_api_key_is_refused() -> None:
    with pytest.raises(LiveTradingForbiddenError, match="LIVE"):
        assert_paper_only(api_key="AKXXXXXXXXXXXXXXXXXX", environ={})


def test_paper_api_key_is_accepted() -> None:
    assert_paper_only(api_key="PKXXXXXXXXXXXXXXXXXX", environ={})
