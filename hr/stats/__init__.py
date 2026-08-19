"""hr2.stats — statistical engine (variance/sensitivity/separation archived)."""
from .bootstrap import paired_bootstrap_separation, classify, ci
from .sequential import SequentialStopper, SequentialConfig

__all__ = [
    "paired_bootstrap_separation", "classify", "ci",
    "SequentialStopper", "SequentialConfig",
]