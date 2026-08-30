
from __future__ import annotations

import argparse
import sys
from pathlib import Path


from hr.config import itemrepo_path
from hr.stats.sequential import SequentialConfig

# Reuse stage0's helpers and DB plumbing.
from hr.stage0 import (
    _AdapterFacade,
    _connect,
    _print_matrix,
)
from hr.stage1_plan import build_finals_plan, print_finals_plan
from hr.stage1_selection import (
    DEFAULT_THRESHOLDS_PATH,
    STAGE1_DECIDING_BATTERIES,
    STAGE1_N_INITIAL,
    STAGE1_N_MAX,
    STAGE1_SEAT_CODE,
    STAGE1_TOKEN_CAP,
    FinalistSelection,
    load_full_banks,
    select_finalists_from_stage0,
)
def read_finals_separation_from_db(sweep_id: str) -> dict[str, list[dict]]:
    """Load the persisted separation matrix for a Stage 1 sweep."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie
            FROM hr.separation WHERE sweep_id = %s
                ORDER BY battery_id, model_a, model_b
                """,
                (sweep_id,),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    sep: dict[str, list[dict]] = {}
    for battery_id, a, b, ps, pw, pt in rows:
        battery_code = battery_id.replace("battery-", "")
        sep.setdefault(battery_code, []).append(
            {
                "model_a": a,
                "model_b": b,
                "p_separated": float(ps),
                "p_weak": float(pw),
                "p_tie": float(pt),
            }
        )
    return sep


def list_finals_sweeps() -> list[tuple[str, str, str]]:
    """Return (sweep_id, purpose, created_at) for all Stage 1 finals sweeps in DB."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sweep_id, purpose, created_at
            FROM hr.sweep WHERE seat_code = %s
                ORDER BY created_at DESC
                """,
                (STAGE1_SEAT_CODE,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hr.stage1", description="Stage 1 finals runner.")
    p.add_argument("--dry-run", action="store_true", help="Print plan + finalist selection (no API calls)")
    p.add_argument("--run", action="store_true", help="Run the finals sweep with live adapter")
    p.add_argument("--report", action="store_true", help="Print separation matrix from DB")
    p.add_argument("--sweep-id", default=None, help="Specific sweep id for --report or --run resume")
    p.add_argument("--token-cap", type=int, default=STAGE1_TOKEN_CAP, help="Token budget cap (default 90M)")
    p.add_argument("--n-initial", type=int, default=STAGE1_N_INITIAL, help="Pilot rounds (default 3)")
    p.add_argument("--n-max", type=int, default=STAGE1_N_MAX, help="Max rounds per battery (default 10)")
    p.add_argument("--thresholds", default=str(DEFAULT_THRESHOLDS_PATH), help="Path to thresholds.yaml")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model ids (overrides finalist selection from DB)",
    )
    p.add_argument(
        "--item-repo",
        default=None,
        help="Path to item repo (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Do not record to the DB (for local testing)",
    )
    p.add_argument(
        "--use-routed-adapter",
        action="store_true",
        help="Use RoutedAdapter (dispatches bailian/kimi→Anthropic, deepseek→OpenAI)",
    )
    return p


def _cli_main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # Parse optional model override.
    finalists_override: list[str] | None = None
    if args.models:
        finalists_override = [m.strip() for m in args.models.split(",") if m.strip()]

    item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
    thresholds_path = Path(args.thresholds)

    # --dry-run: just print the plan.
    if args.dry_run:
        if finalists_override is not None:
            # Override is authoritative — no Stage-0 DB read and no fleet
            # discovery needed (the override replaces the selection anyway).
            selection = FinalistSelection(
                per_battery={},
                finalists=list(finalists_override),
                rationale=f"User-provided finalist list: {finalists_override}",
            )
        else:
            try:
                selection = select_finalists_from_stage0(
                    deciding_batteries=STAGE1_DECIDING_BATTERIES,
                    allow_db_missing=False,
                )
            except RuntimeError as e:
                print(f"Cannot select finalists: {e}", file=sys.stderr)
                return 1
        full_banks = load_full_banks(item_repo, batteries=STAGE1_DECIDING_BATTERIES)
        if thresholds_path.exists():
            seq_config = SequentialConfig.from_yaml(str(thresholds_path))
        else:
            seq_config = SequentialConfig(
                thresholds={"reasoning": 2.0, "hallucination": 2.0, "tool_a": 3.0, "vision": 3.0},
                n_initial=args.n_initial,
                n_max=args.n_max,
            )
        seq_config.n_initial = args.n_initial
        seq_config.n_max = args.n_max
        plan = build_finals_plan(selection.finalists, full_banks, seq_config, budget_cap=args.token_cap)
        plan.finalist_selection = selection
        print_finals_plan(plan, selection)
        return 0

    # --report: read from DB.
    if args.report:
        sweep_id = args.sweep_id
        if sweep_id is None:
            try:
                sweeps = list_finals_sweeps()
            except Exception as e:
                print(f"DB not available: {e}", file=sys.stderr)
                return 1
            if not sweeps:
                print("No Stage 1 finals sweeps recorded yet.", file=sys.stderr)
                return 1
            sweep_id = sweeps[0][0]
        sep = read_finals_separation_from_db(sweep_id)
        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return 0

    # --run (default): run with live adapter.
    from hr.adapters import RoutedAdapter

    adapter: _AdapterFacade = RoutedAdapter()
    try:
        from hr.stage1 import run_finals

        run_finals(
            adapter=adapter,
            item_repo=item_repo,
            finalists=finalists_override,
            thresholds_path=thresholds_path,
            n_initial=args.n_initial,
            n_max=args.n_max,
            token_cap=args.token_cap,
            sweep_id=args.sweep_id,
            init_db=not args.no_db,
            record_to_db=not args.no_db,
        )
    except Exception as e:
        print(f"Stage 1 failed: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return _cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
