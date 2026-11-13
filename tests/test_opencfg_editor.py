"""Tests for the line-scoped JSONC plugin editor (hr.opencfg_editor).

Fixtures carry comments everywhere a mutation could land: the contract is
that every untouched byte — comments included — survives splice-in and
splice-out, and that both operations are idempotent/exact-string guarded.
The bulk pins the V1-era ``plugin`` key explicitly (old receipts replay
through it); V2 default-key, alias-guard and empty-array cases follow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr.opencfg import strip_jsonc_comments
from hr.opencfg_editor import (
    ensure_plugin_entry,
    remove_empty_array_key,
    remove_plugin_entry,
)

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
    changed, _msg = ensure_plugin_entry(path, ENTRY, key="plugin")
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
    assert ensure_plugin_entry(path, ENTRY, key="plugin")[0] is True
    changed, msg = ensure_plugin_entry(path, ENTRY, key="plugin")
    assert changed is False and "already present" in msg
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugin"].count(ENTRY) == 1


def test_inline_array_splice_keeps_parse(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"plugin": ["a@1", "b@2"]}\n')
    ensure_plugin_entry(path, ENTRY, key="plugin")
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugin"] == [ENTRY, "a@1", "b@2"]


def test_empty_array_and_missing_key_and_missing_file(tmp_path) -> None:
    path = _write(tmp_path / "tui.json", '{"theme": "dark", "plugin": []}')
    ensure_plugin_entry(path, "opencode-fastdraw@latest", key="plugin")
    assert json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))["plugin"] == [
        "opencode-fastdraw@latest"
    ]

    path2 = _write(tmp_path / "opencode.jsonc", '{\n  // only comments and one key\n  "model": "x"\n}\n')
    ensure_plugin_entry(path2, ENTRY, key="plugin")
    data2 = json.loads(strip_jsonc_comments(path2.read_text(encoding="utf-8")))
    assert data2["plugin"] == [ENTRY] and data2["model"] == "x"

    path3 = tmp_path / "deep" / "opencode.jsonc"
    changed, _msg = ensure_plugin_entry(path3, ENTRY, key="plugin")
    assert changed and path3.is_file()
    assert json.loads(strip_jsonc_comments(path3.read_text(encoding="utf-8")))["plugin"] == [ENTRY]


def test_empty_object_gets_no_trailing_comma(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", "{}\n")
    ensure_plugin_entry(path, ENTRY, key="plugin")
    assert json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))["plugin"] == [ENTRY]


def test_remove_exact_string_roundtrip_keeps_rest(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", COMMENTED)
    ensure_plugin_entry(path, ENTRY, key="plugin")
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
        ensure_plugin_entry(path, ENTRY, key="plugin")
    assert path.read_text(encoding="utf-8") == "{ this is not jsonc at all ]"


# ---------------------------------------------------------------------------
# V2: default key `plugins`, package-logical idempotency, empty-array cleanup
# ---------------------------------------------------------------------------

V2_CFG = '{\n  "model": "frontier/x",\n  "plugin": ["legacy-user@1"]\n}\n'


def test_v2_default_key_creates_plugins_and_keeps_legacy(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", V2_CFG)
    changed, _msg = ensure_plugin_entry(path, ENTRY)
    assert changed
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugins"] == [ENTRY]
    assert data["plugin"] == ["legacy-user@1"]  # V1 key is user-owned: never written


def test_v2_alias_entry_refuses_double_registration(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"plugins": ["opencode-hr-agent@0.2.1"]}\n')
    changed, msg = ensure_plugin_entry(path, ENTRY)  # same package, newer spec
    assert changed is False and "different spec" in msg
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data["plugins"] == ["opencode-hr-agent@0.2.1"]  # stale pin untouched here


def test_v2_object_entry_alias_is_detected(tmp_path) -> None:
    path = _write(
        tmp_path / "opencode.jsonc",
        '{"plugins": [{"package": "opencode-hr-agent@latest", "options": {}}]}\n',
    )
    changed, _msg = ensure_plugin_entry(path, ENTRY)
    assert changed is False
    assert path.read_text(encoding="utf-8").count("opencode-hr-agent") == 1


def test_remove_empty_array_key_roundtrips_created_key(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", V2_CFG)
    ensure_plugin_entry(path, ENTRY)
    ensure_plugin_entry(path, "opencode-fastdraw@latest")
    remove_plugin_entry(path, ENTRY, key="plugins")
    remove_plugin_entry(path, "opencode-fastdraw@latest", key="plugins")
    changed, _msg = remove_empty_array_key(path)
    assert changed
    assert path.read_text(encoding="utf-8") == V2_CFG  # byte-for-byte to pre-install


def test_remove_empty_array_key_refuses_non_empty(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"plugins": ["keep@1"], "x": 2}')
    changed, msg = remove_empty_array_key(path)
    assert changed is False and "still holds entries" in msg
    assert '"keep@1"' in path.read_text(encoding="utf-8")


def test_remove_empty_array_key_middle_member_keeps_parse(tmp_path) -> None:
    path = _write(tmp_path / "opencode.jsonc", '{"a": 1, "plugins": [], "b": 2}\n')
    changed, _msg = remove_empty_array_key(path)
    assert changed
    data = json.loads(strip_jsonc_comments(path.read_text(encoding="utf-8")))
    assert data == {"a": 1, "b": 2}
