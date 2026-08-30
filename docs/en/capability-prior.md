# The blended capability prior (`min(live, c·ref + (1−c)·70)`)

## What it is

`hr recommend` scores every model against a **capability prior** per
(model, capability-category) pair, defined in `hr/recommend.py::_blend_value`:

    eff_ref = c·ref + (1−c)·70          # confidence-shrunk published reference
    score   = min(live, eff_ref)        # capped at what our own endpoint measured

where

- `live` — the model's most recent **measured** score for that capability
  (latest row per `benchmark_name` in the legacy `hr_benchmarks` table);
- `ref` — the **published reference** score for the capability from the
  single curated store `hr/reference.py::REFERENCE_SCORES`
  (seeded into `hr_reference` by `hr reference --seed`);
- `c` — the curator's **confidence** in the reference score (0..1);
- `70` — `_REFERENCE_PRIOR`, the conservative baseline an unproven
  (low-confidence) model regresses toward.

The `min(...)` caps the blend at the live measurement, so a model that
cannot reproduce its reputation through our own endpoint is held to what we
actually measured (a live 0 forces a 0). The confidence shrink makes a real
leaderboard score beat an optimistic low-confidence estimate, which matters
because curated scores are the differentiator among frontier models whose
live graded tests saturate near 100.

## Where it feeds

The capability prior is applied in two places:

1. `RecommendationEngine.seat_recommendations()` — every seat from
   `configs/seats.yaml` (the authoritative seat list; no code tables) is
   scored against each model's blended capability profile: eligibility via
   the seat's `required_capabilities` hard gates, ranking via the seat's
   primary capabilities. Output of `hr recommend`.
2. `RecommendationEngine._blended_scores()` / `score_model()` — the
   per-model `Set A` blended profile (general composite) used by the
   library-level report.

`hr recommend` is strictly **read-only** with respect to assignments: the
unified verdict engine (`hr verdict`) is the only seat-assignment engine in
the system.

## Seam: the unified verdict fitness

The unified verdict/ranker (`hr/cli.py::verdict`) computes seat
fitness from **livebench measurements** (`hr.measurement`) through the
knob→battery mapping (`_KNOB_TO_BATTERY`), and owns all assignment writes.
It does **not** consume the v1 capability prior — no code was added to the
ranker to import it (deliberate; wiring it into fitness would require new
machinery, and the plan scopes this todo to expose the prior, not to
re-plumb the ranker).

The documented seam for any future consumer: the prior is available as
per-(model, capability) blended scores in `hr_recommend`'s output and via
`RecommendationEngine._blended_scores(model_fk)`, and its inputs
(`REFERENCE_SCORES`, `_REFERENCE_PRIOR`, `_blend_value`) are module-level
and importable. A future todo that wants the prior inside verdict fitness
should consume `_blend_value`/`REFERENCE_SCORES` (or the `hr_reference`
table) as a **prior** term — e.g. blended into fitness with its own weight —
rather than duplicating the curated dataset.

## Rationale (why 70)

`_REFERENCE_PRIOR` predates the unification (v1 `recommend.py`); the
unification todo preserves it verbatim so seat/model ranking stays stable
versus the v1 behavior. 70 is a deliberately conservative middle-of-scale
anchor: unmeasured capabilities never float, and low-confidence references
pull toward it rather than toward the model's optimistic claim.
