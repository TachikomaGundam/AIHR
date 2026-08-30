"""Benchmark prompts + long-context haystack + vision test image (task 12).

Everything is a verbatim port from v1's benchmark engine — no item content
was authored new, only relocated so the bench package has zero model names
and zero wire-specific code (prompts are pure data; the engine in
:mod:`hr.bench.engine` turns them into ChatRequests).
"""

from __future__ import annotations

import random
import string
import uuid

from hr.bench.prompt_image import build_test_image_png

# ---------------------------------------------------------------------------
# Prompts (HARSH frontier-limit v4)
# ---------------------------------------------------------------------------

CODE_GEN_PROMPT: str = (
    "Write these three Python functions. Output ONLY all three inside a single "
    "```python code fence, exact names, type hints, no prose, no test code.\n\n"
    "1. def sliding_window_median(nums: list[int], k: int) -> list[float]\n"
    "   For every window of k consecutive elements return its median as a float. "
    "Even k -> average of the two middle values.\n\n"
    "2. def burst_balloons(nums: list[int]) -> int\n"
    "   Add boundary 1s outside; bursting balloon i yields nums[left]*nums[i]*"
    "nums[right] coins. Return the maximum total coins from bursting all "
    "balloons.\n\n"
    "3. def count_inversions(arr: list[int]) -> int\n"
    "   Count pairs i<j with arr[i] > arr[j]. MUST be efficient (n up to 100000): "
    "an O(n log n) merge-sort-count approach; a naive O(n^2) will time out.\n"
)

REASONING_PROMPT: str = (
    "Solve each. Work briefly, then give each answer on its own line as "
    "'A1: <integer>' .. 'A13: <integer>' (integers only).\n"
    "Q1.  How many integers n with 1 <= n <= 1,000,000 satisfy n ≡ 1 (mod 3), "
    "n ≡ 2 (mod 5), n ≡ 3 (mod 7), and n is NOT divisible by 11?\n"
    "Q2.  What is the sum of the decimal digits of 7^777?\n"
    "Q3.  How many positive divisors does 10! have?\n"
    "Q4.  What is the 1000th prime number?\n"
    "Q5.  What is the 10th Catalan number C_10 (the number of monotonic lattice paths "
    "from (0,0) to (10,10) that never rise above the diagonal y=x)?\n"
    "Q6.  What are the last three digits of 13^500?\n"
    "Q7.  How many integers in [1, 10000] are either a perfect square or a perfect cube (or both)?\n"
    "Q8.  How many onto (surjective) functions are there from a 4-element set to a 3-element set?\n"
    "Q9.  How many trailing zeros does 200! have?\n"
    "Q10. How many derangements (permutations with no fixed point) are there of 7 elements? (i.e. !7)\n"
    "Q11. What is the smallest positive integer n that has exactly 100 positive divisors?\n"
    "Q12. How many subsets of {1, 2, ..., 20} have the property that the sum of "
    "their elements is divisible by 5?\n"
    "Q13. What is the sum of all prime factors (counted with multiplicity) of 100!?\n"
)

TOOL_TASK_PROMPT: str = (
    "You have a `calculate` tool. Compute an order total: 3 items at $17.50 each, "
    "plus 2 items at $24.99 each. First find the subtotal. If the subtotal is greater "
    "than $100, apply a 5% loyalty discount to the subtotal. Then add 8.5% sales tax "
    "on the (possibly discounted) subtotal. Use calculate for the arithmetic steps. "
    "End by stating 'TOTAL: <number>'."
)

INSTRUCTION_PROMPT: str = (
    "Describe a clock tower. Satisfy ALL constraints; return ONLY one JSON object "
    '{"lines": [ ... ]} (no code fences, no extra text):\n'
    '1.  "lines" is a list of exactly 8 strings.\n'
    "2.  Each string is exactly one sentence ending with a period.\n"
    "3.  The eight sentences begin, in order, with: The, Every, Tall, Its, Big, At, When, Now (case-insensitive).\n"
    "4.  Each sentence has between 6 and 10 words inclusive.\n"
    "5.  Across all sentences, exactly 5 words end with the letter 's' (whole words, case-insensitive).\n"
    "6.  No sentence contains the letter 'z'.\n"
    "7.  The fourth sentence contains exactly one comma.\n"
    "8.  The eighth (last) sentence has the fewest words of all eight sentences.\n"
    "9.  The entire text contains exactly 2 digits in total.\n"
    "10. No word is longer than 10 letters.\n"
    '11. The second sentence contains the word "hour" exactly once.\n'
    '12. The fifth sentence contains the word "tower" exactly once.\n'
    "13. The total word count across all 8 sentences is between 50 and 70 inclusive.\n"
    "14. The third sentence contains exactly one digit.\n"
    '15. The last sentence contains the word "midnight" exactly once.\n'
    "16. The JSON object is the ONLY output (no code fences, no extra text).\n"
)

