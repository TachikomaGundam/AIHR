"""Line-scoped JSONC editing of opencode config files (comments are sacred).

:mod:`hr.opencfg` parses opencode's JSONC read-only; :mod:`hr.jsonc_scan`
locates spans. This module owns the writes: ensuring/removing one entry in a
top-level ``plugin`` array by splicing a single element line into (or out
of) the original text, so every comment, every unrelated key, and the user's
own formatting survive byte-for-byte. Before any write the mutated text is
re-parsed; a mutation that would corrupt the file is aborted, not shipped.
"""

from __future__ import annotations

import json
from pathlib import Path

from hr.jsonc_scan import ArrayInfo, Element, line_indent, scan_top_array, skip_ws_comments
from hr.opencfg import strip_jsonc_comments


def _splice_array(text: str, arr: ArrayInfo, entry: str) -> str:
    """The new text with ``entry`` as the array's first element (comma-aware)."""
    quoted = f'"{entry}"'
    if not arr.elements:
        body = f'\n{arr.indent}    {quoted}\n{arr.indent}'
        return text[: arr.open_ + 1] + body + text[arr.close :]
    first = arr.elements[0]
    head = text[: arr.open_ + 1]
    tail = text[arr.open_ + 1 :]
    same_line = text.rfind("\n", 0, first.start) == text.rfind("\n", 0, arr.open_)
    if same_line:  # inline array like ["a", "b"]: splice before the first element
        return head + f'{quoted}, ' + tail
    line_start = text.rfind("\n", 0, first.start) + 1
    elem_indent = text[line_start:first.start]  # match the existing element style
    return text[:line_start] + f'{elem_indent}{quoted},\n' + text[line_start:]


def _rfind_comma(text: str, pos: int) -> int | None:
    i = pos - 1
    while i >= 0:
        if text[i] == ",":
            return i
        if not text[i].isspace():
            return None
        i -= 1
    return None


def _remove_element(text: str, arr: ArrayInfo, element: Element) -> str:
    """Delete one element plus the comma that belongs to it (no trailing comma).

    Whitespace-symmetric with :func:`_splice_array`: an element that lived on
    its own line takes the whole line with it, so ensure+remove round-trips
    byte-for-byte.
    """
    del arr  # the element spans are positions into ``text``; the array frame is implied
    after = skip_ws_comments(text, element.end)
    line_start = text.rfind("\n", 0, element.start) + 1
    own_line = text[line_start:element.start].strip() == ""
    if after < len(text) and text[after] == ",":
        cut_end = after + 1
        nxt = skip_ws_comments(text, cut_end)
        if nxt < len(text) and text[nxt] == "]":  # JSONC trailing comma: drop the previous one
            prev = _rfind_comma(text, element.start)
            if prev is not None:
                return text[:prev] + text[cut_end:]
        if own_line and text.startswith("\n", cut_end):
            return text[:line_start] + text[cut_end + 1 :]
        if not own_line:  # inline splice ("entry", b): the space we added goes too
            while cut_end < len(text) and text[cut_end] in " \t":
                cut_end += 1
        return text[: element.start] + text[cut_end:]
    before = _rfind_comma(text, element.start)
    if before is not None:
        return text[:before] + text[after:]
    if own_line and text.startswith("\n", element.end):
        return text[:line_start] + text[element.end + 1 :]
    return text[: element.start] + text[element.end :]


def _render_new_array(indent: str, key: str, entry: str, comma: bool) -> str:
    block = f'\n{indent}  "{key}": [\n{indent}    "{entry}"\n{indent}  ]'
    return block + ("," if comma else "")


def ensure_plugin_entry(path: Path, entry: str, *, key: str = "plugin") -> tuple[bool, str]:
    """Idempotently place ``entry`` in the top-level ``key`` array.

    Creates the file, the array, or the element line as needed; a no-op when
    the entry is already present. Everything outside the spliced line — all
    comments included — is preserved byte-for-byte.
    """
    if not path.is_file():
        text = f'{{\n  "{key}": [\n    "{entry}"\n  ]\n}}\n'
        _validate(text, key, entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True, f'created {path} with {key}: ["{entry}"]'
    text = path.read_text(encoding="utf-8")
    arr, first_brace, empty_object = scan_top_array(text, key)
    if arr is not None and arr.find(entry) is not None:
        return False, f"{key} entry already present in {path}: {entry}"
    if arr is not None:
        updated = _splice_array(text, arr, entry)
    else:
        if first_brace < 0:
            raise ValueError(f"no top-level object found in {path}")
        updated = (
            text[: first_brace + 1]
            + _render_new_array(line_indent(text, first_brace), key, entry, not empty_object)
            + text[first_brace + 1 :]
        )
    _validate(updated, key, entry)
    path.write_text(updated, encoding="utf-8")
    verb = "added array with" if arr is None else "added"
    return True, f"{verb} {key} entry in {path}: {entry}"


def remove_plugin_entry(path: Path, entry: str, *, key: str = "plugin") -> tuple[bool, str]:
    """Remove ``entry`` only when the exact string is present; else no-op."""
    if not path.is_file():
        return False, f"{path} not found — nothing to remove"
    text = path.read_text(encoding="utf-8")
    arr, _brace, _empty = scan_top_array(text, key)
    if arr is None:
        return False, f"no {key} array in {path} — nothing to remove"
    element = arr.find(entry)
    if element is None:
        return False, f"{key} entry not present in {path}: {entry}"
    updated = _remove_element(text, arr, element)
    _validate(updated, key, None, must_not_contain=entry)
    path.write_text(updated, encoding="utf-8")
    return True, f"removed {key} entry from {path}: {entry}"


def array_is_empty(path: Path, *, key: str = "plugin") -> bool:
    """True when the file's ``key`` array exists with no entries (never created files)."""
    if not path.is_file():
        return False
    arr, _brace, _empty = scan_top_array(path.read_text(encoding="utf-8"), key)
    return arr is not None and not arr.elements


def _validate(
    text: str, key: str, must_contain: str | None, *, must_not_contain: str | None = None
) -> None:
    try:
        data = json.loads(strip_jsonc_comments(text))
    except ValueError as exc:
        raise ValueError(f"refusing to write invalid JSONC ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError("refusing to write a non-object JSONC document")
    array = data.get(key)
    if not isinstance(array, list):
        raise ValueError(f"refusing to write: {key} is not an array")
    if must_contain is not None and must_contain not in array:
        raise ValueError(f"refusing to write: {must_contain} missing after splice")
    if must_not_contain is not None and must_not_contain in array:
        raise ValueError(f"refusing to write: {must_not_contain} still present")
