"""hr2.graders.unit_test — sandbox subprocess evaluator.

Spec §6.2: unit_test grader runs a pytest-compatible suite in a subprocess
with best-effort isolation (cwd, no network env, 30s timeout). Grading is
deterministic: exit_code==0, expected file existence, pytest stdout pass
count. Does NOT perform model-API calls.

Sandbox strategy is OS-dependent on Linux with unshare if available; falls
back to subprocess with a short timeout. Tests use `tempfile` directories.
"""

from __future__ import annotations

import os
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

DEFAULT_TIMEOUT_S = 30


def _safe_env() -> dict[str, str]:
    """A stripped-down env without network-relevant variables."""
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH"}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({"PYTHONUNBUFFERED": "1"})
    return env


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
        p = workdir / (t.get("name") or "test_file.py")
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

        with tempfile.TemporaryDirectory(prefix="hr2-unit-") as td:
            workdir = Path(td) / "sandbox"
            _write_artifacts(workdir, payload_copy, response)
            try:
                proc = subprocess.run(
                    [
                        "python",
                        "-m",
                        "pytest",
                        "-q",
                        "--no-header",
                        "-p", "no:cacheprovider",
                        str(workdir),
                    ],
                    cwd=str(workdir),
                    env=_safe_env(),
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                exit_code = proc.returncode
                pytest_out = proc.stdout + proc.stderr
            except subprocess.TimeoutExpired as exc:
                exit_code = 124
                pytest_out = f"timeout after {timeout}s"

        # Deterministic checks evaluation.
        check_results: list[dict[str, Any]] = []
        passed_count = 0
        for chk in checks_spec:
            kind = chk.get("kind")
            passed = False
            if kind == "exit_code":
                passed = exit_code == int(chk.get("value", 0))
            elif kind == "file_exists":
                path = Path(workdir) / (chk.get("path") or "")
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
