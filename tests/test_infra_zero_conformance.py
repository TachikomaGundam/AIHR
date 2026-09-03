"""Audit-fix T1 conformance: infra-failed and unscored calls are NOT
score-bearing observations (audit bug 4).

Covers plan acceptance cases (1)-(7):
  (1)/(2) infra failure -> zero ``INSERT INTO hr.measurement``, run row kept,
          infra_incident written, run marked ``inconclusive`` +
          ``infra_failure_during_execution``;
  (3)/(4) ok-but-unscored (no_routing / grader_error) -> zero measurement,
          zero incident, run marked ``inconclusive`` +
          ``unscored_call_during_execution``;
  (5)     an all-unscored round mutates NO stopper state (battery, per-model,
          pair, n_rounds_done);
  (6)     pair feeds are key-aligned on the shared (item_key, rep) set:
          equal-length disjoint rounds change nothing; intersection rounds
          pair only the shared items positionally;
  (7a)    resume rebuilds pair sequences per-model (widened Q1 projection +
          expected_model = expected // max(len(finalists), 1));
  (7b)    resume hole-round rewind re-invokes only the hole items, exactly once.

Hermetic: in-process scripted connections only — no live DB, no network, no
real HOME (tests/conftest.py seals it).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hr.graders import build_default_registry
from hr.items.schema import GradingSpec, ItemEnvelope, ItemMeta
from hr.stage0_call import SingleCallResult
from hr.stage0_loop import _run_sweep_loop
from hr.stage0_stats import SweepState
from hr.stage1 import load_full_banks
from hr.stage1_loop import _run_finals_loop
from hr.stage1_resume import _rebuild_stopper_from_db
from hr.stage1_state import Stage1SweepState
from hr.stats.sequential import SequentialConfig

ITEM_REPO = Path(__file__).resolve().parents[1] / "itemrepo"


# ---------------------------------------------------------------------------
# In-process connection fakes (module-local; the tests/bench scratch_conn
# fixture is not visible from root-level tests)
# ---------------------------------------------------------------------------
class FakeCursor:
    """Records every execute() against its parent FakeConn; ``fetchone``
    serves the kind lookup the measurement writer performs (kind tool_a)."""

    def __init__(self, conn: "FakeConn", kind_row: tuple[str, ...] = ("tool_a",)) -> None:
        self.conn = conn
        self.kind_row = kind_row

    def execute(self, sql: str, params: object = None) -> None:
        self.conn.executed.append((sql, params))

    def fetchone(self) -> tuple[str, ...] | None:
        return self.kind_row

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class FakeConn:
    """Connection stand-in: cursor() context managers + commit counting."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1


class ScriptedCursor:
    """Routes fetchall() by SQL discriminator across the three queries of
    _rebuild_stopper_from_db: Q1 on ``r.round`` ALONE (the widened Q1 also
    projects ``m.item_id, m.repetition``, which would match the Q2 pattern),
    Q2 on ``battery_code, m.item_id``, Q3 on ``COALESCE(SUM``."""

    def __init__(self, conn: "ScriptedConn") -> None:
        self.conn = conn
        self.rows: list[tuple] = []

    def execute(self, sql: str, params: object = None) -> None:
        if "r.round" in sql:
            self.rows = self.conn.q1
        elif "battery_code, m.item_id" in sql:
            self.rows = self.conn.q2
        elif "COALESCE(SUM" in sql:
            self.rows = self.conn.q3
        else:
            raise AssertionError(f"unexpected SQL: {sql!r}")

    def fetchall(self) -> list[tuple]:
        return self.rows

    def __enter__(self) -> "ScriptedCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class ScriptedConn:
    def __init__(
        self, q1: list[tuple], q2: list[tuple], q3: list[tuple]
    ) -> None:
        self.q1 = q1
        self.q2 = q2
        self.q3 = q3

    def cursor(self) -> ScriptedCursor:
        return ScriptedCursor(self)

    def close(self) -> None:
        return None


