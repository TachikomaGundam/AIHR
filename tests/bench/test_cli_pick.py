"""CLI tests for ``hr bench --pick`` interactive selection (task 22).

Selection source is the same static enumeration ``hr discover`` uses
(hr/discover.py, in-scope filter) — reused, never duplicated. All selection
tests run in --dry-run mode so the engine/DB are never touched; one
credential-gated e2e proves the picked models feed the real bench run set.

Scripted stdin goes through CliRunner(input=...): "1,3" picks the 1st and
3rd entries, "99" errors and re-prompts, invalid formats re-prompt, EOF is
a clean error (never an infinite loop).
"""

from __future__ import annotations

import json
import textwrap

import pytest
from typer.testing import CliRunner

import hr.bench.engine as engine_mod
from hr.cli import _selection_indices, app
from hr.discover import enumerate_models, scope_providers
from tests.bench.fake_adapter import FakeAdapter
from tests.conftest import materialize_templates

runner = CliRunner()

_GLOBAL_JSONC = """\
{
  "provider": {
    "bailian-token-plan": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "https://token-plan.example/v1" },
      "models": {
        "qwen3.7-max": { "name": "Qwen 3.7 Max" },
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" }
      }
    },
    "kimi-for-coding": {
      "npm": "@ai-sdk/anthropic",
      "options": { "baseURL": "https://api.kimi.com/coding/v1" },
      "models": { "kimi-k2.7-code": { "name": "Kimi K2.7 Code" } }
    }
  }
}
"""


@pytest.fixture
def pick_env(hr_sandbox: dict):
    """Global opencode.jsonc (2 providers/3 models, all in default scope);
    empty project cwd; no auth files (auth markers irrelevant to picking)."""
    materialize_templates(hr_sandbox)  # engine e2e needs tracked configs (thresholds.yaml)
    (hr_sandbox["config_dir"] / "opencode.jsonc").write_text(
        textwrap.dedent(_GLOBAL_JSONC), encoding="utf-8"
    )
    return hr_sandbox["project"]


def _ordered_discover_ids() -> list[str]:
    """Full ids of the in-scope discover list, in menu order."""
    discovered = [m for m in enumerate_models(scope_providers()) if m.in_scope]
    return [f"{m.provider}/{m.model_id}" for m in discovered]


# ---------------------------------------------------------------------------
# selection semantics (dry-run): menu order == discover order
# ---------------------------------------------------------------------------


def test_pick_comma_selection_takes_exactly_first_and_third(pick_env):
    ids = _ordered_discover_ids()
    assert len(ids) == 3
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="1,3\n")
    assert result.exit_code == 0, result.output
    assert f"  1. {ids[0]}" in result.output
    assert f"  3. {ids[2]}" in result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[0]}" in sel
    assert f"  • {ids[2]}" in sel
    assert f"  • {ids[1]}" not in sel


def test_pick_range_selection(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="2-3\n")
    assert result.exit_code == 0, result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[1]}" in sel
    assert f"  • {ids[2]}" in sel
    assert f"  • {ids[0]}" not in sel


def test_pick_dedupes_repeated_indices(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="1,1,3\n")
    assert result.exit_code == 0, result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert sel.count("  • ") == 2
    assert f"  • {ids[0]}" in sel
    assert f"  • {ids[2]}" in sel


# ---------------------------------------------------------------------------
# failure handling: error message + re-prompt, never a crash
# ---------------------------------------------------------------------------


def test_pick_out_of_range_errors_and_reprompts(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="99\n2\n")
    assert result.exit_code == 0, result.output
    assert "out of range 1..3" in result.output
    assert "99" in result.output
    # the 2nd prompt's reply wins
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[1]}" in sel
    assert f"  • {ids[0]}" not in sel


def test_pick_non_numeric_errors_and_reprompts(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="abc\n1\n")
    assert result.exit_code == 0, result.output
    assert "not a number" in result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[0]}" in sel


def test_pick_descending_range_errors_and_reprompts(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="3-1\n1\n")
    assert result.exit_code == 0, result.output
    assert "descending" in result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[0]}" in sel


