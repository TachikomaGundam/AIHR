"""hr preflight - S3 control baseline / first-shot gate (SIM-LADDER-v2 S3).

Judgment is pure (:func:`judge`); effects live in the executor and the marker
helpers. Invariants (S3-MINI-DESIGN-20261024.md): a real sweep may only start
on a fresh preflight PASS marker; infrastructure-sick cells are non-measurements
(re-queued by name, never scored, never an attempt); bands/budgets/anchors come
from the versioned config artifact with the machine's real values in the
gitignored ``preflight.local.yaml`` overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

import yaml

from hr.adapters.base import Adapter, Capabilities
from hr.bench.engine_results import BenchOutcome, make_sweep_id
from hr.decision import PREFLIGHT_PURPOSE
from hr.graders.base import ModelResponse
Verdict = Literal["pass", "fail", "cannot-answer"]


@dataclass(frozen=True, slots=True)
class PreflightConfig:
    max_requests: int
    max_total_tokens: int
    per_request_max_tokens: int
    abort_error_rate: float
    freshness_hours: int
    default_models: tuple[str, ...]
    default_batteries: tuple[str, ...]
    bands: Mapping[str, tuple[float, float]]


_DEFAULTS: dict[str, Any] = {
    "budget": {"max_requests": 50, "max_total_tokens": 400_000,
               "per_request_max_tokens": 4096},
    "abort": {"error_rate": 0.4},
    "freshness_hours": 12,
    "models": [],
    "batteries": ["reasoning", "factuality"],
    "bands": {},
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_preflight_config(tracked: Path | None = None) -> PreflightConfig:
    """Parse tracked preflight.yaml plus the optional local overlay (same
    deep-merge discipline as every other configs/ surface: local wins per key,
    lists replace). Fail-loud on a malformed shape - a broken gate never
    reads as a permissive one."""
    if tracked is not None:
        return _parse_preflight_doc(_read_tracked_with_overlay(tracked))

    from hr.config_resources import load_yaml

    raw: dict[str, Any] = dict(_DEFAULTS)
    try:
        raw = _deep_merge(raw, load_yaml("preflight.yaml"))
    except FileNotFoundError:
        pass  # no tracked config anywhere: defaults, as before
    return _parse_preflight_doc(raw)


def _read_tracked_with_overlay(path: Path) -> dict[str, Any]:
    """Explicit-file path (tests and callers with a hand-picked file):
    the file plus a same-directory .local.yaml, no ambient layers - the
    HR_HOME/user-layer world belongs to the loader, not to this path."""
    raw: dict[str, Any] = dict(_DEFAULTS)
    if path.is_file():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        raw = _deep_merge(raw, doc)
    overlay = path.with_name(path.stem + ".local.yaml")
    if overlay.is_file():
        odoc = yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}
        if not isinstance(odoc, dict):
            raise ValueError(f"{overlay}: top level must be a mapping")
        raw = _deep_merge(raw, odoc)
    return raw


def _parse_preflight_doc(raw: dict[str, Any]) -> PreflightConfig:
    """Shape validation + band parsing; fail-loud - a broken gate never
    reads as a permissive one."""
    bands_raw = raw.get("bands") or {}
    bands: dict[str, tuple[float, float]] = {}
    for battery_code, pair in bands_raw.items():
        if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
            raise ValueError(f"bands.{battery_code} must be [lo, hi]")
        lo, hi = float(pair[0]), float(pair[1])
        if lo > hi:
            raise ValueError(f"bands.{battery_code}: lo {lo} > hi {hi}")
        bands[str(battery_code)] = (lo, hi)
    budget_sec = raw.get("budget") or {}
    abort_sec = raw.get("abort") or {}

    def _scalar(section: dict[str, Any], key: str, cast: Any, section_name: str) -> Any:
        try:
            return cast(section[key]) if key in section else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{section_name}.{key} must be a number (got {section[key]!r})") from exc


    return PreflightConfig(
        max_requests=_scalar(budget_sec, "max_requests", int, "budget") or 50,
        max_total_tokens=_scalar(budget_sec, "max_total_tokens", int, "budget") or 400_000,
        per_request_max_tokens=_scalar(budget_sec, "per_request_max_tokens", int, "budget") or 4096,
        abort_error_rate=_scalar(abort_sec, "error_rate", float, "abort") or 0.4,
        freshness_hours=int(raw.get("freshness_hours", 12)),
        default_models=tuple(str(m) for m in raw.get("models") or ()),
        default_batteries=tuple(str(b) for b in raw.get("batteries") or ("reasoning", "factuality")),
        bands=bands,
    )


@dataclass(frozen=True, slots=True)
class CellView:
    """One model x battery cell, reduced to what the gates read.

    ``scored`` cells feed the gates. ``infra`` cells are sick transport (they
    abort the session past the error threshold). ``not_applicable`` cells are
    honest capability gaps: excluded from the gates AND from the error
    denominator - a blind endpoint is not a sick endpoint.
    """

    model_id: str
    battery: str
    scored: bool
    infra: bool
    empty_response: bool
    score: float
    detail: str = ""

    @property
    def name(self) -> str:
        return f"{self.model_id}/{self.battery}"


@dataclass(frozen=True, slots=True)
class Judgment:
    verdict: Verdict
    g1_violations: tuple[str, ...]
    g2_violations: tuple[str, ...]
    error_rate: float
    requeue: tuple[str, ...]
    summary: str


def cell_view(outcome: BenchOutcome) -> CellView:
    raw = outcome.raw_output or ""
    scored = outcome.status == "scored"
    infra = outcome.status == "inconclusive"
    # G1 emptiness = no transport evidence anywhere: the aggregate text OR
    # any per-item response counts as an answer having arrived. Multi-item
    # batteries structurally leave the aggregate empty (factuality fills a
    # log, reasoning leaves "") - aggregate-only emptiness was a false G1
    # violation class that masked the real per-item signal.
    text = outcome.response_text or ""
    evidence = [text, *(it.response_text or "" for it in outcome.items)]
    return CellView(
        model_id=outcome.model_id,
        battery=outcome.battery.value,
        scored=scored,
        infra=infra,
        empty_response=not any(t.strip() for t in evidence),
        score=outcome.score,
        detail=raw[:200],
    )


def judge(cells: Sequence[CellView], cfg: PreflightConfig) -> Judgment:
    """Pure three-gate judgment. Empty cells answer nothing (never a fake
    clean). Infra share above the abort threshold aborts the whole session as
    cannot-answer with the affected cells named for re-queue."""
    answerable = tuple(c for c in cells if c.scored or c.infra)
    if not answerable:
        return Judgment("cannot-answer", (), (), 0.0, (),
                        "no answerable cells (all skipped as not-applicable)")
    infra = tuple(c for c in cells if c.infra)
    error_rate = len(infra) / len(answerable)
    requeue = tuple(c.name for c in infra)
    if error_rate > cfg.abort_error_rate:
        return Judgment(
            "cannot-answer", (), (), error_rate, requeue,
            f"infrastructure error rate {error_rate:.0%} exceeds {cfg.abort_error_rate:.0%}; "
            f"cells re-queued, attempts untouched",
        )
    measured = tuple(c for c in cells if c.scored)
    g1 = tuple(c.name for c in measured if c.empty_response)
    g2: list[str] = []
    for c in measured:
        lo, hi = cfg.bands.get(c.battery, (0.0, 100.0))
        if not lo <= c.score <= hi:
            g2.append(f"{c.name}: score {c.score:.1f} outside band [{lo}, {hi}]")
    if g1 or g2:
        return Judgment(
            "fail", g1, tuple(g2), error_rate, requeue,
            f"G1 empty-response cells: {len(g1)}; G2 band violations: {len(g2)}",
        )
    return Judgment("pass", (), (), error_rate, requeue,
                    f"{len(measured)} measured cells pass G1+G2 (infra {len(infra)})")


class _BatteryRunner(Protocol):
    def run_battery(self, model_id: str, battery: Any) -> BenchOutcome: ...


@dataclass(slots=True)
class Budget:
    max_requests: int
    max_total_tokens: int
    requests: int = 0
    tokens: int = 0

    def spent(self) -> bool:
        return self.requests >= self.max_requests or self.tokens >= self.max_total_tokens

    def consume(self, outcome: BenchOutcome) -> None:
        self.requests += 1
        self.tokens += max(outcome.tokens_in, 0) + max(outcome.tokens_out, 0)


@dataclass(slots=True)
class PreflightRun:
    judgment: Judgment
    sweep_id: str
    cells: tuple[CellView, ...] = ()
    g3_ok: bool = False
    budget: Budget = field(default_factory=lambda: Budget(0, 0))


def run_preflight(
    runner: _BatteryRunner,
    *,
    models: Sequence[str],
    batteries: Sequence[str],
    cfg: PreflightConfig,
    store: Callable[[str, str, str, BenchOutcome], None],
    read_back: Callable[[str], int],
    marker_row: Callable[[str], None],
    new_sweep_id: Callable[[], str] = make_sweep_id,
) -> PreflightRun:
    """Execute the session: run every cell under the budget, store every
    outcome, verify read-back counts (G3), and only a fully clean pass writes
    the freshness marker. ``store``/``read_back``/``marker_row`` are injected so
    the executor is testable against the real storage seam."""
    from hr.models import BenchmarkCategory

    sweep_id = new_sweep_id()
    budget = Budget(cfg.max_requests, cfg.max_total_tokens)
    cells: list[CellView] = []
    budget_hit = False
    for model_id in models:
        for battery_code in batteries:
            if budget.spent():
                budget_hit = True
                break
            outcome = runner.run_battery(model_id, BenchmarkCategory(battery_code))
            budget.consume(outcome)
            store(sweep_id, model_id, battery_code, outcome)
            cells.append(cell_view(outcome))
        if budget_hit:
            break
    judgment = judge(cells, cfg)
    if budget_hit:
        judgment = Judgment(
            "cannot-answer", judgment.g1_violations, judgment.g2_violations,
            judgment.error_rate, judgment.requeue,
            f"budget cap reached ({budget.requests} requests, {budget.tokens} tokens) "
            f"after {len(cells)} cells - session incomplete, answers nothing",
        )
    stored = read_back(sweep_id)
    g3_ok = stored >= len(cells) and len(cells) > 0
    if judgment.verdict == "pass" and g3_ok:
        marker_row(sweep_id)
    return PreflightRun(judgment, sweep_id, tuple(cells), g3_ok, budget)


def write_marker(conn, sweep_id: str) -> None:
    """Freshness marker = a sweep row with purpose='preflight-pass'."""
    from hr.bench.engine_storage import SEAT_CODE
    from hr.stage0_storage import _insert_sweep

    # Its own row id: the data sweep row under sweep_id carries purpose
    # 'livebench'; the marker must survive the ON CONFLICT DO NOTHING. The
    # seat_code FK requires a registered seat, so the marker rides the
    # capability seat code like every other sweep row.
    _insert_sweep(conn, f"{sweep_id}-pass", SEAT_CODE, PREFLIGHT_PURPOSE)


def marker_age_hours(conn) -> float | None:
    """Hours since the newest preflight marker; None when there is none."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT max(created_at) FROM hr.sweep WHERE purpose = %s",
            (PREFLIGHT_PURPOSE,),
        )
        newest = cur.fetchone()[0]
    if newest is None:
        return None
    return (datetime.now(timezone.utc) - newest).total_seconds() / 3600.0


