from __future__ import annotations

import re
from typing import Callable

from hr.bench.scorer_shared import _BenchmarkOutcome
from hr.bench.stress_prompts import (
    STRESS_BANNED_WORD,
    STRESS_BULLET_LINES,
    STRESS_BULLET_PREFIX,
    STRESS_CONSTRAINT_PREFIX,
    STRESS_MAX_WORDS,
)

def score_long_context(text: str) -> _BenchmarkOutcome:
    """Grade needle retrieval: alpha/bravo/charlie fragments must both appear.

    The 3 decoy fragments are informational only (a ``decoy_trapped`` note),
    matching v1 — a model that relays a decoy code still scores on its
    needles but the trap is surfaced in the raw output.
    """
    alpha_ok = "4471" in text and "2210" in text
    bravo_ok = "9938" in text and "6643" in text
    charlie_ok = "1057" in text and "8830" in text
    score = (int(alpha_ok) + int(bravo_ok) + int(charlie_ok)) / 3 * 100.0
    decoy_hit = ("4472" in text or "9939" in text or "1058" in text)
    note = "decoy_trapped!" if decoy_hit else "clean"
    item_scores = [("alpha", alpha_ok), ("bravo", bravo_ok), ("charlie", charlie_ok)]
    return _BenchmarkOutcome(
        score=score, passed=(score == 100.0),
        raw_output=f"[{note}] alpha={alpha_ok} bravo={bravo_ok} charlie={charlie_ok}; {text[:300]}",
        item_scores=item_scores,
    )


# ---------------------------------------------------------------------------
# attention_probe — 8 binary probes (position sweep / assoc pair / distractor)
# ---------------------------------------------------------------------------

#: Reserved expected-dict key carrying the comma-joined decoy numbers.
_DISTRACTOR_KEY = "__distractors__"


def score_attention_probe(text: str, expected: dict[str, str]) -> _BenchmarkOutcome:
    """Grade the 8 attention probes; per-item binary, score = mean * 100.

    ``expected`` maps each item label (see ``_ITEM_LABELS[attention_probe]``)
    to the answer string, plus the reserved ``__distractors__`` key holding
    the four decoy numbers comma-joined. An item passes on a case-insensitive
    whitespace-normalized substring match; decoy numbers echoed in the text
    never fail an item but surface as a ``distractor_confused`` note.
    """
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip()).lower()

    normalized = _norm(text)
    distractors = [
        n for n in (expected.get(_DISTRACTOR_KEY, "") or "").split(",") if n
    ]

    item_scores: list[tuple[str, bool]] = []
    misses: list[str] = []
    # expected is built in registry label order; the reserved key is skipped.
    for label in expected:
        if label == _DISTRACTOR_KEY:
            continue
        ok = _norm(expected[label]) in normalized
        item_scores.append((label, ok))
        if not ok:
            misses.append(label)

    confused = any(n in normalized for n in distractors)
    notes = ["distractor_confused"] if confused else ["clean"]
    if misses:
        notes.append(f"missing={','.join(misses)}")
    score = (len(item_scores) - len(misses)) / len(item_scores) * 100.0
    return _BenchmarkOutcome(
        score=score, passed=(score == 100.0),
        raw_output=f"[{'; '.join(notes)}]; {text[:300]}",
        item_scores=item_scores,
    )


# ---------------------------------------------------------------------------
# attention_stress — 5-constraint survival at 4 checkpoints of a 20-turn
# scripted conversation (constraint ids in instruction order)
# ---------------------------------------------------------------------------

def score_attention_stress(checkpoint_responses: dict[str, str], token: str) -> _BenchmarkOutcome:
    """Grade the 4 checkpoints; 100 iff ALL 5 constraints still obeyed.

    ``checkpoint_responses`` maps each checkpoint label (see
    ``_ITEM_LABELS[attention_stress]``) to the response text at that turn;
    the runner captures them in registry order. A checkpoint passes only if
    every constraint holds on its latest response; the note names the
    constraints broken at the EARLIEST failing checkpoint.
    """
    # Constraint ids in instruction order (end_token closes over the token).
    constraints: tuple[tuple[str, Callable[[str], bool]], ...] = (
        ("start_tag", lambda t: t.lstrip().startswith(STRESS_CONSTRAINT_PREFIX)),
        ("end_token", lambda t: t.rstrip().endswith(token)),
        ("neg_word", lambda t: re.search(
            rf"\b{re.escape(STRESS_BANNED_WORD)}\b", t, re.IGNORECASE) is None),
        ("bullet_lines", lambda t: sum(
            1 for ln in t.splitlines()
            if ln.lstrip().startswith(STRESS_BULLET_PREFIX)) == STRESS_BULLET_LINES),
        ("word_count", lambda t: len(t.split()) <= STRESS_MAX_WORDS),
    )

    def _check(text: str) -> tuple[str, ...]:
        return tuple(
            cid for cid, check in constraints if not check(text)
        )

    item_scores: list[tuple[str, bool]] = []
    n_ok = 0
    earliest: str | None = None
    for label, text in checkpoint_responses.items():
        broken = _check(text)
        ok = not broken
        item_scores.append((label, ok))
        if ok:
            n_ok += 1
        elif earliest is None:
            earliest = f"{label}: {','.join(broken)}"
    score = n_ok / len(item_scores) * 100.0
    note = earliest or "clean"
    last = next(reversed(checkpoint_responses.values()), "")
    return _BenchmarkOutcome(
        score=score, passed=(score == 100.0),
        raw_output=f"[{note}] survived={n_ok}/{len(item_scores)}; {last[:300]}",
        item_scores=item_scores,
    )

__all__ = ["score_long_context", "score_attention_probe", "score_attention_stress"]
