import datetime as dt

import pytest

from hr.parity import (
    assert_tier_ceiling,
    assert_pricing_parity,
    check_parity,
    check_pricing_parity,
    check_tier_ceiling,
    classify,
    resolve_price,
)

FAMILIES: list[dict[str, str]] = [
    {"family": "qwen", "pattern": r"^qwen(\d+)\.(\d+)"},
    {"family": "deepseek", "pattern": r"^deepseek-v(\d+)(?:\.(\d+))?"},
    {"family": "glm", "pattern": r"^glm-(\d+)\.(\d+)"},
    {"family": "kimi", "pattern": r"^k(\d+)(?:$|[-\d])"},
    {"family": "kimi", "pattern": r"^kimi"},
]

MANIFEST: dict[str, object] = {
    "as_of": "2026-10-16",
    "stale_days": 90,
    "families": [
        {"family": "qwen", "pattern": r"^qwen(\d+)\.(\d+)"},
        {"family": "deepseek", "pattern": r"^deepseek-v(\d+)(?:\.(\d+))?"},
        {"family": "glm", "pattern": r"^glm-(\d+)\.(\d+)"},
        {"family": "kimi", "pattern": r"^k(\d+)(?:$|[-\d])"},
        {"family": "kimi", "pattern": r"^kimi"},
    ],
}

FLEET = [
    "bailian-token-plan/qwen3.8-max",
    "bailian-token-plan/qwen3.8-flash-next",
    "local-qwen/qwen3.8-flash-next",
    "deepseek/deepseek-flash",
    "kimi-code-plan-cn/k3",
    "kimi-code-plan-cn/kimi-for-coding",
]


def fam():
    return list(FAMILIES)


def run(anchors, today=dt.date(2026, 10, 16), fleet=FLEET):
    return check_parity(anchors, fleet, today=today, manifest=MANIFEST)


def test_classify_versions_and_flags():
    assert classify("bailian-token-plan/qwen3.7-plus", fam()).version == (3, 7)
    assert classify("bailian-token-plan/glm-5.2", fam()).family == "glm"
    assert classify("kimi-code-plan-cn/kimi-for-coding", fam()).version is None
    assert classify("kimi-code-plan-cn/k3", fam()).version == (3, 0)
    assert classify("openai/gpt-9o", fam()).family is None


def test_anchor_drift_real_seating_case():
    anchors = {
        "cheap": "bailian-token-plan/deepseek-v4-flash",
        "mid": "bailian-token-plan/qwen3.7-plus",
        "expensive": "bailian-token-plan/glm-5.2",
    }
    report = run(anchors)
    mid = next(d for d in report.drifted if d.anchor_key == "mid")
    assert mid.reason == "version-increment"
    assert mid.successor.version == (3, 8)
    assert report.seating_stale is True
    assert any("cheap" in s for s in report.incomparable)
    assert any("expensive" in s for s in report.incomparable)


def test_same_generation_under_other_provider_is_alive():
    report = run({"mid": "local-qwen/qwen3.8-flash-next"})
    assert not any(d.anchor_key == "mid" for d in report.drifted)
    assert "mid" in report.clean


def test_older_fleet_than_anchor_reports_generation_absent():
    fleet = ["bailian-token-plan/qwen3.6-plus"]
    report = run({"mid": "bailian-token-plan/qwen3.8-flash"}, fleet=fleet)
    mid = next(d for d in report.drifted if d.anchor_key == "mid")
    assert mid.reason == "generation-absent"


def test_provider_change_when_only_other_provider_advances():
    fleet = ["other-provider/qwen3.9-ultra"]
    report = run({"mid": "bailian-token-plan/qwen3.8-flash"}, fleet=fleet)
    mid = next(d for d in report.drifted if d.anchor_key == "mid")
    assert mid.reason == "provider-change"


def test_versionless_anchor_is_incomparable_never_silently_clean():
    report = run({"k": "kimi-code-plan-cn/kimi-for-coding"})
    assert "k" not in report.clean
    assert any("k:" in s for s in report.incomparable)
    assert report.seating_stale is False


