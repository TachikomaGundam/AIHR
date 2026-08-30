"""Stage-1 finals CLI contract tests (committed surface).

Exercises hr.stage1_cli subcommands against the committed argparse CLI:
dry-run with and without a finalist override, DB separation report,
and the live finals path with fakes. Offline and deterministic.
"""

from __future__ import annotations

import pytest

import hr.adapters as adapters_mod
import hr.stage1 as stage1_mod
import hr.stage1_cli as cli


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
def _patch_runtime_deps(monkeypatch, tmp_path):
    opts = {"thresholds": str(tmp_path / "no-such-thresholds.yaml")}
    monkeypatch.setattr(adapters_mod, "RoutedAdapter", _FakeRoutedAdapter)
    monkeypatch.setattr(stage1_mod, "run_finals", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_connect", lambda: _FakeConn([]))
    monkeypatch.setattr(
        cli, "load_full_banks", lambda repo, batteries: {"reasoning": [], "hallucination": []}
    )
    monkeypatch.setattr(cli, "itemrepo_path", lambda: tmp_path / "itemrepo")
    yield opts


def test_read_finals_separation_from_db(monkeypatch) -> None:
    conn = _FakeConn([("battery-reasoning", "a", "b", 0.9, 0.1, 0.0)])
    monkeypatch.setattr(cli, "_connect", lambda: conn)
    sep = cli.read_finals_separation_from_db("s1")
    assert conn.closed
    assert sep["reasoning"][0]["p_separated"] == 0.9


def test_list_finals_sweeps(monkeypatch) -> None:
    conn = _FakeConn([("sweep-x", "stage1", "2026-08-01")])
    monkeypatch.setattr(cli, "_connect", lambda: conn)
    assert cli.list_finals_sweeps() == [("sweep-x", "stage1", "2026-08-01")]
    assert conn.closed


def test_dry_run_with_override(monkeypatch, capsys, _patch_runtime_deps) -> None:
    rc = cli._cli_main(
        ["--dry-run", "--models", "m1,m2", "--thresholds", _patch_runtime_deps["thresholds"]]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== Stage 1 Finals Call Plan ===" in out
    assert "User-provided finalist list" in out
    assert "Finalists (2):" in out
    assert "m1" in out
    assert "Sequential-n:" in out


def test_dry_run_without_override_db_failure(monkeypatch, capsys, _patch_runtime_deps) -> None:
    def _boom(**kwargs):
        raise RuntimeError("no separation rows")

    monkeypatch.setattr(cli, "select_finalists_from_stage0", _boom)
    rc = cli._cli_main(["--dry-run", "--thresholds", _patch_runtime_deps["thresholds"]])
    assert rc == 1
    assert "Cannot select finalists: no separation rows" in capsys.readouterr().err


def test_report_with_sweep_id(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "read_finals_separation_from_db",
        lambda sweep_id: {
            "reasoning": [
                {"model_a": "m1", "model_b": "m2", "p_separated": 0.9, "p_weak": 0.1, "p_tie": 0.0}
            ]
        },
    )
    rc = cli._cli_main(["--report", "--sweep-id", "sweep-1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Sweep ID: sweep-1" in out
    assert "Battery: reasoning" in out


def test_report_no_sweeps(capsys) -> None:
    rc = cli._cli_main(["--report"])
    assert rc == 1
    assert "No Stage 1 finals sweeps recorded yet." in capsys.readouterr().err


def test_report_db_unavailable(capsys, monkeypatch) -> None:
    def _boom():
        raise ValueError("connection refused")

    monkeypatch.setattr(cli, "list_finals_sweeps", _boom)
    rc = cli._cli_main(["--report"])
    assert rc == 1
    assert "DB not available: connection refused" in capsys.readouterr().err


def test_run_success(monkeypatch, _patch_runtime_deps) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        stage1_mod,
        "run_finals",
        lambda **kwargs: seen.update(kwargs),
    )
    rc = cli._cli_main(["--run", "--models", "m1", "--sweep-id", "sweep-9"])
    assert rc == 0
    assert seen["finalists"] == ["m1"]
    assert seen["sweep_id"] == "sweep-9"
    assert seen["init_db"] is True
    assert seen["record_to_db"] is True


def test_run_no_db_flag(monkeypatch, _patch_runtime_deps) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(stage1_mod, "run_finals", lambda **kwargs: seen.update(kwargs))
    rc = cli._cli_main(["--run", "--no-db"])
    assert rc == 0
    assert seen["init_db"] is False
    assert seen["record_to_db"] is False


def test_run_failure_prints_clean_error(capsys, monkeypatch, _patch_runtime_deps) -> None:
    def _boom(**kwargs):
        raise RuntimeError("adapter auth exploded")

    monkeypatch.setattr(stage1_mod, "run_finals", _boom)
    rc = cli._cli_main(["--run"])
    assert rc == 1
    assert "Stage 1 failed: adapter auth exploded" in capsys.readouterr().err