LONG_CONTEXT_FOLLOW_UP: str = (
    "This document contains three RECOVERY codes (alpha, bravo, charlie). It also "
    "contains similar-looking VERIFICATION decoy codes — ignore those. Retrieve the "
    "three RECOVERY codes and reply with exactly: 'alpha: <code>', 'bravo: <code>', "
    "'charlie: <code>'."
)

VISION_PROMPT: str = (
    "Look at this image: several solid colored squares on a white background. "
    "(a) How many colored squares? (b) List each color. (c) State the position of "
    "each (top-left, top-right, bottom-left, bottom-right)."
)

SPEED_PROMPT: str = "Say hello in 10 different languages, one per line."

LONG_HORIZON_PROMPT: str = (
    "You are given a project with 6 tasks and dependencies:\n"
    "  Task A: 3 days, no dependencies\n"
    "  Task B: 5 days, depends on A\n"
    "  Task C: 2 days, depends on A\n"
    "  Task D: 4 days, depends on B\n"
    "  Task E: 6 days, depends on B and C\n"
    "  Task F: 1 day, depends on D and E\n\n"
    "1. Find the critical path (longest path through the dependency graph). "
    "State it as a sequence of tasks.\n"
    "2. Calculate the minimum project duration (total days along the critical path).\n"
    "3. Calculate the slack (float) for each NON-critical task.\n"
    "4. If the project duration exceeds 15 days, propose one task to fast-track "
    "(reduce by 2 days) that would shorten the critical path. State which task and "
    "the new duration.\n\n"
    "Reply with EXACTLY:\n"
    "  CRITICAL_PATH: <task1>-><task2>->...\n"
    "  DURATION: <number> days\n"
    "  SLACK: <taskX>=<days>, <taskY>=<days>, ...\n"
    "  ACTION: <task> fast-track, new duration <number> days | NONE\n"
)

# ---------------------------------------------------------------------------
# Long-context constants (3 needles + 3 decoys, ~240K chars)
# ---------------------------------------------------------------------------

HAYSTACK_CHARS: int = 240_000
NEEDLE_A: str = "The alpha recovery code is 4471-KILO-2210."
NEEDLE_B: str = "The bravo recovery code is 9938-ECHO-6643."
NEEDLE_C: str = "The charlie recovery code is 1057-TANGO-8830."
DECOY_A: str = "The alpha verification code is 4472-KILO-2211."
DECOY_B: str = "The bravo verification code is 9939-ECHO-6644."
DECOY_C: str = "The charlie verification code is 1058-TANGO-8831."
HAYSTACK_FILLER: str = (
    "The quick brown fox jumps over the lazy dog. "
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "
    "Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
    "Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris "
    "nisi ut aliquip ex ea commodo consequat. Duis aute irure dolor in "
    "reprehenderit in voluptate velit esse cillum dolore eu fugiat nulla pariatur."
)


def build_haystack() -> str:
    """Filler repeated to HAYSTACK_CHARS with decoy+needle pairs at 30/60/90%."""
    repeat = HAYSTACK_CHARS // len(HAYSTACK_FILLER) + 1
    base = (HAYSTACK_FILLER * repeat)[:HAYSTACK_CHARS]
    pos_a = int(0.30 * len(base))
    pos_b = int(0.60 * len(base))
    pos_c = int(0.90 * len(base))
    # Insert deepest first so offsets don't shift.
    result = (
        base[:pos_c]
        + "\n" + DECOY_C + "\n" + NEEDLE_C + "\n"
        + base[pos_c:pos_b]
        + "\n" + DECOY_B + "\n" + NEEDLE_B + "\n"
        + base[pos_b:pos_a]
        + "\n" + DECOY_A + "\n" + NEEDLE_A + "\n"
        + base[pos_a:]
    )
    return result


# ---------------------------------------------------------------------------
# attention_probe — 8 runtime-generated probes (position sweep + assoc pair
# + distractor resistance) inside the same 240K-char haystack
# ---------------------------------------------------------------------------

#: Depth bands (fractions of haystack length) for the 5 position-sweep
#: needles. Lost-in-the-middle coverage: head, early-middle, mid, late-middle,
#: tail — one needle per band at an rng-chosen offset.
ATTENTION_PROBE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("pos_head", 0.00, 0.12),
    ("pos_mid_early", 0.18, 0.32),
    ("pos_mid", 0.42, 0.58),
    ("pos_mid_late", 0.68, 0.82),
    ("pos_tail", 0.88, 1.00),
)

#: Small built-in landmark -> city table (unambiguous, one city each).
LANDMARK_CITIES: tuple[tuple[str, str], ...] = (
    ("Kiasma museum", "Helsinki"),
    ("Sagrada Familia", "Barcelona"),
    ("CN Tower", "Toronto"),
    ("Brandenburg Gate", "Berlin"),
    ("Colosseum", "Rome"),
    ("Mount Fuji", "Tokyo"),
    ("Sydney Opera House", "Sydney"),
    ("Taj Mahal", "Agra"),
    ("Eiffel Tower", "Paris"),
    ("Table Mountain", "Cape Town"),
    ("Burj Khalifa", "Dubai"),
    ("Statue of Liberty", "New York"),
)