def test_stale_manifest_refuses_certification():
    report = run({"mid": "bailian-token-plan/qwen3.8-flash"},
                 today=dt.date(2027, 2, 1))
    assert report.stale_manifest is not None
    assert report.seating_stale is True


# ---- gate #2: pricing parity (charter drift-gate plan item 2) ----

def test_resolve_price_exact_and_bare_fallback() -> None:
    pricing = {"qwen3.6-flash": 1.0}
    assert resolve_price(pricing, "bailian/qwen3.6-flash") == 1.0
    assert resolve_price(pricing, "qwen3.6-flash") == 1.0
    assert resolve_price(pricing, "bailian/nosuch") is None


def test_pricing_gate_missing_is_fail_loud() -> None:
    rep = check_pricing_parity(["a/x", "a/k3"], pricing={"k3": 3.0})
    assert rep.missing == ("a/x",)
    assert not rep.ok
    with pytest.raises(ValueError, match="missing=\['a/x'\]"):
        assert_pricing_parity(["a/x", "a/k3"], pricing={"k3": 3.0})


def test_pricing_gate_shadow_conflict() -> None:
    rep = check_pricing_parity(["p/m"], pricing={"p/m": 1.0, "m": 2.0})
    assert rep.shadow_conflicts and not rep.ok


def test_pricing_gate_all_priced_ok() -> None:
    rep = check_pricing_parity(["p/k3", "glm"],
                               pricing={"k3": 3.0, "glm": 2.0})
    assert rep.ok and set(rep.priced) == {"p/k3", "glm"}


def test_pricing_gate_empty_fleet_refuses() -> None:
    with pytest.raises(ValueError, match="refusing"):
        check_pricing_parity([], pricing={})


def test_estimate_cost_shares_the_resolver() -> None:
    # refactor guard: recommend must resolve through hr.parity.resolve_price
    from hr.recommend import _estimate_cost
    assert _estimate_cost("p/k3", 1_000_000, {"k3": 3.0}) == 3.0
    assert _estimate_cost("p/nope", 100, {}) is None


# ---- gate #3: tier ceiling (ruler health; the P5 disease made mechanical) ----

def test_ceiling_all_max_is_frozen() -> None:
    rep = check_tier_ceiling({"vision": [100.0] * 6})
    assert rep.frozen == ("vision",) and rep.no_signal == ("vision",)


def test_ceiling_mixed_is_healthy() -> None:
    rep = check_tier_ceiling({"code_gen": [100.0, 99.0, 100.0, 88.0, 100.0, 100.0]})
    assert rep.healthy == ("code_gen",) and not rep.no_signal


def test_ceiling_flat_offceiling_is_flagged() -> None:
    rep = check_tier_ceiling({"speed": [0.0] * 8})
    assert rep.flat == ("speed",)  # uniform-zero = grader-broken, not ceiling


def test_ceiling_insufficient_never_silent_clean() -> None:
    rep = check_tier_ceiling({"factuality": [55.0, 60.0]}, min_samples=6)
    assert rep.insufficient == ("factuality:n=2<6",) and rep.healthy == ()


def test_ceiling_empty_map_refuses() -> None:
    with pytest.raises(ValueError, match="refusing"):
        check_tier_ceiling({})


def test_ceiling_assert_lists_offenders() -> None:
    with pytest.raises(ValueError, match="frozen=\['vision'\]"):
        assert_tier_ceiling({"vision": [100.0] * 6,
                             "code_gen": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]})


def test_pricing_gate_names_malformed_entry() -> None:
    """A dict-shaped pricing entry (wrong overlay schema) must fail loud by
    name at gate entry — not crash deep in float() (191 field pass, 0.6.11)."""
    import pytest

    from hr.parity import check_pricing_parity

    bad = {"m/one": {"per_1k_input_tokens": 0.001}}
    with pytest.raises(ValueError, match=r"pricing\['m/one'\]"):
        check_pricing_parity(["m/one"], pricing=bad)
