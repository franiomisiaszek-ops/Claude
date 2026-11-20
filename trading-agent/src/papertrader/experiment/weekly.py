"""The weekly comparison message (sent by the hub, which sees every system)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import Engine

from papertrader.config import AppConfig
from papertrader.experiment.measure import compare, get_start, report_text
from papertrader.experiment.switches import review, review_text


def weekly_text(engines: Mapping[str, Engine], config: AppConfig, now: datetime) -> str:
    """The comparison; in the first week of a month also B's switch review."""
    start = get_start(engines["A"])
    comparison = compare(engines, config, start, now)
    text = report_text(comparison, config)
    if start is not None and "B" in engines and now.day <= 7:
        text += "\n\n" + review_text(review(engines["B"], config, start, now))
    return text
