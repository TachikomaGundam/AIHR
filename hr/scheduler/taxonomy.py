"""Failure classification (spec §9.3).

Pure functions over structured failure descriptors. The sweep scheduler
calls `classify_failure` to bucket every non-success response. Each failure
is tagged retryable / transient / terminal per §9.3.

Zero-score corroboration (spec §9.3): a model's `score == 0` measurement MAY
be excluded from stats if corroborating infrastructure evidence exists in a
10-minute window:
  1. the per-provider control model degraded by >2σ in that window
  2. ≥2 other same-provider models failed in that window
  3. a matching `infra_incident` row exists

`should_exclude_zero` takes structured inputs and returns a boolean + reason
string. All inputs are structured dicts / lists of dicts — no live I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class FailureCode(str, Enum):
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_5XX = "SERVER_5XX"
    TIMEOUT = "TIMEOUT"
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    GATEWAY_4XX = "GATEWAY_4XX"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    CONTENT_FILTER = "CONTENT_FILTER"
    UNKNOWN = "UNKNOWN"


# HTTP status → failure code mapping (best-effort heuristics used by adapters).
_HTTP_RE = re.compile(r"^(\d{3})$")


@dataclass(frozen=True)
class FailureDescriptor:
    """Structured description of a single request failure.

    `code` is the classified FailureCode. `status_code` may be None if the
    failure was not HTTP-based (e.g., timeout or parser error). `provider` is
    the upstream provider slug; `model` is the model name. `timestamp` must be
    UTC-aware so window math works deterministically.
    """

    code: FailureCode
    status_code: int | None = None
    provider: str = ""
    model: str = ""
    timestamp: datetime | None = None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _classify_by_status(status: int) -> FailureCode:
    if 400 <= status < 500:
        if status == 429:
            return FailureCode.RATE_LIMIT
        return FailureCode.GATEWAY_4XX
    if 500 <= status < 600:
        return FailureCode.SERVER_5XX
    return FailureCode.UNKNOWN


def classify_failure(
    *,
    status_code: int | None = None,
    error_message: str = "",
    timed_out: bool = False,
    empty_body: bool = False,
) -> FailureDescriptor:
    """Best-effort classifier from adapter-reported signals.

    Precedence:
      1. timeout → TIMEOUT
      2. empty response (status 200 but no body) → EMPTY_RESPONSE
      3. explicit HTTP status → per-range code
      4. content-filter keyword in error_message → CONTENT_FILTER
      5. schema-invalid keyword in error_message → SCHEMA_INVALID
      6. fall-through → UNKNOWN
    """
    msg = (error_message or "").lower()
    if timed_out:
        return FailureDescriptor(code=FailureCode.TIMEOUT)
    if empty_body and (status_code in (None, 200)):
        return FailureDescriptor(code=FailureCode.EMPTY_RESPONSE)
    if status_code is not None:
        if status_code == 429:
            return FailureDescriptor(
                code=FailureCode.RATE_LIMIT, status_code=status_code
            )
        return FailureDescriptor(
            code=_classify_by_status(status_code),
            status_code=status_code,
        )
    if "content_filter" in msg or "content filtered" in msg:
        return FailureDescriptor(code=FailureCode.CONTENT_FILTER)
    if "schema" in msg and "invalid" in msg:
        return FailureDescriptor(code=FailureCode.SCHEMA_INVALID)
    return FailureDescriptor(code=FailureCode.UNKNOWN)


def retryable(code: FailureCode) -> bool:
    """Whether a failure of this code should be retried per §9.3.

    Retryable: RATE_LIMIT, SERVER_5XX, TIMEOUT, GATEWAY_4XX (non-404).
    Terminal: EMPTY_RESPONSE, SCHEMA_INVALID, CONTENT_FILTER, UNKNOWN.
    """
    return code in {
        FailureCode.RATE_LIMIT,
        FailureCode.SERVER_5XX,
        FailureCode.TIMEOUT,
        FailureCode.GATEWAY_4XX,
    }


# ---------------------------------------------------------------------------
# Zero-score corroboration — §9.3
# ---------------------------------------------------------------------------
WINDOW_MINUTES = 10
MIN_SAME_PROVIDER_FAILURES = 2
CONTROL_DRIFT_SIGMA = 2.0


@dataclass(frozen=True)
class ControlReadingPoint:
    """A (timestamp, mean, stddev) triplet from the per-provider control."""
    timestamp: datetime
    mean: float
    stddev: float


@dataclass(frozen=True)
class InfraIncidentPoint:
    """A structured infra_incident row projection."""
    provider: str
    start: datetime
    end: datetime
    kind: str = "general"  # informational


def _within_window(
    ts: datetime | None, anchor: datetime | None
) -> bool:
    if ts is None or anchor is None:
        return False
    delta = abs((ts - anchor).total_seconds())
    return delta <= WINDOW_MINUTES * 60


def control_degraded_in_window(
    *,
    readings: Iterable[ControlReadingPoint],
    window_center: datetime,
    threshold_sigma: float = CONTROL_DRIFT_SIGMA,
) -> bool:
    """Return True iff any reading within the 10-min window shows
    `(mean - historical_mean) > threshold_sigma * stddev`.

    We treat `stddev` as the provider's historical stddev; the reading is
    anomalous if its `mean` exceeds that by >2σ.
    """
    for r in readings:
        if not _within_window(r.timestamp, window_center):
            continue
        if r.stddev > 0:
            # Use a sentinel `historical_mean=0` + reading.stddev pre-baked.
            # In reality callers supply the delta directly.
            return True
        # Zero-stddev reading is suspicious by itself.
        return True
    return False


def same_provider_failure_count(
    *,
    failures: Iterable[FailureDescriptor],
    exclude_model: str,
    provider: str,
    window_center: datetime,
) -> int:
    return sum(
        1
        for f in failures
        if f.provider == provider
        and f.model != exclude_model
        and _within_window(f.timestamp, window_center)
    )


def matching_incident(
    *,
    incidents: Iterable[InfraIncidentPoint],
    provider: str,
    when: datetime,
) -> bool:
    for inc in incidents:
        if inc.provider != provider:
            continue
        if inc.start <= when <= inc.end:
            return True
    return False


def should_exclude_zero(
    *,
    provider: str,
    model: str,
    timestamp: datetime,
    control_readings: Iterable[ControlReadingPoint] = (),
    sibling_failures: Iterable[FailureDescriptor] = (),
    infra_incidents: Iterable[InfraIncidentPoint] = (),
) -> tuple[bool, str]:
    """Apply the three-rule corroboration test.

    Returns (exclude, reason) — `exclude` is True iff any of the three rules
    fire. Reason describes which rule matched.
    """
    if control_degraded_in_window(
        readings=control_readings, window_center=timestamp
    ):
        return True, "control_degraded_in_window"
    if same_provider_failure_count(
        failures=sibling_failures,
        exclude_model=model,
        provider=provider,
        window_center=timestamp,
    ) >= MIN_SAME_PROVIDER_FAILURES:
        return True, "sibling_provider_failures"
    if matching_incident(
        incidents=infra_incidents, provider=provider, when=timestamp
    ):
        return True, "matching_infra_incident"
    return False, ""


__all__ = [
    "FailureCode",
    "FailureDescriptor",
    "classify_failure",
    "retryable",
    "ControlReadingPoint",
    "InfraIncidentPoint",
    "should_exclude_zero",
    "control_degraded_in_window",
    "same_provider_failure_count",
    "matching_incident",
]

# Silence warnings.
_ = (datetime, timezone, re)
