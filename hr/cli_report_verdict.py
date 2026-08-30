from __future__ import annotations

from .cli_app import _runtime_load_deployable
from .cli_report_base import (
    _tag_retired_rows,
    _verdict_gates,
    _verdict_seats,
    build_sweeps_report,
)
from .decision import (
    battery_codes,
    capability_means,
    latest_sweep_id,
    measurement_count,
    model_capabilities,
    seat_rows,
    separation_probabilities,
)
from .health import summary_table, sweep_health

def build_verdict_report(
    conn, sweep_id: str, *, include_retired: bool = False,
    deployable: set[str] | None = None,
) -> str:
    dep = deployable if deployable is not None else _runtime_load_deployable()
    means = capability_means(conn, sweep_id)
    reports = sweep_health(conn, sweep_id)
    codes = battery_codes(conn)
    n_meas = measurement_count(conn, sweep_id)
    seat_db = seat_rows(conn)
    caps_db = model_capabilities(conn)
    separations = separation_probabilities(conn, sweep_id)

    model_ids = sorted(means)
    retired = sorted(m for m in model_ids if m not in dep)
    retired_set = set(retired)
    pool = set(model_ids) if include_retired else set(model_ids) - retired_set

    header = (
        f"# Verdict — sweep {sweep_id}\n"
        f"n measurements: {n_meas} · "
        "zero new API calls (mined from existing measurements) · "
        f"deployable pool: {len(pool)}/{len(model_ids)} models "
        "(iron rule 5: retired models never assigned)"
    )

    # (a) capability battery averages
    cap_lines = ["| model | " + " | ".join(codes) + " |"]
    cap_lines.append("|---" * (len(codes) + 1) + "|")
    for model_id in model_ids:
        shown = model_id + (" ⚠ retired" if model_id in retired_set else "")
        cap_lines.append(
            "| " + shown + " | "
            + " | ".join(
                f"{means[model_id].get(bc, 0.0):.3f}" if bc in means[model_id] else "—"
                for bc in codes
            )
            + " |"
        )
    cap_table = "\n".join(cap_lines)

    # (b) health
    health_table = (
        _tag_retired_rows(summary_table(reports), retired_set)
        if reports else "_no health data_"
    )

    # (c) gates
    gate_table = "\n".join(
        f"| {level} | {cells} |" for level, cells in _verdict_gates(reports)
    ) or "_no models_"

    # (c2) retired section
    retired_table = ""
    if retired:
        retired_table = (
            "| model | sweep measurements | status |\n|---|---|---|\n"
            + "\n".join(
                f"| {mid} | {reports[mid].n_measurements if mid in reports else '—'} "
                f"| retired from opencode.jsonc; excluded from assignment |"
                for mid in retired
            )
        )

    # (d) seats
    seat_note = (
        "Recommended assignments use rank() with a documented simplified "
        "fitness: rolespec knob weights mapped onto runtime DB battery codes "
        "({reasoning→reasoning, top_tool_fraction→tool_a, "
        "coverage→hallucination, longctx→livebench_long_context, "
        "speed_cost→livebench_speed} — repointable via the `knob_battery:` "
        "section of configs/thresholds.yaml; a knob whose battery has no "
        "data contributes 0 with a logged warning); ranking input = weighted "
        "battery means. Directional bootstrap separation resolves statistically "
        "supported top-pair outcomes. Models failing a seat's health gate are "
        "excluded (gate_level per seat). "
        "Candidates limited to the deployable set (iron rule 5); "
        f"{'retired models included and tagged ⚠' if include_retired else 'retired models are never assigned'}."
    )
    seat_rows_out = _verdict_seats(
        pool,
        means,
        reports,
        seat_db,
        caps_db,
        codes,
        retired_set,
        include_retired,
        separations,
    )
    seat_lines = [
        "| seat | gate level | primary | fallback 1/2 | eliminated |",
        "|---|---|---|---|---|",
    ]
    for r in seat_rows_out:
        seat_lines.append("| " + " | ".join(str(c) for c in r) + " |")
    seat_table = "\n".join(seat_lines)

    sections = [
        header,
        f"## Capability battery averages (per model)\n{cap_table}",
        f"## Health (full pool)\n{health_table}",
        f"## Health gate status per level\n{gate_table}",
    ]
    if retired_table:
        sections.append(f"## Retired models (excluded from assignment)\n{retired_table}")
    sections.append(f"## Recommended seat assignment\n{seat_note}\n{seat_table}")
    return "\n\n".join(sections)


def build_status_report(conn) -> str:
    """DB-only status: sweeps and latest-sweep capability means.

    Zero API calls — everything is mined from already-run measurements,
    mirroring ``sweeps``/``verdict``. Retired models (not in the deployable
    pool) are tagged ⚠ like ``build_verdict_report`` does.
    """
    sweep_id = latest_sweep_id(conn)
    means = capability_means(conn, sweep_id)
    codes = battery_codes(conn)
    deployable = set(_runtime_load_deployable())
    model_ids = sorted(means)
    retired = sorted(m for m in model_ids if m not in deployable)

    cap_lines = ["| model | " + " | ".join(codes) + " |"]
    cap_lines.append("|---" * (len(codes) + 1) + "|")
    for model_id in model_ids:
        shown = model_id + (" ⚠ retired" if model_id in retired else "")
        cap_lines.append(
            "| " + shown + " | "
            + " | ".join(
                f"{means[model_id].get(bc, 0.0):.3f}" if bc in means[model_id] else "—"
                for bc in codes
            )
            + " |"
        )
    cap_table = "\n".join(cap_lines)

    return (
        f"# Status — latest sweep {sweep_id}\n"
        f"deployable pool: {len(set(model_ids) & deployable)}/{len(model_ids)} "
        "models · zero new API calls (mined from existing measurements)\n\n"
        f"{build_sweeps_report(conn)}\n\n"
        f"## Capability battery averages (per model)\n{cap_table}"
    )
