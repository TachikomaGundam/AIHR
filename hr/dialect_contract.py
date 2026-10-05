"""Dialect contracts: never score what we have not measured.

A contract is the set of facts this server demonstrated when probed
(accepted reasoning-effort vocabulary, which thinking delta key it uses,
whether it reports usage, whether it honours tool calls, how much answer
survives its default thinking at a small budget). Every battery run is
gated on the facts its request shape depends on; every measurement row is
bound to the contract that justified it. Unknown dialects become honest
gaps, never loud lies.

Probe budget: at most 7 tiny requests per endpoint per validity window.
"""

from __future__ import annotations

import logging

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import psycopg2.extensions

from hr.models import BenchmarkCategory

PROBE_VERSION = 2
VALID_DAYS = 14

# batteries whose request shape leans on a server dialect fact
HARD_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "thinking": ("accepted_efforts",),
    "tool_calls": ("tool_calls_ok",),
}
log = logging.getLogger(__name__)

# Batteries whose gold answers are long-form OUTPUT: on servers where the
# small-budget probe shows thinking eating the answer (survival chars below
# SURVIVAL_MIN_CHARS), force the cheapest accepted effort (testbed 191:
# code_gen 1/13 functions, tool_use cut at "$97.356" mid-calculation).
SURVIVAL_MIN_CHARS = 200
SURVIVAL_DOWNGRADE_BATTERIES: frozenset[BenchmarkCategory] = frozenset({
    BenchmarkCategory.code_gen,
    BenchmarkCategory.tool_use,
})

BATTERY_REQUIRES: dict[BenchmarkCategory, tuple[str, ...]] = {
    BenchmarkCategory.reasoning: ("thinking",),
    BenchmarkCategory.long_horizon: ("thinking",),
    BenchmarkCategory.attention_probe: ("thinking",),
    BenchmarkCategory.attention_stress: ("thinking",),
    BenchmarkCategory.tool_use: ("tool_calls",),
}


@dataclass(slots=True)
class DialectFacts:
    endpoint_url: str
    model_slug: str
    accepted_efforts: list[str] = field(default_factory=list)
    thinking_key: str | None = None
    usage_in_stream: bool = False
    tool_calls_ok: bool = False
    answer_chars_at_small_budget: int | None = None
    probe_version: int = PROBE_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> "DialectFacts":
        return DialectFacts(**json.loads(raw))


def probe_dialect(
    url: str,
    headers: dict[str, str],
    slug: str,
    post: Callable[..., Any],
    iter_lines: Callable[[Any], Any] | None = None,
) -> DialectFacts:
    """Run the dialect probe battery against an openai-compatible endpoint.

    ``post(url, *, headers, json, timeout, stream)`` mirrors requests.post.
    ``iter_lines(response)`` yields SSE lines when streaming.
    """
    facts = DialectFacts(endpoint_url=url, model_slug=slug)
    answered = 0
    for effort in ("low", "medium", "high", "xhigh", "max"):
        try:
            resp = post(
                url,
                headers=headers,
                json={
                    "model": slug,
                    "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
                    "max_tokens": 1,
                    "reasoning_effort": effort,
                },
                timeout=60,
            )
            if resp.status_code == 200:
                facts.accepted_efforts.append(effort)
            if resp.status_code in (200, 400, 422):
                answered += 1  # endpoint is alive and speaking the chat API
        except Exception:  # noqa: BLE001 — connection failure is not a fact
            continue

    # one streaming call decides thinking-key, usage, and budget survival
    try:
        resp = post(
            url,
            headers=headers,
            json={
                "model": slug,
                "messages": [{"role": "user", "content": "Count slowly from 1 to 30, then answer 30."}],
                "max_tokens": 256,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            timeout=120,
            stream=True,
        )
        content_chars = 0
        lines = list(iter_lines(resp)) if iter_lines else list(resp.iter_lines(decode_unicode=True))
        for line in lines:
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                continue
            chunk = json.loads(payload)
            if chunk.get("usage"):
                facts.usage_in_stream = True
            for choice in chunk.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("reasoning_content"):
                    facts.thinking_key = "reasoning_content"
                elif delta.get("reasoning"):
                    facts.thinking_key = "reasoning" if facts.thinking_key is None else facts.thinking_key
                if delta.get("content"):
                    content_chars += len(delta["content"])
        facts.answer_chars_at_small_budget = content_chars
        answered += 1
    except Exception:  # noqa: BLE001
        pass

    try:
        resp = post(
            url,
            headers=headers,
            json={
                "model": slug,
                "messages": [{"role": "user", "content": "What is 2+3? You must call the calc tool."}],
                "max_tokens": 128,
                "tools": [{
                    "type": "function",
                    "function": {
                        "name": "calc",
                        "description": "add two numbers",
                        "parameters": {
                            "type": "object",
                            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            "required": ["a", "b"],
                        },
                    },
                }],
                "tool_choice": "required",
            },
            timeout=120,
        )
        if resp.status_code == 200:
            answered += 1
            body = resp.json()
            msg = (body.get("choices") or [{}])[0].get("message", {})
            facts.tool_calls_ok = bool(msg.get("tool_calls"))
    except Exception:  # noqa: BLE001
        pass

    if answered == 0:
        raise ProbeUnreachableError(f"no chat-completions response from {url} - probe inconclusive")
    return facts


class ProbeUnreachableError(RuntimeError):
    """Every probe request failed to reach a live chat endpoint.

    Distinguished from "endpoint answered and declined everything": an
    unreachable endpoint must NOT persist zero-capability facts, which would
    freeze the gate on a fake measurement until the contract expires.
    """


# ---------------------------------------------------------------------------
# persistence (hr.model_contract)
# ---------------------------------------------------------------------------

def contract_id_for(facts: DialectFacts) -> str:
    seed = f"{facts.endpoint_url}|{facts.model_slug}|{facts.probe_version}|{sorted(facts.accepted_efforts)}|{facts.thinking_key}|{facts.tool_calls_ok}|{facts.usage_in_stream}"
    return f"dc-{hashlib.sha256(seed.encode()).hexdigest()[:16]}"


def upsert_contract(conn: psycopg2.extensions.connection, model_id: str, facts: DialectFacts) -> str:
    cid = contract_id_for(facts)
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO hr.model_contract (contract_id, model_id, endpoint_url, model_slug, facts_json, probe_version, probed_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s) "
            "ON CONFLICT (contract_id) DO UPDATE SET probed_at = EXCLUDED.probed_at",
            (cid, model_id, facts.endpoint_url, facts.model_slug, facts.to_json(), facts.probe_version, now),
        )
    conn.commit()
    return cid


