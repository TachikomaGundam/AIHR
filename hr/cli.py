"""Unified public CLI facade and command registry."""

from __future__ import annotations

from .db import connect
from .decision import (
    battery_codes,
    capability_means,
    latest_sweep_id,
    model_capabilities,
    seat_assignments,
    seat_rows,
)
from .deployable import load_deployable
from .cli_app import app
from .cli_report_base import (
    _fetch,
    _tag,
    _tag_retired_rows,
    _verdict_gates,
    _verdict_seats,
    build_health_report,
    build_sweeps_report,
)
from .cli_report_verdict import build_status_report, build_verdict_report
from .cli_inventory import bench, discover, seed
from .cli_selection import _pick_models_interactive, _selection_indices
from .cli_report_commands import calibrate, health, sweeps, verdict
from .cli_knowledge import publish, recommend, reference, research
from .cli_apply import apply, status
from .deployment_manager import register_release_commands

register_release_commands(app)

__all__ = [
    "app",
    "apply",
    "battery_codes",
    "bench",
    "build_health_report",
    "build_status_report",
    "build_sweeps_report",
    "build_verdict_report",
    "calibrate",
    "capability_means",
    "connect",
    "discover",
    "health",
    "latest_sweep_id",
    "load_deployable",
    "model_capabilities",
    "publish",
    "recommend",
    "reference",
    "research",
    "seat_assignments",
    "seat_rows",
    "seed",
    "status",
    "sweeps",
    "verdict",
    "_fetch",
    "_pick_models_interactive",
    "_selection_indices",
    "_tag",
    "_tag_retired_rows",
    "_verdict_gates",
    "_verdict_seats",
]
