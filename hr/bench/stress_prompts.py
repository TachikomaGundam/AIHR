"""attention_stress prompt content — a 20-turn constraint-survival script (task).

The battery measures instruction decay over a long agentic-style conversation:
turn 1 sets 5 deterministic checkable constraints, turns 2-20 are bland canned
prompts, and checkpoints at turns 5/10/15/20 grade constraint survival.
Broken out of :mod:`hr.bench.prompts` to keep that module under its LOC
ceiling; the checker constants live here too so the instruction text and the
scorer can never drift apart (:mod:`hr.bench.scorers` imports them).
"""

from __future__ import annotations

import random

from hr.bench.prompts import _probe_token

# ---------------------------------------------------------------------------
# Checker-friendly constraint constants (shared with the scorer)
# ---------------------------------------------------------------------------

#: Every response must start with this exact tag.
STRESS_CONSTRAINT_PREFIX: str = "[ROGER]"
#: Every response must end with the run-generated token (this exact format).
STRESS_TOKEN_FORMAT: str = "XXXX-XXXX"
#: The banned word — never allowed in a response (negative constraints decay
#: first per research, so it is asserted case-insensitively).
STRESS_BANNED_WORD: str = "important"
#: Every response must contain exactly this many lines starting with the
#: bullet prefix.
STRESS_BULLET_PREFIX: str = "- "
STRESS_BULLET_LINES: int = 3
#: Every response must be at most this many words (whitespace split).
STRESS_MAX_WORDS: int = 50
#: Turns after which the LATEST response is checkpointed (turn 1 is the
#: instruction, turns 2-20 the canned prompts).
STRESS_CHECKPOINT_TURNS: tuple[int, ...] = (5, 10, 15, 20)

# ---------------------------------------------------------------------------
# Turn-1 instruction (deterministic text; only the token varies per run)
# ---------------------------------------------------------------------------


def build_stress_instruction(token: str) -> str:
    """The turn-1 message: five constraints, each deterministically checkable."""
    return (
        "We are starting a long working session and I will ask you many "
        "questions. You must obey these five rules in EVERY single response:\n"
        f"1. Begin every response with the exact tag {STRESS_CONSTRAINT_PREFIX}\n"
        f"2. End every response with the exact token {token}\n"
        f"3. Never write the word {STRESS_BANNED_WORD} (case-insensitive)\n"
        f"4. Every response must contain exactly {STRESS_BULLET_LINES} lines "
        f"starting with {STRESS_BULLET_PREFIX!r}\n"
        f"5. Every response must be at most {STRESS_MAX_WORDS} words\n"
        "Follow all five rules now with your first reply."
    )


# ---------------------------------------------------------------------------
# Turns 2-20: 19 canned bland topical prompts (deterministic; 3 bait turns
# that naturally invite the banned word without containing it)
# ---------------------------------------------------------------------------

STRESS_CANNED_TURNS: tuple[str, ...] = (
    "Summarize our project plan in one line.",
    "Suggest three names for a lighthouse.",
    "What color should we paint the office walls?",
    "Explain how a bicycle gear system works.",
    "Write a short slogan for a coffee brand.",
    "Tell me why hitting this deadline matters.",
    "List two things to bring on a hike.",
    "Describe today's weather in a haiku.",
    "What is the capital of Finland?",
    "Give one reason we should move to weekly releases.",
    "Name three vegetables that grow in spring.",
    "Summarize how batteries store energy.",
    "What tool should we use for diagramming?",
    "Explain the password reset flow in two lines.",
    "Why must we prioritize the bot crash fix?",
    "Write one random trivia fact.",
    "Suggest a name for the release train.",
    "What should the team automate next?",
    "Tell me a short joke about running.",
)


def make_stress_token(rng: random.Random) -> str:
    """'XXXX-XXXX' uppercase hex token (same rng style as attention_probe)."""
    return _probe_token(rng)


__all__ = [name for name in globals() if name.isupper()] + [
    "build_stress_instruction",
    "make_stress_token",
]