def load_valid_facts(
    conn: psycopg2.extensions.connection, model_id: str, endpoint_url: str, slug: str
) -> DialectFacts | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT facts_json::text FROM hr.model_contract "
            "WHERE model_id = %s AND endpoint_url = %s AND model_slug = %s AND probe_version = %s "
            "AND probed_at > %s ORDER BY probed_at DESC LIMIT 1",
            (model_id, endpoint_url, slug, PROBE_VERSION, datetime.now(timezone.utc) - timedelta(days=VALID_DAYS)),
        )
        row = cur.fetchone()
    return DialectFacts.from_json(row[0]) if row else None


# ---------------------------------------------------------------------------
# gating + adaptation decisions (pure)
# ---------------------------------------------------------------------------

def unmet_requirements(
    battery: BenchmarkCategory, caps_supports_thinking: bool, facts: DialectFacts | None
) -> str | None:
    """None = this battery may be scored; else the honest skip reason."""
    if facts is None:
        return "no dialect contract (probe did not run)"
    needs = BATTERY_REQUIRES.get(battery, ())
    for need in needs:
        if need == "thinking" and not caps_supports_thinking:
            continue  # battery runs without thinking params; nothing to prove
        for fact_key in HARD_REQUIREMENTS.get(need, ()):
            value = getattr(facts, fact_key)
            if not value:
                return f"{fact_key} unproven for {need}"
    return None


def survival_downgrade(battery: BenchmarkCategory, facts: DialectFacts | None) -> bool:
    """True when the contract says this server's thinking starves long answers."""
    if facts is None or battery not in SURVIVAL_DOWNGRADE_BATTERIES:
        return False
    survived = facts.answer_chars_at_small_budget
    return survived is not None and survived < SURVIVAL_MIN_CHARS


def pick_effort(
    facts: DialectFacts | None, budget: int, fallback: str, force_low: bool = False
) -> str | None:
    """Choose an effort the server actually accepted; None = do not send one."""
    accepted = facts.accepted_efforts if facts else []
    if not accepted:
        return fallback if facts is None else None
    if force_low:
        return "low" if "low" in accepted else accepted[0]
    if budget >= 16384 and "xhigh" in accepted:
        return "xhigh"
    if budget >= 4096 and "medium" in accepted:
        return "medium"
    if "low" in accepted:
        return "low"
    return accepted[0]


def measurement_flags(
    response_text: str | None, tokens_in: int | None, tokens_out: int | None, latency_ms: int | None
) -> str | None:
    """Canary: shapes that historically meant fake measurement data."""
    flags: list[str] = []
    if response_text and not tokens_in and not tokens_out:
        flags.append("zero_usage_with_text")
    if response_text and len(response_text) > 200 and latency_ms is not None and latency_ms < 50:
        flags.append("implausible_latency")
    return json.dumps(flags) if flags else None


