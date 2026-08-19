"""Reading of opencode's live config files (JSONC-tolerant, project/global).

The single canonical way hr reads opencode configuration: the global
``opencode.jsonc`` (``OPENCODE_CONFIG_DIR`` env > ``~/.config/opencode``)
plus the project ``opencode.jsonc`` / ``.opencode/opencode.jsonc``, merged
with project blocks overwriting global ones — the same precedence FastDraw's
origins.ts implements. Consumers: :mod:`hr.fleet` (model inventory),
:mod:`hr.discover` (enumeration), :mod:`hr.deployable` (single-file parse),
:mod:`hr.adapters.fleet` (wire routing).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hr.config import opencode_config_dir


def strip_jsonc_comments(text: str) -> str:
    """Remove single-line (//) and block (/* */) comments from JSONC text."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
    return text


def parse_config_file(path: Path) -> dict:
    """JSONC-tolerant parse of one opencode config file.

    Raises ValueError naming the path on any read/parse failure (clean CLI
    error surface — never a traceback).
    """
    try:
        raw = path.read_text(encoding="utf-8")
        cleaned = strip_jsonc_comments(raw)
        data = json.loads(cleaned)
    except (OSError, ValueError) as exc:
        raise ValueError(f"invalid opencode config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"invalid opencode config {path}: top level is not an object")
    return data


def opencode_config_files() -> list[Path]:
    """Candidate config files, lowest precedence first: global dir, then the
    project's ``opencode.jsonc`` and ``.opencode/opencode.jsonc`` (project
    blocks overwrite global ones)."""
    return [
        opencode_config_dir() / "opencode.jsonc",
        Path.cwd() / "opencode.jsonc",
        Path.cwd() / ".opencode" / "opencode.jsonc",
    ]


def read_providers() -> dict[str, dict]:
    """Merged ``provider.*`` blocks from every config file (project wins).

    An absent file contributes nothing; a malformed one raises ValueError
    naming it. No live opencode runtime is consulted.
    """
    merged: dict[str, dict] = {}
    for path in opencode_config_files():
        if not path.exists():
            continue
        data = parse_config_file(path)
        providers = data.get("provider", {})
        if not isinstance(providers, dict):
            continue
        for provider, block in providers.items():
            if isinstance(block, dict):
                merged[provider] = block
    return merged