#!/usr/bin/env python3
"""Seed ``hr2.seat`` from ``configs/seats.yaml`` (hard-gate plumbing).

Equivalent invocation of the same entry point: ``python3 -m hr.seats.seed``.
Full semantics live in ``hr/seats/seed.py``: idempotent upserts keyed by
``seat_code``, yaml-only seat data, and the generic sweep-seat fallback used
by stage0/stage1. Exit 0 on success, 1 on failure.
"""
from hr.seats.seed import main

if __name__ == "__main__":
    raise SystemExit(main())