"""Seat rolespec contract tests (committed surface).

Covers hr.seats.rolespec: agent-name normalization, agent->seat mapping,
SeatProfile validation, and the default weight tables behind inferred
profiles. Offline and deterministic.
"""

from __future__ import annotations

import pytest

from hr.seats.rolespec import (
    AGENT_TO_SEAT,
    DEFAULT_BATTERY_BY_SEAT,
    DEFAULT_OUTPUT_FORM_BY_SEAT,
    SEAT_CODES,
    SeatProfile,
    build_inferred_profile,
    map_agent_to_seat,
    normalize_agent,
)


def test_normalize_agent_cases() -> None:
    assert normalize_agent(None) is None
    assert normalize_agent("") is None
    assert normalize_agent("  Sisyphus-Junior  ") == "sisyphus_junior"
    assert normalize_agent("Momus - Plan Critic") == "momus_plan_critic"
    assert normalize_agent("oracle") == "oracle"


def test_map_agent_to_seat() -> None:
    assert map_agent_to_seat("oracle") == "oracle"
    assert map_agent_to_seat("Sisyphus-Junior") == "sisyphus_junior"
    assert map_agent_to_seat("ultraworker") == "ultrabrain"
    assert map_agent_to_seat("prometheus_plan_builder") == "prometheus"
    assert map_agent_to_seat("explore") == "explore"
    assert map_agent_to_seat("totally-unknown-agent") is None
    assert map_agent_to_seat("") is None
    assert map_agent_to_seat(None) is None


def test_seat_codes_cover_mappings() -> None:
    assert set(SEAT_CODES) == set(DEFAULT_BATTERY_BY_SEAT.keys())
    assert set(SEAT_CODES) == set(DEFAULT_OUTPUT_FORM_BY_SEAT.keys())
    for agent, seat in AGENT_TO_SEAT.items():
        assert seat in SEAT_CODES


def test_inferred_profiles_normalize_weights() -> None:
    for seat in SEAT_CODES:
        weights = DEFAULT_BATTERY_BY_SEAT[seat]
        assert sum(weights.values()) == pytest.approx(1.0, abs=0.02)
        forms = DEFAULT_OUTPUT_FORM_BY_SEAT[seat]
        assert sum(forms.values()) == pytest.approx(1.0, abs=0.02)


def test_seat_profile_validation() -> None:
    profile = SeatProfile(code="oracle", task_count=3)
    assert profile is not None
    assert profile.source == "inferred"
    assert profile.task_count == 3
    assert profile.inferred is False
    with pytest.raises(ValueError, match="unknown seat code"):
        SeatProfile(code="not-a-seat", task_count=0)
    with pytest.raises(ValueError, match="task_count"):
        SeatProfile(code="oracle", task_count=-1)
    with pytest.raises(Exception):
        SeatProfile(code="oracle", task_count=0, extra_field=1)


def test_build_inferred_profile() -> None:
    profile = build_inferred_profile("oracle")
    assert profile.inferred is True
    assert profile.source == "inferred"
    assert profile.task_count == 0
    assert profile.battery_weights == DEFAULT_BATTERY_BY_SEAT["oracle"]
    assert profile.output_form == DEFAULT_OUTPUT_FORM_BY_SEAT["oracle"]
    assert "no log data" in profile.notes
    custom = build_inferred_profile("oracle", notes="custom note")
    assert custom.notes == "custom note"
    with pytest.raises(ValueError, match="unknown seat"):
        build_inferred_profile("bogus")


def test_normalize_agent_matches_known_keys() -> None:
    for raw, seat in AGENT_TO_SEAT.items():
        assert map_agent_to_seat(raw) == seat