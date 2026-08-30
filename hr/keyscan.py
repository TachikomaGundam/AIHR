"""Outbound-text secret detector (spec §12.1).

Detects the following classes of leaked secrets:
  - sk-sp-*        : dashscope-style keys (aliyun / bailian)
  - sk-kimi-*      : moonshot / kimi keys
  - sk-ant-*       : anthropic keys
  - JWT            : eyJ<base64>.<base64>.<base64>
  - postgres(ql)://: connection strings (with userinfo)
  - Bearer <token> : Authorization header values
  - PEM private-key headers
  - high-entropy password assignments
      (password= / passwd= / pwd= / secret= / api_key= / api-key= / token=
       followed by a high-entropy value)

Public API:
  - scan_outbound(text)  — returns None if no leak; else raises SecretLeakError
    listing the matched PATTERN NAMES (NEVER the secret content).
  - redact(text)         — returns text with each matched region replaced by
    «REDACTED:pattern-name».

This module is purely local — no network / no model-API calls. It may be
invoked from any outbound text formatter (email, wiki push, logging) to
defang accidental leaks.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable


# ---------------------------------------------------------------------------
# Pattern registry — order matters for redaction overlap resolution.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Pattern:
    name: str
    regex: re.Pattern[str]


# Helper: base64-url character class (JWT payloads).
_B64URL = r"[A-Za-z0-9_\-]+"

# JWT — eyJ<segment>.<segment>.<segment>
_JWT_RE = re.compile(
    r"\beyJ" + _B64URL + r"\." + _B64URL + r"\." + _B64URL + r"\b"
)

# DB connection strings with userinfo — must be redacted with the WHOLE url.
_PG_URL_RE = re.compile(
    r"\b(?:postgres(?:ql)?):\/\/[^\s\"'`]+",
)

# Bearer token — redact the token portion too, but the whole header for safety.
_BEARER_RE = re.compile(
    r"\bBearer\s+[A-Za-z0-9._\-+/=]{16,}",
    flags=re.IGNORECASE,
)

# PEM private-key header.
_PEM_RE = re.compile(
    r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----",
)

# API keys — explicit vendor prefixes.
_KEY_PREFIX_RE = re.compile(
    r"\b(sk-sp-[A-Za-z0-9_\-]{12,}|sk-kimi-[A-Za-z0-9_\-]{12,}|"
    r"sk-ant-[A-Za-z0-9_\-]{12,})\b"
)


# High-entropy password assignment detectors. Covers:
#   password = "<val>"
#   passwd="<val>"
#   PWD=...
#   api-key='...'
#   token=`...`   (backtick style in shell)
_PWD_ASSIGN_RE = re.compile(
    r"\b(?:password|passwd|pwd|api_key|api-key|apikey|secret|auth_token|"
    r"access_token|access-token|secret_key|secret-key|db_password|"
    r"private_key)\s*[=:]\s*"
    r"(?:"
    r"'[^'\"\s]{16,}'|"     # single-quoted
    r"\"[^'\"\s]{16,}\"|"   # double-quoted
    r"`[^'\"\s]{16,}`|"     # backtick-quoted
    r"=[^\"'\s\n]{16,}|"    # =<raw>
    r":\s*[^\"'\s\n]{16,}|" # : <raw>
    r"[^\"'\s\n`]{16,}"     # <raw> (last, greedy-ish)
    r")",
    flags=re.IGNORECASE,
)

# Also detect generic high-entropy strings in common key-like env exports.
_ENV_EXPORT_RE = re.compile(
    r"\b[A-Z_]{3,}_(?:KEY|SECRET|TOKEN|PASSWORD)=([\"']?)([^\"'\s]{24,})\1",
)

# All patterns — list order defines redaction precedence (longest / most
# specific first).
_PATTERNS: list[_Pattern] = [
    _Pattern("jwt", _JWT_RE),
    _Pattern("postgres_url", _PG_URL_RE),
    _Pattern("bearer_token", _BEARER_RE),
    _Pattern("pem_private_key", _PEM_RE),
    _Pattern("sk_sp", re.compile(r"\bsk-sp-[A-Za-z0-9_\-]{12,}\b")),
    _Pattern("sk_kimi", re.compile(r"\bsk-kimi-[A-Za-z0-9_\-]{12,}\b")),
    _Pattern("sk_ant", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}\b")),
    _Pattern("api_key_prefix", _KEY_PREFIX_RE),
    _Pattern("password_assignment", _PWD_ASSIGN_RE),
    _Pattern("env_secret_export", _ENV_EXPORT_RE),
]


# ---------------------------------------------------------------------------
# Entropy filter — high-entropy values only matter if they actually look
# random (Shannon entropy over the string after normalization is high).
# ---------------------------------------------------------------------------
def _shannon(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(text)
    ent = 0.0
    for c in counts.values():
        p = c / n
        if p:
            ent -= p * math.log2(p)
    return ent


_HIGH_ENT_THRESHOLD = 3.5


def _is_high_entropy(text: str) -> bool:
    return _shannon(text) >= _HIGH_ENT_THRESHOLD


# ---------------------------------------------------------------------------
# Public objects
# ---------------------------------------------------------------------------
class SecretLeakError(ValueError):
    """Raised by scan_outbound() to signal detected secret material.

    The error message lists the PATTERN names that fired, never the actual
    secret substrings. Use `patterns` to inspect programmatically.
    """

    def __init__(self, patterns: list[str]) -> None:
        super().__init__(
            "secret material detected in outbound text; patterns: "
            + ", ".join(sorted(set(patterns)))
        )
        self.patterns = sorted(set(patterns))


@dataclass
class Match:
    """A single detected region."""
    pattern: str
    start: int
    end: int


def _matches(text: str) -> list[Match]:
    """Return all matches across all patterns (name, start, end)."""
    hits: list[Match] = []
    for pat in _PATTERNS:
        for m in pat.regex.finditer(text):
            # For password-assignment + env-secret-export, apply entropy filter.
            if pat.name in ("password_assignment", "env_secret_export"):
                # Strip the key prefix, leave the value.
                value = m.group(0)
                # Find a "value-like" substring after the first =,:,quote.
                for splitter in ("=", ":", "'", '"', "`"):
                    if splitter in value:
                        value = value.split(splitter, 1)[1]
                        break
                value = value.strip().strip("'\"`")
                if not _is_high_entropy(value):
                    continue
            hits.append(Match(pattern=pat.name, start=m.start(), end=m.end()))
    return hits


def _collapse(matches: list[Match]) -> list[Match]:
    """Merge overlapping matches into a single Match with the first pattern
    name wins."""
    if not matches:
        return []
    ordered = sorted(matches, key=lambda m: (m.start, -m.end))
    collapsed: list[Match] = []
    cur = ordered[0]
    for nxt in ordered[1:]:
        if nxt.start <= cur.end:
            # Overlap — extend end.
            cur = Match(
                pattern=cur.pattern,
                start=cur.start,
                end=max(cur.end, nxt.end),
            )
        else:
            collapsed.append(cur)
            cur = nxt
    collapsed.append(cur)
    return collapsed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def scan_outbound(text: str) -> None:
    """Scan text for outbound secret leaks.

    Raises SecretLeakError if ANY pattern fires, listing the PATTERN NAMES
    (not the secrets). Returns None otherwise.
    """
    if not text:
        return
    matches = _matches(text)
    if not matches:
        return
    names = sorted({m.pattern for m in matches})
    raise SecretLeakError(names)


def redact(text: str) -> str:
    """Return `text` with each detected region replaced by
    «REDACTED:pattern-name». Overlapping regions collapse, first-pattern name
    wins.
    """
    if not text:
        return text
    matches = _collapse(_matches(text))
    if not matches:
        return text
    out = []
    cursor = 0
    for m in matches:
        out.append(text[cursor:m.start])
        out.append(f"«REDACTED:{m.pattern}»")
        cursor = m.end
    out.append(text[cursor:])
    return "".join(out)


def patterns() -> list[str]:
    """Return the ordered list of (unique) pattern names."""
    return [p.name for p in _PATTERNS]


__all__ = [
    "SecretLeakError",
    "Match",
    "scan_outbound",
    "redact",
    "patterns",
]

# Silence unused warning.
_ = (Iterable, math)
