"""hr preflight (S3 first-shot gate) - judgment is tested as a pure function;
one @db line fixture exercises the real storage seam and the freshness marker.
Fake-scored cells come from the bench harness's FakeAdapter, not hand-made
rows: the gate must judge what the engine actually produces."""
from __future__ import annotations

from pathlib import Path

import pytest

from hr.bench.engine_results import BenchOutcome, ItemResult
from hr.bench.livebench import battery_item_id
from hr.models import BenchmarkCategory
from hr.preflight import (
    CellView,
    PreflightConfig,
    cell_view,
    judge,
    load_preflight_config,
)

yaml = pytest.importorskip("yaml")


def _cfg(**over: object) -> PreflightConfig:
    base: dict[str, object] = {
        "max_requests": 50,
        "max_total_tokens": 400_000,
        "per_request_max_tokens": 4096,
        "abort_error_rate": 0.4,
        "freshness_hours": 12,
        "default_models": (),
        "default_batteries": ("reasoning", "factuality"),
        "bands": {"reasoning": (40.0, 100.0)},
    }
    base.update(over)
    return PreflightConfig(**base)  # type: ignore[arg-type]


def _cell(model: str = "prov/anchor", battery: str = "reasoning", *, scored: bool = True,
          infra: bool = False, empty: bool = False, score: float = 80.0) -> CellView:
    return CellView(model_id=model, battery=battery, scored=scored, infra=infra,
                    empty_response=empty, score=score)


class TestJudge:
    def test_pass_when_measured_cells_in_band(self) -> None:
        j = judge([_cell(), _cell(battery="factuality")], _cfg())
        assert j.verdict == "pass"
        assert j.summary

    def test_empty_cells_answer_nothing(self) -> None:
        assert judge([], _cfg()).verdict == "cannot-answer"

    def test_g1_flags_empty_response_cells(self) -> None:
        j = judge([_cell(), _cell(empty=True)], _cfg())
        assert j.verdict == "fail"
        assert j.g1_violations == ("prov/anchor/reasoning",) or "prov/anchor/reasoning" in j.g1_violations

    def test_g2_names_score_band_and_never_the_other_way(self) -> None:
        j = judge([_cell(score=12.5)], _cfg(bands={"reasoning": (40.0, 100.0)}))
        assert j.verdict == "fail"
        assert len(j.g2_violations) == 1
        assert "12.5" in j.g2_violations[0] and "40.0" in j.g2_violations[0]

    def test_missing_band_defaults_permissive(self) -> None:
        j = judge([_cell(battery="factuality", score=0.1)], _cfg())
        assert j.verdict == "pass"  # no band pinned -> [0,100]

    def test_infra_share_above_threshold_aborts_with_requeue(self) -> None:
        cells = [_cell(), _cell(model="prov/x", scored=False, infra=True), _cell(model="prov/y", scored=False, infra=True)]
        j = judge(cells, _cfg(abort_error_rate=0.4))
        assert j.verdict == "cannot-answer"
        assert set(j.requeue) == {"prov/x/reasoning", "prov/y/reasoning"}
        assert j.summary

    def test_infra_below_threshold_excluded_from_gates(self) -> None:
        cells = [_cell(), _cell(), _cell(model="prov/x", scored=False, infra=True, empty=True)]
        j = judge(cells, _cfg(abort_error_rate=0.5))
        assert j.verdict == "pass"  # the empty infra cell never impersonates G1

    def test_all_infra_is_cannot_answer(self) -> None:
        j = judge([_cell(scored=False, infra=True)], _cfg(abort_error_rate=0.99))
        assert j.verdict == "cannot-answer"


def _outcome(battery: BenchmarkCategory, model: str, *, status: str, raw: str, text: str) -> BenchOutcome:
    label_items = [ItemResult(label="L1", item_id=battery_item_id(battery, "L1"), score=90.0, passed=True)]
    return BenchOutcome(battery=battery, model_id=model, score=90.0, passed=True,
                        status=status, items=label_items, raw_output=raw, response_text=text)


class TestCellView:
    def test_error_inconclusive_is_infra_not_measurement(self) -> None:
        c = cell_view(_outcome(BenchmarkCategory.reasoning, "m", status="inconclusive",
                               raw="ERROR: HTTP 503", text=""))
        assert not c.scored

    def test_dialect_skip_is_na_not_infra(self) -> None:
        c = cell_view(_outcome(BenchmarkCategory.reasoning, "m", status="not_applicable",
                               raw="SKIP: dialect_unproven (thinking)", text=""))
        assert not c.scored and not c.infra

    def test_na_cells_excluded_from_error_denominator(self) -> None:
        cells = [_cell(), _cell(model="prov/na", scored=False), _cell(model="prov/na2", scored=False)]
        j = judge(cells, _cfg(abort_error_rate=0.4))
        assert j.verdict == "pass"  # two honest capability gaps, zero sickness

    def test_scored_cell_keeps_score_and_text(self) -> None:
        c = cell_view(_outcome(BenchmarkCategory.reasoning, "m", status="scored",
                               raw="", text="answer"))
        assert c.scored and not c.empty_response and c.score == 90.0


