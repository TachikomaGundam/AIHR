"""Report provenance tri-state (SIM-LADDER S0, 2026-10-16).

Every human-facing report is generated from one of exactly three data
worlds:

- ``mock``      — in-process fixtures; no live endpoint was called
- ``cassette``  — recorded wire frames replayed through the real parser
- ``live``      — fresh calls to the configured provider endpoints

The distinction is load-bearing: the machine owner's standing order is
that half-validated artifacts must never reach real-user testing, and a
report that cannot state where its numbers came from cannot be triaged.
``source`` is therefore a REQUIRED keyword on every report builder —
omitting it is a Python TypeError before any output exists, and an
out-of-vocabulary value raises ValueError naming the closed set.
There is deliberately no default: defaulting to "live" is precisely the
failure mode this field forbids.
"""

from __future__ import annotations

SOURCE_STATES: tuple[str, ...] = ("mock", "cassette", "live")


def require_source(value: object) -> str:
    """Validate against the closed vocabulary; return the value."""
    if not isinstance(value, str) or value not in SOURCE_STATES:
        raise ValueError(
            f"unknown report source {value!r} — source must be one of "
            "mock/cassette/live (closed vocabulary, hr/report_source.py)"
        )
    return value


__all__ = ["SOURCE_STATES", "require_source"]
