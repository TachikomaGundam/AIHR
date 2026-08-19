"""Sequential stopping per spec §10.7.

SequentialStopper: pilot n=3, per-battery half-width thresholds, recompute
CI after each round, stop when half-width below threshold or n_max=10.
"""
from __future__ import annotations

import numpy as np
from typing import Dict, Optional
from dataclasses import dataclass, field
import yaml


@dataclass
class SequentialConfig:
    """Per-battery half-width thresholds + stopping params."""
    thresholds: Dict[str, float]  # {battery_code: half_width}
    n_initial: int = 3            # pilot n
    n_max: int = 10               # max rounds

    @classmethod
    def from_yaml(
        cls, path: str, required_batteries: Optional[list[str]] = None
    ) -> "SequentialConfig":
        """Load per-battery thresholds from thresholds.yaml.

        ``required_batteries`` collects the exact battery set the caller
        expects half_width config for; a battery missing from the yaml
        raises so a forgotten thresholds entry fails loud instead of
        silently never stopping.
        """
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        thresholds = data.get("half_width", {})
        if required_batteries:
            missing = [
                b for b in required_batteries
                if b not in thresholds or thresholds[b] is None
            ]
            if missing:
                raise ValueError(
                    f"thresholds.yaml missing half_width entry/entries for: {', '.join(missing)}"
                )
        return cls(
            thresholds=thresholds,
            n_initial=data.get("n_initial", 3),
            n_max=data.get("n_max", 10),
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
            # No threshold configured — never stop (or use a default)
            return False
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
