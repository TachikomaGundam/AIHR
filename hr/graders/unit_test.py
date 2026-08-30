"""Run item-authored tests in a network- and filesystem-isolated sandbox."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from hr.graders.base import (
    GradeResult,
    GraderError,
    ModelResponse,
)
from hr.sandbox import SandboxUnavailableError, run_sandboxed

DEFAULT_TIMEOUT_S = 30


def _workspace_path(workdir: Path, value: Any, label: str) -> Path:
    relative_path = Path(str(value or ""))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise GraderError(f"invalid {label} path: {relative_path}")
    return workdir / relative_path


def _write_artifacts(
    workdir: Path, payload: dict[str, Any], response: ModelResponse
) -> None:
    """Materialize a pytest-friendly test tree:
      - test_response.py (or user-supplied test file content under `tests`)
      - response.txt (the model's text)
    """
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "response.txt").write_text(
        response.text or "", encoding="utf-8"
    )
    tests = payload.get("test_files") or []
    if not tests:
        # Default: a single test_response.py that checks existence + content.
        answer = payload.get("expected") or ""
        src = textwrap.dedent(
            f"""
            from pathlib import Path
            def test_response_file_exists():
                assert Path("response.txt").is_file()
            def test_response_contains_answer():
                txt = Path("response.txt").read_text(encoding="utf-8")
                assert {answer!r} in txt
            """
        ).strip()
        tests = [{"name": "test_response.py", "content": src}]
    for t in tests:
        p = _workspace_path(workdir, t.get("name") or "test_file.py", "test file")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(t.get("content") or "", encoding="utf-8")


class UnitTestGrader:
    """Spec §6.2: subprocess sandbox + pytest-style checks."""

    name = "unit_test"
    version = "1.0"

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        timeout = int(
            grading_params.get("timeout", DEFAULT_TIMEOUT_S)
        )
        checks_spec = list(grading_params.get("checks", []))
        test_files = item_payload.get("test_files")
        payload_copy: dict[str, Any] = dict(item_payload)
        if test_files is not None:
            payload_copy["test_files"] = test_files

        with tempfile.TemporaryDirectory(prefix="hr-unit-") as td:
            workdir = Path(td) / "sandbox"
            _write_artifacts(workdir, payload_copy, response)
            try:
                proc = run_sandboxed(
                    workdir,
                    [
                        "-m",
                        "pytest",
                        "-q",
                        "--no-header",
                        "-p", "no:cacheprovider",
                        "/work",
                    ],
                    timeout,
                )
                exit_code = proc.returncode
                pytest_out = proc.stdout + proc.stderr
            except subprocess.TimeoutExpired:
                exit_code = 124
                pytest_out = f"timeout after {timeout}s"
            except SandboxUnavailableError as exc:
                exit_code = 126
                pytest_out = str(exc)

            check_results: list[dict[str, Any]] = []
            passed_count = 0
            for chk in checks_spec:
                kind = chk.get("kind")
                passed = False
                if kind == "exit_code":
                    passed = exit_code == int(chk.get("value", 0))
                elif kind == "file_exists":
                    path = _workspace_path(workdir, chk.get("path"), "check file")
                    passed = path.exists()
                elif kind == "pytest_pass":
                    passed = ("passed" in pytest_out) and (exit_code == 0)
                elif kind == "stdout_contains":
                    passed = (chk.get("value") or "") in pytest_out
                else:
                    raise GraderError(f"unknown check kind: {kind}")
                check_results.append({
                    "kind": kind,
                    "expected": chk.get("value"),
                    "passed": passed,
                })
                if passed:
                    passed_count += 1

        total = max(len(check_results), 1)
        score = passed_count / total
        return GradeResult(
            score=score,
            passed=score == 1.0,
            detail={
                "exit_code": exit_code,
                "pytest_stdout": pytest_out[:2000],
                "checks": check_results,
            },
        )
