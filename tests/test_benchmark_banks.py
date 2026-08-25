"""Tests for benchmark bank versioning and exposure tracking (hr-evolution T5).

Two layers:

* Offline unit tests (MagicMock connection) pin the pure selection /
  exclusion / state / disclosure logic against a fake 3-tuple exposure
  row (total, unique, last) and fake version/selection rows.
* Live ``@pytest.mark.db`` tests seed a real scratch-PostgreSQL bank and
  prove the SQL shapes (version resolution, stratified selection,
  exclusion of overexposed/holdout/flagged/retired items, durable state
  writes, exposure recomputation straight from ``hr.measurement``).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extensions
import pytest

from hr.bench.livebench import (
    LIVEBENCH_BATTERIES,
    battery_item_stratum,
    battery_strata,
)
from hr.bench.manifest import ExperimentManifest
from hr.benchmark_banks import (
    BankSelectionPolicy,
    BenchmarkBankManager,
    ItemExposure,
    ItemState,
)
from hr.models import BenchmarkCategory
from tests._db_contracts_helpers import (
    connect,
    seed_battery,
    seed_battery_item,
    seed_control_model,
    seed_item_pool,
    seed_measurement,
    seed_provider_models,
    seed_run,
    seed_seat,
    seed_sweep,
)


def _mock_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# ItemExposure
# ---------------------------------------------------------------------------


def test_item_exposure_is_safe_below_threshold() -> None:
    exposure = ItemExposure(
        item_id="test-item",
        total_exposures=50,
        unique_models_exposed=10,
        last_exposed_at="2024-01-01",
        contamination_risk=0.5,
    )

    assert exposure.is_safe_for_evaluation(max_exposures=100) is True


def test_item_exposure_is_unsafe_above_threshold() -> None:
    exposure = ItemExposure(
        item_id="test-item",
        total_exposures=150,
        unique_models_exposed=20,
        last_exposed_at="2024-01-01",
        contamination_risk=1.0,
    )

    assert exposure.is_safe_for_evaluation(max_exposures=100) is False


# ---------------------------------------------------------------------------
# get_item_exposure (3-col SELECT contract: total, unique, last)
# ---------------------------------------------------------------------------


def test_get_item_exposure_with_no_data() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = None

    manager = BenchmarkBankManager(_mock_conn(cursor))
    exposure = manager.get_item_exposure("test-item")

    assert exposure.item_id == "test-item"
    assert exposure.total_exposures == 0
    assert exposure.unique_models_exposed == 0
    assert exposure.last_exposed_at is None
    assert exposure.contamination_risk == 0.0


def test_get_item_exposure_with_data() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = (75, 15, datetime(2024, 6, 1))

    manager = BenchmarkBankManager(_mock_conn(cursor))
    exposure = manager.get_item_exposure("test-item")

    assert exposure.item_id == "test-item"
    assert exposure.total_exposures == 75
    assert exposure.unique_models_exposed == 15
    assert exposure.contamination_risk == 0.75


def test_get_safe_items_for_evaluation() -> None:
    cursor = MagicMock()
    cursor.fetchall.return_value = [
        ("item-1",),
        ("item-2",),
        ("item-3",),
    ]

    manager = BenchmarkBankManager(_mock_conn(cursor))
    items = manager.get_safe_items_for_evaluation("test-bank", count=3)

    assert items == ["item-1", "item-2", "item-3"]


def test_get_bank_version_not_found() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = None

    manager = BenchmarkBankManager(_mock_conn(cursor))
    version = manager.get_bank_version("nonexistent-bank")

    assert version is None


def test_get_bank_version_latest() -> None:
    cursor = MagicMock()
    # First query returns bank info
    cursor.fetchone.side_effect = [
        ("test-bank", "v1", 10, datetime(2024, 1, 1)),
        (0,),  # holdout count
    ]
    # Second query returns difficulty distribution
    cursor.fetchall.return_value = [
        ("easy", 3),
        ("medium", 5),
        ("hard", 2),
    ]

    manager = BenchmarkBankManager(_mock_conn(cursor))
    version = manager.get_bank_version("test-bank", version="latest")

    assert version is not None
    assert version.bank_code == "test-bank"
    assert version.version == "v1"
    assert version.item_count == 10
    assert version.difficulty_distribution == {"easy": 3, "medium": 5, "hard": 2}
    assert version.holdout_count == 0


# ---------------------------------------------------------------------------
# Item state (get/set with holdout-key sync)
# ---------------------------------------------------------------------------


def test_get_item_state_defaults_to_active_when_unset() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = (None,)

    manager = BenchmarkBankManager(_mock_conn(cursor))
    assert manager.get_item_state("plain-item") is ItemState.ACTIVE


def test_get_item_state_reads_stored_state() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = ("retired",)

    manager = BenchmarkBankManager(_mock_conn(cursor))
    assert manager.get_item_state("retired-item") is ItemState.RETIRED


def test_get_item_state_prefers_holdout_flag() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = ("holdout",)

    manager = BenchmarkBankManager(_mock_conn(cursor))
    assert manager.get_item_state("holdout-item") is ItemState.HOLDOUT


def test_set_item_state_returns_rowcount_and_syncs_holdout_key() -> None:
    cursor = MagicMock()
    cursor.rowcount = 1

    manager = BenchmarkBankManager(_mock_conn(cursor))
    assert manager.set_item_state("some-item", ItemState.FLAGGED) == 1
    # the SQL must keep the legacy 'holdout' flag in sync with 'state'
    sql = cursor.execute.call_args[0][0]
    assert "state" in sql
    assert "holdout" in sql


# ---------------------------------------------------------------------------
# select_items — stratified, exposure-aware, state-aware, traceable
# ---------------------------------------------------------------------------


def _selection_rows() -> list[tuple]:
    return [
        ("item-e1", "easy", "active", 0, 0, None),
        ("item-e2", "easy", "active", 1, 1, datetime(2024, 6, 1)),
        ("item-e3", "easy", "active", 5, 2, datetime(2024, 6, 2)),
        ("item-h1", "hard", "active", 0, 0, None),
        ("item-h2", "hard", "active", 0, 0, None),
        ("item-h3", "hard", "active", 0, 0, None),
    ]


def test_select_items_returns_traceable_stratified_selection() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = ("v1",)
    cursor.fetchall.return_value = _selection_rows()

    manager = BenchmarkBankManager(_mock_conn(cursor))
    result = manager.select_items(
        "my-bank", 4, policy=BankSelectionPolicy(max_exposures=3, seed=42)
    )

    # exposure cap: item-e3 (5 exposures >= 3) must be excluded
    assert result.selected[0].item_id != "item-e3"
    assert result.bank_version == "v1"
    assert len(result.selected) == 4
    # stratified proportional: 2 easy (of 2) + 2 hard (of 3) by
    # largest-remainder allocation (4/5 of each stratum, rem to easy)
    easy = [it for it in result.selected if it.stratum == "easy"]
    hard = [it for it in result.selected if it.stratum == "hard"]
    assert len(easy) == 2
    assert len(hard) == 2
    # traceability per item
    for item in result.selected:
        assert item.bank_version == "v1"
        assert item.state is ItemState.ACTIVE
        assert item.is_safe is True
        assert 0 <= item.contamination_risk <= 1.0
    # deterministic under the same seed
    again = manager.select_items(
        "my-bank", 4, policy=BankSelectionPolicy(max_exposures=3, seed=42)
    )
    assert [it.item_id for it in again.selected] == [
        it.item_id for it in result.selected
    ]


def test_select_items_excludes_holdout_flagged_and_retired() -> None:
    rows = [
        ("item-x1", "easy", "holdout", 0, 0, None),
        ("item-x2", "easy", "flagged", 0, 0, None),
        ("item-x3", "easy", "retired", 0, 0, None),
        ("item-x4", "easy", "active", 0, 0, None),
    ]
    cursor = MagicMock()
    cursor.fetchone.side_effect = [("v1",), None]
    cursor.fetchall.return_value = rows

    manager = BenchmarkBankManager(_mock_conn(cursor))
    result = manager.select_items("my-bank", 4, policy=BankSelectionPolicy(seed=42))

    assert [it.item_id for it in result.selected] == ["item-x4"]
    reasons = {exc.item_id: exc.reason for exc in result.exclusions}
    assert reasons == {
        "item-x1": ItemState.HOLDOUT.value,
        "item-x2": ItemState.FLAGGED.value,
        "item-x3": ItemState.RETIRED.value,
    }


def test_select_items_reports_underfill_and_disclosure() -> None:
    cursor = MagicMock()
    cursor.fetchone.side_effect = [("v1",), None]
    # only one eligible item, four requested
    cursor.fetchall.return_value = [
        ("item-a", "easy", "active", 0, 0, None),
        ("item-b", "easy", "holdout", 0, 0, None),
    ]

    manager = BenchmarkBankManager(_mock_conn(cursor))
    result = manager.select_items("my-bank", 4, policy=BankSelectionPolicy(seed=42))

    assert len(result.selected) == 1
    assert any("underfill" in c or "available" in c for c in result.caveats)
    report = result.to_report_dict()
    # machine-readable disclosure
    assert report["bank_code"] == "my-bank"
    assert report["bank_version"] == "v1"
    assert report["requested_count"] == 4
    assert isinstance(report["contamination_method"], list) and report["contamination_method"]
    assert any(e["item_id"] == "item-b" for e in report["exclusions"])
    assert report["selected"][0]["stratum"] == "easy"
    assert set(report["selected"][0]) >= {
        "item_id",
        "bank_version",
        "stratum",
        "exposure_count",
        "unique_models_exposed",
        "last_exposed_at",
        "contamination_risk",
        "is_safe",
        "state",
    }


def test_select_items_unknown_bank_raises() -> None:
    cursor = MagicMock()
    cursor.fetchone.return_value = None  # no version row

    manager = BenchmarkBankManager(_mock_conn(cursor))
    with pytest.raises(ValueError):
        manager.select_items("ghost-bank", 2, policy=BankSelectionPolicy())


# ---------------------------------------------------------------------------
# Reporting disclosure — experiment manifests carry the bank identity
# ---------------------------------------------------------------------------


def test_experiment_manifest_discloses_bank_identity() -> None:
    manifest = ExperimentManifest.create(
        seed=42,
        model_ids=["prov/model"],
        batteries=[BenchmarkCategory.reasoning],
        code_revision="abc123",
        bank={
            "bank_code": "reasoning",
            "bank_version": "v1",
            "strata": 2,
        },
    )
    assert manifest.payload["bank"] == {
        "bank_code": "reasoning",
        "bank_version": "v1",
        "strata": 2,
    }
    # digest is stable for the same payload
    clone = ExperimentManifest.create(
        seed=42,
        model_ids=["prov/model"],
        batteries=[BenchmarkCategory.reasoning],
        code_revision="abc123",
        bank={
            "bank_code": "reasoning",
            "bank_version": "v1",
            "strata": 2,
        },
    )
    assert clone.digest == manifest.digest


def test_experiment_manifest_omits_bank_when_absent() -> None:
    manifest = ExperimentManifest.create(
        seed=1,
        model_ids=["m"],
        batteries=[BenchmarkCategory.speed],
        code_revision="rev",
    )
    assert "bank" not in manifest.payload


# ---------------------------------------------------------------------------
# livebench stratum helper
# ---------------------------------------------------------------------------


def test_battery_item_stratum_maps_code_gen_labels_to_problems() -> None:
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.00") == "sliding_window_median"
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.07") == "sliding_window_median"
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.08") == "burst_balloons"
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.10") == "burst_balloons"
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.11") == "count_inversions"
    assert battery_item_stratum(BenchmarkCategory.code_gen, "test.12") == "perf_gate"


def test_battery_item_stratum_falls_back_to_battery() -> None:
    assert battery_item_stratum(BenchmarkCategory.reasoning, "q1") == "reasoning"
    assert battery_item_stratum(BenchmarkCategory.speed, "speed") == "speed"


def test_battery_strata_partitions_all_labels() -> None:
    for battery in LIVEBENCH_BATTERIES:
        strata = battery_strata(battery)
        labels = [l for group in strata.values() for l in group]
        assert len(labels) == len(set(labels))
        if battery is BenchmarkCategory.code_gen:
            assert set(strata) == {
                "sliding_window_median",
                "burst_balloons",
                "count_inversions",
                "perf_gate",
            }
            assert len(strata["sliding_window_median"]) == 8
            assert len(strata["burst_balloons"]) == 3


# ---------------------------------------------------------------------------
# Live-schema coverage (scratch PostgreSQL)
#
# Scope note: the ``@pytest.mark.db``/``@pytest.mark.integration`` markers
# MUST stay on this class only — the offline unit tests above are plain.
# ---------------------------------------------------------------------------

MODEL_A = "prov-a/model-a"
MODEL_B = "prov-a/model-b"


def _seed_item(
    conn: psycopg2.extensions.connection, item_id: str, *, meta: dict[str, object]
) -> None:
    seed_item_pool(conn, item_id, domain="general", meta=meta)


def _policy(max_exposures: int = 3) -> BankSelectionPolicy:
    return BankSelectionPolicy(max_exposures=max_exposures, seed=42)


class TestLiveBank:
    """Live-schema bank governance: selection, exclusions, durability.

    Runs only when ``HR_TEST_PG_DSN`` is configured; skips offline via the
    shared ``scratch_db`` fixture.
    """

    pytestmark = [pytest.mark.db, pytest.mark.integration]

    @pytest.fixture(scope="module")
    def bank_conn(self, scratch_db: tuple[str, str]) -> psycopg2.extensions.connection:
        _dbname, dsn = scratch_db
        conn = connect(dsn)
        yield conn
        conn.close()

    @pytest.fixture(scope="module")
    def seeded_bank(self, bank_conn: psycopg2.extensions.connection) -> str:
        """A bank whose items cover every state plus one overexposed item.

        Returns the battery code.
        """
        seed_provider_models(bank_conn, (MODEL_A, MODEL_B))
        seed_control_model(bank_conn, "prov-a", MODEL_A)
        seed_seat(bank_conn, "oracle", "High-IQ consultant")
        seed_sweep(bank_conn, "sw-t5", seat_code="oracle")
        battery = seed_battery(bank_conn, "t5bank")
        _seed_item(bank_conn, "t5-a1", meta={"difficulty": "easy"})
        _seed_item(bank_conn, "t5-a2", meta={"difficulty": "easy", "holdout": True})
        _seed_item(bank_conn, "t5-a3", meta={"difficulty": "easy", "state": "flagged"})
        _seed_item(bank_conn, "t5-a4", meta={"difficulty": "easy", "state": "retired"})
        _seed_item(bank_conn, "t5-a5", meta={"difficulty": "easy"})
        _seed_item(bank_conn, "t5-a6", meta={"difficulty": "hard"})
        for position, item_id in enumerate(
            ("t5-a1", "t5-a2", "t5-a3", "t5-a4", "t5-a5", "t5-a6")
        ):
            seed_battery_item(bank_conn, battery, item_id, position)
        # t5-a1 is overexposed: 3 exposures across 2 runs / 2 models
        seed_run(bank_conn, "run-t5-1", "sw-t5", MODEL_A, battery)
        seed_run(bank_conn, "run-t5-2", "sw-t5", MODEL_B, battery)
        seed_measurement(bank_conn, "meas-t5a1a", "run-t5-1", "t5-a1", repetition=1, score=0.8)
        seed_measurement(bank_conn, "meas-t5a1b", "run-t5-1", "t5-a1", repetition=2, score=0.7)
        seed_measurement(bank_conn, "meas-t5a1c", "run-t5-2", "t5-a1", repetition=1, score=0.8)
        # t5-a5 is lightly exposed (1 exposure, 1 model) — still safe at cap 3
        seed_measurement(bank_conn, "meas-t5a5", "run-t5-1", "t5-a5", repetition=1, score=0.6)
        return "t5bank"

    def test_live_select_items_stratified_traceable(
        self, bank_conn: psycopg2.extensions.connection, seeded_bank: str
    ) -> None:
        manager = BenchmarkBankManager(bank_conn)
        result = manager.select_items(seeded_bank, 2, policy=_policy())

        # the only eligible items are active t5-a5 (easy) and t5-a6 (hard)
        assert result.bank_version == "v1"
        assert sorted(it.item_id for it in result.selected) == ["t5-a5", "t5-a6"]
        by_id = {it.item_id: it for it in result.selected}
        # traceability: exposure recomputed from hr.measurement
        a5 = by_id["t5-a5"]
        assert a5.exposure_count == 1
        assert a5.unique_models_exposed == 1
        assert a5.contamination_risk == pytest.approx(0.01)
        assert a5.last_exposed_at is not None
        assert a5.is_safe is True
        assert a5.state is ItemState.ACTIVE
        a6 = by_id["t5-a6"]
        assert a6.exposure_count == 0
        assert a6.unique_models_exposed == 0
        assert a6.contamination_risk == 0.0

    def test_live_select_items_deterministic_double_run(
        self, bank_conn: psycopg2.extensions.connection, seeded_bank: str
    ) -> None:
        manager = BenchmarkBankManager(bank_conn)
        first = manager.select_items(seeded_bank, 2, policy=_policy())
        second = manager.select_items(seeded_bank, 2, policy=_policy())
        assert [it.item_id for it in first.selected] == [it.item_id for it in second.selected]
        assert first.to_report_dict() == second.to_report_dict()

    def test_live_select_items_excludes_every_non_active_state(
        self, bank_conn: psycopg2.extensions.connection, seeded_bank: str
    ) -> None:
        manager = BenchmarkBankManager(bank_conn)
        result = manager.select_items(seeded_bank, 6, policy=_policy())

        reasons = {exc.item_id: exc.reason for exc in result.exclusions}
        assert set(reasons) == {"t5-a1", "t5-a2", "t5-a3", "t5-a4"}
        assert reasons["t5-a1"] == "overexposed"
        assert reasons["t5-a2"] == ItemState.HOLDOUT.value
        assert reasons["t5-a3"] == ItemState.FLAGGED.value
        assert reasons["t5-a4"] == ItemState.RETIRED.value
        # overexposed items are excluded for exposure, not for their state
        assert any(e.item_id == "t5-a1" for e in result.exclusions)

    def test_live_get_item_exposure_recomputes_from_measurements(
        self, bank_conn: psycopg2.extensions.connection, seeded_bank: str
    ) -> None:
        manager = BenchmarkBankManager(bank_conn)
        exposed = manager.get_item_exposure("t5-a1")
        assert exposed.total_exposures == 3
        assert exposed.unique_models_exposed == 2
        assert exposed.contamination_risk == pytest.approx(0.03)
        assert not exposed.is_safe_for_evaluation(max_exposures=3)
        clean = manager.get_item_exposure("t5-a6")
        assert clean.total_exposures == 0
        assert clean.contamination_risk == 0.0


    def test_live_set_item_state_is_durable_and_syncs_holdout_key(
        self, bank_conn: psycopg2.extensions.connection
    ) -> None:
        """State writes round-trip and a holdout never enters evaluation.

        Uses its own throwaway bank so the module-scoped ``seeded_bank``
        fixture stays pristine for the other tests.
        """
        battery = seed_battery(bank_conn, "t5bank2")
        _seed_item(bank_conn, "t5-c1", meta={"difficulty": "easy"})
        _seed_item(bank_conn, "t5-c2", meta={"difficulty": "easy"})
        seed_battery_item(bank_conn, battery, "t5-c1", 0)
        seed_battery_item(bank_conn, battery, "t5-c2", 1)
        manager = BenchmarkBankManager(bank_conn)

        # default state is active
        assert manager.get_item_state("t5-c1") is ItemState.ACTIVE

        assert manager.set_item_state("t5-c1", ItemState.RETIRED) == 1
        assert manager.get_item_state("t5-c1") is ItemState.RETIRED
        with bank_conn.cursor() as cur:
            cur.execute(
                "SELECT json_meta->>'holdout' FROM hr.item_pool WHERE item_id = 't5-c1'"
            )
            assert cur.fetchone()[0] == "false"

        assert manager.set_item_state("t5-c2", ItemState.HOLDOUT) == 1
        assert manager.get_item_state("t5-c2") is ItemState.HOLDOUT
        with bank_conn.cursor() as cur:
            cur.execute(
                "SELECT json_meta->>'holdout' FROM hr.item_pool WHERE item_id = 't5-c2'"
            )
            assert cur.fetchone()[0] == "true"

        # a holdout item can never enter normal evaluation
        result = manager.select_items("t5bank2", 3, policy=BankSelectionPolicy(seed=42))
        assert [it.item_id for it in result.selected] == []
        reasons = {e.item_id: e.reason for e in result.exclusions}
        assert reasons == {
            "t5-c1": ItemState.RETIRED.value,
            "t5-c2": ItemState.HOLDOUT.value,
        }