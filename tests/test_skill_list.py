"""``hr skill list`` contract tests — local facts always listed; the engine
column is honest (advertised / NOT advertised / unreachable), never silent.

Also unit-tests the engine probe itself against the three measured v2.2.7
quirks (HANDOFF §3): outer key ``data``, URL-encoded ``location[directory]``
parameter, and the cold-start empty-first-answer retry.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from hr.cli import app
from hr.cli_skill import engine_skills
from tests._skill_helpers import (
    engine_rows,
    engine_unreachable,
    make_skill,
    runner,
    seal_skill_roots,
)


def _completed(payload: object, rc: int = 0) -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout=json.dumps(payload), stderr="")


def test_lists_local_facts_and_engine_advertisement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "shown")
    bad = authority / "quiet"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
    engine_rows(monkeypatch, [
        {"id": "shown", "description": "advertised one"},
        {"id": "quiet", "description": ""},
    ])
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0
    assert "shown" in result.output and "advertised" in result.output
    assert "NOT advertised (no description)" in result.output
    assert "quiet" in result.output and "INVALID" in result.output


def test_engine_unreachable_still_lists_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "solo")
    engine_unreachable(monkeypatch)
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0
    assert "UNREACHABLE" in result.output
    assert "solo" in result.output  # local facts survive an engine outage


def test_json_surface_is_strictly_parseable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "json-skill")
    engine_rows(monkeypatch, [{"id": "json-skill", "description": "d"}])
    result = runner.invoke(app, ["skill", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["engine"]["reachable"] is True
    assert payload["skills"][0]["id"] == "json-skill"
    assert payload["skills"][0]["valid"] is True
    assert payload["skills"][0]["advertised"] is True


def test_json_surface_unreachable_engine_advertised_is_null(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority, _ = seal_skill_roots(monkeypatch, tmp_path)
    make_skill(authority, "offline-skill")
    engine_unreachable(monkeypatch)
    result = runner.invoke(app, ["skill", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["engine"]["reachable"] is False
    assert payload["skills"][0]["advertised"] is None


def test_list_on_missing_root_exits_zero_with_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HR_SKILL_SOURCE", str(tmp_path / "nowhere"))
    engine_rows(monkeypatch, [])
    result = runner.invoke(app, ["skill", "list"])
    assert result.exit_code == 0
    assert "does not exist" in result.output


# --- engine_skills probe: the three measured quirks -------------------------


def test_engine_skills_uses_encoded_url_and_data_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._skill_helpers import fake_subprocess

    target = tmp_path / "my skills"  # a space must be percent-encoded
    calls = fake_subprocess(monkeypatch, [
        _completed({"data": [{"id": "s1", "description": "d"}]}),
    ])
    rows = engine_skills(target)
    assert rows == [{"id": "s1", "description": "d"}]
    assert calls[0][:3] == ["opencode", "api", "get"]
    assert calls[0][3] == (
        f"/api/skill?location%5Bdirectory%5D={quote(str(target), safe='/')}"
    )
    assert "%20" in calls[0][3]


def test_engine_skills_retries_cold_start_empty_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First query on a never-opened directory legitimately returns [];
    counting that cold miss as absence is the misread the retry prevents."""
    from tests._skill_helpers import fake_subprocess

    calls = fake_subprocess(monkeypatch, [
        _completed({"data": []}),
        _completed({"data": [{"id": "s1", "description": "d"}]}),
    ])
    rows = engine_skills(tmp_path)
    assert [r["id"] for r in rows] == ["s1"]
    assert len(calls) == 2


def test_engine_skills_empty_after_all_attempts_is_a_legitimate_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._skill_helpers import fake_subprocess

    calls = fake_subprocess(monkeypatch, [_completed({"data": []})])
    assert engine_skills(tmp_path) == []
    assert len(calls) == 3  # warm-up budget fully spent, then honestly empty


def test_engine_skills_nonzero_exit_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._skill_helpers import fake_subprocess

    fake_subprocess(monkeypatch, [SimpleNamespace(returncode=7, stdout="", stderr="boom")])
    with pytest.raises(RuntimeError, match="exited 7"):
        engine_skills(tmp_path)


def test_engine_skills_unparseable_json_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._skill_helpers import fake_subprocess

    fake_subprocess(monkeypatch, [SimpleNamespace(returncode=0, stdout="<html>", stderr="")])
    with pytest.raises(RuntimeError, match="unparseable"):
        engine_skills(tmp_path)


def test_engine_skills_missing_binary_raises_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests._skill_helpers import fake_subprocess

    fake_subprocess(monkeypatch, [FileNotFoundError("opencode")])
    with pytest.raises(RuntimeError, match="not found on PATH"):
        engine_skills(tmp_path)
