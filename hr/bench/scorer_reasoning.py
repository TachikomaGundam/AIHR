from __future__ import annotations

import re

from hr.bench.scorer_shared import _BenchmarkOutcome, _parse_number
from hr.bench.truths import long_horizon_truths, reasoning_truths

def score_reasoning(text: str) -> _BenchmarkOutcome:
    """Lenient per-problem answer extraction (v1 semantics unchanged).

    The A1..A13 structured format is nice but not required: strong reasoning
    models frequently write verbose working and end each question section with
    the final number rather than an 'A<N>:' line. The scorer accepts three
    candidate sources per question (in priority order — any match makes it
    correct): (1) 'A<N>: <value>' lines, (2) \\boxed{...} / {..} values in that
    problem's section, (3) the last numeric token of the section; plus a
    whole-text fallback when no Q<N> markers exist.
    """
    truths = reasoning_truths()
    n_questions = len(truths)

    # --- Whole-text numeric-token regex (reused) ---
    NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:\s*/\s*-?\d+(?:\.\d+)?)?")

    # --- Strategy 1: collect all A<N>: values from the full text ---
    a_candidates_by_q: dict[int, list[float]] = {}
    for m in re.finditer(r"\bA(\d+)\s*:\s*([^\n]+)", text):
        idx = int(m.group(1))
        if 1 <= idx <= n_questions:
            parsed = _parse_number(m.group(2))
            if parsed is not None:
                a_candidates_by_q.setdefault(idx, []).append(parsed)

    # --- Strategy 2/3: build Q<N> sections and per-section candidates ---
    section_candidates: dict[int, list[float]] = {}
    q_markers = [(m.start(), m.end(), int(m.group(1)))
                 for m in re.finditer(r"\bQ(\d+)\b[^A-Za-z0-9]*", text)
                 if 1 <= int(m.group(1)) <= n_questions]

    for i, (_start, end, q) in enumerate(q_markers):
        next_start = q_markers[i + 1][0] if i + 1 < len(q_markers) else len(text)
        section = text[end:next_start]
        cands: list[float] = []

        # Strategy 2: \boxed{X} or \boxed{X} (LaTeX with or without trailing space)
        for bm in re.finditer(r"\\boxed\s*\{([^{}]*?)\}", section):
            p = _parse_number(bm.group(1))
            if p is not None:
                cands.append(p)
        # Bare-braced number: {123} when not preceded by backslash
        for bm in re.finditer(r"(?<!\\)\{\s*(-?\d+(?:\.\d+)?(?:\s*/\s*-?\d+(?:\.\d+)?)?)\s*\}", section):
            p = _parse_number(bm.group(1))
            if p is not None:
                cands.append(p)

        # Strategy 3: last numeric token in the section
        all_nums_in_section = NUM_RE.findall(section)
        if all_nums_in_section:
            p = _parse_number(all_nums_in_section[-1])
            if p is not None:
                cands.append(p)

        if cands:
            section_candidates[q] = cands

    # --- Fallback: if no Q-sections were found, use global last-number ---
    global_last_number: float | None = None
    if not q_markers:
        all_nums = NUM_RE.findall(text)
        if all_nums:
            global_last_number = _parse_number(all_nums[-1])

    # --- Score: a question is correct iff ANY of its candidates matches truth ---
    correct = 0
    verdicts: list[str] = []
    item_scores: list[tuple[str, bool]] = []
    any_candidate_at_all = False
    for q in range(1, n_questions + 1):
        truth = truths[q]
        cands: list[float] = []
        if q in a_candidates_by_q:
            cands.extend(a_candidates_by_q[q])
        if q in section_candidates:
            cands.extend(section_candidates[q])
        if not q_markers and global_last_number is not None:
            cands.append(global_last_number)
        any_candidate_at_all = any_candidate_at_all or bool(cands)

        matched = False
        if cands:
            # Truths are integers; compare parsed-as-integer.
            for c in cands:
                if int(round(c)) == truth:
                    matched = True
                    break
        verdicts.append(f"Q{q}:{'✓' if matched else '✗'}")
        item_scores.append((f"q{q}", matched))
        if matched:
            correct += 1

    if not any_candidate_at_all:
        return _BenchmarkOutcome(
            score=0.0, passed=False,
            raw_output=f"ERROR: no candidates extracted; text={text[:200]}",
        )

    score = (correct / n_questions) * 100.0
    return _BenchmarkOutcome(
        score=score, passed=(score == 100.0),
        raw_output=f"{correct}/{n_questions} correct; {','.join(verdicts)}",
        item_scores=item_scores,
    )


# ---------------------------------------------------------------------------
# long_horizon — 4 components (25 points each) over the 6-task CPM graph
# ---------------------------------------------------------------------------


