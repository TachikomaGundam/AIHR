from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hr.adapters import adapter_for
from hr.calibration_items import ACCEPTANCE_BANDS, BATTERY_TYPES, CONCURRENCY_PER_PROVIDER, EST_TOKENS_PER_CALL, TOKEN_CAP, _compute_pool_hash, load_anchors, load_item_repo
from hr.calibration_models import _report_to_dict, print_rendered_report
from hr.calibration_runner import CalibrationRunner
from hr.config import itemrepo_path

def dry_run_report(
    item_repo: Path,
    *,
    anchors: dict[str, str] | None = None,
    batteries: list[str] | None = None,
    token_cap: int = TOKEN_CAP,
) -> str:
    """Human-readable dry-run plan without any API calls."""
    anchors = dict(anchors) if anchors else load_anchors()
    wanted = list(batteries or BATTERY_TYPES.keys())
    items_by_battery = load_item_repo(item_repo, batteries=wanted)

    lines: list[str] = ["== hr calibration --dry-run ==", ""]
    lines.append("Item counts per battery:")
    total_items = 0
    for bat in wanted:
        n = len(items_by_battery.get(bat, []))
        total_items += n
        lines.append(f"  {bat}: {n}")
    lines.append(f"  TOTAL: {total_items}")
    lines.append("")

    lines.append("Anchors:")
    for key, model_id in anchors.items():
        lines.append(f"  {key}: {model_id}")
    lines.append("")

    total_calls = total_items * len(anchors)
    est_tokens = total_calls * EST_TOKENS_PER_CALL
    est_wall = (total_calls / max(len(anchors), 1)) / CONCURRENCY_PER_PROVIDER
    lines.append(f"Total calls: {total_calls}")
    lines.append(
        f"Estimated tokens: {est_tokens} "
        f"(cap = {token_cap}, {EST_TOKENS_PER_CALL}/call est.)"
    )
    lines.append(
        f"Estimated wall-clock: ~{est_wall:0.1f}s at "
        f"{CONCURRENCY_PER_PROVIDER} concurrent/provider"
    )
    lines.append("")

    lines.append("Acceptance bands (spec §11):")
    for tier, (label, lo, hi) in ACCEPTANCE_BANDS.items():
        lines.append(f"  tier{tier} ({label}): [{lo:0.0%}, {hi:0.0%}]")
    lines.append("")
    lines.append("Batteries checked:")
    for bat in wanted:
        lines.append(f"  - {bat}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hr.calibrate",
        description="Stage-0 anchor calibration runner",
    )
    parser.add_argument(
        "--item-repo",
        type=Path,
        default=None,
        help="Path to item repo (default: HR_ITEMREPO env or HR_HOME/itemrepo)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print call plan WITHOUT calling APIs and exit",
    )
    parser.add_argument(
        "--anchors",
        type=str,
        default=None,
        help="Comma-separated anchor keys "
        "(e.g. 'cheap,mid' or 'cheap,mid,expensive')",
    )
    parser.add_argument(
        "--batteries",
        type=str,
        default=None,
        help="Comma-separated battery names (default: all Stage-0)",
    )
    parser.add_argument(
        "--token-cap",
        type=int,
        default=TOKEN_CAP,
        help=f"Token budget cap (default: {TOKEN_CAP})",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip (anchor, item) pairs already recorded",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print JSON report after run",
    )
    args = parser.parse_args(argv)

    item_repo = args.item_repo if args.item_repo is not None else itemrepo_path()

    anchor_keys = (
        [k.strip() for k in args.anchors.split(",")] if args.anchors else None
    )
    anchors: dict[str, str] | None = None
    if anchor_keys:
        known = load_anchors()
        anchors = {k: known[k] for k in anchor_keys if k in known}
        missing = [k for k in anchor_keys if k not in known]
        if missing:
            print(f"unknown anchor: {missing}", file=sys.stderr)
            return 2

    batteries = (
        [b.strip() for b in args.batteries.split(",")]
        if args.batteries
        else None
    )

    if args.dry_run:
        print(
            dry_run_report(
                item_repo,
                anchors=anchors,
                batteries=batteries,
                token_cap=args.token_cap,
            )
        )
        return 0

    # Live run path — route anchors through adapter_for (provider config
    # decides the wire; the current anchors all resolve to the Anthropic
    # adapter, but a future re-pointing needs no code change here).
    adapter = adapter_for(load_anchors()["cheap"])
    try:
        from hr import db as hdb
        db_conn = hdb
    except Exception:
        db_conn = None

    runner = CalibrationRunner(
        adapter=adapter,
        item_repo=item_repo,
        anchors=anchors,
        batteries=batteries,
        token_cap=args.token_cap,
        db=db_conn,
        pool_hash=_compute_pool_hash(item_repo, batteries),
        resume=args.resume,
    )
    report = runner.run()
    print_rendered_report(report)
    if args.json:
        print(json.dumps(_report_to_dict(report), indent=2, default=str))
    return 0 if report.all_passed else 1


def main() -> int:
    return _cli()

__all__ = ["dry_run_report", "_cli", "main"]
