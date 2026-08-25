from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from hr.models import BenchmarkCategory


@dataclass(frozen=True)
class ExperimentManifest:
    payload: dict[str, object]
    digest: str

    @classmethod
    def create(
        cls,
        *,
        seed: int,
        model_ids: Iterable[str],
        batteries: Iterable[BenchmarkCategory],
        code_revision: str,
        item_bank_hash: str | None = None,
        prompt_template_hash: str | None = None,
        grader_version: str | None = None,
        adapter_provider: str | None = None,
        execution_settings: dict[str, object] | None = None,
        runtime_info: dict[str, str] | None = None,
        bank: dict[str, object] | None = None,
    ) -> "ExperimentManifest":
        payload: dict[str, object] = {
            "schema_version": 2,
            "seed": seed,
            "models": sorted(set(model_ids)),
            "batteries": sorted(battery.value for battery in batteries),
            "code_revision": code_revision,
        }
        if item_bank_hash is not None:
            payload["item_bank_hash"] = item_bank_hash
        if prompt_template_hash is not None:
            payload["prompt_template_hash"] = prompt_template_hash
        if grader_version is not None:
            payload["grader_version"] = grader_version
        if adapter_provider is not None:
            payload["adapter_provider"] = adapter_provider
        if execution_settings is not None:
            payload["execution_settings"] = execution_settings
        if runtime_info is not None:
            payload["runtime_info"] = runtime_info
        if bank is not None:
            payload["bank"] = bank
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return cls(payload=payload, digest=hashlib.sha256(encoded).hexdigest())


__all__ = ["ExperimentManifest"]
