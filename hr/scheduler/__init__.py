"""hr2.scheduler — failure taxonomy (control/sweep state machines archived)."""

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