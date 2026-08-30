from __future__ import annotations

from tests.test_calibrate import (
    FakeAdapter,
    ITEM_REPO,
    _CalibrationConnection,
    _CalibrationDatabase
)


def test_calibration_pool_hash_uses_canonical_digest_order() -> None:
    from hr.calibrate import load_item_repo
    from hr.calibration_items import _compute_pool_hash
    from hr.items.loader import pool_hash

    # Given: a real item pool whose path order differs from digest order.
    items = load_item_repo(ITEM_REPO, batteries=["vision"])["vision"]
    expected = pool_hash(
        [item.content_hash or item.compute_content_hash() for item in items]
    )

    # When/Then: calibration identifies the pool by the canonical invariant.
    assert _compute_pool_hash(ITEM_REPO, ["vision"]) == expected

def test_budget_guard_stops_at_cap() -> None:
    from hr.calibrate import CalibrationRunner

    fake = FakeAdapter(
        canned_text="x",
        canned_tokens_in=1_000_000,
        canned_tokens_out=500_000,
    )
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["vision"],  # 15 items -> each call = 1.5M tokens
        token_cap=3_500_000,
    )
    report = runner.run()
    # After 2 calls (~3M) we'd approach the cap. After the 3rd call we'd
    # exceed it. The runner should stop before processing all 15 items.
    assert report.stopped_at_cap is True
    assert len(report.measurements) < 15
    # The total tokens in+out should be near (but >=) the cap at the
    # moment it stopped — not wildly more than cap + one call.
    total = report.total_tokens_in + report.total_tokens_out
    assert total >= 3_500_000
    assert total < 3_500_000 + 2_000_000

def test_resume_skips_recorded_pairs() -> None:
    from hr.calibrate import CalibrationRunner

    fake = FakeAdapter(canned_text="x", canned_tokens_in=10, canned_tokens_out=10)
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "bailian-token-plan/deepseek-v4-flash"},
        batteries=["vision"],
        resume=True,
    )
    loaded = __import__("hr.calibrate", fromlist=["load_item_repo"]).load_item_repo(
        ITEM_REPO, batteries=["vision"]
    )["vision"]
    runner._recorded_pairs = {
        ("cheap", env.item_key)
        for env in loaded[:10]
    }
    report = runner.run()
    assert len(report.measurements) == len(loaded) - 10

def test_resume_reads_canonical_calibration_events() -> None:
    from hr.calibrate import CalibrationRunner

    # Given: a persisted anchor measurement for the current pool.
    connection = _CalibrationConnection(
        [
            (
                "provider/model",
                "item-1",
                "reasoning",
                1,
                "reasoning",
                1.0,
                True,
                10,
                5,
                12,
                None,
                {},
            )
        ]
    )
    runner = CalibrationRunner(
        adapter=FakeAdapter(),
        item_repo=ITEM_REPO,
        db=_CalibrationDatabase(connection),
        pool_hash="pool-1",
        resume=True,
    )

    # When: resume state is loaded.
    runner._load_recorded_pairs()

    # Then: it reads calibration events rather than nonexistent run columns.
    sql, params = connection.cursor_.executed[0]
    assert "FROM hr.calibration_event" in sql
    assert "JOIN hr.run" not in sql
    assert params == ("pool-1",)
    assert runner._recorded("provider/model", "item-1")

def test_resume_restores_complete_measurements_by_anchor_key() -> None:
    from hr.calibrate import CalibrationRunner, load_item_repo

    # Given: one complete event persisted under the configured anchor key.
    first = load_item_repo(ITEM_REPO, batteries=["vision"])["vision"][0]
    connection = _CalibrationConnection(
        [
            (
                "cheap",
                first.item_key,
                "vision",
                first.tier,
                first.type.value,
                1.0,
                True,
                10,
                5,
                12,
                None,
                {},
            )
        ]
    )
    fake = FakeAdapter(canned_text="x", canned_tokens_in=2, canned_tokens_out=1)
    runner = CalibrationRunner(
        adapter=fake,
        item_repo=ITEM_REPO,
        anchors={"cheap": "provider/model"},
        batteries=["vision"],
        db=_CalibrationDatabase(connection),
        pool_hash="pool-1",
        resume=True,
    )

    # When: calibration resumes the same pool.
    report = runner.run()

    # Then: the event is not called again and remains in report accounting.
    assert len(fake.call_log) == len(report.measurements) - 1
    assert report.measurements[0].item_key == first.item_key
    assert report.total_tokens_in >= 10

def test_persist_writes_a_complete_calibration_event() -> None:
    from hr.calibrate import CalibrationReport, CalibrationRunner, Measurement

    # Given: one completed anchor measurement.
    connection = _CalibrationConnection()
    runner = CalibrationRunner(
        adapter=FakeAdapter(),
        item_repo=ITEM_REPO,
        db=_CalibrationDatabase(connection),
        pool_hash="pool-1",
    )
    report = CalibrationReport(
        pool_hash="pool-1",
        measurements=[
            Measurement(
                anchor="provider/model",
                item_key="item-1",
                battery="reasoning",
                tier=1,
                item_type="reasoning",
                score=0.75,
                passed=True,
                latency_ms=12,
                tokens_in=10,
                tokens_out=5,
            )
        ],
        verdicts=[],
        stopped_at_cap=False,
        total_tokens_in=10,
        total_tokens_out=5,
    )

    # When: the report is persisted.
    runner._persist(report)

    # Then: required audit fields and resume fields are written atomically.
    sql, params = connection.cursor_.executed[0]
    assert "event_id" in sql
    assert "item_id" in sql
    assert "kind" in sql
    assert "evidence_json" in sql
    assert "pool_hash" in sql
    assert params[1:4] == ("item-1", "anchor_measurement", "pool-1")
    assert connection.commits == 1
