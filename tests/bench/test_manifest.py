from __future__ import annotations

from hr.bench.manifest import ExperimentManifest
from hr.models import BenchmarkCategory


def test_manifest_is_stable_for_equivalent_experiment_inputs() -> None:
    first = ExperimentManifest.create(
        seed=17,
        model_ids=["acme/b", "acme/a"],
        batteries=[BenchmarkCategory.vision, BenchmarkCategory.reasoning],
        code_revision="abc123",
    )
    second = ExperimentManifest.create(
        seed=17,
        model_ids=["acme/a", "acme/b"],
        batteries=[BenchmarkCategory.reasoning, BenchmarkCategory.vision],
        code_revision="abc123",
    )

    assert first.digest == second.digest
    assert first.payload["seed"] == 17
    assert first.payload["models"] == ["acme/a", "acme/b"]


def test_manifest_includes_optional_fields_when_provided() -> None:
    manifest = ExperimentManifest.create(
        seed=123,
        model_ids=["test-model"],
        batteries=[BenchmarkCategory.reasoning],
        code_revision="def456",
        grader_version="1.0.0",
        runtime_info={"python": "3.11.0", "hr_agent": "0.2.0"},
    )

    assert manifest.payload["grader_version"] == "1.0.0"
    assert manifest.payload["runtime_info"] == {"python": "3.11.0", "hr_agent": "0.2.0"}


def test_manifest_excludes_optional_fields_when_none() -> None:
    manifest = ExperimentManifest.create(
        seed=456,
        model_ids=["model-x"],
        batteries=[BenchmarkCategory.code_gen],
        code_revision="ghi789",
    )

    assert "grader_version" not in manifest.payload
    assert "runtime_info" not in manifest.payload
    assert "item_bank_hash" not in manifest.payload


def test_manifest_accepts_all_optional_fields() -> None:
    manifest = ExperimentManifest.create(
        seed=789,
        model_ids=["model-y"],
        batteries=[BenchmarkCategory.vision],
        code_revision="jkl012",
        item_bank_hash="abc123def456",
        prompt_template_hash="prompt789",
        grader_version="2.0.0",
        adapter_provider="openai",
        execution_settings={"timeout_s": 30, "max_tokens": 4096},
        runtime_info={"python": "3.12.0"},
    )

    assert manifest.payload["item_bank_hash"] == "abc123def456"
    assert manifest.payload["prompt_template_hash"] == "prompt789"
    assert manifest.payload["grader_version"] == "2.0.0"
    assert manifest.payload["adapter_provider"] == "openai"
    assert manifest.payload["execution_settings"] == {"timeout_s": 30, "max_tokens": 4096}
    assert manifest.payload["runtime_info"] == {"python": "3.12.0"}
