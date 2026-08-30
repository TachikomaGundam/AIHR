"""Published benchmark reference scores loaded from the knowledge store."""

from __future__ import annotations

from hr.config import load_yaml


ReferenceScore = tuple[float, float, str]


def load_reference_scores() -> dict[str, dict[str, ReferenceScore]]:
    """Return model and capability reference scores from knowledge.yaml."""
    try:
        data = load_yaml("knowledge.yaml")
    except FileNotFoundError:
        return {}
    raw_scores = data.get("reference_scores") or {}
    if not isinstance(raw_scores, dict):
        raise ValueError(
            "invalid configs/knowledge.yaml: 'reference_scores' is not an object"
        )
    scores: dict[str, dict[str, ReferenceScore]] = {}
    for model_id, categories in raw_scores.items():
        if not isinstance(categories, dict):
            continue
        scores[str(model_id)] = {
            str(category): (float(score), float(confidence), str(source))
            for category, (score, confidence, source) in categories.items()
        }
    return scores


def get_reference_scores(model_id: str) -> dict[str, tuple[float, float]]:
    """Return score and confidence pairs for a model from the source store."""
    categories = load_reference_scores().get(model_id, {})
    return {
        category: (score, confidence)
        for category, (score, confidence, _source) in categories.items()
    }
