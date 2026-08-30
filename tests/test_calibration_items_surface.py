"""Calibration-item surface contract tests against the COMMITTED shape.

Exercises hr.calibration_items: anchor loading (fail-loud), per-type
message building, vision image attach, grading-param augmentation,
repo walking with battery filters, and the pool-hash computation.
Offline and deterministic.
"""

from __future__ import annotations

import json

import pytest

import hr.calibration_items as ci
from hr.items.schema import ItemType, build_envelope


def make_env(item_key: str, type_: ItemType, **payload) -> object:
    return build_envelope(
        item_key=item_key,
        type=type_,
        payload=payload,
        grading={"grader": "passthrough@1.0"},
        meta={"seats": ["f1"]},
    )


def test_load_anchors_fails_loud_when_file_missing(monkeypatch) -> None:
    def _missing(_name: str):
        raise FileNotFoundError("no seats.yaml")

    monkeypatch.setattr(ci, "load_yaml", _missing)
    with pytest.raises(RuntimeError, match="calibration anchors unavailable"):
        ci.load_anchors()


def test_load_anchors_fails_loud_when_section_missing(monkeypatch) -> None:
    monkeypatch.setattr(ci, "load_yaml", lambda _name: {"seats": []})
    with pytest.raises(RuntimeError, match="calibration anchors not found"):
        ci.load_anchors()


def test_load_anchors_returns_map(monkeypatch) -> None:
    monkeypatch.setattr(
        ci, "load_yaml",
        lambda _name: {"calibration_anchors": {"cheap": "a/b", "mid": "c/d"}},
    )
    assert ci.load_anchors() == {"cheap": "a/b", "mid": "c/d"}


@pytest.mark.parametrize(
    ("item_key", "type_", "payload", "expected_roles"),
    [
        ("reasoning.1", ItemType.REASONING, {"question": "q?"}, ("user",)),
        ("factuality_qa.1", ItemType.FACTUALITY_QA, {"question": "q?"}, ("user",)),
        ("unanswerable.1", ItemType.UNANSWERABLE, {"question": "q?"}, ("user",)),
        ("citation.1", ItemType.CITATION, {"question": "q?"}, ("user",)),
        ("vision.1", ItemType.VISION, {"question": "q?"}, ("user",)),
        ("replay.1", ItemType.REPLAY, {"question": "q?"}, ("user",)),
    ],
)
def test_build_messages_simple_shapes(item_key, type_, payload, expected_roles) -> None:
    msgs = ci.build_messages(make_env(item_key, type_, **payload))
    assert [m["role"] for m in msgs] == list(expected_roles)


def test_build_messages_tool_a_with_system() -> None:
    msgs = ci.build_messages(
        make_env("tool_a.calc.1", ItemType.TOOL_A, system="python", user="add 1+1")
    )
    assert msgs[0]["content"] == "SYSTEM: python"
    assert msgs[1]["content"] == "Understood."
    assert msgs[2]["content"] == "add 1+1"


def test_build_messages_tool_a_without_system() -> None:
    msgs = ci.build_messages(make_env("tool_a.calc.1", ItemType.TOOL_A, user="hi"))
    assert len(msgs) == 1


def test_build_messages_tool_b_with_env_and_turns() -> None:
    msgs = ci.build_messages(
        make_env(
            "tool_b.r1.1",
            ItemType.TOOL_B,
            env="python",
            turns=[{"user": "t1"}, {"user": "t2"}],
        )
    )
    assert msgs[0]["content"] == "SYSTEM: sandbox=python"
    assert msgs[-1]["content"] == "t2"


def test_maybe_vision_image_returns_none_for_non_vision() -> None:
    env = make_env("reasoning.1", ItemType.REASONING, question="q")
    assert ci.maybe_vision_image(env, "anywhere") is None


