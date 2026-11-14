"""Model versions: saving, loading, and the promotion rule.

A newly trained model only replaces the active one if it does better on the latest
out-of-sample window. "Better" is the mean daily rank correlation (IC) between its
probabilities and what the stocks then did, on rows neither model was trained on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import joblib
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from papertrader.db import models as m
from papertrader.db.types import utcnow
from papertrader.model.train import TrainedModel, daily_ic


@dataclass
class Promotion:
    promoted: bool
    reason: str
    candidate_ic: float | None
    current_ic: float | None


def active_version(session: Session) -> m.ModelVersion | None:
    return session.scalars(
        select(m.ModelVersion).where(m.ModelVersion.is_active).order_by(m.ModelVersion.id.desc())
    ).first()


def load_model(version: m.ModelVersion) -> TrainedModel:
    model = joblib.load(version.artifact_path)
    assert isinstance(model, TrainedModel)
    return model


def decide_promotion(
    session: Session,
    latest_window: pd.DataFrame,
    candidate_ic: float | None,
    horizon: int,
    *,
    brier: float | None = None,
    brier_naive: float | None = None,
) -> Promotion:
    """Compare the candidate with the active model on the latest out-of-sample window.

    `latest_window` holds the rows of the last walk-forward fold, already scored by a
    candidate that was trained without them.
    """
    current = active_version(session)
    if candidate_ic is None:
        return Promotion(
            False, "too little data in the latest window to measure the new model", None, None
        )
    # A model with no measurable skill must not influence decisions, even if it is the
    # first one: no model at all is the honest state then.
    if candidate_ic <= 0:
        return Promotion(
            False, "no ranking skill on the latest unseen window (IC not above zero)",
            candidate_ic, None,
        )  # fmt: skip
    if brier is not None and brier_naive is not None and brier >= brier_naive:
        return Promotion(
            False, "its probabilities are no better than always guessing the base rate",
            candidate_ic, None,
        )  # fmt: skip
    if current is None:
        return Promotion(
            True, "first model with measurable skill out of sample", candidate_ic, None
        )

    # The active model may only be judged on days after its own training data ended.
    fair = latest_window
    if current.trained_to is not None:
        fair = latest_window[latest_window["day"] > current.trained_to]
    current_ic: float | None = None
    if len(fair):
        scored = fair.copy()
        scored["prob"] = load_model(current).predict(fair)
        current_ic = daily_ic(scored, horizon)
    if current_ic is None:
        recorded = (current.metrics or {}).get("latest_window_ic")
        if recorded is None or candidate_ic > float(recorded):
            return Promotion(True, "the active model could not be tested on unseen days; the new "
                                   "one beats its recorded score", candidate_ic, recorded)  # fmt: skip
        return Promotion(
            False, "does not beat the active model's recorded score", candidate_ic, recorded
        )
    if candidate_ic > current_ic:
        return Promotion(True, "better than the active model on the latest unseen window",
                         candidate_ic, current_ic)  # fmt: skip
    return Promotion(False, "not better than the active model on the latest unseen window",
                     candidate_ic, current_ic)  # fmt: skip


def save_version(
    session: Session,
    model: TrainedModel,
    *,
    folder: Path,
    trained_from: date | None,
    trained_to: date | None,
    metrics: dict[str, Any],
    params: dict[str, Any],
    promotion: Promotion,
    now: datetime | None = None,
) -> m.ModelVersion:
    now = now or utcnow()
    version = f"pm-{now:%Y%m%d-%H%M%S}"
    target = folder / version
    target.mkdir(parents=True, exist_ok=True)
    artifact = target / "model.joblib"
    joblib.dump(model, artifact)

    if promotion.promoted:
        for row in session.scalars(select(m.ModelVersion).where(m.ModelVersion.is_active)):
            row.is_active = False
    row = m.ModelVersion(
        version=version, created_at=now, trained_from=trained_from, trained_to=trained_to,
        params=params, metrics=metrics, artifact_path=str(artifact),
        is_active=promotion.promoted, promoted_at=now if promotion.promoted else None,
        notes=promotion.reason,
    )  # fmt: skip
    session.add(row)
    session.flush()
    return row
