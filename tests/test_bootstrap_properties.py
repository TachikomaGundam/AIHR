"""Audit bug 5: separation bootstrap pair-alignment regression + properties.

Regression + hypothesis property net for ``_bootstrap_separation_from_state``
(hr/stage0_stats.py). The audit bug: pairs were flattened over ALL items in
dict-insertion order and zipped positionally, so uneven item coverage or
ragged per-item repetition counts crashed ``paired_bootstrap_separation``
(ValueError: identical shape) or silently misaligned scores.

Fix contract under test:
  * pair alignment is restricted to the intersection of shared item keys,
  * each side is flattened in ``sorted(shared_keys)`` order (resume-stable:
    stage1_resume rebuilds ``ORDER BY item_id``; vs HEAD's insertion-order
    flatten a multi-item fixture whose insertion order was not sorted may
    shift p — ACCEPTED positional-pairing trade-off),
  * per shared item, each side is truncated to the first
    ``k = min(len_a, len_b)`` scores (positional pairing, not round-aligned:
    stage0 records scores, not (round, score) tuples),
  * a pair with no shared usable items is SKIPPED with a warning and emits
    no row (never raises, never zips mismatched shapes).

Property settings: every property runs >= 200 examples
(``@settings(max_examples=200, deadline=None)``) with a pinned seed
(``@seed(42)``) for byte-identical CI re-runs. Equality-based invariants
use a deterministic array-seeded stand-in for the bootstrap (same shape
guard and resampling recipe, RNG seeded from the arrays instead of OS
entropy) so exact row equality is assertable; the never-raises/bounds and
monotone-dominance properties run the REAL bootstrap (their assertions are
RNG-immune).
"""

from __future__ import annotations

import hashlib
import logging
from unittest.mock import patch

import numpy as np
import pytest
from hypothesis import given, seed, settings
from hypothesis import strategies as st

import hr.stats.bootstrap as bootstrap_module
from hr.stage0_stats import SweepState, _bootstrap_separation_from_state

BATTERY = "reasoning"
ITEMS = ("i0", "i1", "i2")
MODELS = ("a", "b", "c")

PROP_SEED = 42
PROP_SETTINGS = settings(max_examples=200, deadline=None)


def _state_with(measurements: dict[str, dict[str, list[float]]]) -> SweepState:
    state = SweepState(sweep_id="s1")
    state.measurements_by_model_battery = measurements
    return state


def _deterministic_p(scores_a, scores_b) -> float:
    """Deterministic stand-in for the real bootstrap (equality properties).

    Mirrors ``hr.stats.bootstrap.paired_bootstrap_separation``: same shape
    guard, same resampling recipe — but the RNG is seeded from the arrays
    themselves instead of OS entropy, and a +-1e-9 antisymmetric dither
    breaks exact p_a == p_b ties. Post-fix these ties are the only source of
    winner-rule ambiguity, so removing them makes the symmetry and
    permutation invariants exact (and still order-sensitive: any flatten
    misalignment changes the byte-seeded draws).
    """
    a = np.asarray(scores_a, dtype=float)
    b = np.asarray(scores_b, dtype=float)
    if a.shape != b.shape:
        raise ValueError("scores_a and scores_b must have identical shape")
    if a.size == 0:
        return 0.0
    abytes = a.tobytes()
    bbytes = b.tobytes()
    digest = hashlib.sha1(abytes + bbytes).digest()
    rng = np.random.default_rng(int.from_bytes(digest[:8], "big") % (2**32))
    diffs = np.zeros(200)
    for i in range(200):
        idx = rng.integers(0, a.size, size=a.size)
        diffs[i] = np.mean(a[idx] - b[idx])
    p = float(np.mean(diffs > 0))
    return p + (1e-9 if abytes < bbytes else -1e-9)


# ---------------------------------------------------------------------------
# Regression unit tests (red on HEAD, green after the pair-alignment fix)
# ---------------------------------------------------------------------------


