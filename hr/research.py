"""Qualitative model findings loaded from the knowledge store."""

from __future__ import annotations

from hr.config import load_yaml


ResearchFinding = tuple[str, str, float | None, str]


def load_findings() -> dict[str, list[ResearchFinding]]:
    """Return model findings as category, text, confidence, and source tuples."""
    try:
        data = load_yaml("knowledge.yaml")
    except FileNotFoundError:
        return {}
    raw = data.get("findings") or {}
    if not isinstance(raw, dict):
        raise ValueError("invalid configs/knowledge.yaml: 'findings' is not an object")
    findings: dict[str, list[ResearchFinding]] = {}
    for model_id, entries in raw.items():
        if not isinstance(entries, list):
            continue
        rows: list[ResearchFinding] = []
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) != 4:
                continue
            category, finding, confidence, source_url = entry
            rows.append(
                (
                    str(category),
                    str(finding),
                    float(confidence) if confidence is not None else None,
                    str(source_url),
                )
            )
        findings[str(model_id)] = rows
    return findings
