"""Switch 1: turn the judge's confidence into an honest probability, and abstain when the
data cannot vouch for a trade.

Calibration (isotonic regression). The judge says 0.8; how often did ideas it scored
0.8 actually beat the market? Isotonic regression learns that mapping from past ideas
whose outcome is known, under one assumption only: a higher score should never mean a
lower chance. The result is the calibrated probability B trades on.

Abstaining (split conformal prediction). The newest 30% of the resolved ideas are kept
aside. On them we measure how wrong the calibrated probabilities were, and pick the
level q at which the set of plausible outcomes {worked, failed} is right at least
1 - alpha of the time. For a new idea with probability p, "worked" is plausible when
1 - p <= q and "failed" when p <= q. B trades only when "worked" is the only plausible
outcome; if both are, it abstains. With little or noisy data, q is large and B abstains
more: that is the point.

Until there are `min_ideas` resolved ideas, nothing is calibrated and the judge's raw
confidence and A's threshold are used; the decision log says "waiting".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
from sklearn.isotonic import IsotonicRegression

from papertrader.b.models import Calibration
from papertrader.b.outcomes import Resolved
from papertrader.config import ConfidenceSwitch

FIT_SHARE = 0.7


@dataclass(frozen=True)
class Calibrator:
    active: bool
    n_total: int
    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    quantile: float | None = None
    base_rate: float | None = None
    note: str = ""

    def probability(self, raw: float) -> float:
        if not self.active or not self.xs:
            return raw
        return float(np.clip(np.interp(raw, self.xs, self.ys), 0.0, 1.0))

    def verdict(self, probability: float) -> str:
        """trade | abstain | waiting (no conformal level yet)."""
        if not self.active or self.quantile is None:
            return "waiting"
        q = self.quantile
        worked_plausible = 1 - probability <= q
        failed_plausible = probability <= q
        return "trade" if worked_plausible and not failed_plausible else "abstain"


def _brier(probabilities: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not outcomes:
        return None
    return float(np.mean([(p - y) ** 2 for p, y in zip(probabilities, outcomes, strict=True)]))


def fit(
    resolved: Sequence[Resolved], cfg: ConfidenceSwitch, now: datetime
) -> tuple[Calibrator, Calibration]:
    """Fit on the older ideas, measure the conformal level on the newer ones."""
    ordered = sorted(resolved, key=lambda r: r.sample.as_of)
    n = len(ordered)
    rate = float(np.mean([r.worked for r in ordered])) if ordered else None
    alpha = cfg.abstain_error_rate
    n_cal_needed = math.ceil(1 / alpha) - 1
    split = int(n * FIT_SHARE)
    fit_part, cal_part = ordered[:split], ordered[split:]
    if n < cfg.min_ideas or len(cal_part) < n_cal_needed or len(fit_part) < 10:
        note = f"waiting for data: {n} of {cfg.min_ideas} resolved ideas"
        calibrator = Calibrator(False, n, base_rate=rate, note=note)
        row = Calibration(fitted_at=now, n_total=n, n_fit=len(fit_part), n_conformal=len(cal_part),
                          base_rate=rate, active=False, note=note)  # fmt: skip
        return calibrator, row

    x_fit = np.array([r.sample.raw for r in fit_part])
    y_fit = np.array([1 if r.worked else 0 for r in fit_part])
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip").fit(x_fit, y_fit)
    xs = [float(x) for x in iso.X_thresholds_]
    ys = [float(y) for y in iso.y_thresholds_]
    if len(xs) == 1:  # every score the same: a flat line
        xs, ys = [0.0, 1.0], [ys[0], ys[0]]

    probe = Calibrator(True, n, xs, ys)
    p_cal = [probe.probability(r.sample.raw) for r in cal_part]
    y_cal = [1 if r.worked else 0 for r in cal_part]
    scores = sorted(1 - (p if y else 1 - p) for p, y in zip(p_cal, y_cal, strict=True))
    k = math.ceil((len(scores) + 1) * (1 - alpha))
    quantile = 1.0 if k > len(scores) else float(scores[k - 1])

    note = (f"fitted on {len(fit_part)} ideas, abstain level measured on {len(cal_part)}; "
            f"{rate:.0%} of all ideas beat their benchmark")  # fmt: skip
    calibrator = Calibrator(True, n, xs, ys, quantile, rate, note)
    row = Calibration(
        fitted_at=now, n_total=n, n_fit=len(fit_part), n_conformal=len(cal_part), base_rate=rate,
        quantile=quantile, xs=xs, ys=ys,
        brier_raw=_brier([r.sample.raw for r in cal_part], y_cal),
        brier_calibrated=_brier(p_cal, y_cal), active=True, note=note,
    )  # fmt: skip
    return calibrator, row
