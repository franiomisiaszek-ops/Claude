"""Live scoring: the active pattern model's probability for today's event stocks."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from papertrader.config import AppConfig
from papertrader.db import models as m
from papertrader.model.dataset import build_dataset
from papertrader.model.registry import active_version, load_model

log = logging.getLogger(__name__)


def score_live(
    session: Session, config: AppConfig, as_of: datetime, cycle_id: int | None = None
) -> dict[str, float]:
    """{ticker: calibrated probability of beating SPY over the primary horizon}.

    Empty when no model is active or nothing happened today. Uses the same feature code
    as training, on data available at `as_of` only.
    """
    version = active_version(session)
    if version is None:
        return {}
    try:
        model = load_model(version)
        rows = build_dataset(session, config, as_of=as_of, live=True)
        if rows.empty:
            return {}
        probabilities = model.predict(rows)
    except Exception:  # a broken model file must never stop the research cycle
        log.exception("pattern model scoring failed", extra={"version": version.version})
        return {}

    scores = {str(t): float(p) for t, p in zip(rows["ticker"], probabilities, strict=True)}
    if cycle_id is not None:
        for ticker, probability in scores.items():
            session.add(
                m.ModelScore(
                    cycle_id=cycle_id, model_version_id=version.id, ticker=ticker, as_of=as_of,
                    horizon_days=model.horizon, probability_up=probability, features={},
                )
            )  # fmt: skip
    return scores
