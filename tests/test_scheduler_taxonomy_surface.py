"""Scheduler failure-taxonomy contract tests (committed surface).

Covers hr.scheduler.taxonomy: failure classification precedence,
retry policy, the 10-minute corroboration window helpers, and the
three-rule zero-score exclusion decision. Offline and deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hr.scheduler.taxonomy import (
    ControlReadingPoint,
    FailureCode,
    FailureDescriptor,
    InfraIncidentPoint,
    MIN_SAME_PROVIDER_FAILURES,
    WINDOW_MINUTES,
    _within_window,
    classify_failure,
    control_degraded_in_window,
    matching_incident,
    retryable,
    same_provider_failure_count,
    should_exclude_zero,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def test_classify_timeout_takes_precedence() -> None:
    d = classify_failure(status_code=200, timed_out=True)
    assert d.code == FailureCode.TIMEOUT


def test_classify_empty_response_on_200_or_unknown() -> None:
    assert classify_failure(status_code=200, empty_body=True).code == FailureCode.EMPTY_RESPONSE
    assert classify_failure(status_code=None, empty_body=True).code == FailureCode.EMPTY_RESPONSE
    assert classify_failure(status_code=500, empty_body=True).code == FailureCode.SERVER_5XX


def test_classify_by_status_ranges() -> None:
    assert classify_failure(status_code=429).code == FailureCode.RATE_LIMIT
    assert classify_failure(status_code=404).code == FailureCode.GATEWAY_4XX
    assert classify_failure(status_code=503).code == FailureCode.SERVER_5XX
    assert classify_failure(status_code=302).code == FailureCode.UNKNOWN
    d = classify_failure(status_code=429)
    assert d.status_code == 429


def test_classify_message_keywords() -> None:
    assert classify_failure(error_message="content_filter matched").code == FailureCode.CONTENT_FILTER
    assert classify_failure(error_message="content filtered by policy").code == FailureCode.CONTENT_FILTER
    assert classify_failure(error_message="schema is invalid").code == FailureCode.SCHEMA_INVALID
    assert classify_failure(error_message="Schema Invalid!").code == FailureCode.SCHEMA_INVALID
    assert classify_failure(error_message="random failure").code == FailureCode.UNKNOWN


def test_retryable_policy() -> None:
    assert retryable(FailureCode.RATE_LIMIT) is True
    assert retryable(FailureCode.SERVER_5XX) is True
    assert retryable(FailureCode.TIMEOUT) is True
    assert retryable(FailureCode.GATEWAY_4XX) is True
    assert retryable(FailureCode.EMPTY_RESPONSE) is False
    assert retryable(FailureCode.SCHEMA_INVALID) is False
    assert retryable(FailureCode.CONTENT_FILTER) is False
    assert retryable(FailureCode.UNKNOWN) is False


def test_within_window() -> None:
    assert _within_window(NOW, NOW + timedelta(minutes=WINDOW_MINUTES)) is True
    assert _within_window(NOW, NOW + timedelta(minutes=WINDOW_MINUTES + 1)) is False
    assert _within_window(None, NOW) is False
    assert _within_window(NOW, None) is False


def test_control_degraded_in_window() -> None:
    outside = ControlReadingPoint(NOW - timedelta(hours=2), 100.0, 5.0)
    assert control_degraded_in_window(readings=[outside], window_center=NOW) is False
    anomalous = ControlReadingPoint(NOW, 100.0, 0.0)
    assert control_degraded_in_window(readings=[anomalous], window_center=NOW) is True
    assert control_degraded_in_window(readings=[], window_center=NOW) is False


def test_same_provider_failure_count_filters() -> None:
    failures = [
        FailureDescriptor(FailureCode.RATE_LIMIT, provider="p1", model="m1", timestamp=NOW),
        FailureDescriptor(FailureCode.TIMEOUT, provider="p1", model="m2", timestamp=NOW),
        FailureDescriptor(FailureCode.TIMEOUT, provider="p2", model="m3", timestamp=NOW),
        FailureDescriptor(FailureCode.TIMEOUT, provider="p1", model="m4", timestamp=NOW - timedelta(hours=1)),
    ]
    assert (
        same_provider_failure_count(
            failures=failures, exclude_model="m1", provider="p1", window_center=NOW
        )
        == 1
    )


def test_matching_incident() -> None:
    inc = InfraIncidentPoint("p1", NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    assert matching_incident(incidents=[inc], provider="p1", when=NOW) is True
    assert matching_incident(incidents=[inc], provider="p2", when=NOW) is False
    assert matching_incident(incidents=[inc], provider="p1", when=NOW + timedelta(hours=1)) is False


def test_should_exclude_zero_three_rules() -> None:
    degraded = ControlReadingPoint(NOW, 3.0, 0.0)
    ok, reason = should_exclude_zero(
        provider="p1", model="m1", timestamp=NOW, control_readings=[degraded]
    )
    assert (ok, reason) == (True, "control_degraded_in_window")

    siblings = [
        FailureDescriptor(FailureCode.RATE_LIMIT, provider="p1", model="other", timestamp=NOW),
        FailureDescriptor(FailureCode.TIMEOUT, provider="p1", model="other2", timestamp=NOW),
    ]
    ok, reason = should_exclude_zero(
        provider="p1", model="m1", timestamp=NOW, sibling_failures=siblings
    )
    assert (ok, reason) == (True, "sibling_provider_failures")
    assert MIN_SAME_PROVIDER_FAILURES == 2

    inc = InfraIncidentPoint("p1", NOW - timedelta(minutes=1), NOW + timedelta(minutes=1))
    ok, reason = should_exclude_zero(
        provider="p1", model="m1", timestamp=NOW, infra_incidents=[inc]
    )
    assert (ok, reason) == (True, "matching_infra_incident")

    ok, reason = should_exclude_zero(provider="p1", model="m1", timestamp=NOW)
    assert (ok, reason) == (False, "")