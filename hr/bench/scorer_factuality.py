"""B2 context-grounded factuality scoring (zero-LLM, deterministic)."""
from __future__ import annotations

from hr.bench.scorer_shared import _BenchmarkOutcome


def score_factuality(card_results: list[tuple[str, str]]) -> _BenchmarkOutcome:
    """card_results: (answerable_response, trap_response) per card, order-fixed.

    item passes iff the key fact appears AND the trap refusal is clean
    (no forbidden sheet value fabricated into the trap answer). An
    always-refuse model fails every key check; an always-answer model fails
    every trap - the balanced design has no free pass (see design doc).
    """
    item_scores: list[tuple[str, bool]] = []
    for i, (card, (ans, trap)) in enumerate(zip(_cards(), card_results)):
        key_hit = str(card["key"]).lower() in ans.lower()
        trap_low = trap.lower()
        forbidden = card["forbidden"]
        forbidden = (forbidden,) if isinstance(forbidden, str) else tuple(forbidden)
        fabricated = any(str(f).lower() in trap_low for f in forbidden)
        item_scores.append((f"f{i:02d}", key_hit and not fabricated))
    n_pass = sum(1 for _, ok in item_scores if ok)
    score = 100.0 * n_pass / len(item_scores)
    return _BenchmarkOutcome(
        score=score, passed=score >= 100.0,
        raw_output=f"{n_pass}/{len(item_scores)} factual (key-hit, no fabrication)",
        item_scores=item_scores,
    )


def _cards():
    from hr.bench import prompts
    return prompts.FACTUALITY_CARDS
