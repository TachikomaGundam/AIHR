"""Research knowledge-surface contract tests (committed shape).

Covers hr.research: findings loading from configs/knowledge.yaml
(including fail-loud and skip shapes). The legacy DB-seeding flow
(seed_research / discover_models / upsert_model / save_research) was
superseded by the unified file-based knowledge store and removed with the
hr-ship W1 commit; its surface tests were superseded with it. Offline and
deterministic.
"""

from __future__ import annotations

import pytest

import hr.research as res


def test_load_findings_missing_file_is_empty(monkeypatch) -> None:
    def _missing(_name: str):
        raise FileNotFoundError("no knowledge.yaml")

    monkeypatch.setattr(res, "load_yaml", _missing)
    assert res.load_findings() == {}


def test_load_findings_not_an_object_raises(monkeypatch) -> None:
    monkeypatch.setattr(res, "load_yaml", lambda _name: {"findings": "oops"})
    with pytest.raises(ValueError, match="'findings' is not an object"):
        res.load_findings()


def test_load_findings_skips_malformed_and_types_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        res,
        "load_yaml",
        lambda _name: {
            "findings": {
                "m1": [("reasoning", "strong coder", 0.8, "url")],
                "m2": "not-a-list",
                "m3": [("a", "b", 0.5)],  # wrong length
                "m4": [("a", "b", None, "u"), ("c", "d", 0.9, "v")],
            }
        },
    )
    findings = res.load_findings()
    assert findings["m1"] == [("reasoning", "strong coder", 0.8, "url")]
    assert "m2" not in findings
    assert findings["m3"] == []
    assert findings["m4"][0][2] is None
    assert findings["m4"][1][2] == 0.9


def test_load_findings_empty_data(monkeypatch) -> None:
    monkeypatch.setattr(res, "load_yaml", lambda _name: {})
    assert res.load_findings() == {}