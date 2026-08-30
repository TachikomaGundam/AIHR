"""Research knowledge-surface contract tests (committed shape).

Covers hr.research: findings loading from configs/knowledge.yaml
(including fail-loud and skip shapes) and the idempotent seeding flow
against fakes. Offline and deterministic.
"""

from __future__ import annotations

import pytest

import hr.research as res


class _FakeCursor:
    def __init__(self, rows=None):  # noqa: ANN001
        self._rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql, params=None) -> None:  # noqa: ARG002
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows=None):  # noqa: ANN001
        self._rows = rows or []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self) -> None:
        self.closed = True


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


def test_seed_research_inserts_and_skips(monkeypatch) -> None:
    from types import SimpleNamespace

    saved: list[tuple] = []
    monkeypatch.setattr(res, "discover_models", lambda: [SimpleNamespace(model_id="m1")])
    monkeypatch.setattr(res, "upsert_model", lambda p: 7)
    conn = _FakeConn()
    monkeypatch.setattr(res, "get_connection", lambda: conn)
    monkeypatch.setattr(res, "save_research", lambda finding: saved.append(finding))
    monkeypatch.setattr(
        res,
        "load_findings",
        lambda: {"m1": [("reasoning", "finding-a", None, "u1")]},
    )
    stats = res.seed_research()
    assert conn.closed
    assert stats == {"models": 1, "inserted": 1, "skipped": 0}
    (finding,) = saved
    assert finding.model_fk == 7
    assert finding.source_url == "u1"

    conn2 = _FakeConn([(7, "u1", "finding-a")])
    monkeypatch.setattr(res, "get_connection", lambda: conn2)
    stats2 = res.seed_research()
    assert stats2 == {"models": 1, "inserted": 0, "skipped": 1}


def test_seed_research_unknown_model_skipped(monkeypatch) -> None:
    monkeypatch.setattr(res, "discover_models", lambda: [])
    monkeypatch.setattr(res, "get_connection", lambda: _FakeConn())
    monkeypatch.setattr(
        res, "load_findings", lambda: {"ghost": [("a", "b", None, "u")]}
    )
    assert res.seed_research() == {"models": 0, "inserted": 0, "skipped": 0}