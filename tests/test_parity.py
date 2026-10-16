import datetime as dt

from hr.parity import check_parity, classify

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