def test_bootstrap_unequal_rep_counts_align_min_rep() -> None:
    """Audit bug 5 regression: ragged per-item repetition counts.

    Model a recorded two scores for item i1, model b one. HEAD flattens to
    [0.8, 0.9] vs [0.7] and the bootstrap raises ValueError (shape (2,) vs
    (1,)). The fix aligns the pair on the shared item truncated to
    k = min(2, 1) = 1 first-k score per side, so only (0.8, 0.7) is paired:
    p_separated is exactly 1.0 (every one of the 2000 draws is 0.8 - 0.7
    > 0, so the golden is RNG-immune).
    """
    state = _state_with(
        {
            "a|reasoning": {"i1": [0.8, 0.9]},
            "b|reasoning": {"i1": [0.7]},
        }
    )
    result = _bootstrap_separation_from_state(state)
    (pair,) = result["reasoning"]
    assert pair["model_a"] == "a"
    assert pair["model_b"] == "b"
    assert pair["p_separated"] == 1.0
    assert pair["p_weak"] == 0.0
    assert pair["p_tie"] == 0.0


def test_bootstrap_partial_overlap_uses_only_shared_items_sorted(monkeypatch) -> None:
    """Exact alignment pin: shared items only, sorted flatten, MIN-length first-k.

    The recording fake observes the exact arrays handed to the bootstrap.
    Fixture: model a inserts i2 BEFORE i1 and owns a third item i3 that model
    b lacks; per shared item the sides have ragged repetition counts
    (a: 2x i1, 3x i2; b: 1x i1, 2x i2). Expected aligned pair: common keys
    sorted -> [i1, i2]; k(i1) = min(2, 1) = 1, k(i2) = min(3, 2) = 2 -> first-k
    positional append gives sa = [0.8, 0.9, 0.9], sb = [0.2, 0.1, 0.1];
    i3 contributes to neither side; the two calls are the two directions.
    On HEAD the same fixture misaligns (insertion-order full flatten), so this
    also serves as red evidence for the silent-misalignment bug class.
    """
    captured: list[tuple[list[float], list[float]]] = []

    def recording_paired(sa, sb) -> float:
        captured.append((list(sa), list(sb)))
        return 0.5

    monkeypatch.setattr(bootstrap_module, "paired_bootstrap_separation", recording_paired)
    state = _state_with(
        {
            "a|reasoning": {"i2": [0.9, 0.9, 0.9], "i1": [0.8, 0.8], "i3": [1.0]},
            "b|reasoning": {"i2": [0.1, 0.1], "i1": [0.2]},
        }
    )
    _bootstrap_separation_from_state(state)
    assert captured == [
        ([0.8, 0.9, 0.9], [0.2, 0.1, 0.1]),
        ([0.2, 0.1, 0.1], [0.8, 0.9, 0.9]),
    ]


def test_bootstrap_disjoint_items_skip_with_warning(caplog) -> None:
    """Disjoint key coverage: pair skipped, warning names both models, no row."""
    state = _state_with(
        {
            "a|reasoning": {"i1": [0.8]},
            "b|reasoning": {"i2": [0.7]},
        }
    )
    with caplog.at_level(logging.WARNING, logger="hr.stage0_stats"):
        result = _bootstrap_separation_from_state(state)
    assert result == {"reasoning": []}
    (record,) = caplog.records
    assert record.name == "hr.stage0_stats"
    assert record.getMessage() == (
        "skipping separation pair a vs b on reasoning: no shared items (1 vs 1)"
    )


def test_bootstrap_empty_dict_model_pair_skipped(caplog) -> None:
    """stage1-shape: a model present as an EMPTY {} dict never crashes the pair.

    The pair has zero shared items -> skipped with a warning; the battery key
    survives with an empty pair list (stage0 omits the key, stage1_loop sets
    {} unconditionally — both shapes must be handled).
    """
    state = _state_with(
        {
            "a|reasoning": {"i1": [0.5]},
            "b|reasoning": {},
        }
    )
    with caplog.at_level(logging.WARNING, logger="hr.stage0_stats"):
        result = _bootstrap_separation_from_state(state)
    assert result == {"reasoning": []}
    (record,) = caplog.records
    assert record.getMessage() == (
        "skipping separation pair a vs b on reasoning: no shared items (1 vs 0)"
    )


