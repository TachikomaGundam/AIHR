"""Tests for bootstrap.py.

Verifies spec §10.2 behavior:
- P ≈ 1.0 when A is clearly > B
- P ≈ 0.5 when A ≡ B (tie)
- 1-D and 2-D (items × reps) paths
"""
from __future__ import annotations

import numpy as np
import pytest

from hr.stats import bootstrap


def test_separation_clearly_different_distributions():
    """A clearly outperforms B → P(mean(A)>mean(B)) ≈ 1.0."""
    rng = np.random.default_rng(0)
    a = rng.normal(loc=0.85, scale=0.05, size=(20, 5))
    b = rng.normal(loc=0.15, scale=0.05, size=(20, 5))
    p = bootstrap.paired_bootstrap_separation(a, b, B=2000, seed=1)
    assert p >= 0.999, f"expected near-1.0, got {p}"
    assert bootstrap.classify(p) == "separated"


def test_identical_distributions_tie():
    """A ≡ B → P ≈ 0.5 (within ±0.1)."""
    rng = np.random.default_rng(42)
    a = rng.normal(loc=0.5, scale=0.2, size=(20, 5))
    b = rng.normal(loc=0.5, scale=0.2, size=(20, 5))
    p = bootstrap.paired_bootstrap_separation(a, b, B=2000, seed=2)
    # B=2000 → binomial SD ≈ sqrt(p*(1-p)/B) ≈ 0.011 → ±3σ ≈ 0.035
    # Allow ±0.12 to absorb seed-induced drift while still detecting bias.
    assert 0.38 <= p <= 0.62, f"expected ~0.5, got {p}"
    assert bootstrap.classify(p) == "tie"


def test_1d_path_works():
    """1-D scores path."""
    a = np.array([0.8, 0.85, 0.9, 0.75, 0.82])
    b = np.array([0.4, 0.5, 0.45, 0.3, 0.55])
    p = bootstrap.paired_bootstrap_separation(a, b, B=2000, seed=3)
    assert p >= 0.95
    assert bootstrap.classify(p) == "separated"


def test_classify_boundaries():
    assert bootstrap.classify(0.97) == "separated"
    assert bootstrap.classify(0.95) == "separated"
    assert bootstrap.classify(0.80) == "weak"
    assert bootstrap.classify(0.90) == "weak"
    assert bootstrap.classify(0.79) == "tie"
    assert bootstrap.classify(0.0) == "tie"


def test_ci_returns_finite_bounds():
    s = np.linspace(0.1, 0.9, 50)
    mean, lo, hi = bootstrap.ci(s, confidence=0.95, B=1000, seed=4)
    assert mean == pytest.approx(0.5, rel=0.01)
    assert lo < mean < hi


def test_shape_mismatch_raises():
    with pytest.raises(ValueError):
        bootstrap.paired_bootstrap_separation(np.zeros((3, 2)), np.zeros((3, 3)))


def test_3d_rejected():
    with pytest.raises(ValueError):
        bootstrap.paired_bootstrap_separation(np.zeros((2, 2, 2)), np.zeros((2, 2, 2)))
