from __future__ import annotations

import typer
from typer.testing import CliRunner

from hr.cli_apply import register_apply_commands

from tests.test_apply import (
    PRESETS_FILENAME,
    STATE_FILENAME,
    _KeyedConn,
    _MODEL,
    _router,
    app,
    json,
    pytest,
    re,
    runner,
    validate_agents,
    write_preset,
    write_state
)

class TestNeverClobberWithEmptyData:
    """An empty verdict must refuse WITHOUT touching an existing presets
    file — presets are never clobbered with empty data (contract, todo 18)."""

    def test_empty_verdict_preserves_existing_presets_file(self, monkeypatch, tmp_path):
        presets_path = tmp_path / PRESETS_FILENAME
        payload = json.dumps(
            {"presets": {"prod-lock": {"description": "locked", "createdAt": "x",
                                       "agents": {"oracle": "p/m1"}}}},
            indent=2,
        ) + "\n"
        presets_path.write_text(payload, encoding="utf-8")
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router(means=False)))
        monkeypatch.setattr("hr.apply.load_deployable", lambda: {_MODEL})

        result = runner.invoke(app, ["apply"])

        assert result.exit_code == 1
        assert "no verdict seating" in result.output
        # byte-identical: the failed run never wrote the store
        assert presets_path.read_text() == payload

    def test_validate_agents_refuses_when_one_binding_lacks_slash(self):
        # mixed map: one valid, one invalid -> the whole seating is refused
        with pytest.raises(RuntimeError, match="provider/model"):
            validate_agents({"oracle": "p/m1", "deep": "deepseek-v4-flash"})

class TestFileContractShape:
    """The written files satisfy FastDraw's contract: presets store shape,
    isModelMap "/" rule, JSON.stringify(store, null, 2) formatting."""

    def test_write_preset_creates_nested_dir_and_exact_shape(self, tmp_path):
        cfg = tmp_path / "nested" / "config"
        agents = {"oracle": "p/m1", "visual-engineering": "p/m2"}
        path = write_preset(
            agents, "prod-lock", cfg,
            description="desc", created_at="2026-01-01T00:00:00.000Z",
        )
        assert path == cfg / PRESETS_FILENAME
        assert path.parent.is_dir()
        store = json.loads(path.read_text(encoding="utf-8"))
        assert set(store) == {"presets"}  # contract: only the presets key
        entry = store["presets"]["prod-lock"]
        assert set(entry) == {"description", "createdAt", "agents"}
        assert entry["description"] == "desc"
        assert entry["createdAt"] == "2026-01-01T00:00:00.000Z"
        assert entry["agents"] == agents
        # isModelMap rule at the FILE level: every agents value contains "/"
        for agent, model in entry["agents"].items():
            assert re.fullmatch(r".+/.+", model), f"{agent}: {model!r} lacks /"
        # JSON.stringify(store, null, 2) parity: 2-space indent + trailing NL
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n")
        assert '\n  "presets"' in text
        assert '\n    "prod-lock"' in text

    def test_write_state_exact_shape(self, tmp_path):
        agents = {"oracle": "p/m1", "visual-engineering": "p/m2"}
        path = write_state(agents, tmp_path)
        assert path == tmp_path / STATE_FILENAME
        state = json.loads(path.read_text(encoding="utf-8"))
        assert set(state) == {"agents"}  # contract: only the agents key
        assert state["agents"] == agents
        text = path.read_text(encoding="utf-8")
        assert text.endswith("\n") and '\n  "agents"' in text


def _assignments() -> list:
    from hr.decision import SeatAssignment

    return [
        SeatAssignment(
            seat_code="oracle",
            gate_level="strict",
            primary="test/model",
            fallbacks=[],
            eliminated=[],
            unassigned=None,
        )
    ]


def _standalone_cli() -> typer.Typer:
    cli = typer.Typer()
    register_apply_commands(cli)
    return cli


