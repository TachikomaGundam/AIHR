
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


from hr.config import itemrepo_path
from hr.fleet import fleet_models
from hr.items.schema import ItemEnvelope

# Reuse stage0's helpers and DB plumbing.
from hr.stage0 import (
    STAGE0_SEAT_CODE,
)



# ---------------------------------------------------------------------------
# Stage 1 constants
# ---------------------------------------------------------------------------
#: The four deciding batteries for Stage 1 finals (spec §5.4 v0.2/§10.7).
STAGE1_DECIDING_BATTERIES: tuple[str, ...] = ("reasoning", "tool_a", "hallucination", "vision")

#: Stage 1 runs full banks, NOT Stage 0's reduced subsets.
STAGE1_FULL_BANK_SIZES: dict[str, int] = {
    "reasoning": 60,
    "hallucination": 70,
    "tool_a": 100,
    "vision": 22,
}

#: Take top-k finalists per deciding battery (spec §5.4: top 5–6).
STAGE1_FINALISTS_PER_BATTERY: int = 6

#: Sequential-n parameters.
STAGE1_N_INITIAL: int = 3  # pilot rounds
STAGE1_N_MAX: int = 10  # budget cap per battery

#: Stage 1 token cap (spec §9.1 v0.3).
STAGE1_TOKEN_CAP: int = 90_000_000
EST_TOKENS_PER_CALL: int = 5_000

#: Seat code for the finals sweep (separate from Stage 0's _stage0_sweep).
STAGE1_SEAT_CODE: str = "_stage1_finals"

DEFAULT_THRESHOLDS_PATH: Path = Path(__file__).resolve().parents[1] / "configs" / "thresholds.yaml"


# ---------------------------------------------------------------------------
# Finalist selection from Stage 0 DB
@dataclass
class FinalistSelection:
    """Rationale for which models were selected per battery + union of finalists."""

    per_battery: dict[str, list[tuple[str, float]]]
    # battery_code -> [(model_id, mean_score), ...] top-k, sorted desc
    finalists: list[str]
    # union of all per-battery top-k, sorted for determinism
    rationale: str
    # human-readable rationale text


def select_finalists_from_stage0(
    *,
    deciding_batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
    top_k: int = STAGE1_FINALISTS_PER_BATTERY,
    allow_db_missing: bool = False,
) -> FinalistSelection:
    """Query Stage 0 DB, rank models per battery by mean score, take top-k.

    Returns FinalistSelection with per-battery top-k + union of finalists.
    Falls back to all models if the DB is empty and allow_db_missing=True.
    Raises RuntimeError if DB is empty and allow_db_missing=False.
    """
    from hr.db import connect

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.model_id, b.battery_code, AVG(m.score) AS mean_score
          FROM hr.measurement m
          JOIN hr.run r ON m.run_id = r.run_id
          JOIN hr.sweep s ON r.sweep_id = s.sweep_id
          JOIN hr.battery b ON r.battery_id = b.battery_id
                WHERE s.seat_code = %s
                GROUP BY r.model_id, b.battery_code
                ORDER BY b.battery_code ASC, mean_score DESC, r.model_id ASC
                """,
                (STAGE0_SEAT_CODE,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        if allow_db_missing:
            # Degenerate fallback — useful for unit tests without a DB.
            return FinalistSelection(
                per_battery={},
                finalists=sorted(fleet_models()),
                rationale=(
                    "Stage 0 DB is empty; falling back to full pool. "
                    "This is a test-only path — real finals selection requires Stage 0 results."
                ),
            )
        raise RuntimeError(
            f"No Stage 0 measurements found in DB (seat_code={STAGE0_SEAT_CODE}). "
            "Run Stage 0 first before selecting finalists."
        )

    # Group by battery_code preserving rank order.
    per_battery_scores: dict[str, list[tuple[str, float]]] = {}
    for model_id, battery_code, mean_score in rows:
        per_battery_scores.setdefault(battery_code, []).append((model_id, float(mean_score)))

    # Take top-k per deciding battery.
    selection: dict[str, list[tuple[str, float]]] = {}
    finalists_set: set[str] = set()
    for battery in deciding_batteries:
        ranked = per_battery_scores.get(battery, [])
        top = ranked[:top_k]
        selection[battery] = top
        finalists_set.update(m for m, _ in top)

    finalists_list = sorted(finalists_set)
    rationale_parts = [f"Stage 1 finalist selection (top-{top_k} per deciding battery):"]
    for battery in deciding_batteries:
        top = selection[battery]
        rationale_parts.append(f"  {battery}: {[m for m, _ in top]}")
    rationale_parts.append(f"Union of finalists ({len(finalists_list)}): {finalists_list}")
    rationale = "\n".join(rationale_parts)

    return FinalistSelection(
        per_battery=selection,
        finalists=finalists_list,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Full-bank item loading
# ---------------------------------------------------------------------------
def load_full_banks(
    item_repo: Path | None = None,
    *,
    batteries: tuple[str, ...] = STAGE1_DECIDING_BATTERIES,
) -> dict[str, list[ItemEnvelope]]:
    """Load full item banks for the deciding batteries (no subsetting).

    Uses stage0's BATTERY_ITEM_TYPES to map batteries to ItemTypes, then
    walks the repo without invoking select_subsets. ``item_repo`` defaults
    to :func:`hr.config.itemrepo_path`.
    """
    from hr.calibrate import load_item_repo

    if item_repo is None:
        item_repo = itemrepo_path()
    bundles = load_item_repo(item_repo, batteries=list(batteries))
    # Filter to only the deciding batteries (load_item_repo returns all it was asked).
    return {b: bundles.get(b, []) for b in batteries}
