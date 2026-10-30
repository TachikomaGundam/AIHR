"""Stage-0 CLI contract tests (committed surface).

Exercises hr.stage0_cli subcommands against the committed argparse CLI:
dry-run planning, separation readback, the live sweep path with fakes,
and the stderr failure contracts. Offline, deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hr.adapters as adapters_mod
import hr.calibrate as calibrate_mod
import hr.stage0 as stage0_mod
import hr.stage0_cli as cli
from hr.items.schema import ItemType

from tests.test_stage0_selection_unit import make_env as _make_env


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql, params) -> None:  # noqa: ARG002
        pass

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)

    def close(self) -> None:
        self.closed = True


class _FakeRoutedAdapter:
    pass


@pytest.fixture(autouse=True)
def _patch_runtime_deps(monkeypatch):
    monkeypatch.setattr(cli, "_connect", lambda: _FakeConn([]))
    monkeypatch.setattr(cli, "fleet_models", lambda: ("m1", "m2"))
    monkeypatch.setattr(
        calibrate_mod,
        "load_item_repo",
        lambda repo, batteries: {
            b: [e for e in _subsets().get(b, [])] for b in batteries
        },
    )
    monkeypatch.setattr(adapters_mod, "RoutedAdapter", _FakeRoutedAdapter)
    monkeypatch.setattr(stage0_mod, "run_sweep", lambda **kwargs: None)
    # the S4 pricing gate would refuse the synthetic m1/m2 fleet; these
    # tests exercise OTHER seams, so certify the fake fleet through it and
    # let the dedicated pricing-gate tests below drive the real checker
    from hr.parity import PricingReport

    monkeypatch.setattr(
        "hr.parity.check_pricing_parity",
        lambda fleet, pricing=None: PricingReport((), (), tuple(fleet)),
    )


def _subsets() -> dict[str, list[object]]:
    return {
        "reasoning": [_make_env(f"reasoning.{i:03d}", ItemType.REASONING) for i in range(20)],
        "hallucination": [
            _make_env(f"hallucination.qa.{i:02d}", ItemType.FACTUALITY_QA) for i in range(25)
        ],
        "tool_a": [_make_env(f"tool_a.calc.{i:02d}", ItemType.TOOL_A) for i in range(30)],
        "vision": [_make_env(f"vision.ui_read.{i:02d}", ItemType.VISION) for i in range(15)],
        "tool_b": [_make_env(f"tool_b.r1.{i:02d}", ItemType.TOOL_B) for i in range(10)],
    }


def test_read_separation_from_db(monkeypatch) -> None:
    conn = _FakeConn([("battery-reasoning", "a", "b", 0.9, 0.1, 0.0)])
    monkeypatch.setattr(cli, "_connect", lambda: conn)
    sep = cli.read_separation_from_db("s1")
    assert conn.closed
    assert set(sep.keys()) == {"reasoning"}
    assert sep["reasoning"][0]["model_a"] == "a"
    assert sep["reasoning"][0]["p_separated"] == 0.9


def test_list_sweeps(monkeypatch) -> None:
    conn = _FakeConn([("sweep-1", "stage0", "2026-08-01")])
    monkeypatch.setattr(cli, "_connect", lambda: conn)
    assert cli.list_sweeps() == [("sweep-1", "stage0", "2026-08-01")]
    assert conn.closed


def test_dry_run_prints_plan(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_stage0_token_cap", lambda: 60_000_000)
    rc = cli._cli_main(["--dry-run", "--models", "m1,m2", "--n-initial", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== Stage 0 Call Plan ===" in out
    assert "Models (2):" in out
    assert "reasoning: 20 items" in out
    assert "tool_b: 10 items" in out
    assert "within cap" in out


def test_dry_run_token_cap_default_uses_config(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_stage0_token_cap", lambda: 424_242)
    rc = cli._cli_main(["--dry-run", "--models", "m1"])
    assert rc == 0
    assert "424,242 tokens" in capsys.readouterr().out


def test_dry_run_unknown_models_rejected(capsys) -> None:
    rc = cli._cli_main(["--dry-run", "--models", "m1,nope"])
    assert rc == 1
    assert "Unknown model ids: ['nope']" in capsys.readouterr().err


def test_dry_run_item_repo_flag_reaches_loader(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_load(repo, batteries):
        seen["repo"] = repo
        seen["batteries"] = batteries
        return _subsets()

    monkeypatch.setattr(calibrate_mod, "load_item_repo", fake_load)
    rc = cli._cli_main(["--dry-run", "--models", "m1", "--item-repo", "/tmp/some-repo"])
    assert rc == 0
    assert seen["repo"] == Path("/tmp/some-repo")
    assert seen["batteries"] == ["reasoning", "hallucination", "tool_a", "vision", "tool_b"]


def test_separation_with_sweep_id(capsys, monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "read_separation_from_db",
        lambda sweep_id: {
            "reasoning": [
                {"model_a": "m1", "model_b": "m2", "p_separated": 0.9, "p_weak": 0.1, "p_tie": 0.0}
            ]
        },
    )
    rc = cli._cli_main(["--separation", "--sweep-id", "sweep-9"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sweep ID: sweep-9" in out
    assert "=== Stage 0 Separation Matrix ===" in out
    assert "--- Battery: reasoning ---" in out


def test_separation_no_sweeps_recorded(capsys) -> None:
    rc = cli._cli_main(["--separation"])
    assert rc == 1
    assert "No Stage 0 sweeps recorded yet." in capsys.readouterr().err


def test_separation_db_unavailable(capsys, monkeypatch) -> None:
    def boom():
        raise ValueError("connection refused")

    monkeypatch.setattr(cli, "list_sweeps", boom)
    rc = cli._cli_main(["--separation"])
    assert rc == 1
    assert "DB not available: connection refused" in capsys.readouterr().err


def test_live_run_success(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        stage0_mod,
        "run_sweep",
        lambda **kwargs: seen.update(kwargs),
    )
    rc = cli._cli_main(["--models", "m1", "--skip-preflight"])
    assert rc == 0
    assert seen["models"] == ("m1",)
    assert seen["n_initial"] == 3
    assert seen["init_db"] is True
    assert seen["record_to_db"] is True
    assert capsys.readouterr().out == ""


def test_live_run_no_db_flag(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(stage0_mod, "run_sweep", lambda **kwargs: seen.update(kwargs))
    rc = cli._cli_main(["--models", "m1", "--no-db"])
    assert rc == 0
    assert seen["init_db"] is False
    assert seen["record_to_db"] is False


def test_live_run_unknown_models_rejected(capsys) -> None:
    rc = cli._cli_main(["--models", "nope"])
    assert rc == 1
    assert "Unknown model ids: ['nope']" in capsys.readouterr().err


def test_live_run_failure_prints_clean_error(capsys, monkeypatch) -> None:
    def boom(**kwargs):
        raise RuntimeError("adapter auth exploded")

    monkeypatch.setattr(stage0_mod, "run_sweep", boom)
    rc = cli._cli_main(["--skip-preflight"])
    assert rc == 1
    assert "Stage 0 failed: adapter auth exploded" in capsys.readouterr().err

def test_preflight_gate_refuses_without_marker(monkeypatch, capsys) -> None:
    monkeypatch.setattr("hr.preflight.marker_age_hours", lambda conn: None)
    monkeypatch.setattr("hr.db.connect", lambda *a, **k: object())
    rc = cli._cli_main(["--models", "m1"])
    assert rc == 2
    assert "preflight gate" in capsys.readouterr().err


def test_preflight_gate_passes_on_fresh_marker(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(stage0_mod, "run_sweep", lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr("hr.preflight.marker_age_hours", lambda conn: 0.5)
    monkeypatch.setattr("hr.db.connect", lambda *a, **k: object())
    rc = cli._cli_main(["--models", "m1"])
    assert rc == 0 and seen


def test_preflight_gate_override_is_logged(monkeypatch, capsys) -> None:
    monkeypatch.setattr(stage0_mod, "run_sweep", lambda **kwargs: None)
    monkeypatch.setattr("hr.preflight.marker_age_hours", lambda conn: None)
    monkeypatch.setattr("hr.db.connect", lambda *a, **k: object())
    rc = cli._cli_main(["--models", "m1", "--skip-preflight"])
    assert rc == 0
    assert "OVERRIDE" in capsys.readouterr().err


class TestPricingGateSeam:
    def test_live_sweep_refuses_unpriced_fleet(self, monkeypatch, capsys) -> None:
        from hr.parity import PricingReport

        monkeypatch.setattr(
            "hr.parity.check_pricing_parity",
            lambda fleet, pricing=None: PricingReport(("m1",), (), ()),
        )
        rc = cli._cli_main(["--models", "m1", "--skip-preflight"])
        assert rc == 2
        assert "pricing gate" in capsys.readouterr().err

    def test_dry_run_never_meets_the_pricing_gate(self, monkeypatch, capsys) -> None:
        def _boom(*a, **k):
            raise AssertionError("dry-run must not reach the pricing gate")

        monkeypatch.setattr("hr.parity.check_pricing_parity", _boom)
        rc = cli._cli_main(["--dry-run"])
        assert rc == 0
