"""Tests for the line-scoped JSONC plugin editor (hr.opencfg_editor).

Fixtures carry comments everywhere a mutation could land: the contract is
that every untouched byte — comments included — survives splice-in and
splice-out, and that both operations are idempotent/exact-string guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr.opencfg import strip_jsonc_comments
from hr.opencfg_editor import ensure_plugin_entry, remove_plugin_entry

ENTRY = "opencode-hr-agent@latest"

COMMENTED = """{
  // global voice of the fleet
  "$schema": "https://opencode.ai/config.json",
  /* provider block, keep me */
  "provider": {
    "local-qwen": { "options": { "baseURL": "http://127.0.0.1:8010/v1" } } // loopback proxy
  },
  "plugin": [
    // user's own plugin, with a comment above it
    "already-here@1.2.3",
    "second@latest" // trailing comment
  ],
  // model pins live below
  "model": "frontier/x"
}
"""


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_insert_preserves_every_comment_and_other_line_byte(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", COMMENTED)
    changed, _msg = ensure_plugin_entry(path, ENTRY)
    assert changed
    text = path.read_text(encoding="utf-8")
    data = json.loads(strip_jsonc_comments(text))
    assert data["plugin"][0] == ENTRY and "already-here@1.2.3" in data["plugin"]
    for comment in ("// global voice of the fleet", "/* provider block, keep me */",
                    "// loopback proxy", "// user's own plugin, with a comment above it",
                    "// trailing comment", "// model pins live below"):
        assert comment in text  # every comment survives verbatim
    assert '"model": "frontier/x"' in text
    # insert-first-element is a line splice: original lines reappear unchanged
    for line in COMMENTED.splitlines():
        assert line in text.splitlines()


def test_double_run_is_a_no_op(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", COMMENTED)
    assert ensure_plugin_entry(path, ENTRY)[0] is True
    changed, msg = ensure_plugin_entry(path, ENTRY)
    assert changed is False and "already present" in msg
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugin"].count(ENTRY) == 1


def test_inline_array_splice_keeps_parse(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"plugin": ["a@1", "b@2"]}\n')
    ensure_plugin_entry(path, ENTRY)
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugin"] == [ENTRY, "a@1", "b@2"]


def test_empty_array_and_missing_key_and_missing_file(tmp_path) -> None:
    path = _write(tmp_path / "tui.json", '{"theme": "dark", "plugin": []}')
    ensure_plugin_entry(path, "opencode-fastdraw@latest")
    assert json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))["plugin"] == [
        "opencode-fastdraw@latest"
    ]

    path2 = _write(tmp_path / "opencode.jsonc", '{\n  // only comments and one key\n  "model": "x"\n}\n')
    ensure_plugin_entry(path2, ENTRY)
    data2 = json.loads(strip_jsonc_comments(path2.read_text(encoding="utf-8")))
    assert data2["plugin"] == [ENTRY] and data2["model"] == "x"

    path3 = tmp_path / "deep" / "opencode.jsonc"
    changed, _msg = ensure_plugin_entry(path3, ENTRY)
    assert changed and path3.is_file()
    assert json.loads(strip_jsonc_comments(path3.read_text(encoding="utf-8")))["plugin"] == [ENTRY]


def test_empty_object_gets_no_trailing_comma(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", "{}\n")
    ensure_plugin_entry(path, ENTRY)
    assert json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))["plugin"] == [ENTRY]


def test_remove_exact_string_roundtrip_keeps_rest(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", COMMENTED)
    ensure_plugin_entry(path, ENTRY)
    changed, _msg = remove_plugin_entry(path, ENTRY)
    assert changed
    text = path.read_text(encoding="utf-8")
    assert text == COMMENTED  # full roundtrip back to the pre-state
    data = json.loads(strip_jsonc_comments(text))
    assert ENTRY not in data["plugin"]


def test_remove_last_element_cleans_preceding_comma(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"plugin": ["a@1", "' + ENTRY + '"]}')
    remove_plugin_entry(path, ENTRY)
    assert json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))["plugin"] == ["a@1"]


def test_remove_absent_is_noop(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", COMMENTED)
    changed, msg = remove_plugin_entry(path, ENTRY)
    assert changed is False and "not present" in msg
    changed2, msg2 = remove_plugin_entry(tmp_path / "missing.jsonc", ENTRY)
    assert changed2 is False and "not found" in msg2


def test_malformed_input_raises_without_writing(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", "{ this is not jsonc at all ]")
    with pytest.raises(ValueError):
        ensure_plugin_entry(path, ENTRY)
    assert path.read_text(encoding="utf-8") == "{ this is not jsonc at all ]"