class _CappedAdapter:
    """Adapter proxy: clamps every chat's max_output to the session cap and
    forwards everything else (probe_capabilities, endpoint_for, ...) verbatim.
    Chat signature mirrors hr.adapters.base.Adapter.chat - the engine calls it
    with model_id/messages positional and the rest by keyword."""

    def __init__(self, inner: Adapter, cap: int) -> None:
        self._inner = inner
        self._cap = cap

    def chat(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        images: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        thinking_budget: int | None = None,
        max_output: int = 16384,
        timeout_s: int = 600,
    ) -> ModelResponse:
        return self._inner.chat(
            model_id,
            messages,
            images=images,
            tools=tools,
            thinking_budget=thinking_budget,
            max_output=min(max_output, self._cap),
            timeout_s=timeout_s,
        )

    def probe_capabilities(self, model_id: str) -> Capabilities:
        return self._inner.probe_capabilities(model_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def with_output_cap(
    factory: Callable[[str], Adapter], cap: int
) -> Callable[[str], Adapter]:
    """Wrap an adapter factory so every request's max_output is clamped to
    ``cap`` - the bound for unbounded-thinking anchors that turns an
    uncontrolled 16K-token generation into a budgeted one (S3 budget item)."""

    def make(model_id: str) -> Adapter:
        return _CappedAdapter(factory(model_id), cap)

    return make


__all__ = [
    "with_output_cap",
    "Budget",
    "CellView",
    "Judgment",
    "PREFLIGHT_PURPOSE",
    "PreflightConfig",
    "PreflightRun",
    "cell_view",
    "judge",
    "load_preflight_config",
    "marker_age_hours",
    "run_preflight",
    "write_marker",
]