def score_long_horizon(text: str) -> _BenchmarkOutcome:
    """Grade the project-planning answer across 4 independently-verifiable
    components (critical path / duration / slack / action), 25 points each."""
    truths = long_horizon_truths()
    critical_tasks: list[str] = truths["critical_path"]
    duration = int(truths["duration"])
    nc_slack: dict[str, int] = truths["non_critical_slack"]
    action_task: str | None = truths["action_task"]
    action_new_dur: int | None = truths["action_new_duration"]

    item_scores: list[tuple[str, bool]] = []
    verdicts: list[str] = []

    # --- 1. CRITICAL_PATH: critical tasks must appear in order (lenient). ---
    path_m = re.search(r"CRITICAL_PATH\s*:\s*([^\n]+)", text, re.IGNORECASE)
    path_ok = False
    if path_m:
        tokens = re.findall(r"\b([A-F])\b", path_m.group(1).upper())
        idx = 0
        for tok in tokens:
            if idx < len(critical_tasks) and tok == critical_tasks[idx]:
                idx += 1
            elif tok in critical_tasks:
                break  # out-of-order critical task → fail
        path_ok = idx == len(critical_tasks)
    item_scores.append(("critical_path", path_ok))
    verdicts.append(f"CP:{'OK' if path_ok else 'FAIL'}")

    # --- 2. DURATION: must match computed project end exactly. ---
    dur_m = re.search(r"DURATION\s*:\s*(\d+)", text, re.IGNORECASE)
    dur_ok = dur_m is not None and int(dur_m.group(1)) == duration
    item_scores.append(("duration", dur_ok))
    verdicts.append(f"DUR:{'OK' if dur_ok else 'FAIL'}")

    # --- 3. SLACK: every non-critical task must be reported with its slack. ---
    slack_m = re.search(r"SLACK\s*:\s*([^\n]+)", text, re.IGNORECASE)
    parsed_slack: dict[str, int] = {}
    if slack_m:
        for m in re.finditer(r"([A-F])\s*=\s*(\d+)", slack_m.group(1), re.IGNORECASE):
            parsed_slack[m.group(1).upper()] = int(m.group(2))
    slack_ok = all(parsed_slack.get(t) == s for t, s in nc_slack.items())
    item_scores.append(("slack", slack_ok))
    verdicts.append(f"SLK:{'OK' if slack_ok else 'FAIL'}")

    # --- 4. ACTION: NONE iff duration <= threshold; otherwise fast-track. ---
    act_m = re.search(r"ACTION\s*:\s*([^\n]+)", text, re.IGNORECASE)
    act_ok = False
    if act_m:
        act_text = act_m.group(1).strip()
        if action_task is None:
            act_ok = re.search(r"\bNONE\b", act_text, re.IGNORECASE) is not None
        else:
            ft = re.search(r"\b([A-F])\b.*?(\d+)\s*days?", act_text, re.IGNORECASE)
            if ft:
                proposed = ft.group(1).upper()
                new_dur = int(ft.group(2))
                act_ok = (proposed in critical_tasks) and (new_dur == action_new_dur)
    item_scores.append(("action", act_ok))
    verdicts.append(f"ACT:{'OK' if act_ok else 'FAIL'}")

    correct = sum(1 for _lbl, ok in item_scores if ok)
    score = (correct / 4) * 100.0
    return _BenchmarkOutcome(
        score=score,
        passed=(score == 100.0),
        raw_output=f"{correct}/4 correct [{' '.join(verdicts)}]; "
                   f"expected CP={'->'.join(critical_tasks)} dur={duration} "
                   f"slack={nc_slack} action={action_task or 'NONE'}; "
                   f"{text[:200]}",
        item_scores=item_scores,
    )


# ---------------------------------------------------------------------------
# tool_use — final-text grading vs target 105.63
# ---------------------------------------------------------------------------


def score_tool_use_text(text: str, tool_used: bool) -> _BenchmarkOutcome:
    """Grade the final text from the tool loop (target 105.63, v1 semantics).

    100 with a tool call in the loop, 60 without; 20 for close (<= 2.0);
    0 otherwise. Extraction tries TOTAL:/keyword/last-number, stripping
    currency symbols and commas first.
    """
    target = 105.63
    clean = text.replace("$", "").replace(",", "")

    got_num: float | None = None

    # Strategy 1: explicit 'TOTAL:' / 'TOTAL =' header.
    m = re.search(r"TOTAL\s*[=:]\s*([-\d.]+)", clean, re.IGNORECASE)
    if m:
        got_num = _parse_number(m.group(1))

    # Strategy 2: any result-keyword followed by a number.
    if got_num is None:
        kw = re.search(
            r"\b(?:total|total is|equals|=|answer|final answer|final|"
            r"therefore|result|result is)\s*[:=]?\s*([-\d.]+)\b",
            clean, re.IGNORECASE,
        )
        if kw:
            got_num = _parse_number(kw.group(1))

    # Strategy 3: last number appearing anywhere in the text.
    if got_num is None:
        all_nums = re.findall(r"[-]?\d+(?:\.\d+)?", clean)
        if all_nums:
            got_num = _parse_number(all_nums[-1])

    if got_num is None:
        return _BenchmarkOutcome(
            score=0.0, passed=False,
            raw_output=f"no TOTAL parsed; final={text[:300]}",
        )
    if abs(got_num - target) <= 0.5:
        score = 100.0 if tool_used else 60.0
        return _BenchmarkOutcome(
            score=score, passed=(score >= 100.0),
            raw_output=f"correct ({got_num}); tool_used={tool_used}",
            item_scores=[("total", score >= 100.0)],
        )
    if abs(got_num - target) <= 2.0:
        return _BenchmarkOutcome(
            score=20.0, passed=False,
            raw_output=f"close but wrong ({got_num} vs {target})",
            item_scores=[("total", False)],
        )
    return _BenchmarkOutcome(
        score=0.0, passed=False,
        raw_output=f"wrong ({got_num} vs {target})",
        item_scores=[("total", False)],
    )

__all__ = ["score_reasoning", "score_long_horizon", "score_tool_use_text"]