def test_maybe_vision_image_missing_ref_or_file(tmp_path) -> None:
    env = make_env("vision.1", ItemType.VISION, question="q", image_ref="no.png")
    assert ci.maybe_vision_image(env, tmp_path) is None
    env2 = make_env("vision.2", ItemType.VISION, question="q")
    assert ci.maybe_vision_image(env2, tmp_path) is None


def test_maybe_vision_image_encodes_base64(tmp_path) -> None:
    img_dir = tmp_path / "vision"
    img_dir.mkdir()
    img = img_dir / "shot.png"
    img.write_bytes(b"\x89PNG-fake")
    env = make_env("vision.3", ItemType.VISION, question="q", image_ref="shot.png")
    out = ci.maybe_vision_image(env, tmp_path)
    assert out == [{"data": "iVBORy1mYWtl", "media_type": "image/png"}]


def test_build_grading_params_verifiable_answer() -> None:
    env = make_env(
        "factuality_qa.1",
        ItemType.FACTUALITY_QA,
        question="q",
        verifiable_answer="42",
    )
    params = ci.build_grading_params(env)
    assert params["expected"] == "42"


def test_build_grading_params_citation_first_claim() -> None:
    env = make_env(
        "citation.1",
        ItemType.CITATION,
        question="q",
        required_claims=["claim-1", "claim-2"],
    )
    params = ci.build_grading_params(env)
    assert params["expected"] == "claim-1"


def test_build_grading_params_passthrough_and_item_params() -> None:
    env = build_envelope(
        item_key="tool_b.r1.1",
        type=ItemType.TOOL_B,
        payload={},
        grading={"grader": "unit_test@1.0", "params": {"timeout": 30}},
        meta={"seats": ["f1"]},
    )
    params = ci.build_grading_params(env)
    assert params["timeout"] == 30


def test_load_item_repo_groups_and_filters(tmp_path) -> None:
    def write_item(key: str, type_: str) -> None:
        (tmp_path / f"{key}.json").write_text(
            json.dumps(
                build_envelope(
                    item_key=key,
                    type=type_,
                    payload={"question": "q"},
                    grading={"grader": "x@1.0"},
                    meta={"seats": ["f1"]},
                ).model_dump(mode="json", by_alias=True)
            ),
            encoding="utf-8",
        )

    write_item("reasoning.a", "reasoning")
    write_item("hallucination.qa.b", "factuality_qa")
    write_item("tool_a.c.c", "tool_a")
    write_item("longctx.d", "longctx")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "notitem.json").write_text(json.dumps({"foo": 1}), encoding="utf-8")

    groups = ci.load_item_repo(tmp_path)
    assert set(groups.keys()) == {
        "reasoning", "hallucination", "tool_a", "vision", "tool_b"
    }
    assert len(groups["reasoning"]) == 1
    assert len(groups["hallucination"]) == 1
    assert len(groups["tool_a"]) == 1
    assert groups["vision"] == [] and groups["tool_b"] == []
    # longctx excluded; broken/notitem skipped silently.

    filtered = ci.load_item_repo(tmp_path, batteries=["tool_a"])
    assert set(filtered.keys()) == {"tool_a"}


def test_compute_pool_hash_stable_and_sensitive(tmp_path) -> None:
    def write_item(key: str, type_: str, q: str = "q") -> None:
        (tmp_path / f"{key}.json").write_text(
            json.dumps(
                build_envelope(
                    item_key=key,
                    type=type_,
                    payload={"question": q},
                    grading={"grader": "x@1.0"},
                    meta={"seats": ["f1"]},
                ).model_dump(mode="json", by_alias=True)
            ),
            encoding="utf-8",
        )

    write_item("reasoning.a", "reasoning")
    write_item("hallucination.qa.b", "factuality_qa")
    h1 = ci._compute_pool_hash(tmp_path, None)
    assert isinstance(h1, str) and h1
    h2 = ci._compute_pool_hash(tmp_path, None)
    assert h1 == h2
    write_item("reasoning.a", "reasoning", q="changed question")
    assert ci._compute_pool_hash(tmp_path, None) != h1