def test_bootstrap_shared_items_without_paired_scores_skip(caplog) -> None:
    """Shared key but trunctation to zero scores: skipped, never an empty-array p.

    k = min(0, 1) = 0 leaves no paired scores; calling the bootstrap with two
    empty arrays would yield NaN p (mean of empty slice). The fix skips the
    pair with a warning instead.
    """
    state = _state_with(
        {
            "a|reasoning": {"i1": []},
            "b|reasoning": {"i1": [0.5]},
        }
    )
    with caplog.at_level(logging.WARNING, logger="hr.stage0_stats"):
        result = _bootstrap_separation_from_state(state)
    assert result == {"reasoning": []}
    (record,) = caplog.records
    assert record.getMessage() == (
        "skipping separation pair a vs b on reasoning: shared items have no paired scores (1 vs 1)"
    )


def test_bootstrap_equal_length_sorted_golden_unchanged() -> None:
    """Equal-length reduction: the old numeric behavior is preserved exactly.

    Two items with identical per-item repetition counts (8 each) inserted in
    REVERSE sorted order. Sorted-key flatten makes the aligned arrays identical
    to what HEAD computed on the same fixture (insertion order == sorted order
    in the legacy goldens), and the all-1.0 vs all-0.0 scores make every
    bootstrap draw positive -> p_separated == 1.0 exactly (RNG-immune golden).
    """
    state = _state_with(
        {
            "a|reasoning": {"i2": [1.0] * 8, "i1": [1.0] * 8},
            "b|reasoning": {"i2": [0.0] * 8, "i1": [0.0] * 8},
        }
    )
    result = _bootstrap_separation_from_state(state)
    (pair,) = result["reasoning"]
    assert pair["model_a"] == "a"
    assert pair["model_b"] == "b"
    assert pair["p_separated"] == 1.0
    assert pair["p_weak"] == 0.0
    assert pair["p_tie"] == 0.0


# ---------------------------------------------------------------------------
# Hypothesis properties
# ---------------------------------------------------------------------------


item_dict_st = st.dictionaries(
    keys=st.sampled_from(ITEMS),
    values=st.lists(
        st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False),
        min_size=0,
        max_size=3,
    ),
    min_size=0,
    max_size=3,
)
model_item_dicts_st = st.dictionaries(
    keys=st.sampled_from(MODELS),
    values=item_dict_st,
    min_size=0,
    max_size=3,
)
two_item_dicts_st = st.tuples(item_dict_st, item_dict_st)
perm_st = st.permutations(list(ITEMS))


@PROP_SETTINGS
@seed(PROP_SEED)
@given(model_item_dicts=model_item_dicts_st)
def test_property_never_raises_and_bounds(
    model_item_dicts: dict[str, dict[str, list[float]]],
) -> None:
    """Pins: NEVER raises + every emitted p in [0, 1] + battery-key contract.

    Arbitrary per-(battery, model) coverage: disjoint / partial / identical
    key sets, ragged per-item repetition counts (including empty score lists
    and EMPTY {} model dicts), and 1-3 models per battery — the full shape
    space the sweep produces post-T1 (infra-skips create ragged reps) plus
    stage1's {} entries. Runs the REAL bootstrap, so any failed alignment
    surfaces as the ValueError the audit bug produced; the classifier keeps
    each row's three probabilities summing to 1.0.
    """
    measurements = {f"{m}|{BATTERY}": d for m, d in model_item_dicts.items()}
    result = _bootstrap_separation_from_state(_state_with(measurements))
    # Battery-key contract: every battery present in the measurements has a
    # result key (possibly with an empty pair list); none are invented.
    assert set(result) == (set() if not measurements else {BATTERY})
    for pairs in result.values():
        for row in pairs:
            for key in ("p_separated", "p_weak", "p_tie"):
                assert 0.0 <= row[key] <= 1.0, (key, row)
            assert row["p_separated"] + row["p_weak"] + row["p_tie"] == pytest.approx(1.0)
            assert row["model_a"] != row["model_b"]
            assert row["directional"] is True


_RELABEL = {"a": "b", "b": "a"}


