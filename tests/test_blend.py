"""Blend math tests (hr-unification todo 26): ``hr.recommend._blend_value``.

The blend formula turns a model's live-measured capability and its
confidence-shrunk authoritative reference into ONE conservative number —
the capability PRIOR consumed by ``RecommendationEngine`` and the seat
recommendations:

    eff_ref = c * ref + (1 - c) * 70        (70 = _REFERENCE_PRIOR)
    blend   = min(live, eff_ref)            (live never exceeds the cap;
                                             a live 0 forces a 0)

The plan's "tiebreak" slot was ``_role_reliability`` (a per-role reliability
tiebreak between two models with equal blended scores). It was REMOVED in
todo 14 (zero callers); since then the only deterministic tiebreak in the
subsystem is the ``min(live, eff_ref)`` cap inside ``_blend_value``
itself — a model that cannot reproduce its reputation through our own
endpoint is held to what we measured. These tests pin that math.

Pure offline: ``_blend_value`` is a pure function, no DB, no network.
"""

from __future__ import annotations

import pytest

from hr.recommend import _REFERENCE_PRIOR, _blend_value


class TestBlendValueFormula:
    def test_prior_constant_is_70(self):
        """The conservative prior an unproven model regresses toward."""
        assert _REFERENCE_PRIOR == 70.0

    def test_both_none_returns_zero(self):
        assert _blend_value(None, None) == 0.0

    def test_no_reference_uses_live_alone(self):
        assert _blend_value(81.5, None) == 81.5

    def test_no_live_uses_effective_reference(self):
        # eff_ref = 0.5*90 + 0.5*70 = 80
        assert _blend_value(None, (90.0, 0.5)) == 80.0

    def test_full_confidence_effective_ref_is_ref_score(self):
        # c=1.0 -> eff_ref = ref exactly
        assert _blend_value(None, (95.0, 1.0)) == 95.0

    def test_none_confidence_treated_as_full(self):
        # conf=None -> c=1.0 (docstring contract); live 98 capped at ref 90
        assert _blend_value(98.0, (90.0, None)) == 90.0

    def test_zero_confidence_collapses_to_prior(self):
        # c=0.0 -> eff_ref = 70 = prior
        assert _blend_value(None, (95.0, 0.0)) == 70.0


class TestBlendValueTiebreak:
    """The min(live, eff_ref) cap: reputation only wins when the live
    measurement does not contradict it."""

    def test_live_below_effective_ref_kept(self):
        # live measured 60 < eff_ref 95 -> the honest measurement is kept
        assert _blend_value(60.0, (95.0, 1.0)) == 60.0

    def test_live_above_effective_ref_capped(self):
        # live 80 > eff_ref 75 -> capped at the shrunk reference
        assert _blend_value(80.0, (75.0, 1.0)) == 75.0

    def test_live_zero_forces_zero(self):
        # a live 0 through our own endpoint forces a 0 (docstring contract)
        assert _blend_value(0.0, (95.0, 1.0)) == 0.0

    def test_boundary_equality_returns_value(self):
        # live == eff_ref == 80: min() tie returns the value itself
        assert _blend_value(80.0, (90.0, 0.5)) == 80.0

    def test_zero_ref_low_confidence_halfway_to_prior(self):
        # eff_ref = 0.5*0 + 0.5*70 = 35 (formula, not live)
        assert _blend_value(None, (0.0, 0.5)) == 35.0

    def test_high_confidence_reputation_beats_mediocre_live(self):
        # eff_ref = 0.9*99 + 0.1*70 = 96.1 > live 50 -> 50
        assert _blend_value(50.0, (99.0, 0.9)) == 50.0

    def test_live_float_passthrough_type(self):
        assert _blend_value(70, None) == 70.0
        assert isinstance(_blend_value(70, None), float)