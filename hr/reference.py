"""Published-benchmark reference dataset for model capability scoring.

Curated scores, normalized 0-100 per capability category, sourced from
authoritative leaderboards (SWE-bench, FrontierSWE, Terminal-Bench, GPQA
Diamond, AIME, HMMT, MCP Mark, BFCL, throughput, context window). Seeds
into ``hr_reference`` idempotently and exposes a read helper for the
recommendation engine.

The curated data lives in ``configs/knowledge.yaml`` (``reference_scores``),
loaded through :func:`hr.config.load_yaml` — this module is logic only.
Models without an entry are skipped at seed time (safe default), so the
knowledge overlay never needs to chase the fleet.
"""
from __future__ import annotations

from hr.config import load_yaml

from hr.database import get_connection, upsert_model
from hr.models import ModelProfile
from hr.registry import discover_models


def load_reference_scores() -> dict[str, dict[str, tuple[float, float, str]]]:
    """Curated reference scores from ``configs/knowledge.yaml``.

    Shape: ``model_id -> category -> (score, confidence, source)``, keyed by
    bare model slug. Returns ``{}`` when the file is absent (safe default);
    a malformed entry raises ValueError naming the file.
    """
    try:
        data = load_yaml("knowledge.yaml")
    except FileNotFoundError:
        return {}
    raw_scores = data.get("reference_scores") or {}
    if not isinstance(raw_scores, dict):
        raise ValueError(
            "invalid configs/knowledge.yaml: 'reference_scores' is not an object"
        )
    scores: dict[str, dict[str, tuple[float, float, str]]] = {}
    for model_id, categories in raw_scores.items():
        if not isinstance(categories, dict):
            continue
        scores[str(model_id)] = {
            str(cat): (float(score), float(confidence), str(source))
            for cat, (score, confidence, source) in categories.items()
        }
    return scores

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hr_reference (
    id SERIAL PRIMARY KEY,
    model_fk INT NOT NULL REFERENCES hr_models(id) ON DELETE CASCADE,
    category VARCHAR(50) NOT NULL,
    score FLOAT NOT NULL,
    confidence FLOAT,
    source TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    UNIQUE (model_fk, category)
);
"""


def init_reference_table() -> None:
    """Create the ``hr_reference`` table if it does not exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def seed_reference() -> dict[str, int]:
    """Seed or refresh ``hr_reference`` from the curated knowledge store.

    Only models that are both present in ``configs/knowledge.yaml`` and
    discoverable via :func:`hr.registry.discover_models` are seeded. Returns
    a count dict ``{"models": N, "upserted": M}`` where ``upserted`` is the
    number of (model, category) rows written.

    Idempotent: rows are upserted on the ``(model_fk, category)`` unique
    constraint so a second run writes identical values without changing state.
    """
    discovered: dict[str, ModelProfile] = {p.model_id: p for p in discover_models()}
    model_id_to_fk: dict[str, int] = {}
    for model_id, profile in discovered.items():
        model_id_to_fk[model_id] = upsert_model(profile)

    upserted = 0
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            for model_id, categories in load_reference_scores().items():
                if model_id not in model_id_to_fk:
                    continue
                fk = model_id_to_fk[model_id]
                for category, (score, confidence, source) in categories.items():
                    cur.execute(
                        """
                        INSERT INTO hr_reference
                            (model_fk, category, score, confidence, source)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (model_fk, category) DO UPDATE SET
                            score = EXCLUDED.score,
                            confidence = EXCLUDED.confidence,
                            source = EXCLUDED.source
                        RETURNING (xmax = 0) AS inserted
                        """,
                        (fk, category, score, confidence, source),
                    )
                    if cur.fetchone()[0]:
                        upserted += 1
        conn.commit()
    finally:
        conn.close()

    return {"models": len(model_id_to_fk), "upserted": upserted}


def get_reference_scores(model_id: str) -> dict[str, tuple[float, float]]:
    """Return per-category ``(score, confidence)`` pairs for a model.

    Looks up by ``model_id`` across all providers. Returns ``{}`` if the
    model or any of its reference rows are not present.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.category, r.score, r.confidence
                FROM hr_reference r
                JOIN hr_models m ON m.id = r.model_fk
                WHERE m.model_id = %s
                """,
                (model_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return {category: (score, confidence) for category, score, confidence in rows}


if __name__ == "__main__":
    init_reference_table()
    counts = seed_reference()
    print("seed counts:", counts)
    scores = load_reference_scores()
    if scores:
        sample = next(iter(scores))
        print(f"{sample} reference scores:", get_reference_scores(sample))