class TestConfig:
    def test_defaults_without_files(self, tmp_path: Path) -> None:
        cfg = load_preflight_config(tmp_path / "absent.yaml")
        assert cfg.max_requests == 50 and cfg.abort_error_rate == 0.4

    def test_overlay_wins_per_key_lists_replace(self, tmp_path: Path) -> None:
        (tmp_path / "preflight.yaml").write_text(
            "budget:\n  max_requests: 50\nbatteries: [reasoning, factuality]\nbands: {}\n",
            encoding="utf-8")
        (tmp_path / "preflight.local.yaml").write_text(
            "budget:\n  max_requests: 7\nbatteries: [speed]\nmodels: [prov/a]\nbands:\n  speed: [50, 100]\n",
            encoding="utf-8")
        cfg = load_preflight_config(tmp_path / "preflight.yaml")
        assert cfg.max_requests == 7
        assert cfg.default_batteries == ("speed",)
        assert cfg.default_models == ("prov/a",)
        assert cfg.bands == {"speed": (50.0, 100.0)}

    def test_inverted_band_fails_loud(self, tmp_path: Path) -> None:
        (tmp_path / "preflight.yaml").write_text("bands:\n  reasoning: [80, 20]\n", encoding="utf-8")
        with pytest.raises(ValueError, match="lo .* > hi"):
            load_preflight_config(tmp_path / "preflight.yaml")


class TestOutputCap:
    def test_clamps_oversized_max_output(self) -> None:
        from hr.preflight import with_output_cap

        seen: dict[str, object] = {}

        class Inner:
            def chat(self, model_id, messages, **kwargs):
                seen["max_output"] = kwargs.get("max_output")
                return object()

        adapter = with_output_cap(lambda m: Inner(), 4096)("prov/x")
        adapter.chat("prov/x", [{"role": "user", "content": "hi"}], max_output=16384)
        assert seen["max_output"] == 4096

    def test_small_requests_pass_through(self) -> None:
        from hr.preflight import with_output_cap

        seen: dict[str, object] = {}

        class Inner:
            def chat(self, model_id, messages, **kwargs):
                seen["max_output"] = kwargs.get("max_output")
                return object()

        adapter = with_output_cap(lambda m: Inner(), 4096)("prov/x")
        adapter.chat("prov/x", [{"role": "user", "content": "hi"}], max_output=64)
        assert seen["max_output"] == 64


class TestCliPayload:
    def test_payload_carries_cell_scores(self) -> None:
        from hr.preflight import Budget, CellView, PreflightRun, judge, load_preflight_config

        cells = (
            CellView("m/1", "speed", True, False, False, 0.5),
            CellView("m/1", "reasoning", True, False, False, 0.75),
            CellView("m/1", "factuality", False, True, False, 0.0, "boom"),
        )
        run = PreflightRun(
            judgment=judge(cells, load_preflight_config()),
            sweep_id="s1",
            cells=cells,
            g3_ok=True,
            budget=Budget(3, 300),
        )
        scores = {
            f"{c.model_id}|{c.battery}": (
                round(c.score, 4) if c.scored and c.score is not None else None
            )
            for c in run.cells
        }
        assert scores == {"m/1|speed": 0.5, "m/1|reasoning": 0.75, "m/1|factuality": None}


class TestCellViewEmptiness:
    def test_multi_item_battery_with_item_text_is_not_empty(self) -> None:
        from hr.bench.engine_results import BenchOutcome, ItemResult
        from hr.preflight import cell_view
        from hr.models import BenchmarkCategory

        outcome = BenchOutcome(
            battery=BenchmarkCategory.reasoning,
            model_id="m/1",
            score=92.0,
            passed=False,
            items=[ItemResult(label="q1", item_id="i1", score=100.0, passed=True,
                              response_text="A: 5")],
        )
        assert cell_view(outcome).empty_response is False

    def test_battery_with_no_text_anywhere_is_empty(self) -> None:
        from hr.bench.engine_results import BenchOutcome, ItemResult
        from hr.preflight import cell_view
        from hr.models import BenchmarkCategory

        outcome = BenchOutcome(
            battery=BenchmarkCategory.reasoning,
            model_id="m/1",
            score=0.0,
            passed=False,
            items=[ItemResult(label="q1", item_id="i1", score=0.0, passed=False,
                              response_text="   ")],
        )
        assert cell_view(outcome).empty_response is True


class TestStage0Flag:
    def test_parser_accepts_skip_preflight(self) -> None:
        from hr.stage0_cli import _build_arg_parser

        args = _build_arg_parser().parse_args(["--dry-run"])
        assert args.skip_preflight is False


class TestEngineWiring:
    def test_speed_battery_arrives_clamped_at_inner_adapter(self) -> None:
        """Engine -> capped factory -> inner adapter: the runner's 16384 ask
        must reach the wire as <= the session cap. This is the wiring the
        first live fire silently lacked - unit clamp tests alone passed
        while the CLI never wrapped the factory."""
        from hr.adapters.base import Capabilities
        from hr.bench.engine import LivebenchEngine
        from hr.graders.base import ModelResponse
        from hr.models import BenchmarkCategory
        from hr.preflight import with_output_cap

        seen: list[int] = []

        class Inner:
            def chat(self, model_id, messages, **kwargs):
                seen.append(kwargs.get("max_output") or 0)
                return ModelResponse(
                    text="A: 4", tokens_in=10, tokens_out=5, latency_ms=1,
                    finish_reason="stop",
                )

            def probe_capabilities(self, model_id):
                return Capabilities(model_id=model_id, provider="prov")

        engine = LivebenchEngine(
            adapter_factory=with_output_cap(lambda mid: Inner(), 4096),
            timeout_s=5,
        )
        outcome = engine.run_battery("prov/x", BenchmarkCategory.speed)
        assert seen and all(v <= 4096 for v in seen), seen
        assert outcome.status == "scored"
