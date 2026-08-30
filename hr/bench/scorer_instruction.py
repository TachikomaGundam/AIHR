from __future__ import annotations

import json
import re
from typing import Any

from hr.bench.scorer_shared import _BenchmarkOutcome

def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON dict from model output, tolerant of fences and preamble.

    Strong models frequently wrap the JSON in ```json...``` fences or prepend a
    brief intro sentence; the scorer must not 0-score those cases. Try in order:
    (1) parse the whole stripped output, (2) strip markdown fences at boundaries
    and parse, (3) scan for the first balanced {...} respecting string literals
    and parse that slice. Return None if no valid dict-shaped object is found.
    """
    s = text.strip()
    # Pass 1: whole text is already valid JSON.
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Pass 2: strip markdown code fences (```[json] ... ```) and re-parse.
    stripped = s
    stripped = re.sub(r"^```\w*\s*\n", "", stripped)
    stripped = re.sub(r"\n?```\s*$", "", stripped)
    stripped = re.sub(r"```\w*\s*\n", "", stripped)
    stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass

    # Pass 3: try each "{" as a fresh starting position; walk to its matching "}"
    # (respecting string literals and backslash escapes); if that balanced slice
    # isn't valid JSON, advance to the next "{" and retry.
    i = 0
    while True:
        start = s.find("{", i)
        if start < 0:
            return None
        depth = 0
        in_string = False
        escape = False
        j = start
        end = -1
        while j < len(s):
            ch = s[j]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\" and j + 1 < len(s):
                    escape = True
                elif ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1
        if end < 0:
            return None
        candidate = s[start : end + 1]
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
        i = start + 1


def score_instruction_follow(text: str) -> _BenchmarkOutcome:
    """16 independent constraints; score = fraction satisfied (v1 semantics).

    The JSON extraction is robust to markdown code fences and brief
    preamble/postscript text; the constraint checks themselves are unchanged.
    """
    data = _extract_json_object(text)

    checks: list[tuple[str, bool]] = []  # (label, satisfied) in c1..c16 order
    satisfied = 0
    total = 16
    notes: list[str] = []
    lines: list[str] = []

    if isinstance(data, dict) and "lines" in data and isinstance(data["lines"], list):
        lines = [str(s) for s in data["lines"]]

    if not lines:
        return _BenchmarkOutcome(
            score=0.0, passed=False,
            raw_output=f"0/{total} constraints; no usable 'lines' list",
        )

    def _words(s: str) -> list[str]:
        """Split on whitespace; strip leading/trailing non-alphanumerics, lower."""
        return [re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", w).lower() for w in s.split()]

    # 1: lines is exactly 8 strings
    c1 = isinstance(data, dict) and "lines" in data and isinstance(data["lines"], list) and len(lines) == 8
    checks.append(("c1", c1))
    if c1: satisfied += 1
    else: notes.append(f"c1: not exactly 8 lines (got {len(lines)})")

    # 2: every sentence ends with '.'
    c2 = all(s.strip().endswith(".") for s in lines)
    checks.append(("c2", c2))
    if c2: satisfied += 1
    else: notes.append("c2: not all end with '.'")

    # 3: starting words in order
    expected_starts = ["the", "every", "tall", "its", "big", "at", "when", "now"]
    c3 = len(lines) == 8 and all(
        lines[i].strip()
        and re.sub(r"[^a-zA-Z0-9]", "", lines[i].strip().split()[0]).lower() == expected_starts[i]
        for i in range(8)
    )
    checks.append(("c3", c3))
    if c3: satisfied += 1
    else: notes.append("c3: start words mismatch")

    # 4: each sentence 6-10 words
    word_counts = [len(s.split()) for s in lines]
    c4 = all(6 <= wc <= 10 for wc in word_counts)
    checks.append(("c4", c4))
    if c4: satisfied += 1
    else: notes.append(f"c4: word counts {word_counts} not all in 6..10")

    # 5: across all, exactly 5 words end with 's'
    all_words: list[str] = []
    for s in lines:
        all_words.extend(_words(s))
    s_end_count = sum(1 for w in all_words if w and w.endswith("s"))
    c5 = s_end_count == 5
    checks.append(("c5", c5))
    if c5: satisfied += 1
    else: notes.append(f"c5: words ending in 's' = {s_end_count}, expected 5")

    # 6: no sentence contains 'z'
    combined_text = " ".join(s for s in lines)
    c6 = "z" not in combined_text.lower()
    checks.append(("c6", c6))
    if c6: satisfied += 1
    else: notes.append("c6: letter 'z' present")

    # 7: fourth sentence contains exactly one comma
    if len(lines) >= 4:
        comma_count = lines[3].count(",")
        c7 = comma_count == 1
        if not c7: notes.append(f"c7: 4th sentence has {comma_count} commas, expected 1")
    else:
        c7 = False
        notes.append("c7: no 4th sentence")
    checks.append(("c7", c7))
    if c7: satisfied += 1

    # 8: eighth (last) sentence has fewest words of all eight
    if len(lines) == 8:
        min_wc = min(word_counts)
        last_wc = word_counts[7]
        c8 = last_wc == min_wc
        if not c8: notes.append(f"c8: last sentence not shortest ({last_wc} vs min {min_wc})")
    else:
        c8 = False
        notes.append("c8: not 8 sentences")
    checks.append(("c8", c8))
    if c8: satisfied += 1

    # 9: exactly 2 digits total across all text
    digit_count = sum(1 for ch in combined_text if ch.isdigit())
    c9 = digit_count == 2
    checks.append(("c9", c9))
    if c9: satisfied += 1
    else: notes.append(f"c9: {digit_count} digits total, expected 2")

    # 10: no word longer than 10 letters
    max_word_len = max((len(w) for w in all_words if w), default=0)
    c10 = max_word_len <= 10
    checks.append(("c10", c10))
    if c10: satisfied += 1
    else: notes.append(f"c10: longest word {max_word_len} letters")

    # 11: second sentence contains "hour" exactly once
    if len(lines) >= 2:
        hour_count = _words(lines[1]).count("hour")
        c11 = hour_count == 1
        if not c11: notes.append(f"c11: 2nd sentence has {hour_count} 'hour'")
    else:
        c11 = False
        notes.append("c11: no 2nd sentence")
    checks.append(("c11", c11))
    if c11: satisfied += 1

    # 12: fifth sentence contains "tower" exactly once
    if len(lines) >= 5:
        tower_count = _words(lines[4]).count("tower")
        c12 = tower_count == 1
        if not c12: notes.append(f"c12: 5th sentence has {tower_count} 'tower'")
    else:
        c12 = False
        notes.append("c12: no 5th sentence")
    checks.append(("c12", c12))
    if c12: satisfied += 1

    # 13: total word count 50-70
    total_words = sum(word_counts[:8]) if len(lines) >= 8 else sum(word_counts)
    c13 = 50 <= total_words <= 70
    checks.append(("c13", c13))
    if c13: satisfied += 1
    else: notes.append(f"c13: total words {total_words} not in 50..70")

    # 14: third sentence contains exactly one digit
    if len(lines) >= 3:
        s3_digits = sum(1 for ch in lines[2] if ch.isdigit())
        c14 = s3_digits == 1
        if not c14: notes.append(f"c14: 3rd sentence has {s3_digits} digits")
    else:
        c14 = False
        notes.append("c14: no 3rd sentence")
    checks.append(("c14", c14))
    if c14: satisfied += 1

    # 15: last sentence contains "midnight" exactly once
    if lines:
        midnight_count = _words(lines[-1]).count("midnight")
        c15 = midnight_count == 1
        if not c15: notes.append(f"c15: last sentence has {midnight_count} 'midnight'")
    else:
        c15 = False
        notes.append("c15: no lines")
    checks.append(("c15", c15))
    if c15: satisfied += 1

    # 16: JSON is ONLY output (no code fences, no extra text)
    stripped = text.strip()
    code_fence_match = re.search(r"```[^\n]*\n?", stripped)
    json_match = re.search(r"\{.*\}", stripped, re.DOTALL)
    c16_ok = False
    if json_match is not None and code_fence_match is None:
        before_json = stripped[:json_match.start()].strip()
        after_json = stripped[json_match.end():].strip()
        c16_ok = len(before_json) == 0 and len(after_json) == 0
    checks.append(("c16", c16_ok))
    if c16_ok: satisfied += 1
    else: notes.append("c16: non-JSON text or code fence present")

    score = (satisfied / total) * 100.0
    return _BenchmarkOutcome(
        score=score, passed=(satisfied == total),
        raw_output=f"{satisfied}/{total} constraints; {'; '.join(notes) if notes else 'ok'}",
        item_scores=checks,
    )

__all__ = ["_extract_json_object", "score_instruction_follow"]
