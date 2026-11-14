"""Walk-forward training of the pattern model, with calibration.

Rules followed here, all from the spec:

* Time-ordered validation only. The data is never shuffled; every test block lies
  strictly after its training data.
* Purge / embargo. A label looks up to `max horizon` days ahead, so the last days of
  each training window overlap the test window's start. Those days are cut out.
* Calibration. Raw model scores are mapped to probabilities on a held-out, later part
  of the training window, so that "0.60" means about 60% of such cases went up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score

from papertrader.config import PatternModelConfig
from papertrader.model.features import CATEGORICAL, FEATURES, NUMERIC

MIN_TRAIN_ROWS = 300
# Isotonic regression fits a free-form step curve and needs a lot of data; on a small
# calibration set it invents confident-looking steps out of noise. Below this many
# rows the smoother two-parameter Platt curve is used instead, whatever the config says.
MIN_ISOTONIC_ROWS = 1500
# Small-sample caution: probabilities are pulled toward 50% by rows / (rows + this).
SHRINK_ROWS = 300
SEED = 7


@dataclass
class TrainedModel:
    """Everything needed to score a row: the booster, the calibrator and the columns."""

    booster: Any
    calibrator: Any
    calibration: str
    horizon: int
    sectors: list[str]
    calibration_rows: int = 0
    features: list[str] = field(default_factory=lambda: list(FEATURES))

    @property
    def trust(self) -> float:
        """How far the calibrated probability may move away from a coin flip: little
        when the calibrator saw few examples, almost fully with thousands."""
        return self.calibration_rows / (self.calibration_rows + SHRINK_ROWS)

    def predict(self, table: pd.DataFrame) -> np.ndarray:
        raw = self.booster.predict_proba(design(table, self.sectors))[:, 1]
        if self.calibration == "isotonic":
            calibrated = np.asarray(self.calibrator.predict(raw), dtype=float)
        else:
            calibrated = np.asarray(self.calibrator.predict_proba(_logit(raw))[:, 1], dtype=float)
        return 0.5 + self.trust * (calibrated - 0.5)


def _logit(raw: np.ndarray) -> np.ndarray:
    clipped = np.clip(raw, 1e-4, 1 - 1e-4)
    return np.asarray(np.log(clipped / (1 - clipped))).reshape(-1, 1)


def design(table: pd.DataFrame, sectors: list[str]) -> pd.DataFrame:
    """The model's input columns, with the sector as a fixed category set."""
    x = table.reindex(columns=FEATURES).copy()
    x[NUMERIC] = x[NUMERIC].astype(float)
    for column in CATEGORICAL:
        x[column] = pd.Categorical(x[column], categories=sectors)
    return x