def test_pick_malformed_range_token_errors_and_reprompts(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="2-x\n1\n")
    assert result.exit_code == 0, result.output
    assert "expected N or N-M" in result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[0]}" in sel


def test_pick_empty_selection_errors_and_reprompts(pick_env):
    ids = _ordered_discover_ids()
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="\n1\n")
    assert result.exit_code == 0, result.output
    assert "empty selection" in result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert f"  • {ids[0]}" in sel


def test_pick_eof_is_clean_error_not_loop(pick_env):
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="")
    assert result.exit_code == 1
    assert "error: no selection provided (EOF)" in result.output
    assert "Traceback" not in result.output


def test_pick_conflicts_with_models_flag(pick_env):
    result = runner.invoke(
        app, ["bench", "--pick", "--models", "x/y", "--dry-run"], input="1\n"
    )
    assert result.exit_code == 1
    assert "--pick and --models are mutually exclusive" in result.output


# ---------------------------------------------------------------------------
# non-interactive --models path + dry-run purity (no DB, no engine)
# ---------------------------------------------------------------------------


def test_models_dry_run_prints_models_without_db(pick_env, monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("connect() must not run in --dry-run mode")

    monkeypatch.setattr("hr.cli.connect", _explode)
    result = runner.invoke(
        app, ["bench", "--models", "alpha/one,beta/two", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    sel = result.output.split("# dry-run", 1)[1]
    assert "  • alpha/one" in sel
    assert "  • beta/two" in sel


def test_pick_dry_run_never_touches_db_or_engine(pick_env, monkeypatch):
    def _explode(*_args, **_kwargs):
        raise AssertionError("connect() must not run in --dry-run mode")

    monkeypatch.setattr("hr.cli.connect", _explode)
    result = runner.invoke(app, ["bench", "--pick", "--dry-run"], input="1\n")
    assert result.exit_code == 0, result.output
    assert "livebench" not in result.output.split("# dry-run", 1)[1]


# ---------------------------------------------------------------------------
# selection parser unit tests
# ---------------------------------------------------------------------------


def test_selection_indices_comma_and_range_order():
    assert _selection_indices("5-7", 10) == [5, 6, 7]
    assert _selection_indices("3,1", 10) == [3, 1]
    assert _selection_indices("1,,3,", 10) == [1, 3]
    assert _selection_indices("1,1,3", 10) == [1, 3]


def test_selection_indices_invalid_inputs():
    with pytest.raises(ValueError, match="not a number"):
        _selection_indices("1,x", 10)
    with pytest.raises(ValueError, match="descending"):
        _selection_indices("5-3", 10)
    with pytest.raises(ValueError, match="out of range"):
        _selection_indices("11", 10)
    with pytest.raises(ValueError, match="out of range"):
        _selection_indices("0", 10)
    with pytest.raises(ValueError, match="expected N or N-M"):
        _selection_indices("2-x", 10)
    with pytest.raises(ValueError, match="empty selection"):
        _selection_indices("", 10)


# ---------------------------------------------------------------------------
# wiring: the picked models are exactly the bench run set (credential-gated)
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.integration
def test_pick_feeds_engine_run_set(scratch_db, pick_env, monkeypatch):
    """Real CLI -> real scratch DB, fake adapter; entry 1 picked, entry 2 not."""
    ids = _ordered_discover_ids()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: FakeAdapter())
    # hr_sandbox removes DB envs inside the sandbox; point the CLI at the
    # scratch DB explicitly so the real engine path resolves credentials
    monkeypatch.setenv("HR_DSN", scratch_db[1])
    result = runner.invoke(
        app,
        ["bench", "--pick", "--battery", "instruction_follow"],
        input="1\n",
    )
    assert result.exit_code == 0, result.output
    _name, dsn = scratch_db
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
            "SELECT model_id FROM hr.run WHERE sweep_id LIKE 'livebench-%'"
            )
            ran = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    assert ran == [ids[0]]
