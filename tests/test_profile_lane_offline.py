"""0.4.7 offline lane: honest rendering, empty-final guard, --quick selection."""
from __future__ import annotations

from types import SimpleNamespace

from hr.bench.engine_interactive import EngineInteractiveMixin
from hr.bench.engine_results import BenchOutcome, ItemResult
from hr.cli_inventory import bench_row_markdown
from hr.graders.base import ModelResponse


def _outcome(**kw):
    base = dict(battery=None, model_id="m/x", score=0.0, passed=False)
    base.update(kw)
    return BenchOutcome(**base)


def test_not_applicable_renders_skip_not_zero_fail() -> None:
    row = bench_row_markdown(
        "m/x", "livebench_code_gen",
        _outcome(status="not_applicable", raw_output="SKIP: sandbox unavailable (frozen-binary)"),
    )
    assert "SKIP(sandbox unavailable (frozen-binary))" in row
    assert "FAIL" not in row and "0.0" not in row


def test_inconclusive_renders_loudly() -> None:
    row = bench_row_markdown(
        "m/x", "livebench_tool_use",
        _outcome(status="inconclusive", raw_output="INCONCLUSIVE: empty final answer"),
    )
    assert "INCONCLUSIVE(empty final answer)" in row and "FAIL" not in row


def test_scored_rows_keep_pass_fail_contract() -> None:
    items = [ItemResult(item_id="i1", label="i1", passed=True, score=100.0)]
    ok = bench_row_markdown("m/x", "livebench_speed", _outcome(score=100.0, passed=True, items=items))
    bad = bench_row_markdown("m/x", "livebench_speed", _outcome(items=[]))
    assert "| PASS |" in ok and "FAIL" not in ok
    assert "| FAIL |" in bad


class _LoopEng(EngineInteractiveMixin):
    _timeout_s = 5

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._rs = list(responses)

    def _chat(self, model_id, adapter, caps, cr):  # type: ignore[override]
        return self._rs.pop(0)


def _resp(**kw):
    base = dict(text="", thinking="", tool_calls=[], raw={}, latency_ms=1000,
                tokens_in=100, tokens_out=0)
    base.update(kw)
    return ModelResponse(**base)


def test_empty_final_answer_after_generated_tokens_is_inconclusive() -> None:
    """191 sweep 790a73: model generated (tool calls) but no final text was
    recorded — must NOT be stamped scored-0."""
    eng = _LoopEng([
        _resp(tool_calls=[{"id": "t1", "name": "calculate",
                           "input": {"expression": "52.50+49.98"}}], tokens_out=120),
        _resp(tokens_out=30),  # empty content -> loop breaks with tokens spent
    ])
    res = eng._run_tool_use("m/x", None, SimpleNamespace(supports_thinking=True))
    assert res.outcome.status == "inconclusive"
    assert "empty final answer" in res.outcome.raw_output
    assert res.tokens_out == 150  # accounting preserved for the ops view


from typer.testing import CliRunner

from hr.cli import app

_runner = CliRunner()


