"""Research knowledge — qualitative findings and claims the reference store does NOT carry.

The curated per-capability scores live in ``configs/knowledge.yaml``
(``reference_scores``) and are loaded through :func:`hr.config.load_yaml` --
this module is logic only. The qualitative layer here (strength/weakness/
pricing/community findings plus benchmark claims with no matching reference
entry) lives in the same file under ``findings``.
"""
from __future__ import annotations

from hr.config import load_yaml
from hr.database import get_connection, save_research, upsert_model
from hr.models import ResearchFinding
from hr.registry import discover_models

# Each entry: (category, finding, confidence, source_url)
_F = tuple[str, str, float | None, str]


def load_findings() -> dict[str, list[_F]]:
    """Curated research findings from ``configs/knowledge.yaml``.

    Shape: ``model_id -> list of (category, finding, confidence, source_url)``,
    keyed by bare model slug. Returns ``{}`` when the file is absent (safe
    default); a malformed entry raises ValueError naming the file.
    """
    try:
        data = load_yaml("knowledge.yaml")
    except FileNotFoundError:
        return {}
    raw = data.get("findings") or {}
    if not isinstance(raw, dict):
        raise ValueError(
            "invalid configs/knowledge.yaml: 'findings' is not an object"
        )
    findings: dict[str, list[_F]] = {}
    for model_id, entries in raw.items():
        if not isinstance(entries, list):
            continue
        rows: list[_F] = []
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) != 4:
                continue
            category, finding, confidence, source_url = entry
            rows.append(
                (str(category), str(finding),
                 float(confidence) if confidence is not None else None,
                 str(source_url))
            )
        findings[str(model_id)] = rows
    return findings



def _load_existing_keys(conn) -> set[tuple[int, str, str]]:
    """Load all (model_fk, source_url, finding) triples already in the DB."""
    with conn.cursor() as cur:
        cur.execute("SELECT model_fk, COALESCE(source_url, ''), finding FROM hr_research")
        return {(r[0], r[1], r[2]) for r in cur.fetchall()}


def seed_research() -> dict[str, int]:
    """Seed verified research findings. Idempotent — skips already-inserted rows.

    Returns a dict with counts of models upserted, findings inserted, and skipped.
    """
    profiles = discover_models()
    model_fks: dict[str, int] = {}
    for p in profiles:
        model_fks[p.model_id] = upsert_model(p)

    conn = get_connection()
    try:
        existing = _load_existing_keys(conn)
    finally:
        conn.close()

    inserted = skipped = 0
    for model_id, entries in load_findings().items():
        fk = model_fks.get(model_id)
        if fk is None:
            continue
        for cat, finding, conf, src_url in entries:
            key = (fk, src_url, finding)
            if key in existing:
                skipped += 1
                continue
            save_research(ResearchFinding(
                model_fk=fk,
                finding=finding,
                category=cat,
                confidence=conf,
                source_url=src_url,
            ))
            existing.add(key)
            inserted += 1

    return {"models": len(model_fks), "inserted": inserted, "skipped": skipped}


if __name__ == "__main__":
    from hr.database import get_connection

    stats = seed_research()
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM hr_research")
            total_research = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM hr_models")
            total_models = cur.fetchone()[0]
    finally:
        conn.close()

    print(f"Seed complete: {stats}")
    print(f"Models in DB:  {total_models}")
    print(f"Research rows: {total_research}")