#: Small built-in first/last name pools for the associative-pair person.
PERSON_FIRST_NAMES: tuple[str, ...] = (
    "Marta", "Jonas", "Priya", "Diego", "Ingrid", "Wei",
)
PERSON_LAST_NAMES: tuple[str, ...] = (
    "Lindqvist", "Kowalski", "Rahman", "Vega", "Sorensen", "Chen",
)


def _probe_token(rng: random.Random) -> str:
    """'XXXX-XXXX' uppercase hex token drawn from the rng (never fixed)."""
    raw = uuid.UUID(int=rng.getrandbits(128)).hex
    return f"{raw[:4]}-{raw[4:8]}".upper()


def build_attention_probe(rng: random.Random) -> tuple[str, dict[str, str]]:
    """Build one runtime-generated attention probe + expected answers.

    Eight binary probes planted in a 240K-char haystack (deepest-first
    insertion so earlier offsets never shift):
      1-5  position sweep: one UUID token needle per depth band
      6-7  NoLiMa-style associative pair (person -> city, direct + inferred)
      8    decoy resistance: 1 needle among 4 same-format distractors

    Returns (prompt_text, expected) where expected maps each item label to
    the answer string, plus the reserved ``__distractors__`` key (the four
    decoy numbers, comma-joined) the scorer uses for its confusion note.
    """
    repeat = HAYSTACK_CHARS // len(HAYSTACK_FILLER) + 1
    base = (HAYSTACK_FILLER * repeat)[:HAYSTACK_CHARS]

    # 1-5: position-sweep needles, one per depth band.
    probes: list[tuple[int, str, str]] = []  # (pos, needle_text, label)
    expected: dict[str, str] = {}
    for label, lo, hi in ATTENTION_PROBE_BANDS:
        pos = int(len(base) * rng.uniform(lo, hi))
        token = _probe_token(rng)
        probes.append((pos, f"The recovery token for station {label} is {token}.", label))
        expected[label] = token

    # 6-7: NoLiMa-style associative pair, adjacent block in the mid band
    # region (0.40-0.60), re-rolled clear of the pos_mid needle.
    landmark, city = rng.choice(LANDMARK_CITIES)
    name = f"{rng.choice(PERSON_FIRST_NAMES)} {rng.choice(PERSON_LAST_NAMES)}"
    block_pos = int(len(base) * rng.uniform(0.40, 0.60))
    mid_pos = next(p for p, _n, lbl in probes if lbl == "pos_mid")
    if abs(block_pos - mid_pos) < 60:
        block_pos = int(len(base) * rng.uniform(0.40, 0.60))
    literal = f"{name} lives in {city}."
    infer = f"{name} visited the {landmark} last summer."
    probes.append((block_pos, literal, "assoc_literal"))
    probes.append((block_pos + 4, infer, "assoc_infer"))
    expected["assoc_literal"] = city
    expected["assoc_infer"] = city

    # 8: decoy resistance — 1 needle + 4 same-format distractors, scattered.
    tag = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
    number = f"{rng.randrange(0, 10000):04d}"
    probes.append((
        int(len(base) * rng.uniform(0.02, 0.98)),
        f"The archive box labeled {tag} has number {number}.",
        "decoy_resist",
    ))
    expected["decoy_resist"] = number
    distractors: list[str] = []
    for _ in range(4):
        d_tag = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        while d_tag == tag:
            d_tag = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        d_num = f"{rng.randrange(0, 10000):04d}"
        while d_num == number or d_num in distractors:
            d_num = f"{rng.randrange(0, 10000):04d}"
        distractors.append(d_num)
        probes.append((
            int(len(base) * rng.uniform(0.02, 0.98)),
            f"The archive box labeled {d_tag} has number {d_num}.",
            "decoy_resist_distractor",
        ))
    expected["__distractors__"] = ",".join(distractors)

    # Insert deepest-first so earlier offsets don't shift (stable sort keeps
    # equal-position inserts — the assoc block — in insertion order).
    text = base
    for pos, needle, _label in sorted(probes, key=lambda e: e[0], reverse=True):
        text = text[:pos] + "\n" + needle + "\n" + text[pos:]

    follow_up = (
        "This document contains the facts above. Answer each line exactly "
        "(one answer per line, lines 1) through 8)):\n"
        "1) the recovery token for station pos_head\n"
        "2) the recovery token for station pos_mid_early\n"
        "3) the recovery token for station pos_mid\n"
        "4) the recovery token for station pos_mid_late\n"
        "5) the recovery token for station pos_tail\n"
        f"6) which city does {name} live in?\n"
        f"7) which city has {name} visited?\n"
        f"8) what is the number of the archive box labeled {tag}?\n"
    )
    return text + "\n\n" + follow_up, expected


__all__ = [name for name in globals() if name.isupper()] + [
    "build_attention_probe",
    "build_haystack",
    "build_test_image_png",
]