class RaisingAdapter:
    """Adapter whose chat() raises TimeoutError -> classified infra failure."""

    def probe_capabilities(self, model_id: str) -> object:
        from hr.adapters.base import Capabilities

        return Capabilities(
            model_id=model_id,
            provider="",
            supports_thinking=False,
            supports_vision=False,
        )

    def chat(self, model_id: str, messages: list[dict[str, Any]], **_: Any) -> object:
        raise TimeoutError("call timed out")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _unscored(detail: dict[str, Any] | None = None) -> SingleCallResult:
    """A SingleCallResult whose ``scored`` flag is False (set post-construction
    so the test file itself also runs on the unfixed HEAD where the field does
    not yet exist)."""
    res = SingleCallResult(
        score=0.0,
        passed=False,
        detail=detail if detail is not None else {},
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
    )
    res.scored = False
    return res


def _scored(score: float) -> SingleCallResult:
    res = SingleCallResult(
        score=score,
        passed=True,
        detail={},
        tokens_in=100,
        tokens_out=50,
        latency_ms=10,
    )
    res.scored = True
    return res


class ScriptedCalls:
    """call_and_grade stand-in keyed on (model_id, item_key).

    Outcomes not present in ``rules`` fall back to ok=True/unscored, so tests
    can express "only this model scored only these items".
    """

    def __init__(
        self, rules: dict[tuple[str, str], tuple[bool, SingleCallResult]]
    ) -> None:
        self.rules = rules
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, adapter: object, model_id: str, env: ItemEnvelope, item_repo: Path, registry: object
    ) -> tuple[bool, SingleCallResult]:
        self.calls.append((model_id, env.item_key))
        return self.rules.get((model_id, env.item_key), (True, _unscored()))


def _tool_envelope(item_key: str = "tool_a.calc.001") -> ItemEnvelope:
    return ItemEnvelope(
        item_key=item_key,
        type="tool_a",
        tier=2,
        payload={},
        grading=GradingSpec(grader="exact_match@1.0"),
        meta=ItemMeta(seats=["quick"]),
    )


def _run_stage0(
    *,
    state: SweepState,
    conn: object,
    adapter: object,
    subsets: dict[str, list[ItemEnvelope]],
    token_cap: int = 10_000,
) -> None:
    _run_sweep_loop(
        adapter=adapter,
        item_repo=ITEM_REPO,
        models=("a",),
        subsets=subsets,
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        n_initial=1,
        token_cap=token_cap,
        state=state,
        registry=build_default_registry(),
        conn=conn,
        sweep_id="sweep",
        record_to_db=True,
    )


def _run_stage1(
    *,
    state: Stage1SweepState,
    conn: object,
    adapter: object,
    items: list[ItemEnvelope],
    already_recorded: dict[tuple[str, str, int, str, int], float] | None = None,
    prior_rounds: dict[tuple[str, str], int] | None = None,
    n_max: int = 1,
    token_cap: int = 20_000,
    record_to_db: bool = True,
) -> None:
    _run_finals_loop(
        adapter=adapter,
        item_repo=ITEM_REPO,
        finalists=state.finalists,
        full_banks={"vision": items},
        batteries=("vision",),
        battery_ids={"vision": "battery"},
        seq_config=SequentialConfig(thresholds={"vision": 0.0}, n_initial=1, n_max=n_max),
        token_cap=token_cap,
        state=state,
        registry=build_default_registry(),
        conn=conn,
        sweep_id="sweep",
        record_to_db=record_to_db,
        already_recorded=already_recorded or {},
        prior_rounds=prior_rounds or {},
    )


def _sql_list(conn: FakeConn) -> list[str]:
    return [sql for sql, _ in conn.executed]


