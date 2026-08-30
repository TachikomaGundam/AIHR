"""Tests for hr/calibration_cli.py — the stage-0 anchor calibration CLI."""
from __future__ import annotations

from pathlib import Path

import pytest

from hr.calibration_cli import _cli, dry_run_report
from hr.calibration_models import BatteryVerdict, CalibrationReport, TierBandVerdict


def _write_item(repo: Path, key: str, item_type: str) -> None:
    envelope = {
        "item_key": key,
        "type": item_type,
        "tier": 3,
        "payload": {"question": "sample question"},
        "grading": {"grader": "exact_match@1.0"},
        "meta": {"seats": ["oracle"]},
    }
    import json

    (repo / f"{key}.json").write_text(json.dumps(envelope), encoding="utf-8")


def _passing_report(pool_hash: str = "h") -> CalibrationReport:
    tier = TierBandVerdict(
        battery="reasoning",
        tier=1,
        anchor="cheap",
        pass_rate=0.9,
        band_lo=0.8,
        band_hi=1.0,
        passed=True,
    )
    verdict = BatteryVerdict(
        battery="reasoning", anchor="cheap", tier_verdicts=[tier], passed=True
    )
    return CalibrationReport(
        pool_hash=pool_hash,
        measurements=[],
        verdicts=[verdict],
        total_tokens_in=100,
        total_tokens_out=50,
    )


def test_dry_run_report_counts_items_and_anchors(tmp_path: Path) -> None:
    # Given: an item repo with two reasoning items and explicit anchors
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")
    _write_item(repo, "reason.b", "reasoning")
    _write_item(repo, "vision.x", "vision")
    (repo / "broken.json").write_text("not json", encoding="utf-8")

    # When: the dry-run plan is rendered for the reasoning battery only.
    report = dry_run_report(
        repo,
        anchors={"cheap": "test/model-a", "mid": "test/model-b"},
        batteries=["reasoning"],
        token_cap=7_000_000,
    )

    # Then: counts, anchors, call/token estimates and acceptance bands
    # reflect the (valid) items and anchors.
    assert "reasoning: 2" in report
    assert "TOTAL: 2" in report
    assert "cheap: test/model-a" in report
    assert "mid: test/model-b" in report
    assert "Total calls: 4" in report  # 2 items x 2 anchors
    assert "cap = 7000000" in report
    assert "tier1" in report and "tier6" in report
    assert "Batteries checked:" in report
    assert "  - reasoning" in report


def test_dry_run_report_defaults_to_all_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: an item repo and the default anchor store
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")
    monkeypatch.setattr(
        "hr.calibration_cli.load_anchors", lambda: {"cheap": "test/model-a"}
    )

    # When: no batteries or anchors are passed.
    report = dry_run_report(repo)

    # Then: every configured battery appears in the plan.
    assert "reasoning: 1" in report
    assert "vision: 0" in report
    assert "tool_a: 0" in report
    assert "TOTAL: 1" in report
    assert "Total calls: 1" in report


def test_cli_dry_run_prints_plan_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Given: a temp item repo with one valid item
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")

    # When: the CLI runs in dry-run mode with explicit options.
    code = _cli(
        [
            "--item-repo",
            str(repo),
            "--dry-run",
            "--batteries",
            "reasoning",
            "--anchors",
            "cheap",
            "--token-cap",
            "1234",
        ]
    )

    # Then: the plan is printed and the exit code is 0.
    assert code == 0
    assert "reasoning: 1" in capsys.readouterr().out


def test_cli_unknown_anchor_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    # Given: an anchors list that references a key the store does not know
    # When: the CLI runs with --anchors bogus
    code = _cli(["--item-repo", str(tmp_path), "--dry-run", "--anchors", "bogus"])
    # Then: it reports the unknown anchor on stderr and exits 2.
    captured = capsys.readouterr()
    assert "unknown anchor" in captured.err
    assert "bogus" in captured.err
    assert code == 2


def test_cli_live_run_returns_runner_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Given: a spoofed adapter + runner (no API calls, no DB) and a real,
    # passing CalibrationReport for the renderers to consume
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")
    monkeypatch.setattr("hr.calibration_cli.adapter_for", lambda model_id: object())
    calls: list[dict] = []

    class _FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def run(self) -> CalibrationReport:
            return _passing_report(pool_hash="live-run")

    monkeypatch.setattr("hr.calibration_cli.CalibrationRunner", _FakeRunner)

    # When: a live calibration run completes with all_passed=True and --json.
    code = _cli(
        [
            "--item-repo",
            str(repo),
            "--batteries",
            "reasoning,vision",
            "--resume",
            "--json",
        ]
    )

    # Then: the runner saw the parsed options, JSON is emitted, exit code 0.
    assert code == 0
    assert calls and calls[0]["resume"] is True
    assert calls[0]["batteries"] == ["reasoning", "vision"]
    captured = capsys.readouterr()
    assert "== hr calibration report ==" in captured.out
    assert '"pool_hash": "live-run"' in captured.out


def test_cli_live_run_failed_report_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a runner whose report says an anchor band failed
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")
    monkeypatch.setattr("hr.calibration_cli.adapter_for", lambda model_id: object())
    failing = _passing_report()
    failing.verdicts[0].passed = False

    class _FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self) -> CalibrationReport:
            return failing

    monkeypatch.setattr("hr.calibration_cli.CalibrationRunner", _FakeRunner)

    # When: the run finishes with a failing report.
    code = _cli(["--item-repo", str(repo)])

    # Then: the CLI exits 1.
    assert code == 1


def test_cli_live_stopped_at_token_cap_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    # Given: a runner that stopped at the token cap
    repo = tmp_path / "itemrepo"
    repo.mkdir()
    _write_item(repo, "reason.a", "reasoning")
    monkeypatch.setattr("hr.calibration_cli.adapter_for", lambda model_id: object())
    capped = _passing_report()
    capped.stopped_at_cap = True
    capped.total_tokens_out = 200

    class _FakeRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def run(self) -> CalibrationReport:
            return capped

    monkeypatch.setattr("hr.calibration_cli.CalibrationRunner", _FakeRunner)

    # When: a live run ends at the token cap.
    code = _cli(["--item-repo", str(repo)])

    # Then: the warning is printed and the report still counts tokens.
    assert code == 0
    assert "WARNING: stopped at token cap" in capsys.readouterr().out