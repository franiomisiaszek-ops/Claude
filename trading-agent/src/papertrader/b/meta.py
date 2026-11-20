"""Switch 10: meta-labelling.

The judge decides which way to trade ("the primary signal"); a second, deliberately
small model learns from past ideas *when the judge tends to be right*, and trades only
the ideas it expects to work. It is a regularised logistic regression on a handful of
features every past idea has (the judge's confidence, the agents' agreement, the blend,
the holding period, buy or short), so it cannot memorise much from little data.

It stays off until `min_ideas` ideas have a known outcome, and is checked on the newest
30% before it is used: if it does not beat both the blend and the plain hit rate on those
ideas, it is not used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression

from papertrader.b.outcomes import IdeaSample, Resolved
from papertrader.config import MetaLabelSwitch

FEATURES = ("raw", "judge", "agreement", "horizon", "buy")


def features(sample: IdeaSample) -> list[float]:
    return [sample.raw, sample.features.get("judge", sample.raw),
            sample.features.get("agreement", 0.5), sample.horizon_days / 20.0,
            1.0 if sample.action == "buy" else 0.0]  # fmt: skip


@dataclass
class MetaModel:
    active: bool
    note: str
    model: LogisticRegression | None = None

    def probability(self, sample: IdeaSample) -> float | None:
        if not self.active or self.model is None:
            return None
        return float(self.model.predict_proba(np.array([features(sample)]))[0, 1])


def _log_loss(p: Sequence[float], y: Sequence[int]) -> float:
    eps = 1e-6
    return float(-np.mean([yi * np.log(max(pi, eps)) + (1 - yi) * np.log(max(1 - pi, eps))
                           for pi, yi in zip(p, y, strict=True)]))  # fmt: skip


def train(resolved: Sequence[Resolved], cfg: MetaLabelSwitch) -> MetaModel:
    ordered = sorted(resolved, key=lambda r: r.sample.as_of)
    if len(ordered) < cfg.min_ideas:
        return MetaModel(
            False, f"waiting for data: {len(ordered)} of {cfg.min_ideas} resolved ideas"
        )
    y = [1 if r.worked else 0 for r in ordered]
    if len(set(y)) < 2:
        return MetaModel(False, "every past idea had the same result; nothing to learn")
    split = int(len(ordered) * 0.7)
    x = np.array([features(r.sample) for r in ordered])
    model = LogisticRegression(C=0.5, max_iter=500).fit(x[:split], y[:split])
    held_y = y[split:]
    meta_p = model.predict_proba(x[split:])[:, 1]
    blend_p = [min(max(r.sample.raw, 0.01), 0.99) for r in ordered[split:]]
    rate = min(max(sum(y[:split]) / split, 0.01), 0.99)  # "every idea works this often"
    meta_loss = _log_loss(list(meta_p), held_y)
    base_loss = min(_log_loss(blend_p, held_y), _log_loss([rate] * len(held_y), held_y))
    if meta_loss >= base_loss:
        return MetaModel(False, f"not used: no better than the blend or the plain hit rate on the "
                                f"newest ideas (log loss {meta_loss:.3f} vs {base_loss:.3f})")  # fmt: skip
    final = LogisticRegression(C=0.5, max_iter=500).fit(x, y)
    return MetaModel(True, f"trained on {len(ordered)} ideas; log loss {meta_loss:.3f} vs "
                           f"{base_loss:.3f} without it, on the newest", final)  # fmt: skip
