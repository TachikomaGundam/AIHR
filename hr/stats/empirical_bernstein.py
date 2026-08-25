"""Anytime-valid empirical-Bernstein confidence sequences for pair decisions.

Implements the predictable-plug-in empirical-Bernstein CS of Waudby-Smith &
Ramdas, "Estimating means of bounded random variables by betting" (2021/2023),
Theorem 2 (Eq. 13-15), the closed-form version of the Howard et al. (2021)
"Exponential line-crossing inequalities" empirical-Bernstein CS.

Construction over paired differences ``d_i in [-1, 1]`` (scores normalized to
[0, 1] per item, difference taken per item, complete rounds only):

* map ``y_i = (d_i + 1) / 2`` so ``y_i in [0, 1]``;
* expressions use the prior-regularized running mean and variance
  ``mu_hat_t = (1/2 + sum y_i) / (t+1)``,
  ``sig2_hat_t = (1/4 + sum (y_i - mu_hat_i)^2) / (t+1)``;
* predictable bet sizes ``lambda_t = min(sqrt(2 log(2/alpha) /
  (sig2_hat_{t-1} * t * log(1 + t))), c)`` with ``c = 1/2`` (the paper's
  recommended conservative cap);
* the (1 - alpha)-CS at time t is
  ``sum(lambda_i y_i) / sum(lambda_i) +/- (log(2/alpha) + sum v_i psi_e(lambda_i)) / sum(lambda_i)``
  where ``v_i = 4 (y_i - mu_hat_{i-1})^2`` and
  ``psi_e(lambda) = (-log(1 - lambda) - lambda) / 4``.

The CS is valid at *arbitrary stopping times* (time-uniform), so peeking at
every round never inflates the error rate — this is what replaces the
repeated-peek bootstrap-CI half-width stopper for stage1 pair decisions.
The sequence is deterministic (no RNG).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def _psi_e(lam: float) -> float:
    """Empirical-Bernstein cumulant bound for lambdas in [0, 1)."""
    return (-np.log1p(-lam) - lam) / 4.0


def _cs_on_unit(diffs01: np.ndarray, alpha: float, cap: float = 0.5) -> tuple[float, float, float]:
    """Anytime-valid (1-alpha) CS for the mean of [0,1]-valued observations.

    Returns ``(weighted_mean, lower, upper)`` of the predictable-plug-in
    empirical-Bernstein CS (Waudby-Smith & Ramdas, Theorem 2).  ``cap`` is the
    lambda cap ``c``; the recommended default is 1/2 (conservative).
    """
    log_inv = np.log(2.0 / alpha)
    ysum = 0.0
    var_sum = 0.0
    num = log_inv
    den = 0.0
    ysum_w = 0.0
    mu_prev = 0.5  # prior-regularized mean at t=0
    for i, y in enumerate(diffs01, start=1):
        v = 4.0 * (y - mu_prev) ** 2
        sig2 = (0.25 + var_sum) / i
        lam = min(float(np.sqrt(2.0 * log_inv / (sig2 * i * np.log1p(i)))), cap)
        num += v * _psi_e(lam)
        den += lam
        ysum_w += lam * y
        ysum += y
        mu_new = (0.5 + ysum) / (i + 1)
        var_sum += (y - mu_new) ** 2
        mu_prev = mu_new
    if den <= 0.0:
        return (0.5, 0.0, 1.0)
    hw = num / den
    center = ysum_w / den
    return (center, max(0.0, center - hw), min(1.0, center + hw))


@dataclass
class PairDecision:
    """Outcome of an anytime-valid comparison of one finalist pair."""

    model_a: str
    model_b: str
    battery_code: str
    effect: float | None           # mean paired difference (a - b), normalized [-1, 1]
    ci_lower: float | None         # anytime-valid interval on the difference
    ci_upper: float | None
    alpha: float                   # per-pair (Bonferroni-adjusted) level
    min_effect: float              # practical-effect region half-width (normalized)
    n_rounds: int
    n_diffs: int
    status: str                    # "decided" | "unresolvable" | "indeterminate"
    practical_effect: str          # "reject" | "accept" | "indeterminate"
    winner: str | None
    rationale: str = ""

    def to_dict(self) -> dict:
        """Machine-readable record for reports/JSON fixtures."""
        return {
            "model_a": self.model_a,
            "model_b": self.model_b,
            "battery_code": self.battery_code,
            "effect": self.effect,
            "ci_lower": self.ci_lower,
            "ci_upper": self.ci_upper,
            "alpha": self.alpha,
            "min_effect": self.min_effect,
            "n_rounds": self.n_rounds,
            "n_diffs": self.n_diffs,
            "status": self.status,
            "practical_effect": self.practical_effect,
            "winner": self.winner,
            "rationale": self.rationale,
        }


@dataclass
class EmpiricalBernsteinSequence:
    """Anytime-valid sequence over complete-round paired differences.

    Feed per-item paired differences of one *complete* round via
    :meth:`add_round` (partial rounds must be excluded by the caller — see
    ``hr/stage1_loop.py``).  :meth:`decide` then returns the effect estimate,
    the anytime-valid interval, the resolution status and the
    practical-effect decision against the configured ``min_effect`` region.
    """

    battery_code: str = ""
    alpha: float = 0.05            # per-pair level (family alpha / n_pairs)
    min_effect: float = 0.05       # practical-effect region half-width (normalized)
    max_rounds: int = 10           # budget cap in complete rounds
    diffs: list[float] = field(default_factory=list)
    n_rounds: int = 0
    _history: list[tuple[float, float]] = field(default_factory=list)

    @property
    def n_diffs(self) -> int:
        return len(self.diffs)

    def add_round(self, paired_diffs: list[float]) -> None:
        """Append one complete round of paired differences (normalized [-1,1])."""
        if len(paired_diffs) == 0:
            return
        self.diffs.extend(float(d) for d in paired_diffs)
        self.n_rounds += 1
        lower, upper = self._current_interval()
        if self._history:
            lower = max(lower, self._history[-1][0])
            upper = min(upper, self._history[-1][1])
        self._history.append((lower, upper))

    def effect(self) -> float | None:
        """Mean paired difference over all complete rounds; None with no data."""
        if not self.diffs:
            return None
        return float(np.mean(self.diffs))

    def interval(self) -> tuple[float | None, float | None]:
        """Running-intersection anytime-valid (1-alpha) interval on the mean
        difference; ``(None, None)`` with no complete rounds."""
        if not self._history:
            return (None, None)
        return self._history[-1]

    def _current_interval(self) -> tuple[float, float]:
        """(1-alpha) CS over the current prefix of diffs, mapped to the
        difference scale and intersected with the bounded range [-1, 1]."""
        y = 0.5 * (np.asarray(self.diffs, dtype=float) + 1.0)
        center_y, lo_y, hi_y = _cs_on_unit(y, self.alpha)
        lo_d = max(-1.0, 2.0 * lo_y - 1.0)
        hi_d = min(1.0, 2.0 * hi_y - 1.0)
        return (lo_d, hi_d)

    def _classify(self, lower: float, upper: float) -> tuple[str, str]:
        """Resolution status + practical-effect decision for interval (L, U)."""
        m = self.min_effect
        if lower > m:
            return ("decided", "reject")          # a better by > m: reject practical equivalence
        if upper < -m:
            return ("decided", "reject")          # b better by > m
        if lower >= -m and upper <= m:
            return ("unresolvable" if self.n_rounds >= self.max_rounds else "indeterminate", "accept")
        return ("unresolvable" if self.n_rounds >= self.max_rounds else "indeterminate", "indeterminate")

    def status(self) -> str:
        """Current resolution status without materializing a full decision."""
        if not self.diffs:
            return "indeterminate"
        lower, upper = self._history[-1] if self._history else self._current_interval()
        return self._classify(lower, upper)[0]

    def is_resolved(self) -> bool:
        """True when the sequence can no longer produce a winner (decided or
        budget-exhausted unresolvable) — drives battery continuation."""
        return self.status() in {"decided", "unresolvable"}

    def decide(self, *, model_a: str, model_b: str) -> PairDecision:
        """Full decision record for this pair (effect/interval/status/winner)."""
        if not self.diffs:
            return PairDecision(
                model_a=model_a, model_b=model_b, battery_code=self.battery_code,
                effect=None, ci_lower=None, ci_upper=None,
                alpha=self.alpha, min_effect=self.min_effect,
                n_rounds=self.n_rounds, n_diffs=self.n_diffs,
                status="indeterminate", practical_effect="indeterminate",
                winner=None, rationale="no complete rounds",
            )
        lower, upper = self._history[-1] if self._history else self._current_interval()
        status, practical = self._classify(lower, upper)
        effect = self.effect()
        winner: str | None = None
        if status == "decided":
            winner = model_a if lower > self.min_effect else model_b
            side = "above +min_effect" if winner == model_a else "below -min_effect"
            rationale = f"anytime-valid interval [{lower:.4g}, {upper:.4g}] excludes the practical-effect region {side}"
        elif practical == "accept":
            rationale = (
                f"anytime-valid interval [{lower:.4g}, {upper:.4g}] lies inside "
                f"[-{self.min_effect:.4g}, +{self.min_effect:.4g}]: practically equivalent"
            )
        else:
            rationale = (
                f"anytime-valid interval [{lower:.4g}, {upper:.4g}] overlaps the "
                f"practical-effect region and its complement"
            )
        return PairDecision(
            model_a=model_a, model_b=model_b, battery_code=self.battery_code,
            effect=effect, ci_lower=lower, ci_upper=upper,
            alpha=self.alpha, min_effect=self.min_effect,
            n_rounds=self.n_rounds, n_diffs=self.n_diffs,
            status=status, practical_effect=practical, winner=winner,
            rationale=rationale,
        )


__all__ = ["EmpiricalBernsteinSequence", "PairDecision"]