"""CLI tests for ``hr bench`` (task 12).

- ``hr bench --help`` lists the 10 livebench battery names.
- Full mocked e2e through the real CLI: CliRunner + fake adapter (no
  network) against the scratch DB; SQL asserts on the written rows.
- Missing thresholds.yaml entry -> explicit error naming the battery.
"""

from __future__ import annotations

import uuid

import pytest
from typer.testing import CliRunner

import hr.bench.engine as engine_mod
from hr.cli import app
from hr.models import BenchmarkCategory
from tests.bench.fake_adapter import FakeAdapter

runner = CliRunner()

# Deterministic help rendering: rich wraps panels at the detected terminal
# width (80 in CI pty) and colors output — COLUMNS fixes the width for
# shutil.get_terminal_size, NO_COLOR strips ANSI escapes.
_HELP_ENV = {"COLUMNS": "200", "NO_COLOR": "1"}

ALL_10 = [
    "code_gen",
    "reasoning",
    "instruction_follow",
    "tool_use",
    "long_context",
    "attention_probe",
    "attention_stress",
    "vision",
    "speed",
    "long_horizon",
]


def test_bench_help_lists_ten_benchmarks() -> None:
    result = runner.invoke(app, ["bench", "--help"])
    assert result.exit_code == 0
    for name in ALL_10:
        assert name in result.stdout


def test_bench_help_lists_models_and_battery_flags() -> None:
    result = runner.invoke(app, ["bench", "--help"], env=_HELP_ENV)
    assert "--models" in result.stdout
    assert "--battery" in result.stdout


def test_bench_rejects_unknown_battery() -> None:
    result = runner.invoke(app, ["bench", "--battery", "not_a_battery"])
    assert result.exit_code != 0
    assert "not_a_battery" in result.output


@pytest.mark.db
@pytest.mark.integration
def test_bench_mocked_e2e_writes_measurements(
    scratch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real CLI -> real scratch DB, fake adapter (zero network)."""
    monkeypatch.setattr(
        engine_mod, "adapter_for", lambda model_id: FakeAdapter()
    )
    result = runner.invoke(
        app,
        [
            "bench",
            "--models", "fake/e2e-model",
            "--battery", "instruction_follow",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "livebench_instruction_follow" in result.output
    assert "100.0" in result.output

    _name, dsn = scratch_db
    import psycopg2

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(m.measurement_id)::int, AVG(m.score)::float8
          FROM hr.measurement m
          JOIN hr.run r ON r.run_id = m.run_id
                 WHERE r.sweep_id LIKE 'livebench-%'
                """
            )
            count, mean = cur.fetchone()
            cur.execute(
            "SELECT COUNT(*) FROM hr.run "
                "WHERE model_id = 'fake/e2e-model'"
            )
            runs = cur.fetchone()[0]
            cur.execute(
            "SELECT COUNT(*) FROM hr.sweep WHERE purpose = 'livebench'"
            )
            sweeps = cur.fetchone()[0]
    finally:
        conn.close()
    assert sweeps == 1
    assert runs == 1
    assert count == 16
    assert mean == pytest.approx(100.0)


def test_bench_missing_threshold_entry_is_explicit_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    import hr.config as config
    from hr.stats.sequential import SequentialConfig

    configs = tmp_path / "configs"
    configs.mkdir()
    full = {
        "n_initial": 3,
        "n_max": 10,
        "half_width": {
            b: 5.0 for b in (
                "livebench_code_gen", "livebench_reasoning",
                "livebench_instruction_follow", "livebench_tool_use",
                "livebench_long_context", "livebench_vision",
                "livebench_speed", "livebench_long_horizon",
            )
        },
    }
    broken = {**full, "half_width": {k: v for k, v in full["half_width"].items()
                                      if k != "livebench_speed"}}
    (configs / "thresholds.yaml").write_text(yaml.safe_dump(broken), encoding="utf-8")
    monkeypatch.setattr(config, "config_path", lambda name: configs / name)

    engine = LivebenchEngineForCli()
    with pytest.raises(ValueError) as exc:
        engine.require_thresholds([BenchmarkCategory.speed])
    assert "livebench_speed" in str(exc.value)


# Local import indirection so the CLI test exercises the same guard the
# command uses (imported here to avoid a private-name import in hr.cli).
from hr.bench.engine import LivebenchEngine as LivebenchEngineForCli  # noqa: E402


def test_bench_command_uses_threshold_guard(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    import hr.config as config

    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "thresholds.yaml").write_text(
        yaml.safe_dump({"n_initial": 3, "n_max": 10, "half_width": {}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "config_path", lambda name: configs / name)

    result = runner.invoke(app, ["bench", "--battery", "code_gen", "--models", "x/y"])
    assert result.exit_code == 1
    assert "livebench_code_gen" in result.output
