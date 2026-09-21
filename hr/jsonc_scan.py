"""Position-tracking JSONC scanner (strings/comments-aware, offset-based).

The read side of :mod:`hr.opencfg_editor`: finds a top-level ``key: [...]``
array and its direct string elements by exact character spans, so the
editor can splice whole lines without reformatting anything. Handles ``//``
and ``/* */`` comments and both quote styles (matching
:func:`hr.opencfg.strip_jsonc_comments`' string awareness).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Element:
    """One string element of the array: exact text span + decoded value."""

    start: int
    end: int
    value: str


@dataclass(frozen=True)
class ArrayInfo:
    key_start: int
    open_: int
    close: int
    indent: str
    elements: tuple[Element, ...]

    def find(self, entry: str) -> Element | None:
        for element in self.elements:
            if element.value == entry:
                return element
        return None


def skip_ws_comments(text: str, i: int) -> int:
    n = len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith("//", i):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i)
            i = n if end < 0 else end + 2
        else:
            return i
    return n


def read_string(text: str, i: int) -> tuple[int, str]:
    """Decode the string token at ``i``; returns (end_exclusive, value)."""
    quote = text[i]
    out: list[str] = []
    j = i + 1
    while j < len(text):
        char = text[j]
        if char == "\\" and j + 1 < len(text):
            out.append(text[j + 1])
            j += 2
            continue
        if char == quote:
            return j + 1, "".join(out)
        out.append(char)
        j += 1
    raise ValueError(f"unterminated string at offset {i}")


def line_indent(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    lead = text[start:pos]
    return lead if lead.isspace() else ""


def _read_array(text: str, open_: int) -> tuple[tuple[Element, ...], int]:
    """Direct string elements + the offset of the closing bracket."""
    elements: list[Element] = []
    i = open_ + 1
    inner = 2  # depth 1 = object, 2 = inside this array
    while i < len(text):
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            i = skip_ws_comments(text, i)
            continue
        if char in {'"', "'"}:
            end, value = read_string(text, i)
            if inner == 2:
                elements.append(Element(i, end, value))
            i = end
            continue
        if char in "{[":
            inner += 1
        elif char in "}]":
            if inner == 2:
                return tuple(elements), i
            inner -= 1
        i += 1
    raise ValueError("unterminated array")


def scan_top_array(text: str, key: str) -> tuple[ArrayInfo | None, int, bool]:
    """Locate the top-level ``key: [...]`` array in JSONC text.

    Returns ``(array_info | None, first_brace_offset, object_is_empty)``;
    offsets are raw indices into ``text``. Raises ValueError on unparsable
    input (an editor must not splice into a corrupt file).
    """
    depth = 0
    i = 0
    n = len(text)
    first_brace = -1
    members = 0
    pending: ArrayInfo | None = None
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
            continue
        if text.startswith("//", i) or text.startswith("/*", i):
            i = skip_ws_comments(text, i)
            continue
        if char in {'"', "'"}:
            end, value = read_string(text, i)
            if depth == 1:
                members += 1
                after = skip_ws_comments(text, end)
                if after < n and text[after] == ":" and pending is None and value == key:
                    bracket = skip_ws_comments(text, after + 1)
                    if bracket < n and text[bracket] == "[":
                        elements, close = _read_array(text, bracket)
                        pending = ArrayInfo(i, bracket, close, line_indent(text, i), elements)
                        i = close + 1
                        continue
            i = end
            continue
        if char in "{[":
            if char == "{" and first_brace < 0:
                first_brace = i
            depth += 1
        elif char in "}]":
            depth -= 1
        i += 1
    if pending is None and depth != 0:
        raise ValueError(f"unbalanced JSON structure in text (final depth {depth})")
    return pending, first_brace, members == 0
