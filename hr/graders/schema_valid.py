"""JSON Schema subset and argument constraints.

Implements a minimal JSON Schema validator for tool_a payloads:
  - type (string/number/integer/boolean/object/array)
  - properties / required
  - enum
  - pattern (string)
  - minimum/maximum (number)
  - minLength/maxLength (string)
  - items (object-only for this subset)
  - additionalProperties: not supported (always allow extra)

This module does NOT use external libraries (e.g. jsonschema). It handles the
subset required by spec §5.3 tool_a. For more exotic needs, fall back to
llm_judge.
"""

from __future__ import annotations

import re
from typing import Any

from hr.graders.base import (
    GradeResult,
    Grader,
    GraderError,
    ModelResponse,
)


# ---------------------------------------------------------------------------
# JSON Schema subset
# ---------------------------------------------------------------------------
def _validate_value(value: Any, schema: dict[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if not schema:
        return errors

    expected_type = schema.get("type")
    if expected_type:
        if not _matches_type(value, expected_type):
            errors.append(f"{path}: expected type {expected_type}, got {value!r}")
            return errors

    if expected_type == "string" or isinstance(value, str):
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{path}: value {value!r} not in enum {schema['enum']}")
        if "pattern" in schema:
            if not re.search(schema["pattern"], str(value)):
                errors.append(
                    f"{path}: pattern {schema['pattern']!r} missed (value={value!r})"
                )
        if "minLength" in schema and len(str(value)) < int(schema["minLength"]):
            errors.append(f"{path}: minLength {schema['minLength']}")
        if "maxLength" in schema and len(str(value)) > int(schema["maxLength"]):
            errors.append(f"{path}: maxLength {schema['maxLength']}")

    if expected_type in ("number", "integer") or isinstance(value, (int, float)):
        if isinstance(value, bool):
            pass
        else:
            if "enum" in schema and value not in schema["enum"]:
                errors.append(f"{path}: {value!r} not in enum {schema['enum']}")
            if "minimum" in schema and float(value) < float(schema["minimum"]):
                errors.append(f"{path}: below minimum {schema['minimum']}")
            if "maximum" in schema and float(value) > float(schema["maximum"]):
                errors.append(f"{path}: above maximum {schema['maximum']}")

    if expected_type in ("object", "array") or isinstance(value, (dict, list)):
        if isinstance(value, dict):
            for req in schema.get("required", []):
                if req not in value:
                    errors.append(f"{path}: missing required key {req!r}")
            for k, sub in schema.get("properties", {}).items():
                if k in value:
                    errors.extend(_validate_value(value[k], sub, f"{path}.{k}"))
        if isinstance(value, list):
            items_schema = schema.get("items")
            if items_schema:
                for i, v in enumerate(value):
                    errors.extend(
                        _validate_value(v, items_schema, f"{path}[{i}]")
                    )

    return errors


def _matches_type(value: Any, type_name: str) -> bool:
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "null":
        return value is None
    return True  # unknown type → permissive


# ---------------------------------------------------------------------------
# Tool-call extraction
# ---------------------------------------------------------------------------
def _primary_tool_call(response: ModelResponse) -> dict[str, Any] | None:
    if not response.tool_calls:
        return None
    return response.tool_calls[0]


def _validate_tool_call(
    args: dict[str, Any],
    schema: dict[str, Any],
    arg_constraints: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    if schema:
        errors.extend(_validate_value(args, schema, "$"))
    for arg_name, cons in (arg_constraints or {}).items():
        if arg_name not in args:
            if cons.get("required"):
                errors.append(f"$.{arg_name}: missing required arg")
            continue
        val = args[arg_name]
        if "enum" in cons and val not in cons["enum"]:
            errors.append(f"$.{arg_name}: not in enum {cons['enum']}")
        if "pattern" in cons:
            if not re.search(cons["pattern"], str(val)):
                errors.append(
                    f"$.{arg_name}: pattern {cons['pattern']!r} missed "
                    f"(value={val!r})"
                )
        if cons.get("min_value") is not None and isinstance(val, (int, float)):
            if float(val) < float(cons["min_value"]):
                errors.append(f"$.{arg_name}: below {cons['min_value']}")
        if cons.get("max_value") is not None and isinstance(val, (int, float)):
            if float(val) > float(cons["max_value"]):
                errors.append(f"$.{arg_name}: above {cons['max_value']}")
    return errors


class SchemaValidGrader:
    """Verify a tool call's args against JSON Schema + arg_constraints."""

    name = "schema_valid"
    version = "1.0"

    def grade(
        self,
        item_payload: dict[str, Any],
        grading_params: dict[str, Any],
        response: ModelResponse,
    ) -> GradeResult:
        expected = item_payload.get("correct") or {}
        args_schema = (
            expected.get("arguments") if isinstance(expected, dict) else None
        ) or (item_payload.get("args_schema") or {})
        if isinstance(expected, dict) and expected.get("type") == "function":
            args_schema = (
                expected.get("function", {}).get("parameters") or args_schema
            )

        # Tool repo shape: ``correct = {name, arg_constraints}`` (no nested
        # ``arguments`` schema). Fall back to top-level arg_constraints, and
        # resolve args_schema from ``payload.tools[]`` by matching the
        # expected tool name.
        arg_constraints = item_payload.get("arg_constraints") or {}
        if isinstance(expected, dict) and not arg_constraints:
            ac = expected.get("arg_constraints")
            if isinstance(ac, dict):
                arg_constraints = ac
        if arg_constraints:
            arg_constraints = {
                k: (v if isinstance(v, dict) else v.model_dump())
                for k, v in arg_constraints.items()
            }

        call = _primary_tool_call(response)
        if call is None:
            no_call = bool(grading_params.get("no_call_passes", False))
            return GradeResult(
                score=1.0 if no_call else 0.0,
                passed=bool(no_call),
                detail={"no_tool_call": True},
            )

        # Accept both OpenAI-style ``{function: {name, arguments}}`` and
        # Anthropic-style ``{name, input}`` tool-call shapes.
        if isinstance(call, dict):
            args = (
                call.get("arguments")
                or call.get("input")
                or {}
            )
            fn_name = (
                (call.get("function") or {}).get("name")
                or call.get("name")
                or None
            )
        else:
            args = {}
            fn_name = None

        if isinstance(args, str):
            import json as _json
            try:
                args = _json.loads(args)
            except Exception:
                return GradeResult(
                    score=0.0,
                    passed=False,
                    detail={"args_parse_error": True},
                )

        if not args_schema and isinstance(expected, dict):
            expected_name = expected.get("name")
            for t in (item_payload.get("tools") or []):
                if isinstance(t, dict) and t.get("name") == expected_name:
                    s = t.get("schema")
                    if isinstance(s, dict):
                        args_schema = s
                    break

        errors = _validate_tool_call(args, args_schema, arg_constraints)
        score = 1.0 if not errors else 0.0
        return GradeResult(
            score=score,
            passed=not errors,
            detail={
                "errors": errors,
                "args": args,
                "function": fn_name,
            },
        )


__all__ = ["SchemaValidGrader", "_validate_value", "_matches_type"]

# Silence warning.
_ = (Grader, GraderError)
