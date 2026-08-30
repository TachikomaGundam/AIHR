from __future__ import annotations

import sys

from .cli_app import _fail, console

def _selection_indices(spec: str, n: int) -> list[int]:
    """Parse a comma/range selection (``"1,3,5-7"``) into 1..n indices.

    Comma-separated tokens are single indices or inclusive ``N-M`` ranges;
    duplicate indices collapse to their first occurrence (order preserved).
    Tolerates stray commas (empty tokens skipped). Raises ValueError naming
    the offending token on non-numeric input, a descending range, or an
    index outside 1..n — callers turn that into an error message + re-prompt
    (never a crash).
    """
    indices: list[int] = []
    seen: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            lo_s, hi_s = token.split("-", 1)
            if not (lo_s.strip().isdigit() and hi_s.strip().isdigit()):
                raise ValueError(f'invalid selection "{token}": expected N or N-M')
            lo, hi = int(lo_s), int(hi_s)
            if lo > hi:
                raise ValueError(f'invalid selection "{token}": range is descending')
            values = range(lo, hi + 1)
        else:
            if not token.isdigit():
                raise ValueError(f'invalid selection "{token}": not a number')
            values = (int(token),)
        for idx in values:
            if idx < 1 or idx > n:
                raise ValueError(
                    f'invalid selection "{idx}": index out of range 1..{n}'
                )
            if idx not in seen:
                seen.add(idx)
                indices.append(idx)
    if not indices:
        raise ValueError("no models selected (empty selection)")
    return indices


def _pick_models_interactive(discovered: list) -> list[str]:
    """Numbered menu over the discover list; comma/range selection from stdin.

    Returns full model ids (``provider/model_id``) in user-selected order.
    Invalid input prints an error to stderr and re-prompts; EOF aborts with
    an error (never an infinite loop). Pure CLI: sys.stdin only, no TUI or
    opencode runtime.
    """
    n = len(discovered)
    console.print(
        "# models available for benchmarking "
        "(discovered from opencode.jsonc configs):",
        markup=False,
    )
    for i, model in enumerate(discovered, start=1):
        # markup off: ids/names are config data — square brackets would be
        # eaten by rich markup, silently dropping the entry
        console.print(
            f"  {i:>2}. {model.provider}/{model.model_id}  ({model.display_name})",
            markup=False,
        )
    while True:
        console.print(
            "Select models to benchmark (comma/range, e.g. 1,3,5-7): ",
            markup=False,
        )
        line = sys.stdin.readline()
        if line == "":
            _fail("error: no selection provided (EOF)")
        try:
            indices = _selection_indices(line, n)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            continue
        return [f"{discovered[i - 1].provider}/{discovered[i - 1].model_id}"
                for i in indices]


