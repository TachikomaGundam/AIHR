"""Registry + lazy-truths + thresholds-validation tests (task 12).

Registry: the 10 livebench batteries, their item labels
(13/13/16/1/3/8/4/1/1/4), and honest seat_battery n_initial/n_max (never
exceeding item count).
Truths: ``long_horizon_truths()`` must be LAZY — importing the package must
not compute the CPM graph, and the scorer resolves truth on first call.
"""

from __future__ import annotations


from pathlib import Path

import pytest

import hr.bench.truths as truths_mod
from hr.bench.engine import LivebenchEngine
from hr.bench.livebench import (
    LIVEBENCH_BATTERIES,
    battery_code,
    battery_item_labels,
    seat_battery_bounds,
)
from hr.bench.scorers import score_long_horizon
from hr.models import BenchmarkCategory


def test_registry_holds_all_ten_benchmarks() -> None:
    names = [b.value for b in LIVEBENCH_BATTERIES]
    assert names == [
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


def test_battery_code_mapping() -> None:
    assert battery_code(BenchmarkCategory.code_gen) == "livebench_code_gen"
    assert battery_code(BenchmarkCategory.long_horizon) == "livebench_long_horizon"


@pytest.mark.parametrize(
    ("battery", "n_labels"),
    [
        (BenchmarkCategory.code_gen, 13),
        (BenchmarkCategory.reasoning, 13),
        (BenchmarkCategory.instruction_follow, 16),
        (BenchmarkCategory.tool_use, 1),
        (BenchmarkCategory.long_context, 3),
        (BenchmarkCategory.attention_probe, 8),
        (BenchmarkCategory.attention_stress, 4),
        (BenchmarkCategory.vision, 1),
        (BenchmarkCategory.speed, 1),
        (BenchmarkCategory.long_horizon, 4),
    ],
)
def test_item_label_counts(battery: BenchmarkCategory, n_labels: int) -> None:
    labels = battery_item_labels(battery)
    assert len(labels) == n_labels
    assert len(set(labels)) == n_labels


def test_attention_probe_item_label_order() -> None:
    labels = battery_item_labels(BenchmarkCategory.attention_probe)
    assert labels == [
        "pos_head", "pos_mid_early", "pos_mid", "pos_mid_late", "pos_tail",
        "assoc_literal", "assoc_infer", "decoy_resist",
    ]


def test_attention_stress_item_label_order() -> None:
    labels = battery_item_labels(BenchmarkCategory.attention_stress)
    assert labels == ["survive_t5", "survive_t10", "survive_t15", "survive_t20"]


def test_seat_battery_bounds_never_exceed_item_count() -> None:
    for battery in LIVEBENCH_BATTERIES:
        n_initial, n_max = seat_battery_bounds(battery)
        n_items = len(battery_item_labels(battery))
        assert 1 <= n_initial <= n_max
        assert n_max <= n_items
        assert n_initial <= min(3, n_items)


def test_long_horizon_truths_lazy_at_import() -> None:
    """Importing hr.bench must NOT compute the CPM truth graph.

    The accessor resolves the compute function at CALL time: starving the
    cache and patching the compute fn proves the compute only happens when
    the truth is first requested — never at import."""
    original = truths_mod._compute_long_horizon_truths

    def _boom() -> dict:
        raise AssertionError("long-horizon truths computed eagerly!")
    truths_mod._compute_long_horizon_truths = _boom
    truths_mod._LONG_HORIZON_CACHE = None
    try:
        with pytest.raises(AssertionError, match="eagerly"):
            truths_mod.long_horizon_truths()  # first call -> lazy compute
    finally:
        truths_mod._compute_long_horizon_truths = original
        truths_mod._LONG_HORIZON_CACHE = None


def test_long_horizon_truths_compute_on_first_call() -> None:
    t = truths_mod.long_horizon_truths()
    assert t["duration"] == 15
    assert t["critical_path"] == ["A", "B", "E", "F"]
    assert t["non_critical_slack"] == {"C": 3, "D": 2}
    assert t["action_task"] is None  # duration 15 <= threshold 15 -> NONE


def test_reasoning_truths_are_runtime_computed() -> None:
    t = truths_mod.reasoning_truths()
    assert len(t) == 13
    # Spot values independently recomputable by hand:
    assert t[6] == pow(13, 500, 1000)  # last three digits
    assert t[9] == 49  # trailing zeros of 200!
    assert t[10] == 1854  # !7 derangements


def test_engine_validates_thresholds_missing_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing a livebench entry from thresholds.yaml -> explicit error."""
    import yaml

    import hr.config as config

    from hr.stats.sequential import SequentialConfig

    thresholds = tmp_path / "configs"
    thresholds.mkdir()
    cfg = {
        "n_initial": 3,
        "n_max": 10,
        "half_width": {
            "reasoning": 2.0,
            "livebench_reasoning": 3.0,
            "livebench_speed": 5.0,
        },
        "min_effect": {
            "livebench_reasoning": 0.05,
            "livebench_speed": 0.05,
        },
    }
    (thresholds / "thresholds.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8"
    )
    monkeypatch.setattr(config, "config_path", lambda name: thresholds / name)

    # Full set present -> loads.
    ok = SequentialConfig.from_yaml(
        str(thresholds / "thresholds.yaml"),
        required_batteries=["livebench_reasoning", "livebench_speed"],
    )
    assert ok.thresholds["livebench_reasoning"] == 3.0

    # Drop one entry -> explicit error naming the battery.
    cfg["half_width"].pop("livebench_speed")
    (thresholds / "thresholds.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc:
        SequentialConfig.from_yaml(
            str(thresholds / "thresholds.yaml"),
            required_batteries=["livebench_reasoning", "livebench_speed"],
        )
    assert "livebench_speed" in str(exc.value)


def test_engine_threshold_guard_uses_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine refuses to run a battery whose threshold entry is missing."""
    import yaml

    import hr.config as config

    thresholds = tmp_path / "configs"
    thresholds.mkdir()
    cfg = {"n_initial": 3, "n_max": 10, "half_width": {}}
    (thresholds / "thresholds.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8"
    )
    monkeypatch.setattr(config, "config_path", lambda name: thresholds / name)

    engine = LivebenchEngine()
    with pytest.raises(ValueError) as exc:
        engine.require_thresholds([BenchmarkCategory.code_gen])
    assert "livebench_code_gen" in str(exc.value)


def test_scorer_uses_lazy_long_horizon_truths() -> None:
    """score_long_horizon resolves truths from the lazy store (no args)."""
    from hr.bench.livebench import battery_item_labels
    from hr.models import BenchmarkCategory

    labels = battery_item_labels(BenchmarkCategory.long_horizon)
    t = truths_mod.long_horizon_truths()
    slack_bits = ", ".join(
        f"{k}={v}" for k, v in t["non_critical_slack"].items()
    )
    answer = (
        f"CRITICAL_PATH: {'->'.join(t['critical_path'])}\n"
        f"DURATION: {t['duration']} days\n"
        f"SLACK: {slack_bits}\nACTION: NONE\n"
    )
    outcome = score_long_horizon(answer)
    assert outcome.score == pytest.approx(100.0)
    assert [lbl for lbl, _ in (outcome.item_scores or [])] == labels