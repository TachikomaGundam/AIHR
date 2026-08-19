"""Bootstrap statistics per spec §10.2.

Paired bootstrap at item level (content variance) with within-item
repetition resampling (round/gateway variance). Returns P(mean(A−B) > 0)
which is the confidence that A > B.

Classification (spec §10.2):
  p >= 0.95 → separated
  0.80 <= p < 0.95 → weak
  p < 0.80 → tie
"""
from __future__ import annotations

import numpy as np
from typing import Tuple


def paired_bootstrap_separation(
    scores_a: np.ndarray,
    scores_b: np.ndarray,
    B: int = 2000,
    seed: int | None = None,
) -> float:
    """
    Compute P(mean(A) > mean(B)) via paired bootstrap.

    Parameters
    ----------
    scores_a, scores_b : np.ndarray
        1-D array of item-level scores for models A and B.
        If 2-D (items × repetitions), resample items then reps within items.
    B : int
        Number of bootstrap iterations (default 2000 per spec).
    seed : int, optional
        RNG seed for reproducibility.

    Returns
    -------
    float
        Confidence P(mean(A) > mean(B)). Range [0, 1].
    """
    rng = np.random.default_rng(seed)
    a = np.asarray(scores_a, dtype=float)
    b = np.asarray(scores_b, dtype=float)

    if a.shape != b.shape:
        raise ValueError("scores_a and scores_b must have identical shape")

    # 1-D case: item-level bootstrap only
    if a.ndim == 1:
        n_items = len(a)
        diffs = np.zeros(B)
        for i in range(B):
            idx = rng.integers(0, n_items, size=n_items)
            diffs[i] = np.mean(a[idx] - b[idx])
        return float(np.mean(diffs > 0))

    # 2-D case: (items × repetitions) — resample items, then reps within each item
    if a.ndim == 2:
        n_items, n_reps = a.shape
        diffs = np.zeros(B)
        for i in range(B):
            # Step 1: resample items (content variance)
            item_idx = rng.integers(0, n_items, size=n_items)
            a_items = a[item_idx]  # (n_items, n_reps)
            b_items = b[item_idx]
            # Step 2: within each item, resample repetitions (round/gateway variance)
            rep_idx = rng.integers(0, n_reps, size=(n_items, n_reps))
            a_resampled = np.take_along_axis(a_items, rep_idx, axis=1)
            b_resampled = np.take_along_axis(b_items, rep_idx, axis=1)
            # Mean across all resampled items/reps
            diffs[i] = np.mean(a_resampled - b_resampled)
        return float(np.mean(diffs > 0))

    raise ValueError("scores_a/scores_b must be 1-D or 2-D")


def classify(p: float) -> str:
    """
    Classify separation confidence per spec §10.2.

    Returns one of: 'separated', 'weak', 'tie'.
    """
    if p >= 0.95:
        return "separated"
    elif p >= 0.80:
        return "weak"
    else:
        return "tie"


def ci(
    scores: np.ndarray,
    confidence: float = 0.95,
    B: int = 2000,
    seed: int | None = None,
) -> Tuple[float, float, float]:
    """
    Compute bootstrap confidence interval for mean(scores).

    Parameters
    ----------
    scores : np.ndarray
        1-D or 2-D (items × reps). If 2-D, flatten to 1-D first.
    confidence : float
        Confidence level (default 0.95 for 95% CI).
    B : int
        Bootstrap iterations.
    seed : int, optional
        RNG seed.

    Returns
    -------
    (mean, lower, upper) : Tuple[float, float, float]
        Point estimate + CI bounds.
    """
    rng = np.random.default_rng(seed)
    s = np.asarray(scores, dtype=float).ravel()
    n = len(s)
    means = np.zeros(B)
    for i in range(B):
        idx = rng.integers(0, n, size=n)
        means[i] = np.mean(s[idx])
    alpha = 1 - confidence
    lower = float(np.percentile(means, 100 * alpha / 2))
    upper = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return float(np.mean(s)), lower, upper


__all__ = ["paired_bootstrap_separation", "classify", "ci"]
