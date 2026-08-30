"""Sequential stopping per spec §10.7.

SequentialStopper: pilot n=3, per-battery half-width thresholds, recompute
CI after each round, stop when half-width below threshold or n_max=10.

Pair *decisions* (which finalist wins a battery) do NOT use the bootstrap-CI
stopper: they go through the anytime-valid empirical-Bernstein confidence
sequence in :mod:`hr.stats.empirical_bernstein`, fed with complete-round
paired differences normalized to [0, 1]. ``SequentialConfig`` carries the
per-battery ``min_effect`` region and the Bonferroni ``family_alpha`` for
that machinery.
"""
from __future__ import annotations

import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass, field
import yaml

#: Default practical-effect region half-width (normalized units) applied when
#: a battery has no explicit ``min_effect`` entry in thresholds.yaml.
DEFAULT_MIN_EFFECT: float = 0.05


def normalize_bounded_score(score: float, *, max_score: float) -> float:
    """Map a bounded raw score on ``[0, max_score]`` into normalized [0, 1] units.

    Explicit normalization boundary: benchmark scores (``ItemResult.score``,
    0-100) pass ``max_score=100.0``; grader/health scores
    (``GradeResult.score``, 0-1) pass ``max_score=1.0``.  Values are clamped
    into [0, 1] because the empirical-Bernstein bound requires observations
    known to lie in a bounded range.
    """
    if max_score <= 0:
        raise ValueError(f"max_score must be positive, got {max_score}")
    return float(np.clip(float(score) / max_score, 0.0, 1.0))


def bonferroni_pair_alpha(family_alpha: float, n_pairs: int) -> float:
    """Per-pair alpha after a Bonferroni split of the family alpha.

    With ``k`` configured finalist pairs per battery, each pair's sequence
    runs at ``family_alpha / k`` so the family-wise error stays at
    ``family_alpha``.
    """
    if n_pairs < 1:
        raise ValueError(f"n_pairs must be >= 1, got {n_pairs}")
    return family_alpha / n_pairs


@dataclass
class SequentialConfig:
    """Per-battery half-width thresholds + stopping params + decision config."""
    thresholds: Dict[str, float]  # {battery_code: half_width}
    n_initial: int = 3            # pilot n
    n_max: int = 10               # max rounds (budget cap in complete rounds)
    min_effect: Dict[str, float] = field(default_factory=dict)  # {battery_code: practical-effect half-width, normalized}
    family_alpha: float = 0.05    # family-wise error across finalist pairs

    @property
    def max_rounds(self) -> int:
        """Semantic alias for ``n_max``: the budget cap in complete rounds
        per battery.  Reuses the ``n_max`` config key — no second key."""
        return self.n_max

    def min_effect_for(self, battery: str) -> float:
        """Practical-effect region half-width (normalized [0,1] units) for a
        battery; falls back to :data:`DEFAULT_MIN_EFFECT` (0.05) when the
        battery has no explicit entry."""
        value = self.min_effect.get(battery)
        if value is None:
            return DEFAULT_MIN_EFFECT
        if value <= 0:
            raise ValueError(
                f"min_effect must be > 0 (normalized units), got {value} for {battery}"
            )
        return float(value)

    @classmethod
    def from_yaml(
        cls, path: str, required_batteries: Optional[list[str]] = None
    ) -> "SequentialConfig":
        """Load per-battery thresholds from thresholds.yaml.

        ``required_batteries`` collects the exact battery set the caller
        expects half_width *and* min_effect config for; a battery missing
        from either map raises so a forgotten thresholds entry fails loud
        instead of silently never stopping (or silently applying the default
        practical-effect region).
        """
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        thresholds = data.get("half_width", {})
        min_effect = data.get("min_effect", {})
        if required_batteries:
            missing_hw = [
                b for b in required_batteries
                if b not in thresholds or thresholds[b] is None
            ]
            if missing_hw:
                raise ValueError(
                    f"thresholds.yaml missing half_width entry/entries for: {', '.join(missing_hw)}"
                )
            missing_me = [
                b for b in required_batteries
                if b not in min_effect or min_effect[b] is None
            ]
            if missing_me:
                raise ValueError(
                    f"thresholds.yaml missing min_effect entry/entries for: {', '.join(missing_me)}"
                )
        return cls(
            thresholds=thresholds,
            n_initial=data.get("n_initial", 3),
            n_max=data.get("n_max", 10),
            min_effect=min_effect,
            family_alpha=float(data.get("family_alpha", 0.05)),
        )


@dataclass
class SequentialStopper:
    """Track sequential stopping for one battery.

    After each round, update the CI half-width. Stop when:
      - half_width <= threshold (precision met), OR
      - n_rounds >= n_max (budget exhausted).
    """
    battery_code: str
    config: SequentialConfig
    scores: list = field(default_factory=list)
    n_rounds: int = 0

    def add_round(self, scores_for_round: list | np.ndarray) -> None:
        """Append scores from one round (can be multiple reps)."""
        self.scores.extend(np.asarray(scores_for_round).flatten().tolist())
        self.n_rounds += 1

    def half_width(self, confidence: float = 0.95, B: int = 2000) -> float:
        """Compute bootstrap CI half-width for current scores."""
        if len(self.scores) < 2:
            return float("inf")
        arr = np.asarray(self.scores, dtype=float)
        # Bootstrap CI
        rng = np.random.default_rng(42)  # deterministic for comparability
        n = len(arr)
        means = np.zeros(B)
        for i in range(B):
            idx = rng.integers(0, n, size=n)
            means[i] = np.mean(arr[idx])
        alpha = 1 - confidence
        lower = np.percentile(means, 100 * alpha / 2)
        upper = np.percentile(means, 100 * (1 - alpha / 2))
        return float((upper - lower) / 2.0)

    def should_stop(self, confidence: float = 0.95, B: int = 2000) -> bool:
        """Check stopping condition."""
        threshold = self.config.thresholds.get(self.battery_code)
        if threshold is None:
            raise ValueError(
                f"no half_width threshold configured for battery "
                f"{self.battery_code!r}; refusing to guess (from_yaml "
                "requires every battery to have one)"
            )
        if self.n_rounds < self.config.n_initial:
            return False  # still in pilot
        hw = self.half_width(confidence, B)
        return hw <= threshold or self.n_rounds >= self.config.n_max

    def status(self, confidence: float = 0.95, B: int = 2000) -> Dict:
        """Return diagnostic dict."""
        threshold = self.config.thresholds.get(self.battery_code, float("inf"))
        return {
            "battery_code": self.battery_code,
            "n_rounds": self.n_rounds,
            "n_scores": len(self.scores),
            "half_width": self.half_width(confidence, B),
            "threshold": threshold,
            "should_stop": self.should_stop(confidence, B),
        }


__all__ = ["SequentialStopper", "SequentialConfig"]