# ---------------------------------------------------------------------------
# (1) stage0, infra failure (adapter TimeoutError)
# ---------------------------------------------------------------------------
def test_stage0_infra_failure_writes_no_measurement_row() -> None:
    conn = FakeConn()
    state = SweepState(sweep_id="sweep")

    _run_stage0(
        state=state,
        conn=conn,
        adapter=RaisingAdapter(),
        subsets={"vision": [_tool_envelope()]},
    )

    sqls = _sql_list(conn)
    assert "INSERT INTO hr.run" in " ".join(sqls)  # run row still written
    assert "INSERT INTO hr.infra_incident" in " ".join(sqls)  # incident present
    assert not [s for s in sqls if "INSERT INTO hr.measurement" in s]
    # Run UPDATE carries infra failure: params (finished_at, total_tokens,
    # infra_ok, status, failure_reason, run_id).
    updates = [params for s, params in conn.executed if "UPDATE hr.run" in s]
    assert len(updates) == 1
    assert updates[0][2] is False
    assert updates[0][3] == "inconclusive"
    assert updates[0][4] == "infra_failure_during_execution"
    assert state.measurements_by_model_battery == {}


# ---------------------------------------------------------------------------
# (2) stage1, infra failure (adapter TimeoutError)
# ---------------------------------------------------------------------------
def test_stage1_infra_failure_writes_no_measurement_row() -> None:
    conn = FakeConn()
    state = Stage1SweepState(sweep_id="sweep", finalists=["a"])
    item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]

    _run_stage1(state=state, conn=conn, adapter=RaisingAdapter(), items=[item])

    sqls = _sql_list(conn)
    assert "INSERT INTO hr.run" in " ".join(sqls)
    assert "INSERT INTO hr.infra_incident" in " ".join(sqls)
    assert not [s for s in sqls if "INSERT INTO hr.measurement" in s]
    # Stage-1 UPDATE layout: (total_tokens, infra_ok, finished_at, status,
    # failure_reason, run_id).
    updates = [params for s, params in conn.executed if "UPDATE hr.run" in s]
    assert len(updates) == 1
    assert updates[0][1] is False
    assert updates[0][3] == "inconclusive"
    assert updates[0][4] == "infra_failure_during_execution"
    assert state.measurements_by_model_battery["a|vision"] == {}


# ---------------------------------------------------------------------------
# (3) stage0, ok-but-unscored (no_routing) — no incident, no measurement
# ---------------------------------------------------------------------------
def test_stage0_no_routing_unscored_writes_no_measurement_no_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConn()
    state = SweepState(sweep_id="sweep")
    fake = ScriptedCalls(
        {("a", "tool_a.calc.001"): (True, _unscored({"no_routing": True}))}
    )
    monkeypatch.setattr("hr.stage0_loop.call_and_grade", fake)

    _run_stage0(
        state=state,
        conn=conn,
        adapter=RaisingAdapter(),
        subsets={"vision": [_tool_envelope()]},
    )

    sqls = _sql_list(conn)
    assert "INSERT INTO hr.run" in " ".join(sqls)
    assert not [s for s in sqls if "INSERT INTO hr.measurement" in s]
    assert not [s for s in sqls if "INSERT INTO hr.infra_incident" in s]
    updates = [params for s, params in conn.executed if "UPDATE hr.run" in s]
    assert len(updates) == 1
    assert updates[0][2] is True  # infra_ok untouched by ok-but-unscored
    assert updates[0][3] == "inconclusive"
    assert updates[0][4] == "unscored_call_during_execution"
    assert state.measurements_by_model_battery == {}


# ---------------------------------------------------------------------------
# (4) stage1, ok-but-unscored (grader_error) — no incident, no measurement
# ---------------------------------------------------------------------------
def test_stage1_grader_error_unscored_writes_no_measurement_no_incident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = FakeConn()
    state = Stage1SweepState(sweep_id="sweep", finalists=["a"])
    item = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][0]
    fake = ScriptedCalls(
        {("a", item.item_key): (True, _unscored({"grader_error": "boom"}))}
    )
    monkeypatch.setattr("hr.stage1_loop.call_and_grade", fake)

    _run_stage1(state=state, conn=conn, adapter=RaisingAdapter(), items=[item])

    sqls = _sql_list(conn)
    assert "INSERT INTO hr.run" in " ".join(sqls)
    assert not [s for s in sqls if "INSERT INTO hr.measurement" in s]
    assert not [s for s in sqls if "INSERT INTO hr.infra_incident" in s]
    updates = [params for s, params in conn.executed if "UPDATE hr.run" in s]
    assert len(updates) == 1
    assert updates[0][1] is True
    assert updates[0][3] == "inconclusive"
    assert updates[0][4] == "unscored_call_during_execution"
    assert state.measurements_by_model_battery["a|vision"] == {}