class TestPreviewDriftBindingCLI:
    """The apply-preview command RECORDS file hashes; `hr apply` then refuses
    if those files drifted (what was previewed must be what is applied)."""

    def _patch_seating(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr("hr.cli.connect", lambda: _KeyedConn(_router()))
        monkeypatch.setattr(
            "hr.apply.latest_assignments",
            lambda conn, **kw: (_assignments(), "test-sweep-12345"),
        )

    def test_preview_then_apply_without_drift_succeeds(self, monkeypatch, tmp_path):
        cli = _standalone_cli()
        cli_runner = CliRunner()
        self._patch_seating(monkeypatch, tmp_path)

        preview_result = cli_runner.invoke(cli, ["apply-preview", "--preset", "p1", "--set-state"])
        assert preview_result.exit_code == 0
        assert "preview drift" not in preview_result.output.lower()

        apply_result = cli_runner.invoke(cli, ["apply", "--preset", "p1", "--set-state"])
        assert apply_result.exit_code == 0, apply_result.output
        assert "preset 'p1'" in apply_result.output
        store = json.loads((tmp_path / PRESETS_FILENAME).read_text(encoding="utf-8"))
        assert "p1" in store["presets"]
        assert (tmp_path / STATE_FILENAME).exists()

    def test_apply_rejects_when_files_drifted_after_preview(self, monkeypatch, tmp_path):
        cli = _standalone_cli()
        cli_runner = CliRunner()
        self._patch_seating(monkeypatch, tmp_path)
        (tmp_path / PRESETS_FILENAME).write_text('{"presets": {"prod-lock": {}}}', encoding="utf-8")

        preview_result = cli_runner.invoke(cli, ["apply-preview", "--preset", "p1"])
        assert preview_result.exit_code == 0
        # a foreign process edits the presets file between preview and apply
        drifted = (tmp_path / PRESETS_FILENAME).read_text(encoding="utf-8")
        drifted += '/* drifted */\n'
        (tmp_path / PRESETS_FILENAME).write_text(drifted, encoding="utf-8")

        apply_result = cli_runner.invoke(cli, ["apply", "--preset", "p1"])
        assert apply_result.exit_code == 1
        assert "preview drift" in apply_result.output
        # no writes: the drifted bytes are untouched
        assert "/* drifted */" in (tmp_path / PRESETS_FILENAME).read_text(encoding="utf-8")


class TestApplyCliFileCommands:
    """apply-rollback / apply-backups / apply-prune operate on the config dir
    only (no DB) — driven through a standalone app in a temp config dir."""

    def test_apply_rollback_via_cli(self, monkeypatch, tmp_path):
        from hr.plugin_safety import create_backup

        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        (tmp_path / PRESETS_FILENAME).write_text('{"presets": {"old": {}}}', encoding="utf-8")
        create_backup("cli-rollback")
        (tmp_path / PRESETS_FILENAME).write_text('{"presets": {"new": {}}}', encoding="utf-8")

        cli_runner = CliRunner()
        result = cli_runner.invoke(_standalone_cli(), ["apply-rollback", "cli-rollback"])

        assert result.exit_code == 0
        assert "Rolled back" in result.output
        assert '{"presets": {"old": {}}}' == (tmp_path / PRESETS_FILENAME).read_text(encoding="utf-8")

    def test_apply_rollback_via_cli_refuses_corrupt_backup(self, monkeypatch, tmp_path):
        from hr.plugin_safety import create_backup

        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        (tmp_path / PRESETS_FILENAME).write_text('{"presets": {"old": {}}}', encoding="utf-8")
        backup_path = create_backup("cli-corrupt")
        (backup_path / "manifest.json").unlink()

        cli_runner = CliRunner()
        result = cli_runner.invoke(_standalone_cli(), ["apply-rollback", "cli-corrupt"])

        assert result.exit_code == 1
        assert "corrupt" in result.output

    def test_apply_backups_via_cli(self, monkeypatch, tmp_path):
        from hr.plugin_safety import create_backup

        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        create_backup("cli-listing")

        cli_runner = CliRunner()
        result = cli_runner.invoke(_standalone_cli(), ["apply-backups"])

        assert result.exit_code == 0
        assert "cli-listing" in result.output
        assert "snapshot_id" in result.output

    def test_apply_prune_via_cli(self, monkeypatch, tmp_path):
        from hr.plugin_safety import create_backup

        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
        for i in range(12):
            create_backup(f"cli-batch-{i:02d}", prune=False)

        cli_runner = CliRunner()
        result = cli_runner.invoke(_standalone_cli(), ["apply-prune"])

        assert result.exit_code == 0
        assert "Pruned 2" in result.output
        assert "kept 10" in result.output
