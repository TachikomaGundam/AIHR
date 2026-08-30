from __future__ import annotations

import json
import re
from typing import Any

from hr.graders.base import GraderError, ModelResponse

_TOKEN_RE = re.compile(r"\[\s*(-?\d+|\*|'[^']*'|\"[^\"]*\")\s*\]")


def _parse_path(expression: str) -> list[tuple[str, str | int | None]]:
    if not expression.startswith("$"):
        raise GraderError(f"jsonpath must start with '$': {expression!r}")
    rest = expression[1:].lstrip(".")
    segments: list[str] = []
    depth = 0
    current = ""
    for char in rest:
        if char == "[":
            depth += 1
            current += char
        elif char == "]":
            depth -= 1
            current += char
        elif char == "." and depth == 0:
            if current:
                segments.append(current)
            current = ""
        else:
            current += char
    if current:
        segments.append(current)

    steps: list[tuple[str, str | int | None]] = []
    for segment in segments:
        head = segment
        brackets: list[str] = []
        match = _TOKEN_RE.search(segment)
        while match:
            brackets.append(match.group(1))
            head = segment[: match.start()]
            segment = segment[match.end() :]
            match = _TOKEN_RE.search(segment)
        if head:
            steps.append(("field", head))
        for bracket in brackets:
            bracket = bracket.strip()
            if bracket == "*":
                steps.append(("star", None))
            elif bracket.startswith(("'", '"')):
                steps.append(("field", bracket[1:-1]))
            else:
                try:
                    steps.append(("index", int(bracket)))
                except ValueError as exc:
                    raise GraderError(f"unsupported bracket: [{bracket!r}]") from exc
    return steps


def _walk(data: Any, steps: list[tuple[str, str | int | None]]) -> list[Any]:
    current: list[Any] = [data]
    for kind, value in steps:
        following: list[Any] = []
        for node in current:
            if kind == "field" and isinstance(node, dict) and value in node:
                following.append(node[value])
            elif kind == "index" and isinstance(node, list) and isinstance(value, int):
                if -len(node) <= value < len(node):
                    following.append(node[value])
            elif kind == "star" and isinstance(node, list):
                following.extend(node)
        current = following
    return current


def _extract_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False)


def _assert_numeric_eq(extracted: list[Any], value: float, tolerance: float) -> bool:
    for candidate in extracted:
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            numbers = [float(candidate)]
        else:
            numbers = [
                float(match.group(0))
                for match in re.finditer(
                    r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", _extract_text(candidate)
                )
            ]
        denominator = max(abs(value), 1e-9)
        if any(abs(number - value) / denominator <= tolerance for number in numbers):
            return True
    return False


def _apply_assert(assertion: dict[str, Any], extracted: list[Any]) -> bool:
    if "numeric_eq" in assertion:
        spec = assertion["numeric_eq"]
        return _assert_numeric_eq(
            extracted, float(spec["value"]), float(spec.get("tolerance", 0.0))
        )
    text = " ".join(_extract_text(value) for value in extracted)
    if "contains_all" in assertion:
        return all(needle in text for needle in assertion["contains_all"])
    if "not_contains" in assertion:
        return all(needle not in text for needle in assertion["not_contains"])
    if "regex_match" in assertion:
        return bool(re.search(str(assertion["regex_match"]), text))
    raise GraderError(f"unknown assertion: {assertion!r}")


def _extract(
    response: ModelResponse,
    item_payload: dict[str, Any],
    extract_spec: dict[str, Any],
) -> list[Any]:
    del item_payload
    jsonpath = extract_spec.get("jsonpath")
    regex = extract_spec.get("regex")
    if jsonpath is not None:
        try:
            parsed: Any = json.loads(response.text.strip())
        except json.JSONDecodeError:
            parsed = {"text": response.text}
        extracted = _walk(parsed, _parse_path(jsonpath))
    else:
        extracted = [response.text]

    if not regex:
        return extracted
    matcher = re.compile(regex)
    return [value for value in extracted if matcher.search(_extract_text(value))]
