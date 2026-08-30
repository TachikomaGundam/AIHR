"""hr.bench — the 10 live capability benchmarks on unified adapters (task 12).

Port of v1's benchmark engine (:mod:`hr.benchmark` predecessors) onto
:class:`hr.adapters.ChatRequest`. This package REPLACES the old single-module
engine: the bespoke SSE streaming client and the deepseek provider
special-case are deleted — wire handling, streaming, retries and routing
belong to hr.adapters, where they are config-driven.

Modules:
  - :mod:`hr.bench.livebench` — battery registry (battery codes, item labels,
    seat_battery bounds) shared by the engine, the registration script and
    the CLI.
  - :mod:`hr.bench.prompts` — prompt/data content (verbatim v1 port).
  - :mod:`hr.bench.truths` — lazy runtime-computed ground truths.
  - :mod:`hr.bench.scorers` — v1 scorer formulas, semantically intact.
- :mod:`hr.bench.engine` — ChatRequest runners and measurement writer.
"""

from hr.bench.engine import BenchOutcome, ItemResult, LivebenchEngine, make_sweep_id
from hr.bench.livebench import LIVEBENCH_BATTERIES, battery_code

__all__ = [
    "BenchOutcome",
    "ItemResult",
    "LIVEBENCH_BATTERIES",
    "LivebenchEngine",
    "battery_code",
    "make_sweep_id",
]
