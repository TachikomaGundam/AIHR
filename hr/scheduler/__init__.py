"""Failure taxonomy for scheduler decisions."""

from hr.scheduler.taxonomy import (
    FailureCode,
    classify_failure,
    should_exclude_zero,
    retryable,
)

__all__ = [
    "FailureCode",
    "classify_failure",
    "should_exclude_zero",
    "retryable",
]
