"""Tests for graders and the constraint DSL."""
import json

import pytest

from hr.graders.base import GraderError, ModelResponse, build_default_registry
from hr.graders.exact_match import ExactMatchGrader
from hr.graders.constraint import ConstraintGrader, _parse_path, _walk
from hr.graders.schema_valid import SchemaValidGrader
from hr.graders.citation import CitationGrader, _parse_cites
from hr.graders.llm_judge import LLMJudgeGrader, NotConfigured


# ---------------------------------------------------------------------------
# Exact match
# ---------------------------------------------------------------------------
class TestExactMatch:
    def test_plain_string_match(self):
        g = ExactMatchGrader()
        r = g.grade({"answer": "Paris"}, {}, ModelResponse(text="Paris"))
        assert r.score == 1.0
        assert r.passed is True

    def test_whitespace_casefold(self):
        g = ExactMatchGrader()
        r = g.grade({}, {"expected": "HELLO   WORLD"},
                    ModelResponse(text="  hello    world  "))
        assert r.score == 1.0

    def test_numeric_tolerance(self):
        g = ExactMatchGrader()
        r = g.grade({}, {"expected": "1.0", "numeric_tolerance": 0.1},
                    ModelResponse(text="1.05"))
        assert r.passed is True
        r2 = g.grade({}, {"expected": "1.0", "numeric_tolerance": 0.01},
                     ModelResponse(text="1.2"))
        assert r2.passed is False

    def test_mismatch_zero(self):
        g = ExactMatchGrader()
        r = g.grade({}, {"expected": "Paris"}, ModelResponse(text="London"))
        assert r.score == 0.0
        assert r.passed is False

    def test_verifiable_answer_from_payload(self):
        g = ExactMatchGrader()
        r = g.grade(
            {"verifiable_answer": "42"},
            {},
            ModelResponse(text="42"),
        )
        assert r.passed is True


# ---------------------------------------------------------------------------
# Constraint DSL
# ---------------------------------------------------------------------------
class TestConstraintDSL:
    def test_jsonpath_minimal(self):
        steps = _parse_path("$.a.b")
        assert [(k, v) for (k, v) in steps] == [("field", "a"), ("field", "b")]

    def test_jsonpath_brackets(self):
        steps = _parse_path("$.items[0].value")
        assert [("field", "items"), ("index", 0), ("field", "value")] == [
            (k, v) for (k, v) in steps
        ]

    def test_jsonpath_walk(self):
        data = {"items": [{"n": 1}, {"n": 2}, {"n": 3}]}
        steps = _parse_path("$.items[1].n")
        assert _walk(data, steps) == [2]

    def test_jsonpath_star(self):
        data = {"items": [{"n": 10}, {"n": 20}]}
        steps = _parse_path("$.items[*].n")
        assert sorted(_walk(data, steps)) == [10, 20]

    def test_constraint_spec_example_appendix_a(self):
        """Sample per 附录A: extract a nested number and numeric_eq check."""
        g = ConstraintGrader()
        # Model returns JSON with {"report": {"score": 42}}.
        response_text = json.dumps({"report": {"score": 42}})
        params = {
            "checks": [
                {
                    "name": "score_42",
                    "extract": {"jsonpath": "$.report.score"},
                    "assert": {"numeric_eq": {"value": 42, "tolerance": 0}},
                    "weight": 1.0,
                },
                {
                    "name": "contains_ok",
                    "extract": {},
                    "assert": {"contains_all": ["report", "score"]},
                    "weight": 1.0,
                },
                {
                    "name": "no_forbidden",
                    "extract": {},
                    "assert": {"not_contains": ["undefined", "null"]},
                    "weight": 1.0,
                },
            ]
        }
        r = g.grade({}, params, ModelResponse(text=response_text))
        assert r.score == 1.0
        details = r.detail["checks"]
        assert all(c["passed"] for c in details)

    def test_constraint_partial_pass(self):
        g = ConstraintGrader()
        params = {
            "checks": [
                {
                    "name": "weight_2_pass",
                    "extract": {},
                    "assert": {"contains_all": ["hello"]},
                    "weight": 2.0,
                },
                {
                    "name": "weight_1_fail",
                    "extract": {},
                    "assert": {"contains_all": ["MISSING"]},
                    "weight": 1.0,
                },
            ]
        }
        r = g.grade({}, params, ModelResponse(text="hello world"))
        # Pass: 2.0, Total: 3.0 → 2/3.
        assert abs(r.score - (2.0 / 3.0)) < 1e-9

    def test_invalid_assertion_is_a_grader_configuration_error(self):
        # Given: a check with an assertion kind the DSL does not implement.
        grader = ConstraintGrader()
        params = {"checks": [{"assert": {"unknown": True}}]}

        # When/Then: configuration failure is not reported as model failure.
        with pytest.raises(GraderError, match="unknown assertion"):
            grader.grade({}, params, ModelResponse(text="answer"))

    def test_regex_match_assert(self):
        g = ConstraintGrader()
        params = {
            "checks": [
                {
                    "name": "has_email",
                    "extract": {},
                    "assert": {"regex_match": r"[a-z]+@[a-z]+\.[a-z]+"},
                    "weight": 1.0,
                }
            ]
        }
        r = g.grade({}, params, ModelResponse(text="contact me at alice@example.com"))
        assert r.score == 1.0