def valid_contract_id(conn: psycopg2.extensions.connection, model_id: str) -> str | None:
    """Latest contract id inside the validity window; None when unproven."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT contract_id FROM hr.model_contract WHERE model_id = %s "
            "AND probe_version = %s AND probed_at > %s ORDER BY probed_at DESC LIMIT 1",
            (model_id, PROBE_VERSION, datetime.now(timezone.utc) - timedelta(days=VALID_DAYS)),
        )
        row = cur.fetchone()
    return str(row[0]) if row else None


_PROBE_CACHE: dict[tuple[str, str, str], DialectFacts | None] = {}


def ensure_facts(model_id: str, adapter: Any) -> DialectFacts | None:
    """Facts for this adapter's endpoint: process cache, then DB, then probe.

    Persistence and probing are best-effort: any failure yields None, which
    the gate treats as unproven — fail-closed on scores, never a crash.
    """
    try:
        url, headers, slug = adapter.endpoint_for(model_id)
    except Exception:  # noqa: BLE001
        return None
    key = (model_id, url, slug)
    if key in _PROBE_CACHE:
        return _PROBE_CACHE[key]
    facts: DialectFacts | None = None
    conn = None
    try:
        from hr.db import connect
        conn = connect()
        facts = load_valid_facts(conn, model_id, url, slug)
    except Exception as exc:  # noqa: BLE001 — tolerated, but never silently
        log.warning("dialect contract db unavailable for %s: %r", model_id, exc)
        _PERSIST_NOTE[model_id] = "contract db unavailable - persistence skipped"
        conn = None
    if facts is None:
        import requests
        try:
            facts = probe_dialect(url, headers, slug, requests.post)
        except ProbeUnreachableError as exc:
            log.warning("dialect probe unreachable for %s: %s", model_id, exc)
            _PERSIST_NOTE[model_id] = f"probe unreachable - {exc}"
            facts = None
        except Exception as exc:  # noqa: BLE001
            log.warning("dialect probe failed for %s: %r", model_id, exc)
            facts = None
        if facts is not None and conn is not None:
            try:
                upsert_contract(conn, model_id, facts)
            except Exception as exc:  # noqa: BLE001 — loud log, never silent
                log.warning("dialect contract upsert failed for %s: %r", model_id, exc)
                _PERSIST_NOTE[model_id] = "contract persistence FAILED - facts in-process only"
        elif facts is not None:
            _PERSIST_NOTE[model_id] = "contract persistence skipped - no db connection"
        else:
            _PERSIST_NOTE.pop(model_id, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _PROBE_CACHE[key] = facts
    if facts is not None:
        _BINDING[model_id] = contract_id_for(facts)
    return facts


CONTRACT_STATUS_SQL = (
    "SELECT model_id, endpoint_url, facts_json::text, probe_version, probed_at "
    "FROM hr.model_contract ORDER BY probed_at DESC"
)


def status_lines(conn) -> str:
    """Human-readable dialect-contract block for hr status."""
    with conn.cursor() as cur:
        cur.execute(CONTRACT_STATUS_SQL)
        rows = cur.fetchall()
    if not rows:
        return "dialect contracts: none yet — formed on first openai-compatible bench"
    lines = [
        "model | efforts accepted | thinking key | usage | tools | survival chars | probed",
        "---|---|---|---|---|---|---|",
    ]
    for model_id, endpoint, facts_raw, probe_ver, probed in rows:
        try:
            facts = DialectFacts.from_json(facts_raw)
        except (TypeError, ValueError):
            lines.append(f"{model_id} | UNREADABLE contract v{probe_ver} ({probed}) — re-probe pending")
            continue
        lines.append(
            f"{model_id} | {','.join(facts.accepted_efforts) or 'none (never send)'} | "
            f"{facts.thinking_key or '—'} | {'yes' if facts.usage_in_stream else 'no'} | "
            f"{'yes' if facts.tool_calls_ok else 'no'} | "
            f"{facts.answer_chars_at_small_budget if facts.answer_chars_at_small_budget is not None else '—'} | {probed}"
        )
    return "\n".join(lines)


_BINDING: dict[str, str] = {}
_PERSIST_NOTE: dict[str, str] = {}


def last_persist_note(model_id: str) -> str | None:
    """Why contract persistence did not land for this model (None = clean)."""
    return _PERSIST_NOTE.get(model_id)


def bound_contract_id(model_id: str) -> str | None:
    """Contract id established this process (zero extra SQL at write time)."""
    return _BINDING.get(model_id)


__all__ = [
    "PROBE_VERSION",
    "VALID_DAYS",
    "DialectFacts",
    "probe_dialect",
    "ProbeUnreachableError",
    "contract_id_for",
    "upsert_contract",
    "load_valid_facts",
    "unmet_requirements",
    "pick_effort",
    "measurement_flags",
    "valid_contract_id",
    "ensure_facts",
    "bound_contract_id",
    "last_persist_note",
    "status_lines",
    "CONTRACT_STATUS_SQL",
]
