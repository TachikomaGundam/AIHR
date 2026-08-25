from __future__ import annotations

import argparse
import sys
from pathlib import Path

from hr.config import itemrepo_path
from hr.fleet import fleet_models
from hr.stage0_plan import build_call_plan, print_call_plan
from hr.stage0_selection import STAGE0_BATTERIES, STAGE0_SEAT_CODE, _stage0_token_cap, select_subsets
from hr.stage0_stats import _print_matrix
from hr.stage0_storage import _connect

# ---------------------------------------------------------------------------
# Read back from DB (for --separation)
# ---------------------------------------------------------------------------
def read_separation_from_db(sweep_id: str) -> dict[str, list[dict]]:
    """Load the persisted separation matrix for a sweep."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT battery_id, model_a, model_b, p_separated, p_weak, p_tie "
            "FROM hr.separation WHERE sweep_id = %s ORDER BY battery_id, model_a, model_b",
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


def list_sweeps() -> list[tuple[str, str, str]]:
    """Return list of (sweep_id, purpose, created_at) from the DB."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
            "SELECT sweep_id, purpose, created_at FROM hr.sweep "
                "WHERE seat_code = %s ORDER BY created_at DESC",
                (STAGE0_SEAT_CODE,),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hr.stage0", description="Stage 0 full-pool sweep runner.")
    p.add_argument("--dry-run", action="store_true", help="Print call plan (no API calls)")
    p.add_argument("--pilot", action="store_true", help="Run pilot n=3 for all models")
    p.add_argument(
        "--separation",
        action="store_true",
        help="Read separation matrix from DB for the most recent stage0 sweep",
    )
    p.add_argument(
        "--sweep-id",
        default=None,
        help="Sweep ID to query for --separation (default: latest stage0 sweep)",
    )
    p.add_argument("--token-cap", type=int, default=None, help="Token budget cap (default: configs/thresholds.yaml stage0.token_cap)")
    p.add_argument("--n-initial", type=int, default=3, help="Number of pilot repetitions")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model ids to sweep (subset of the pool; e.g. for appending new models)",
    )
    p.add_argument(
        "--item-repo",
        default=None,
        help="Path to the item repository (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    p.add_argument(
        "--no-db",
        action="store_true",
        help="Do not record to the DB (for testing)",
    )
    return p


def _cli_main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    if args.token_cap is None:
        args.token_cap = _stage0_token_cap()

    if args.dry_run:
        # Load items and print plan.
        from hr.calibrate import load_item_repo

        models = fleet_models()
        if args.models:
            wanted = [m.strip() for m in args.models.split(",") if m.strip()]
            unknown = [m for m in wanted if m not in models]
            if unknown:
                print(f"Unknown model ids: {unknown}", file=sys.stderr)
                return 1
            models = tuple(wanted)
        item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
        items_by_battery = load_item_repo(item_repo, batteries=list(STAGE0_BATTERIES))
        subsets = select_subsets(items_by_battery)
        plan = build_call_plan(
            subsets, models=models, n_initial=args.n_initial, budget_cap=args.token_cap
        )
        print_call_plan(plan)
        return 0

    if args.separation:
        sweep_id = args.sweep_id
        if sweep_id is None:
            try:
                sweeps = list_sweeps()
            except Exception as e:
                print(f"DB not available: {e}", file=sys.stderr)
                return 1
            if not sweeps:
                print("No Stage 0 sweeps recorded yet.", file=sys.stderr)
                return 1
            sweep_id = sweeps[0][0]
        sep = read_separation_from_db(sweep_id)
        print(f"Sweep ID: {sweep_id}")
        _print_matrix(sep)
        return 0

    if not (args.pilot or args.separation or args.dry_run):
        # Default: run the full sweep (with live adapter).
        pass

    # Build live adapter. Pool may now include multiple provider families
    # (bailian-token-plan + kimi-for-coding via Anthropic, deepseek via OpenAI),
    # so use the routed adapter that dispatches per model_id.
    from hr.adapters import RoutedAdapter

    models = fleet_models()
    if args.models:
        wanted = [m.strip() for m in args.models.split(",") if m.strip()]
        unknown = [m for m in wanted if m not in models]
        if unknown:
            print(f"Unknown model ids: {unknown}", file=sys.stderr)
            return 1
        models = tuple(wanted)

    adapter = RoutedAdapter()
    item_repo = Path(args.item_repo) if args.item_repo else itemrepo_path()
    try:
        from hr.stage0 import run_sweep

        run_sweep(
            adapter=adapter,
            item_repo=item_repo,
            models=models,
            n_initial=args.n_initial,
            token_cap=args.token_cap,
            init_db=not args.no_db,
            record_to_db=not args.no_db,
        )
    except Exception as e:
        print(f"Stage 0 failed: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    return _cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
