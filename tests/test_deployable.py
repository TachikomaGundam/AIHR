"""Tests for JSONC parsing and deployable-set resolution.

No live opencode.jsonc required: the loader is exercised through tmp_path
fake files; the real machine config is only used implicitly by the CLI.
"""

from __future__ import annotations

import json

import pytest

from hr.deployable import load_deployable, strip_jsonc_comments


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


class TestStripJsonc:
    def test_line_comment_stripped(self):
        src = '{\n  // leading comment\n  "a": 1 // trailing\n}'
        assert json.loads(strip_jsonc_comments(src)) == {"a": 1}

    def test_block_comment_stripped(self):
        src = '{\n  /* block\n     comment */ "a": 1\n}'
        assert json.loads(strip_jsonc_comments(src)) == {"a": 1}

    def test_comment_marker_inside_string_preserved(self):
        src = '{"url": "https://example.com/a//b"}'
        assert json.loads(strip_jsonc_comments(src)) == {
            "url": "https://example.com/a//b"
        }

    def test_block_comment_marker_inside_string_preserved(self):
        src = '{"doc": "see /* not a comment */ here"}'
        assert json.loads(strip_jsonc_comments(src)) == {
            "doc": "see /* not a comment */ here"
        }

    def test_escaped_quotes_inside_string(self):
        src = r'{"msg": "say \"hi\" // still text"}'
        assert json.loads(strip_jsonc_comments(src)) == {
            "msg": 'say "hi" // still text'
        }

    def test_mixed_file_roundtrip(self):
        src = (
            '{\n'
            '  "provider": { // provider section\n'
            '    "p1": {\n'
            '      /* served */\n'
            '      "models": {"m1": {"url": "http://x//y"}}\n'
            '    }\n'
            '  }\n'
            '}\n'
        )
        parsed = json.loads(strip_jsonc_comments(src))
        assert parsed["provider"]["p1"]["models"] == {
            "m1": {"url": "http://x//y"}
        }


class TestLoadDeployable:
    def test_exact_set_from_opencode_plus_yaml(self, tmp_path):
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{\n'
            '  "provider": {\n'
            '    "bailian-token-plan": {\n'
            '      "models": {"qwen3.8-max": {}, "qwen3.7-max": {}}\n'
            '    },\n'
            '    "local-qwen": {\n'
            '      "models": {"qwen3.6-8b": {}}\n'
            '    }\n'
            '  }\n'
            '}\n',
        )
        yaml = _write(
            tmp_path / "deployable.yaml",
            "extra_deployable:\n"
            "  - kimi-for-coding/k3\n"
            "  - deepseek/deepseek-v4-flash\n",
        )
        assert load_deployable(oc, yaml) == {
            "bailian-token-plan/qwen3.8-max",
            "bailian-token-plan/qwen3.7-max",
            "local-qwen/qwen3.6-8b",
            "kimi-for-coding/k3",
            "deepseek/deepseek-v4-flash",
        }

    def test_missing_yaml_means_just_opencode_set(self, tmp_path):
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{"provider": {"p": {"models": {"m1": {}}}}}',
        )
        assert load_deployable(oc, tmp_path / "no-such.yaml") == {"p/m1"}

    def test_missing_opencode_raises_clear_error(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="deployable source missing"):
            load_deployable(tmp_path / "no-such.jsonc", tmp_path / "no.yaml")

    def test_yaml_with_unrelated_keys_ignored(self, tmp_path):
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{"provider": {"p": {"models": {}}}}',
        )
        yaml = _write(
            tmp_path / "deployable.yaml",
            "extra_deployable:\n  - p/model_extra\nother: [1, 2]\n",
        )
        assert load_deployable(oc, yaml) == {"p/model_extra"}

    def test_comments_in_jsonc_do_not_break_parse(self, tmp_path):
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{\n'
            '  // retired today:\n'
            '  "provider": {\n'
            '    "bailian-token-plan": {\n'
            '      /* only live models */\n'
            '      "models": {"qwen3.8-max": {}}\n'
            '    }\n'
            '  }\n'
            '}\n',
        )
        assert load_deployable(oc, tmp_path / "no-such.yaml") == {
            "bailian-token-plan/qwen3.8-max"
        }

    def test_provider_without_models_contributes_nothing(self, tmp_path):
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{"provider": {"p": {"npm": "x"}, "q": {"models": {"m": {}}}}}',
        )
        assert load_deployable(oc, tmp_path / "none.yaml") == {"q/m"}


class TestLoadDeployableDynamicPaths:
    """Default paths resolve through the unified config layer
    (OPENCODE_CONFIG_DIR / HR_HOME) — never a hardcoded ~/.config path."""

    def test_default_oc_path_honors_opencode_config_dir(
        self, tmp_path, monkeypatch
    ):
        config_dir = tmp_path / "opencode"
        config_dir.mkdir()
        (config_dir / "opencode.jsonc").write_text(
            '{"provider": {"p": {"npm": "@ai-sdk/openai", "models": {"m1": {}}}}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config_dir))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("HR_HOME", str(tmp_path / "hr"))
        assert load_deployable() == {"p/m1"}

    def test_extra_duplicating_config_model_is_drift(self, tmp_path):
        """REGRESSION guard (same contract as hr.fleet.merge_with_extras):
        an extra_deployable entry that duplicates a config-declared model is
        rejected loudly via the deployable path too."""
        oc = _write(
            tmp_path / "opencode.jsonc",
            '{"provider": {"p": {"models": {"m1": {}}}}}',
        )
        yaml = _write(
            tmp_path / "deployable.yaml",
            "extra_deployable:\n  - p/m1\n",
        )
        with pytest.raises(ValueError) as exc:
            load_deployable(oc, yaml)
        msg = str(exc.value)
        assert "p/m1" in msg
        assert "extra_deployable" in msg