def _booster(rows: int) -> lgb.LGBMClassifier:
    # Small, heavily regularised trees: the signal in news is weak and the data noisy,
    # so a flexible model would mostly memorise noise.
    return lgb.LGBMClassifier(
        n_estimators=200, learning_rate=0.03, num_leaves=8, max_depth=4,
        min_child_samples=max(20, rows // 100), subsample=0.8, subsample_freq=1,
        colsample_bytree=0.7, reg_lambda=10.0, random_state=SEED, verbose=-1, n_jobs=2,
    )  # fmt: skip


CALIBRATION_BLOCKS = 4


def _out_of_sample_scores(
    data: pd.DataFrame, target: str, sectors: list[str], embargo: int
) -> tuple[np.ndarray, np.ndarray]:
    """Raw scores for the later three quarters of `data`, each from a model that only
    saw earlier days (minus the embargo). A walk-forward inside the training window:
    it gives the calibrator three times more honest examples than one held-out tail."""
    unique = np.array(sorted(data["day"].unique()))
    edges = [len(unique) * k // CALIBRATION_BLOCKS for k in range(CALIBRATION_BLOCKS + 1)]
    scores, outcomes = [], []
    for k in range(1, CALIBRATION_BLOCKS):
        train_end = edges[k] - embargo - 1
        if train_end < 0:
            continue
        train = data[data["day"] <= unique[train_end]]
        block = data[(data["day"] >= unique[edges[k]]) & (data["day"] <= unique[edges[k + 1] - 1])]
        if len(train) < MIN_TRAIN_ROWS // 2 or train[target].nunique() < 2 or block.empty:
            continue
        booster = _booster(len(train))
        booster.fit(design(train, sectors), train[target].astype(int))
        scores.append(np.asarray(booster.predict_proba(design(block, sectors)))[:, 1])
        outcomes.append(block[target].astype(int).to_numpy())
    if not scores:
        return np.array([]), np.array([])
    return np.concatenate(scores), np.concatenate(outcomes)


def fit(table: pd.DataFrame, cfg: PatternModelConfig, horizon: int) -> TrainedModel:
    """Train on all of `table`; calibrate on scores the model family produced for days it
    had not seen."""
    target = f"up_{horizon}d"
    data = table.dropna(subset=[target]).sort_values("day")
    sectors = sorted(data["sector"].dropna().unique())

    raw, outcome = _out_of_sample_scores(data, target, sectors, cfg.embargo_days)
    booster = _booster(len(data))
    booster.fit(design(data, sectors), data[target].astype(int))

    method = cfg.calibration if len(raw) >= MIN_ISOTONIC_ROWS else "platt"
    if len(raw) < 50 or len(set(outcome)) < 2:
        # Nothing to calibrate against: answer "coin flip" rather than trust raw scores.
        calibrator: Any = LogisticRegression().fit(np.array([[-1.0], [1.0]]), [0, 1])
        calibrator.coef_[:] = 0.0
        calibrator.intercept_[:] = 0.0
        method = "platt"
    elif method == "isotonic":
        calibrator = IsotonicRegression(y_min=0.02, y_max=0.98, out_of_bounds="clip")
        calibrator.fit(raw, outcome)
    else:  # Platt scaling: a two-parameter logistic curve through the raw scores
        calibrator = LogisticRegression()
        calibrator.fit(_logit(raw), outcome)
    return TrainedModel(booster, calibrator, method, horizon, sectors, calibration_rows=len(raw))


def walk_forward_folds(days: pd.Series, splits: int, embargo: int) -> list[tuple[date, date, date]]:
    """(train_end, test_start, test_end) per fold, expanding the training window.

    The days are cut into `splits + 1` equal blocks; block 0 is only ever trained on,
    blocks 1..splits are tested in turn on a model that saw everything before them
    except the last `embargo` trading days.
    """
    unique = sorted(days.unique())
    size = len(unique) // (splits + 1)
    folds = []
    for k in range(1, splits + 1):
        start = k * size
        end = len(unique) - 1 if k == splits else (k + 1) * size - 1
        train_end_index = start - embargo - 1
        if train_end_index < 0 or start > end:
            continue
        folds.append((unique[train_end_index], unique[start], unique[end]))
    return folds


@dataclass
class WalkForward:
    predictions: pd.DataFrame  # the out-of-sample rows with a `prob` column and `fold`
    folds: list[dict[str, Any]]
    skipped: list[str]


def walk_forward(table: pd.DataFrame, cfg: PatternModelConfig, horizon: int) -> WalkForward:
    target = f"up_{horizon}d"
    data = table.dropna(subset=[target]).sort_values("day").reset_index(drop=True)
    parts, folds, skipped = [], [], []
    plan = walk_forward_folds(data["day"], cfg.walk_forward_splits, cfg.embargo_days)
    for number, (train_end, test_start, test_end) in enumerate(plan, start=1):
        train = data[data["day"] <= train_end]
        test = data[(data["day"] >= test_start) & (data["day"] <= test_end)]
        if len(train) < MIN_TRAIN_ROWS or train[target].nunique() < 2 or test.empty:
            skipped.append(f"fold {number}: only {len(train)} training rows")
            continue
        model = fit(train, cfg, horizon)
        scored = test.copy()
        scored["prob"] = model.predict(test)
        scored["fold"] = number
        parts.append(scored)
        folds.append(
            {"fold": number, "train_rows": len(train), "train_through": str(train_end),
             "test_from": str(test_start), "test_to": str(test_end), "test_rows": len(test),
             **metrics(scored, horizon)}
        )  # fmt: skip
    predictions = (
        pd.concat(parts, ignore_index=True) if parts else data.head(0).assign(prob=[], fold=[])
    )
    return WalkForward(predictions, folds, skipped)


def daily_ic(scored: pd.DataFrame, horizon: int, min_names: int = 5) -> float | None:
    """Mean of the per-day rank correlation between probability and what happened.

    Computed within each day so it measures picking the right stocks, not timing the
    market. Days with fewer than `min_names` stocks are left out.
    """
    values = []
    for _day, group in scored.groupby("day"):
        if len(group) >= min_names and group["prob"].nunique() > 1:
            rho = spearmanr(group["prob"], group[f"excess_{horizon}d"]).statistic
            if not np.isnan(rho):
                values.append(rho)
    return float(np.mean(values)) if values else None


def metrics(scored: pd.DataFrame, horizon: int) -> dict[str, Any]:
    target, excess = scored[f"up_{horizon}d"].astype(int), scored[f"excess_{horizon}d"]
    prob = scored["prob"]
    if scored.empty:
        return {}
    confident = scored[prob >= prob.quantile(0.9)] if len(scored) >= 20 else scored
    overall = spearmanr(prob, excess).statistic if prob.nunique() > 1 else float("nan")
    return {
        "rows": len(scored),
        "base_rate": float(target.mean()),  # how often a stock beat SPY at all
        "hit_rate": float(((prob > 0.5) == (target == 1)).mean()),
        "hit_rate_top_decile": float(confident[f"up_{horizon}d"].mean()),
        "ic": None if np.isnan(overall) else float(overall),
        "ic_daily": daily_ic(scored, horizon),
        "brier": float(brier_score_loss(target, prob)),
        "brier_naive": float(brier_score_loss(target, np.full(len(target), target.mean()))),
        "auc": float(roc_auc_score(target, prob))
        if target.nunique() > 1 and prob.nunique() > 1
        else None,
        "mean_excess_top_decile": float(confident[f"excess_{horizon}d"].mean()),
    }


def calibration_table(scored: pd.DataFrame, horizon: int, bins: int = 5) -> list[dict[str, Any]]:
    """Predicted probability against what happened, in equal-sized groups."""
    if len(scored) < bins * 10 or scored["prob"].nunique() < 2:
        return []
    groups = pd.qcut(scored["prob"].rank(method="first"), bins, labels=False)
    rows = []
    for _bin, group in scored.groupby(groups):
        rows.append({"predicted": float(group["prob"].mean()),
                     "actual": float(group[f"up_{horizon}d"].mean()), "rows": len(group)})  # fmt: skip
    return rows


def importance(model: TrainedModel, top: int = 12) -> list[tuple[str, float]]:
    """Which inputs the trees leaned on most (share of total split gain)."""
    gain = model.booster.booster_.feature_importance(importance_type="gain")
    total = float(gain.sum()) or 1.0
    ranked = sorted(zip(model.features, gain, strict=True), key=lambda pair: pair[1], reverse=True)
    return [(name, float(value) / total) for name, value in ranked[:top] if value > 0]