# ---------------------------------------------------------------------------
# (5) stage1 all-unscored round: no stopper state mutation at all
# ---------------------------------------------------------------------------
def test_stage1_all_unscored_round_leaves_stopper_state_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:1]
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    fake = ScriptedCalls({})  # every call falls back to ok=True/unscored
    monkeypatch.setattr("hr.stage1_loop.call_and_grade", fake)

    _run_stage1(
        state=state,
        conn=None,
        adapter=RaisingAdapter(),
        items=items,
        record_to_db=False,
    )

    assert state.stoppers["vision"].n_rounds == 0
    assert state.model_stoppers["a|vision"].n_rounds == 0
    assert state.model_stoppers["b|vision"].n_rounds == 0
    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 0
    assert pair.n_diffs == 0
    assert pair.decide(model_a="a", model_b="b").status == "indeterminate"
    assert state.n_rounds_done["vision"] == 0
    assert state.measurements_by_model_battery["a|vision"] == {}
    assert state.measurements_by_model_battery["b|vision"] == {}


# ---------------------------------------------------------------------------
# (6) pair-feed key alignment
# ---------------------------------------------------------------------------
def test_stage1_pair_diff_skipped_when_equal_length_disjoint_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:2]
    x, y = items[0].item_key, items[1].item_key
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    # Equal lengths (1 == 1) but disjoint items: a scored X, b scored Y.
    fake = ScriptedCalls(
        {
            ("a", x): (True, _scored(0.9)),
            ("b", y): (True, _scored(0.1)),
        }
    )
    monkeypatch.setattr("hr.stage1_loop.call_and_grade", fake)

    _run_stage1(state=state, conn=None, adapter=RaisingAdapter(), items=items, record_to_db=False)

    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 0  # no shared (item_key, rep) -> nothing fed
    assert pair.n_diffs == 0
    assert pair.decide(model_a="a", model_b="b").status == "indeterminate"


def test_stage1_pair_diff_uses_shared_item_intersection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:2]
    x, y = items[0].item_key, items[1].item_key
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    # a scored X and Y; b scored only X -> exactly ONE diff, taken from X.
    fake = ScriptedCalls(
        {
            ("a", x): (True, _scored(0.9)),
            ("a", y): (True, _scored(0.7)),
            ("b", x): (True, _scored(0.1)),
        }
    )
    monkeypatch.setattr("hr.stage1_loop.call_and_grade", fake)

    _run_stage1(state=state, conn=None, adapter=RaisingAdapter(), items=items, record_to_db=False)

    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 1
    assert pair.n_diffs == 1
    assert pair.effect() == pytest.approx(0.8)  # (0.9 - 0.1) from X only


