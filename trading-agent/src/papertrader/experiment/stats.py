"""The statistics fixed in docs/PREREGISTRATION.md, in plain Python (no hidden settings).

* mean of daily paired differences
* a moving-block bootstrap interval (blocks of 5 days, 5,000 resamples, fixed seed)
* a Newey-West standard error (5 lags) and its two-sided p-value
* Holm's correction for testing several hypotheses at once
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

BLOCK = 5
RESAMPLES = 5000
LAGS = 5
SEED = 20261120


def ranks(values: Sequence[float]) -> list[float]:
    """Average ranks (ties share their mean rank), starting at 1."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(x: Sequence[float], y: Sequence[float]) -> float | None:
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = ranks(x), ranks(y)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def block_bootstrap(diffs: Sequence[float], *, block: int = BLOCK, resamples: int = RESAMPLES,
                    seed: int = SEED) -> tuple[float, float] | None:  # fmt: skip
    """95% interval for the mean, resampling runs of `block` days to keep their links."""
    n = len(diffs)
    if n < 2 * block:
        return None
    rng = random.Random(seed)  # noqa: S311 - statistics, not security: fixed on purpose
    starts = n - block + 1
    means = []
    for _ in range(resamples):
        sample: list[float] = []
        while len(sample) < n:
            s = rng.randrange(starts)
            sample.extend(diffs[s : s + block])
        means.append(sum(sample[:n]) / n)
    means.sort()
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def newey_west(diffs: Sequence[float], lags: int = LAGS) -> tuple[float, float, float] | None:
    """(mean, standard error, two-sided p-value) with Newey-West weights."""
    n = len(diffs)
    if n < lags + 3:
        return None
    mean = sum(diffs) / n
    e = [d - mean for d in diffs]
    gamma0 = sum(v * v for v in e) / n
    var = gamma0
    for lag in range(1, lags + 1):
        cov = sum(e[t] * e[t - lag] for t in range(lag, n)) / n
        var += 2 * (1 - lag / (lags + 1)) * cov
    se = math.sqrt(max(var, 0.0) / n)
    if se == 0:
        return mean, 0.0, 0.0 if mean else 1.0
    t = mean / se
    p = math.erfc(abs(t) / math.sqrt(2))  # two-sided, normal approximation
    return mean, se, p


def holm(pvalues: Sequence[float | None]) -> list[float | None]:
    """Holm-adjusted p-values (missing ones stay missing and are not counted)."""
    present = sorted((p, i) for i, p in enumerate(pvalues) if p is not None)
    m = len(present)
    out: list[float | None] = [None] * len(pvalues)
    running = 0.0
    for rank, (p, i) in enumerate(present):
        running = max(running, min(1.0, (m - rank) * p))
        out[i] = running
    return out


@dataclass(frozen=True)
class Test:
    name: str
    question: str
    days: int
    mean: float | None
    interval: tuple[float, float] | None
    p: float | None
    p_holm: float | None = None

    @property
    def holds(self) -> bool:
        return (self.p_holm is not None and self.p_holm < 0.05 and self.interval is not None
                and self.interval[0] > 0)  # fmt: skip


def paired_test(name: str, question: str, diffs: Sequence[float]) -> Test:
    interval = block_bootstrap(diffs)
    nw = newey_west(diffs)
    return Test(name, question, len(diffs), sum(diffs) / len(diffs) if diffs else None, interval,
                nw[2] if nw else None)  # fmt: skip
