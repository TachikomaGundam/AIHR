"""S0: report source tri-state — a missing declaration is a generator error."""
import pytest

from hr.cli_report_base import build_sweeps_report
from hr.report_source import SOURCE_STATES, require_source


class _Cur:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): pass
    def fetchall(self): return []

class _Conn:
    def cursor(self): return _Cur()


def test_closed_vocabulary():
    assert SOURCE_STATES == ("mock", "cassette", "live")
    for s in SOURCE_STATES:
        assert require_source(s) == s
    with pytest.raises(ValueError, match="mock/cassette/live"):
        require_source("production")
    with pytest.raises(ValueError, match="mock/cassette/live"):
        require_source(None)


def test_missing_source_raises_before_any_output():
    with pytest.raises(TypeError):
        build_sweeps_report(_Conn())


def test_stamp_reaches_the_human_surface():
    out = build_sweeps_report(_Conn(), source="cassette")
    assert "# source: cassette" in out
