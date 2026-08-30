from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hr.graders.base import GraderError, ModelResponse
from hr.graders.unit_test import UnitTestGrader


def test_checks_files_before_workspace_cleanup(monkeypatch) -> None:
    # Given: sandboxed tests create an artifact required by the grading spec.
    def run_sandboxed(
        workdir: Path, _args: list[str], _timeout: int
    ) -> subprocess.CompletedProcess[str]:
        (workdir / "result.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "1 passed", "")

    monkeypatch.setattr("hr.graders.unit_test.run_sandboxed", run_sandboxed)
    grader = UnitTestGrader()

    # When: the grader evaluates the file_exists check.
    result = grader.grade(
        {"expected": "answer"},
        {"checks": [{"kind": "file_exists", "path": "result.json"}]},
        ModelResponse(text="answer"),
    )

    # Then: the live sandbox artifact is observable before cleanup.
    assert result.passed is True


def test_rejects_artifact_paths_outside_workspace() -> None:
    # Given: an item-authored test file attempts to escape the sandbox workspace.
    grader = UnitTestGrader()

    # When/Then: materialization rejects traversal before writing host files.
    with pytest.raises(GraderError, match="test file path"):
        grader.grade(
            {"test_files": [{"name": "../escaped.py", "content": ""}]},
            {},
            ModelResponse(text="answer"),
        )


def test_rejects_file_check_paths_outside_workspace(monkeypatch) -> None:
    # Given: sandbox execution succeeds before an item-authored file check.
    monkeypatch.setattr(
        "hr.graders.unit_test.run_sandboxed",
        lambda *_args: subprocess.CompletedProcess([], 0, "1 passed", ""),
    )
    grader = UnitTestGrader()

    # When/Then: the check cannot probe files outside the workspace.
    with pytest.raises(GraderError, match="check file path"):
        grader.grade(
            {},
            {"checks": [{"kind": "file_exists", "path": "../outside"}]},
            ModelResponse(text="answer"),
        )
