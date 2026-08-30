"""Grader surface contract tests (committed shapes).

Covers the four committed graders: exact_match (component/alternative
matching, numeric tolerance, case sensitivity), schema_valid (tool-call
schema + arg_constraints validation), citation (claim coverage + source
validity), and constraint (check-list DSL, weighted, named forms).
Offline and deterministic.
"""

from __future__ import annotations

import pytest

from hr.graders.base import GradeResult, ModelResponse
from hr.graders.citation import CitationGrader, _parse_cites
from hr.graders.constraint import ConstraintGrader
from hr.graders.exact_match import (
    ExactMatchGrader,
    _component_present,
    _split_alternatives,
    _split_required,
)
from hr.graders.schema_valid import SchemaValidGrader


def resp(text: str = "", tool_calls=None) -> ModelResponse:  # noqa: ANN001
    return ModelResponse(text=text, tool_calls=tool_calls or [])


class TestExactMatch:
    def test_expected_from_params(self) -> None:
        gr = ExactMatchGrader().grade({}, {"expected": "42"}, resp("the answer is 42"))
        assert gr.score == 1.0 and gr.passed is True

    def test_expected_from_payload_answer_and_verifiable(self) -> None:
        assert ExactMatchGrader().grade({"answer": "cat"}, {}, resp("cat")).passed
        assert ExactMatchGrader().grade({"verifiable_answer": "dog"}, {}, resp("dog")).passed

    def test_missing_expected_raises(self) -> None:
        with pytest.raises(Exception, match="no expected answer"):
            ExactMatchGrader().grade({}, {}, resp("x"))

    def test_alternatives_any_one(self) -> None:
        assert ExactMatchGrader().grade({"answer": "A / B"}, {}, resp("B")).passed
        assert ExactMatchGrader().grade({"answer": "A / B"}, {}, resp("C")).passed is False

    def test_required_components_all(self) -> None:
        gr = ExactMatchGrader().grade({"answer": "A, B"}, {}, resp("A and B"))
        assert gr.score == 1.0
        partial = ExactMatchGrader().grade({"answer": "A, B"}, {}, resp("A only"))
        assert partial.score == pytest.approx(0.5)
        assert partial.passed is False

    def test_numeric_tolerance(self) -> None:
        gr = ExactMatchGrader().grade(
            {"answer": "100"}, {"numeric_tolerance": 0.05}, resp("about 104")
        )
        assert gr.score == 1.0
        strict = ExactMatchGrader().grade(
            {"answer": "100"}, {"numeric_tolerance": 0.001}, resp("about 104")
        )
        assert strict.score == 0.0

    def test_case_sensitive_mode(self) -> None:
        assert ExactMatchGrader().grade({"answer": "A1"}, {"case_sensitive": True}, resp("a1")).passed is False
        assert ExactMatchGrader().grade({"answer": "A1"}, {"case_sensitive": True}, resp("A1")).passed

    def test_answer_keys_list(self) -> None:
        gr = ExactMatchGrader().grade({"answer_keys": ["x", "z"]}, {}, resp("x z"))
        assert gr.score == 1.0

    def test_splitters(self) -> None:
        assert _split_alternatives("X / Y／Z") == ["X", "Y", "Z"]
        assert _split_required("A、B + C and D 和 E") == ["A", "B", "C", "D", "E"]

    def test_component_present_numeric_and_text(self) -> None:
        assert _component_present("5", "5 apples", "5 apples", 0.0) is True
        assert _component_present("7", "5 apples", "5 apples", 0.0) is False
        assert _component_present("apples", "5 apples", "5 apples", 0.0) is True


class TestSchemaValid:
    SCHEMA = {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a"],
    }

    def test_valid_call(self) -> None:
        r = resp(tool_calls=[{"name": "f", "arguments": '{"a": 1}'}])
        gr = SchemaValidGrader().grade({"correct": {"type": "function", "function": {"parameters": self.SCHEMA}}}, {}, r)
        assert gr.passed is True

    def test_missing_required_field(self) -> None:
        r = resp(tool_calls=[{"name": "f", "arguments": '{}'}])
        gr = SchemaValidGrader().grade({"correct": {"type": "function", "function": {"parameters": self.SCHEMA}}}, {}, r)
        assert gr.passed is False
        assert gr.detail["errors"]

    def test_unparsable_arguments(self) -> None:
        r = resp(tool_calls=[{"name": "f", "arguments": "{oops"}])
        gr = SchemaValidGrader().grade({"correct": {"type": "function", "function": {"parameters": self.SCHEMA}}}, {}, r)
        assert gr.score == 0.0
        assert gr.detail.get("args_parse_error") is True

    def test_no_call_policy(self) -> None:
        gr = SchemaValidGrader().grade({}, {}, resp("no tools here"))
        assert gr.score == 0.0 and gr.passed is False
        gr2 = SchemaValidGrader().grade({}, {"no_call_passes": True}, resp("no tools here"))
        assert gr2.score == 1.0 and gr2.passed is True

    def test_arg_constraints_enum(self) -> None:
        constraints = {"a": {"enum": [1]}}
        r = resp(tool_calls=[{"name": "f", "input": {"a": 2}}])
        gr = SchemaValidGrader().grade({"arg_constraints": constraints}, {}, r)
        assert gr.passed is False
        r2 = resp(tool_calls=[{"name": "f", "input": {"a": 1}}])
        gr2 = SchemaValidGrader().grade({"arg_constraints": constraints}, {}, r2)
        assert gr2.passed is True

    def test_tools_schema_fallback(self) -> None:
        payload = {
            "correct": {"name": "calc"},
            "tools": [{"name": "calc", "schema": {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}}],
        }
        r = resp(tool_calls=[{"name": "calc", "input": {"x": 1.5}}])
        gr = SchemaValidGrader().grade(payload, {}, r)
        assert gr.passed is True
        r2 = resp(tool_calls=[{"name": "calc", "input": {}}])
        gr2 = SchemaValidGrader().grade(payload, {}, r2)
        assert gr2.passed is False


