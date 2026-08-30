"""Engine e2e tests — mocked adapter, NO network (task 12).

- run_battery drives every benchmark through a ChatRequest-shaped call.
- store() writes sweep, run, and measurement rows with battery linkage.
- SQL asserts: 10 livebench batteries, item counts 13/13/16/1/3/8/4/1/1/4,
  seat_battery links, item_pool rows, per-battery means == expected scores.
- Garbage adapter -> score 0 run recorded as failed (never crash).
"""

from __future__ import annotations

import uuid  # noqa: F401 (re-export; consumed by sibling test modules)

import pytest

import hr.bench.engine as engine_mod

from hr.adapters.base import ChatRequest  # noqa: F401 (re-export; consumed by sibling test modules)

from hr.bench.engine import LivebenchEngine

from hr.bench.livebench import (
    LIVEBENCH_BATTERIES,
    battery_code,  # noqa: F401 (re-export; consumed by sibling test modules)
    battery_item_labels,  # noqa: F401 (re-export; consumed by sibling test modules)
)


from hr.models import BenchmarkCategory  # noqa: F401 (re-export; consumed by sibling test modules)

from tests.bench.fake_adapter import (
    FakeAdapter,
    FlakyStressAdapter,  # noqa: F401 (re-export; consumed by sibling test modules)
    ForgetfulStressAdapter,  # noqa: F401 (re-export; consumed by sibling test modules)
    NoVisionAdapter,  # noqa: F401 (re-export; consumed by sibling test modules)
    PERFECT_INSTRUCTION_JSON,  # noqa: F401 (re-export; consumed by sibling test modules)
    ToolsRejectedAdapter,  # noqa: F401 (re-export; consumed by sibling test modules)
    )

MODEL = "fake/test-model"

@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> LivebenchEngine:
    fake = FakeAdapter()
    monkeypatch.setattr(engine_mod, "adapter_for", lambda model_id: fake)
    return LivebenchEngine()

def _sql(conn, sql: str, params: tuple | None = None) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return list(cur.fetchall())

def _run_all_batteries(engine: LivebenchEngine, conn, sweep_id: str, model: str) -> None:
    for battery in LIVEBENCH_BATTERIES:
        out = engine.run_battery(model, battery)
        engine.store(conn, sweep_id, model, battery, out)