# ---------------------------------------------------------------------------
# (7a) resume rebuild: widened Q1 projection + per-model expected count
# ---------------------------------------------------------------------------
def test_stage1_resume_rebuilds_pair_sequences_with_expected_model() -> None:
    # Two complete rounds, two finalists, two items -> Q1 rows now project
    # (model_id, battery_code, round, score, item_id, repetition).
    q1 = [
        ("a", "vision", 1, 0.8, "i1", 1), ("a", "vision", 1, 0.7, "i2", 1),
        ("b", "vision", 1, 0.6, "i1", 1), ("b", "vision", 1, 0.5, "i2", 1),
        ("a", "vision", 2, 0.75, "i1", 1), ("a", "vision", 2, 0.65, "i2", 1),
        ("b", "vision", 2, 0.55, "i1", 1), ("b", "vision", 2, 0.45, "i2", 1),
    ]
    q2 = [  # (model_id, battery_code, item_id, repetition, score)
        ("a", "vision", "i1", 1, 0.8), ("a", "vision", "i2", 1, 0.7),
        ("b", "vision", "i1", 1, 0.6), ("b", "vision", "i2", 1, 0.5),
        ("a", "vision", "i1", 1, 0.75), ("a", "vision", "i2", 1, 0.65),
        ("b", "vision", "i1", 1, 0.55), ("b", "vision", "i2", 1, 0.45),
    ]
    connection = ScriptedConn(q1=q1, q2=q2, q3=[(1000, 8)])
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    cfg = SequentialConfig(
        thresholds={"vision": 0.0},
        n_initial=1,
        n_max=2,
        family_alpha=0.05,
        min_effect={"vision": 0.05},
    )

    _rebuild_stopper_from_db(
        state, connection, "sweep", ("vision",), cfg, expected_measurements={"vision": 4}
    )

    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 2  # per-model expected: 4 // max(2, 1) == 2
    assert pair.n_diffs == 4
    assert pair.effect() == pytest.approx(0.2)
    assert pair.min_effect == 0.05
    assert pair.max_rounds == 2
    assert pair.decide(model_a="a", model_b="b").winner is None
    assert state.stoppers["vision"].n_rounds == 2
    assert state.model_stoppers["a|vision"].n_rounds == 2
    assert state.model_stoppers["b|vision"].n_rounds == 2
    assert state.n_rounds_done["vision"] == 2
    assert state.total_tokens == 1000
    assert state.total_calls == 8


# ---------------------------------------------------------------------------
# (7b) resume hole round: rewind + exactly-once re-invocation of hole items
# ---------------------------------------------------------------------------
def test_stage1_resume_hole_round_rewinds_and_reinvokes_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = load_full_banks(ITEM_REPO, batteries=("vision",))["vision"][:2]
    x, y = items[0].item_key, items[1].item_key
    state = Stage1SweepState(sweep_id="sweep", finalists=["a", "b"])
    # Rounds 1-2 complete; round 3 has a hole: model a never scored item y.
    already_recorded: dict[tuple[str, str, int, str, int], float] = {}
    for round_num, (ax, ay, bx, by) in enumerate(
        [
            (0.8, 0.7, 0.6, 0.5),
            (0.75, 0.65, 0.55, 0.45),
            (0.8, None, 0.4, 0.3),  # round 3: hole at (a, y)
        ],
        start=1,
    ):
        already_recorded[("a", "vision", round_num, x, 1)] = ax
        if ay is not None:
            already_recorded[("a", "vision", round_num, y, 2)] = ay  # items enumerate 1-indexed
        already_recorded[("b", "vision", round_num, x, 1)] = bx
        already_recorded[("b", "vision", round_num, y, 2)] = by
    prior_rounds = {("a", "vision"): 3, ("b", "vision"): 3}

    fake = ScriptedCalls({("a", y): (True, _scored(0.7))})  # only the hole item runs
    monkeypatch.setattr("hr.stage1_loop.call_and_grade", fake)

    _run_stage1(
        state=state,
        conn=None,
        adapter=RaisingAdapter(),
        items=items,
        already_recorded=already_recorded,
        prior_rounds=prior_rounds,
        n_max=3,
        record_to_db=False,
    )

    # next_round rewound to 3 (the hole round); only (a, y) is re-invoked,
    # exactly once; every already-recorded item is reused, never re-called.
    assert fake.calls == [("a", y)]
    # Round 3 feeds one complete pair round from the shared set {x, y}:
    # a: x reused (0.8) + y fresh (0.7); b: x, y reused (0.4, 0.3).
    pair = state.pair_stoppers["a|b|vision"]
    assert pair.n_rounds == 1
    assert pair.n_diffs == 2
    assert pair.effect() == pytest.approx(0.4)  # (0.8-0.4 + 0.7-0.3) / 2