class TestCitation:
    def test_parse_cites_expansion(self) -> None:
        assert _parse_cites("see [1,2,4-6] and [9]") == [1, 2, 4, 5, 6, 9]

    def test_all_claims_addressed_with_valid_sources(self, tmp_path) -> None:
        (tmp_path / "s1.txt").write_text("s", encoding="utf-8")
        (tmp_path / "s2.txt").write_text("s", encoding="utf-8")
        grader = CitationGrader(sources_dir=tmp_path)
        gr = grader.grade({"required_claims": ["NASA"]}, {}, resp("NASA founded [1]"))
        assert gr.score == 1.0 and gr.passed is True

    def test_invalid_citation_penalizes(self, tmp_path) -> None:
        (tmp_path / "s1.txt").write_text("s", encoding="utf-8")
        grader = CitationGrader(sources_dir=tmp_path)
        gr = grader.grade({"required_claims": ["NASA"]}, {}, resp("NASA founded [1] and [9]"))
        assert gr.detail["invalid_cites"] == [9]
        assert gr.score == pytest.approx(0.5)

    def test_missing_claim_text_but_cited(self) -> None:
        grader = CitationGrader()
        gr = grader.grade({"required_claims": ["Mars mission"]}, {}, resp("details [1]"))
        assert gr.score == 1.0

    def test_unaddressed_claim_fails(self) -> None:
        grader = CitationGrader()
        gr = grader.grade({"required_claims": ["wormhole"]}, {}, resp("nothing here"))
        assert gr.score == 0.0

    def test_no_required_claims_full_coverage(self) -> None:
        grader = CitationGrader()
        gr = grader.grade({}, {}, resp("anything [1]"))
        assert gr.score == 1.0


class TestConstraint:
    def test_checks_required(self) -> None:
        with pytest.raises(Exception, match="expects params.checks"):
            ConstraintGrader().grade({}, {}, resp("x"))

    def test_numeric_eq(self) -> None:
        params = {"checks": [{"kind": "numeric_eq", "value": 42}]}
        assert ConstraintGrader().grade({}, params, resp("the number is 42")).passed
        assert not ConstraintGrader().grade({}, params, resp("the number is 7")).passed

    def test_contains_all_and_regex(self) -> None:
        ok = {"checks": [{"kind": "contains_all", "value": ["alpha", "beta"]}]}
        assert ConstraintGrader().grade({}, ok, resp("alpha beta gamma")).passed
        assert not ConstraintGrader().grade({}, ok, resp("alpha only")).passed
        rx = {"checks": [{"kind": "regex_match", "value": r"\d{4}-\d{2}"}]}
        assert ConstraintGrader().grade({}, rx, resp("date 2026-08 ok")).passed

    def test_weighted_checks(self) -> None:
        params = {
            "checks": [
                {"kind": "contains_all", "value": ["a"], "weight": 1.0},
                {"kind": "contains_all", "value": ["b"], "weight": 2.0},
            ]
        }
        gr = ConstraintGrader().grade({}, params, resp("a only"))
        assert gr.score == pytest.approx(1.0 / 3.0)
        assert gr.passed is False

    def test_full_form_check(self) -> None:
        params = {
            "checks": [
                {
                    "name": "has-answer",
                    "extract": {},
                    "assert": {"contains_all": ["yes"]},
                    "weight": 1,
                }
            ]
        }
        gr = ConstraintGrader().grade({}, params, resp("yes indeed"))
        assert gr.passed is True

    def test_named_refusal_check(self) -> None:
        params = {"checks": ["must_not_fabricate"]}
        declining = ConstraintGrader().grade({"verifiable_answer": "42"}, params, resp("无法回答，no evidence"))
        assert declining.passed is True
        asserting = ConstraintGrader().grade({"verifiable_answer": "42"}, params, resp("the answer is 42"))
        assert asserting.passed is False
        acceptable = ConstraintGrader().grade(
            {"verifiable_answer": "42", "acceptable": ["42"]}, params, resp("42")
        )
        assert acceptable.passed is True

    def test_invalid_check_raises(self) -> None:
        with pytest.raises(Exception, match="check must be str or dict"):
            ConstraintGrader().grade({}, {"checks": [7]}, resp("x"))