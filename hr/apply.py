"""hr apply — verdict → FastDraw preset/state bridge over the file contract.

``hr apply`` reads the latest verdict seating and writes FastDraw's two JSON
files. The contract is the files only — FastDraw code (fastdraw/server.ts) is
NEVER imported or run, and its sources are read-only; the shapes written here
must satisfy what the plugin parses at load time:

* ``<config_dir>/fastdraw-presets.json``
  ``{"presets": {NAME: {"description": ..., "createdAt": ... ISO-8601 UTC,
  "agents": {agent: "provider/model"}}}}`` — ``isModelMap`` requires every
  agents value to contain ``/``; ``savePresets`` round-trips the store via
  ``JSON.stringify(store, null, 2)`` and ``loadPresets`` refuses anything
  whose ``presets`` member is not an object.
* ``<config_dir>/.fastdraw.json`` — ``{"agents": {agent: "provider/model"}}``;
  the plugin's ``config(cfg)`` hook applies these assignments at opencode
  BOOT, which is why ``--set-state`` effects require an opencode restart.

Config dir resolution goes through the hr config layer
(``hr.config.opencode_config_dir``: ``OPENCODE_CONFIG_DIR`` env >
``~/.config/opencode``) — the same directory the plugin's hardcoded
``CONFIG_DIR = homedir()/.config/opencode`` maps to in the default setup.

The verdict seating is COMPUTED per sweep (verdict queries never persist an
assignment — ``hr.assignment`` has no writers); the bridge therefore
recomputes it with the exact same code path as ``hr verdict``
(``hr.decision.seat_assignments`` + the shared ranker). If that computation finds
no seating (no sweeps, or no seat got a recommendation), the bridge REFUSES
with a non-zero exit naming the cause — presets are never clobbered with
empty data.

Seat keys are normalized to runtime agent names: seat codes use underscores
(``visual_engineering``), opencode agent names use hyphens
(``visual-engineering``).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from hr.decision import (
    SeatAssignment,
    battery_codes,
    capability_means,
    latest_sweep_id,
    model_capabilities,
    seat_assignments,
    seat_rows,
    separation_probabilities,
)
from hr.config import opencode_config_dir
from hr.deployable import load_deployable
from hr.health import sweep_health

PRESETS_FILENAME = "fastdraw-presets.json"
STATE_FILENAME = ".fastdraw.json"

# FastDraw's isModelMap(): every agents value must contain "/".
_MODEL_ID_RULE = ".+/.+"


def _agent_name(seat_code: str) -> str:
    """Runtime opencode agent name for a seat code (underscores → hyphens)."""
    return seat_code.replace("_", "-")


def latest_assignments(conn, deployable: Optional[set[str]] = None) -> tuple[list[SeatAssignment], str]:
    """Recompute the verdict seating for the latest sweep (same path as hr verdict).

    Returns ``(assignments, sweep_id)`` where assignments is
    ``decision.seat_assignments`` output — one structured entry per seat. Raises
    RuntimeError naming the cause when there is no sweep to seat.
    """
    try:
        sweep_id = latest_sweep_id(conn)
    except ValueError as exc:  # no sweeps at all
        raise RuntimeError(
            "cannot apply: no verdict seating exists — no sweeps in the DB "
            "(latest_sweep_id error: %s); refusing to write FastDraw presets "
            "with empty data" % exc
        ) from exc
    means = capability_means(conn, sweep_id)
    reports = sweep_health(conn, sweep_id)
    codes = battery_codes(conn)
    seat_db = seat_rows(conn)
    caps_db = model_capabilities(conn)
    separations = separation_probabilities(conn, sweep_id)
    deployable_ids = set(deployable) if deployable is not None else set(load_deployable())
    pool = set(means) & set(deployable_ids)
    assignments = seat_assignments(
        pool, means, reports, seat_db, caps_db, codes,
        retired_set=set(), include_retired=False,
        separations=separations,
    )
    return assignments, sweep_id


def agents_from_assignments(assignments: list[SeatAssignment]) -> dict[str, str]:
    """{agent name: primary model id} for every seat with a recommendation.

    Seats without a recommendation (``primary`` None) are skipped.
    """
    agents: dict[str, str] = {}
    for a in assignments:
        if a["primary"] is None:
            continue
        agents[_agent_name(a["seat_code"])] = a["primary"]
    return agents


def validate_agents(agents: dict[str, str]) -> None:
    """Refuse to bridge an empty seating or model ids FastDraw will reject.

    Raises RuntimeError naming the cause; never returns a partial verdict.
    Uses a regex render of FastDraw's isModelMap rule (value contains "/"),
    with the file-contract caveat documented on the module.
    """
    if not agents:
        raise RuntimeError(
            "cannot apply: no verdict seating — no seat received a recommended "
            "model (run `hr verdict --latest` to see why); refusing to write "
            "FastDraw presets with empty data"
        )
    bad = {agent: model for agent, model in agents.items() if re.fullmatch(_MODEL_ID_RULE, model) is None}
    if bad:
        raise RuntimeError(
            "cannot apply: %d binding(s) lack the 'provider/model' shape "
            "FastDraw's isModelMap requires: %r; refusing to write "
            "fastdraw-presets.json" % (len(bad), bad)
        )


def _load_store(path: Path) -> dict:
    """Existing presets store, or a fresh one. Corrupt/wrong-shape file → refuse."""
    if not path.exists():
        return {"presets": {}}
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        # OSError: unreadable file — refuse rather than clobber a live plugin file.
        raise RuntimeError(
            "cannot apply: existing presets file is not readable JSON (%s): %s; "
            "refusing to overwrite" % (path, exc)
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("presets"), dict):
        raise RuntimeError(
            "cannot apply: existing presets file has an unexpected shape (%s); "
            "refusing to overwrite" % path
        )
    return raw


def write_preset(
    agents: dict[str, str],
    name: str,
    config_dir: Path,
    *,
    description: Optional[str] = None,
    created_at: Optional[str] = None,
) -> Path:
    """Merge ``{name: {description, createdAt, agents}}`` into the presets store.

    Other presets are preserved byte-for-byte; re-running with the same name
    replaces only that entry. Returns the written path.
    """
    path = config_dir / PRESETS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    store = _load_store(path)
    store["presets"][name] = {
        "description": description or "",
        "createdAt": created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "agents": agents,
    }
    path.write_text(json.dumps(store, indent=2) + "\n")
    return path


def write_state(agents: dict[str, str], config_dir: Path) -> Path:
    """Write the boot-time state file ``{agents: {...}}`` (same map as the preset)."""
    path = config_dir / STATE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": agents}, indent=2) + "\n")
    return path


def apply(
    conn,
    *,
    preset_name: Optional[str] = None,
    set_state: bool = False,
    config_dir: Optional[Path] = None,
    deployable: Optional[set[str]] = None,
) -> str:
    """Bridge: latest verdict seating → FastDraw preset (+ state with ``--set-state``).

    Returns a human summary for the CLI to print. Refuses (RuntimeError) when
    no verdict seating exists or the seating cannot satisfy the file contract.
    """
    cfg_dir = Path(config_dir) if config_dir is not None else opencode_config_dir()
    assignments, sweep_id = latest_assignments(conn, deployable=deployable)
    agents = agents_from_assignments(assignments)
    validate_agents(agents)
    name = preset_name or f"verdict-{date.today().isoformat()}"
    description = f"hr verdict seating from sweep {sweep_id}"
    preset_path = write_preset(agents, name, cfg_dir, description=description)
    summary = [
        f"preset {name!r} -> {preset_path} ({len(agents)} agent"
        f"{'s' if len(agents) != 1 else ''})",
    ]
    if set_state:
        state_path = write_state(agents, cfg_dir)
        summary.append(f"state -> {state_path}")
        summary.append(
            "NOTE: opencode restart required — FastDraw's config hook applies "
            ".fastdraw.json only at boot; presets take effect at tool-call time."
        )
    return "\n".join(summary)
