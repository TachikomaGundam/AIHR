"""HR measurement and decision framework.

The product version has exactly one authored source — ``[project].version``
in pyproject.toml, which the packaging layer (and only the packaging layer)
copies into distribution metadata.  ``__version__`` is *derived* from that
metadata, never restated as a literal here: a second hardcoded copy rotted
silently once the bump path moved to pyproject-only (the byte-exact 0.4.0
bundle stamped itself a "0.2.1" install receipt — hr/lifecycle.py reads this
symbol for the receipt).
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("aihr")
except PackageNotFoundError:  # bare source checkout, nothing installed
    __version__ = "0.0.0+unknown"  # honest sentinel, never a stale imposter

__all__ = ["db", "stats", "assign"]
