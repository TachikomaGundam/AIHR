from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Literal

BenchmarkStatus = Literal["scored", "inconclusive", "not_applicable"]

@dataclass(frozen=True)
class _BenchmarkOutcome:
    score: float
    passed: bool
    latency_ms: int | None = None
    tokens_per_sec: float | None = None
    raw_output: str = ""
    #: Per-graded-unit (label, passed) breakdown, in registry label order.
    item_scores: list[tuple[str, bool]] | None = None
    status: BenchmarkStatus = "scored"


# ---------------------------------------------------------------------------
# Numeric parsing helpers
# ---------------------------------------------------------------------------


def _parse_number(s: str) -> float | None:
    """Parse a numeric string; handles fractions like '1/2'."""
    s = s.strip()
    if not s:
        return None
    if "/" in s:
        parts = s.split("/", 1)
        try:
            num = float(parts[0].strip())
            den = float(parts[1].strip())
            if den == 0:
                return None
            return num / den
        except ValueError:
            pass
    m = re.match(r"([-\d.]+(?:e[-+]?\d+)?)", s, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _numbers_close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# AST-guarded safe arithmetic calculator (for tool_use)
# ---------------------------------------------------------------------------


def _safe_calculate(expression: str) -> str:
    """Evaluate a pure arithmetic expression safely (digits, +-*/, (), unary minus)."""
    if not isinstance(expression, str):
        return "ERROR: expression must be a string"
    expr = expression.strip()
    if not expr:
        return "ERROR: empty expression"
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return "ERROR: invalid expression syntax"
    # Allow only numeric constants and arithmetic operators.
    allowed_binary = {ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow}
    allowed_unary = {ast.UAdd, ast.USub}
    # The AST walk visits operator child nodes (ast.Add, ast.USub, ...) which
    # are ast.operator / ast.unaryop instances. Skip those; they are validated
    # as part of their parent BinOp/UnaryOp.
    allowed_operator_kinds = tuple(allowed_binary | allowed_unary)
    for node in ast.walk(tree):
        if isinstance(node, ast.Expression):
            continue
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)):
                return "ERROR: only numeric constants allowed"
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in allowed_binary:
                return f"ERROR: operator {type(node.op).__name__} not allowed"
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in allowed_unary:
                return f"ERROR: unary {type(node.op).__name__} not allowed"
        elif isinstance(node, (ast.Load, ast.Store, ast.Del)):
            continue
        elif isinstance(node, allowed_operator_kinds):
            # Operator child of BinOp/UnaryOp - already validated above.
            continue
        else:
            return f"ERROR: disallowed construct {type(node).__name__}"
    try:
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})
        if isinstance(result, complex):
            return "ERROR: complex result"
        return str(result)
    except ZeroDivisionError:
        return "ERROR: division by zero"
    except Exception as e:
        return f"ERROR: {e}"

__all__ = ["BenchmarkStatus", "_BenchmarkOutcome", "_parse_number", "_numbers_close", "_safe_calculate"]