# ---------------------------------------------------------------------------
# Schema valid
# ---------------------------------------------------------------------------
class TestSchemaValid:
    def test_basic_schema(self):
        g = SchemaValidGrader()
        args_schema = {
            "type": "object",
            "required": ["name", "age"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "age": {"type": "integer", "minimum": 0, "maximum": 150},
            },
        }
        payload = {"correct": {"arguments": args_schema}}
        resp = ModelResponse(
            tool_calls=[
                {"function": {"name": "f"}, "arguments": {"name": "Alice", "age": 30}}
            ]
        )
        r = g.grade(payload, {}, resp)
        assert r.passed is True

    def test_schema_missing_required(self):
        g = SchemaValidGrader()
        args_schema = {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string"}},
        }
        payload = {"correct": {"arguments": args_schema}}
        resp = ModelResponse(tool_calls=[{"function": {"name": "f"}, "arguments": {}}])
        r = g.grade(payload, {}, resp)
        assert r.passed is False

    def test_arg_constraints_enum(self):
        g = SchemaValidGrader()
        payload = {
            "arg_constraints": {"color": {"enum": ["red", "blue"]}}
        }
        resp = ModelResponse(
            tool_calls=[{"function": {"name": "f"}, "arguments": {"color": "green"}}]
        )
        r = g.grade(payload, {}, resp)
        assert r.passed is False

    def test_no_tool_call_fails(self):
        g = SchemaValidGrader()
        r = g.grade({}, {}, ModelResponse(text=""))
        assert r.passed is False
        assert r.score == 0.0


# ---------------------------------------------------------------------------
# Citation
# ---------------------------------------------------------------------------
class TestCitation:
    def test_parse_cites_simple(self):
        assert _parse_cites("hello [1] world [3]") == [1, 3]

    def test_parse_cites_range(self):
        assert _parse_cites("see [1-3]") == [1, 2, 3]

    def test_addresses_claims(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        for i in range(1, 4):
            (sources / f"src{i}.txt").write_text(f"source {i}")
        g = CitationGrader(sources_dir=sources)
        payload = {
            "required_claims": ["alpha", "bravo"],
        }
        text = "Here is alpha [1]. And bravo is addressed by [2]."
        r = g.grade(payload, {}, ModelResponse(text=text))
        assert r.score == 1.0
        assert r.passed is True

    def test_missing_claim_zero_score(self, tmp_path):
        sources = tmp_path / "sources"
        sources.mkdir()
        (sources / "src1.txt").write_text("src")
        g = CitationGrader(sources_dir=sources)
        payload = {"required_claims": ["alpha", "bravo", "charlie"]}
        text = "mentions alpha [1] but nothing else"
        r = g.grade(payload, {}, ModelResponse(text=text))
        assert r.score < 1.0


# ---------------------------------------------------------------------------
# LLM judge stub
# ---------------------------------------------------------------------------
class TestLLMJudge:
    def test_raises_when_no_adapter(self):
        g = LLMJudgeGrader()
        with pytest.raises(NotConfigured):
            g.grade({}, {}, ModelResponse(text=""))

    def test_adapter_invoked(self):
        called = {}

        def adapter(*, prompt, payload):
            called["prompt"] = prompt
            return "0.8"

        g = LLMJudgeGrader(judge_adapter=adapter)
        r = g.grade(
            {"question": "What is 2+2?"},
            {"rubric_ref": "R1", "pass_threshold": 0.6},
            ModelResponse(text="4"),
        )
        assert r.score == 0.8
        assert r.passed is True
        assert "R1" in called["prompt"]
        assert "blind:True" in called["prompt"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_default_registry_has_all(self):
        reg = build_default_registry()
        expected = {
            "exact_match",
            "constraint",
            "unit_test",
            "schema_valid",
            "citation",
            "llm_judge",
        }
        assert set(reg.list()) == expected