@PROP_SETTINGS
@seed(PROP_SEED)
@given(pair_draw=two_item_dicts_st)
def test_property_symmetry_under_model_relabel(
    pair_draw: tuple[dict[str, list[float]], dict[str, list[float]]],
) -> None:
    """Pins: model-relabeling equivariance of the pair canonization.

    Swapping model a's and model b's measurement dicts must swap the roles of
    the two model names in every emitted row while p values travel with the
    (winner, loser) canon — i.e. r(sigma . S) == sigma . r(S). Exact equality
    uses the deterministic pseudo-bootstrap (RNG-free), whose antisymmetric
    dither removes every source of winner ambiguity EXCEPT one: when the two
    aligned arrays are identical, the p values tie in both relabelings and
    the code's direction handling awards the row to the alphabetically first
    model. That tie-break is direction handling, which the audit scope
    forbids changing (a landmine), so the property then pins only the
    match: both runs agree the row is a tie with identical probabilities.
    """
    d_a, d_b = pair_draw
    with patch("hr.stats.bootstrap.paired_bootstrap_separation", _deterministic_p):
        r1 = _bootstrap_separation_from_state(
            _state_with({"a|reasoning": d_a, "b|reasoning": d_b})
        )
        r2 = _bootstrap_separation_from_state(
            _state_with({"a|reasoning": d_b, "b|reasoning": d_a})
        )
    row1, row2 = r1[BATTERY], r2[BATTERY]
    if row1 and row1[0]["p_tie"] == 1.0:
        # Exact tie (identical aligned arrays): the only sigma-non-equivariant
        # output — direction handling, out of scope. Both relabelings must
        # still agree the row is a tie with identical probabilities.
        assert row2[0]["p_tie"] == 1.0
        assert (row1[0]["p_separated"], row1[0]["p_weak"], row1[0]["p_tie"]) == (
            row2[0]["p_separated"],
            row2[0]["p_weak"],
            row2[0]["p_tie"],
        )
        return
    expected = [
        {
            **row,
            "model_a": _RELABEL[row["model_a"]],
            "model_b": _RELABEL[row["model_b"]],
        }
        for row in row2
    ]
    assert row1 == expected


@PROP_SETTINGS
@seed(PROP_SEED)
@given(d_a=item_dict_st, d_b=item_dict_st)
def test_property_monotone_shift_dominates(
    d_a: dict[str, list[float]],
    d_b: dict[str, list[float]],
) -> None:
    """Pins: a large constant shift of one model's scores forces a separated win.

    With every a score raised by +1000 over b's [0, 1] domain, EVERY bootstrap
    draw of mean(a) - mean(b) is strictly positive, so p_a == 1.0 exactly and
    p_b == 0.0 exactly (RNG-immune), and the row must be (a, b) with
    p_separated == 1.0. Runs the REAL bootstrap end-to-end on the aligned
    post-truncation arrays: any misalignment (mismatched shapes) or truncation
    bug raises/falsifies here. Pairs with no shared usable items are skipped
    (no such row exists — the property is vacuous then).
    """
    shifted = {item: [s + 1000.0 for s in scores] for item, scores in d_a.items()}
    state = _state_with({"a|reasoning": shifted, "b|reasoning": d_b})
    result = _bootstrap_separation_from_state(state)
    for row in result[BATTERY]:
        assert row["model_a"] == "a"
        assert row["model_b"] == "b"
        assert row["p_separated"] == 1.0
        assert row["p_weak"] == 0.0
        assert row["p_tie"] == 0.0


@PROP_SETTINGS
@seed(PROP_SEED)
@given(model_item_dicts=model_item_dicts_st, perm=perm_st)
def test_property_permutation_invariance(
    model_item_dicts: dict[str, dict[str, list[float]]],
    perm: list[str],
) -> None:
    """Pins: item-key INSERTION ORDER never affects the emitted rows.

    Reordering the keys of every model's per-item dict (arbitrary permutation
    of the item pool) must leave the result byte-identical: the fix flattens
    in ``sorted(shared_keys)`` order, whereas HEAD's insertion-order flatten
    changes the paired arrays — and with the order-sensitive deterministic
    pseudo-bootstrap any flatten-order dependence falsifies here. Also pins
    resume-stability: stage1_resume rebuilds measurements ORDER BY item_id,
    so sorted order keeps resumed sweeps' separation stable.
    """
    reordered = {m: {k: d[k] for k in perm if k in d} for m, d in model_item_dicts.items()}
    with patch("hr.stats.bootstrap.paired_bootstrap_separation", _deterministic_p):
        r1 = _bootstrap_separation_from_state(
            _state_with({f"{m}|{BATTERY}": d for m, d in model_item_dicts.items()})
        )
        r2 = _bootstrap_separation_from_state(
            _state_with({f"{m}|{BATTERY}": d for m, d in reordered.items()})
        )
    assert r1 == r2