def test_quick_flag_selects_five_lane_batteries(monkeypatch) -> None:
    result = _runner.invoke(
        app, ["bench", "--models", "fake/x", "--quick", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "tool_use" in result.output and "reasoning" in result.output
    assert "attention_stress" not in result.output  # full-lane batteries excluded
    assert "long_horizon" not in result.output


def test_explicit_battery_beats_quick(monkeypatch) -> None:
    result = _runner.invoke(
        app, ["bench", "--models", "fake/x", "--battery", "speed", "--quick", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "speed" in result.output and "reasoning" not in result.output


def test_starved_knobs_visible_and_mapping_on_livebench_plane() -> None:
    from hr.decision import _KNOB_TO_BATTERY, starved_knobs

    assert _KNOB_TO_BATTERY["reasoning"] == "livebench_reasoning"
    assert _KNOB_TO_BATTERY["top_tool_fraction"] == "livebench_tool_use"
    livebench = set(_KNOB_TO_BATTERY.values())
    assert starved_knobs(livebench) == []  # full plane: zero starvation
    starved = starved_knobs({"livebench_reasoning", "livebench_speed"})
    pairs = dict(starved)
    assert "top_tool_fraction" in pairs and "coverage" in pairs  # reported, not silent
    assert "reasoning" not in pairs  # fed battery never listed


def test_bundled_python_home_env_override(tmp_path, monkeypatch) -> None:
    from hr.sandbox import bundled_python_home

    monkeypatch.delenv("AIHR_PY_HOME", raising=False)
    assert bundled_python_home() is None  # dev install: host lane
    monkeypatch.setenv("AIHR_PY_HOME", str(tmp_path / "missing"))
    assert bundled_python_home() is None
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_text("")
    monkeypatch.setenv("AIHR_PY_HOME", str(tmp_path))
    assert bundled_python_home() == tmp_path


def test_frozen_still_na_without_py(tmp_path, monkeypatch) -> None:
    from hr import sandbox as sb

    monkeypatch.setattr(sb.sys, "frozen", True, raising=False)
    monkeypatch.setattr(sb.shutil, "which", lambda _n: "/usr/bin/bwrap")
    monkeypatch.delenv("AIHR_PY_HOME", raising=False)
    reason = sb.sandbox_available()
    assert reason is not None and "frozen-binary" in reason
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python3").write_text("")
    monkeypatch.setenv("AIHR_PY_HOME", str(tmp_path))
    assert sb.sandbox_available() is None  # vendored py revives the lane


def test_run_sandboxed_mounts_bundled_home_and_hides_host_deps(tmp_path, monkeypatch) -> None:
    from hr import sandbox as sb

    py = tmp_path / "py"
    (py / "bin").mkdir(parents=True)
    (py / "bin" / "python3").write_text("")
    monkeypatch.setenv("AIHR_PY_HOME", str(py))
    monkeypatch.setattr(sb.shutil, "which", lambda _n: "/usr/bin/bwrap")
    captured: list[list[str]] = []

    def fake_run(command, **_kw):
        captured.append(list(command))
        import subprocess as _sp
        return _sp.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    sb.run_sandboxed(tmp_path / "work", ["t.py"], 5)
    cmd = captured[0]
    assert str(py.resolve()) in cmd  # bundled prefix mounted as /runtime
    assert cmd[-2:] == ["/runtime/bin/python3", "t.py"]  # PBS stays one intact home
    joined = " ".join(cmd)
    assert "PYTHONPATH" not in joined  # clean room: no host deps leak in
    assert "/python-bin" not in joined  # no split-bin mount on the bundled lane


def test_normalize_py_prunes_and_preserves(tmp_path) -> None:
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "scripts"))
    from bundle_core import normalize_py

    home = tmp_path / "py-src"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "python3").write_text("#!")
    (home / "include").mkdir()
    (home / "include" / "Python.h").write_text("x")
    lib = home / "lib" / "python3.12"
    (lib / "test").mkdir(parents=True)
    (lib / "test" / "__init__.py").write_text("")
    (lib / "os.py").write_text("")
    (lib / "tkinter").mkdir()
    (lib / "tkinter" / "__init__.py").write_text("")
    sp = lib / "site-packages"
    sp.mkdir()
    (sp / "pip").mkdir()
    (sp / "pip" / "__init__.py").write_text("")
    (sp / "uselibs.pth").write_text("")

    dest = tmp_path / "py"
    normalize_py(home, dest)
    assert (dest / "bin" / "python3").exists()
    assert (dest / "lib" / "python3.12" / "os.py").exists()
    assert not (dest / "include").exists()
    assert not (dest / "lib" / "python3.12" / "test").exists()
    assert not (dest / "lib" / "python3.12" / "tkinter").exists()
    assert not (dest / "lib" / "python3.12" / "site-packages" / "pip").exists()
    assert (dest / "lib" / "python3.12" / "site-packages" / "uselibs.pth").exists()


def test_bundle_member_contract_py_lane() -> None:
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parents[1] / "scripts"))
    from bundle_core import check_bundle_members

    base = ["app/hr", "pg/bin/postgres", "pg/lib/libxml2.so.2",
            "pg/lib/liblzma.so.5", "pg/share/x", "share/aihr/hr.toml.example",
            "share/aihr/configs/models.yaml", "data/.keep", "bin/hr"]
    posix_members = base + ["app/_internal/base", "py/bin/python3",
                            "py/lib/python3.12/os.py"]
    assert not any("py/" in x for x in check_bundle_members(posix_members, "linux"))
    win = [m for m in base if m != "bin/hr"] + ["app/hr.exe", "bin/hr.exe", "py/bin/python3"]
    problems = check_bundle_members(win, "windows")
    assert any("must not ship py/" in x for x in problems)
    stripped = [m for m in posix_members if not m.startswith("py/")]
    assert any("py/bin/python3" in x for x in check_bundle_members(stripped + ["py/bin/idle3.12"], "linux"))
