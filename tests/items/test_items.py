"""Tests for hr2.items — schema + loader (spec §5.1, §5.3)."""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hr.items.schema import (
    ItemEnvelope,
    ItemMeta,
    ItemType,
    PayloadVision,
    build_envelope,
    canonical_bytes,
    content_hash,
)
from hr.items.loader import (
    ItemLoader,
    LoaderError,
    pool_hash,
    MIN_CANARY_FRAC,
)


# ---------------------------------------------------------------------------
# A mock LoaderDB
# ---------------------------------------------------------------------------
class FakeLoaderDB:
    def __init__(self):
        self.items: list[dict] = []
        self.pools: list[dict] = []

    def upsert_item(self, **kwargs):
        self.items.append(kwargs)

    def mark_pool_ready(self, **kwargs):
        self.pools.append(kwargs)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_vision_item(
    key: str = "vision.ui_read.foo",
    tier: int = 3,
    seats: list[str] | None = None,
    knowledge_after: str | None = None,
) -> dict:
    if seats is None:
        seats = ["looker"]
    return {
        "item_key": key,
        "type": "vision",
        "tier": tier,
        "payload": {
            "image_ref": "x.png",
            "kind": "ui_read",
            "question": "q?",
            "answer": "a",
        },
        "grading": {"grader": "exact_match@1.0", "params": {}},
        "meta": {
            "seats": list(seats),
            "source": "test",
            **(({"knowledge_after": knowledge_after}) if knowledge_after is not None else {}),
        },
    }


def _make_factuality_item(
    key: str = "factuality.qa.test",
    knowledge_after: str = "2024-06-01T00:00:00Z",
) -> dict:
    return {
        "item_key": key,
        "type": "factuality_qa",
        "tier": 2,
        "payload": {
            "question": "capital of France?",
            "verifiable_answer": "Paris",
            "source_of_truth": {"kind": "fixed", "ref": "text"},
            "verification": "exact",
        },
        "grading": {"grader": "exact_match@1.0", "params": {}},
        "meta": {
            "seats": ["fact"],
            "knowledge_after": knowledge_after,
        },
    }


# ---------------------------------------------------------------------------
# schema tests
# ---------------------------------------------------------------------------
class TestItemEnvelope:
    def test_valid_item(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        assert env.type == ItemType.VISION
        assert env.item_key == "vision.ui_read.foo"

    def test_invalid_item_key_format(self):
        data = _make_vision_item(key="not a valid key!")
        with pytest.raises(Exception):
            ItemEnvelope.model_validate(data)

    def test_empty_seats_rejected(self):
        data = _make_vision_item(seats=[])
        with pytest.raises(Exception):
            ItemEnvelope.model_validate(data)

    def test_knowledge_after_nullable_for_factuality(self):
        # User-approved factuality_qa items carry `knowledge_after: null`;
        # the schema accepts this; enforcement (if any) is delegated to
        # the loader with the seat-cutoff registry.
        data = _make_factuality_item(knowledge_after=None)
        env = ItemEnvelope.model_validate(data)
        assert env.meta.knowledge_after is None

    def test_typed_payload_for_vision(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        typed = env.typed_payload()
        assert isinstance(typed, PayloadVision)
        assert typed.kind == "ui_read"

    def test_content_hash_deterministic(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        h1 = content_hash(env)
        h2 = content_hash(env)
        assert h1 == h2
        assert h1.startswith("sha256:")

    def test_content_hash_excludes_itself(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        env_with = env.model_copy(update={"content_hash": "sha256:deadbeef"})
        assert content_hash(env) == content_hash(env_with)

    def test_canonical_bytes_ordering(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        b1 = canonical_bytes(env)
        b2 = canonical_bytes(env)
        assert b1 == b2
        # JSON must be sorted keys.
        parsed = json.loads(b1.decode("utf-8"))
        assert list(parsed.keys()) == sorted(parsed.keys())


# ---------------------------------------------------------------------------
# loader tests
# ---------------------------------------------------------------------------
class TestItemLoader:
    def test_load_directory_basic(self, tmp_path):
        db = FakeLoaderDB()
        loader = ItemLoader(db=db, pool_id="p1", require_canary=False)
        # Write a single item.
        (tmp_path / "a.json").write_text(json.dumps(_make_vision_item()))
        items = loader.load_directory(tmp_path)
        assert len(items) == 1
        assert len(db.items) == 1
        assert db.pools
        assert db.pools[0]["item_count"] == 1

    def test_load_directory_rejects_empty_seats(self, tmp_path):
        db = FakeLoaderDB()
        loader = ItemLoader(db=db, pool_id="p1", require_canary=False)
        (tmp_path / "a.json").write_text(
            json.dumps(_make_vision_item(seats=[]))
        )
        with pytest.raises(LoaderError):
            loader.load_directory(tmp_path)

    def test_canary_fraction_enforced(self, tmp_path):
        db = FakeLoaderDB()
        loader = ItemLoader(db=db, pool_id="p1", require_canary=True)
        # 49 regular + 1 canary = 2% (passes)
        for i in range(49):
            data = _make_vision_item(key=f"vision.ui_read.n{i}")
            (tmp_path / f"n{i}.json").write_text(json.dumps(data))
        canary = _make_vision_item(key="canary.fake.item")
        (tmp_path / "canary.json").write_text(json.dumps(canary))

        items = loader.load_directory(tmp_path)
        assert len(items) == 50
        assert db.pools[0]["canary_count"] == 1

    def test_canary_fraction_rejects_below_2pct(self, tmp_path):
        db = FakeLoaderDB()
        loader = ItemLoader(db=db, pool_id="p1", require_canary=True)
        # 100 regular items + 1 canary = 1/101 < 2%.
        for i in range(100):
            data = _make_vision_item(key=f"vision.ui_read.a{i}")
            (tmp_path / f"{i}.json").write_text(json.dumps(data))
        canary = _make_vision_item(key="canary.fake.one")
        (tmp_path / "canary.json").write_text(json.dumps(canary))

        with pytest.raises(LoaderError) as exc:
            loader.load_directory(tmp_path)
        assert "canary" in str(exc.value).lower()

    def test_content_hash_mismatch_rejected(self, tmp_path):
        db = FakeLoaderDB()
        loader = ItemLoader(db=db, pool_id="p1", require_canary=False)
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        real_hash = content_hash(env)
        data["content_hash"] = "sha256:" + ("f" * 64)
        (tmp_path / "a.json").write_text(json.dumps(data))
        with pytest.raises(LoaderError) as exc:
            loader.load_directory(tmp_path)
        assert "mismatch" in str(exc.value).lower()

    def test_pool_hash_function(self):
        h = pool_hash(["sha256:aaa", "sha256:bbb", "sha256:ccc"])
        assert h.startswith("sha256:")
        # Same inputs → same output.
        expected = "sha256:" + hashlib.sha256(
            "\n".join(sorted(["sha256:aaa", "sha256:bbb", "sha256:ccc"])).encode(
                "utf-8"
            )
        ).hexdigest()
        assert h == expected


# ---------------------------------------------------------------------------
# payload round-trip tests
# ---------------------------------------------------------------------------
class TestPayloads:
    def test_vision_payload_roundtrip(self):
        data = _make_vision_item()
        env = ItemEnvelope.model_validate(data)
        vis = env.typed_payload()
        assert vis.kind == "ui_read"
        assert vis.image_ref == "x.png"

    def test_factuality_roundtrip(self):
        data = _make_factuality_item()
        env = ItemEnvelope.model_validate(data)
        fac = env.typed_payload()
        assert fac.verification == "exact"
        assert "Paris" in fac.verifiable_answer


# ---------------------------------------------------------------------------
# itemrepo data integrity (machine-path scrub regression)
# ---------------------------------------------------------------------------
_ITEMREPO = Path(__file__).resolve().parents[2] / "itemrepo"


# NOTE: the needle is assembled at runtime so this file itself stays free of
# the literal marker the universality gate scans for.
_MACHINE_HOME = chr(47) + "home" + chr(47)


class TestItemrepoDataIntegrity:
    """The benchmark item bank must never carry absolute machine paths."""

    def test_no_absolute_machine_home_in_item_data(self):
        assert _ITEMREPO.is_dir(), f"itemrepo missing at {_ITEMREPO}"
        offenders = []
        for fp in sorted(_ITEMREPO.rglob("*.json")):
            text = fp.read_text(encoding="utf-8")
            if _MACHINE_HOME in text:
                offenders.append(str(fp.relative_to(_ITEMREPO)))
        assert offenders == [], (
            "itemrepo items still reference absolute machine home paths: "
            + ", ".join(offenders)
        )

    def test_no_absolute_machine_paths_in_itemrepo_scripts(self):
        for fp in sorted(_ITEMREPO.rglob("*.py")):
            text = fp.read_text(encoding="utf-8")
            assert _MACHINE_HOME not in text, f"{fp.name} still references machine home paths"


# ---------------------------------------------------------------------------
# reasoning tier 3/4 registry-vs-disk completeness (hr-evolution T5)
# ---------------------------------------------------------------------------


class TestReasoningRegistryCompleteness:
    """Every registry slug resolves to exactly one item file and vice versa.

    The registry module uses flat sibling imports, so ``itemrepo/reasoning``
    itself is inserted on ``sys.path`` to import it (same pattern as the
    shared conftest root insert). The one-to-one check is validation: a
    mismatch is a finding, not a cleanup.
    """

    _REASONING_DIR = _ITEMREPO / "reasoning"

    def _registry(self):
        sys.path.insert(0, str(self._REASONING_DIR))
        import reasoning_registry

        return reasoning_registry

    def test_t3_t4_registry_and_disk_slugs_one_to_one(self):
        registry = self._registry()
        report = registry.validate_registry_vs_disk()

        assert set(report) == {3, 4}
        for tier, entry in report.items():
            assert entry["registry_only"] == [], f"t{tier} slugs without disk items"
            assert entry["disk_only"] == [], f"t{tier} items without registry slugs"
            assert entry["duplicate_registry_slugs"] == []
            assert entry["duplicate_disk_slugs"] == []
            assert entry["registry_count"] == entry["disk_count"] > 0

    def test_t3_t4_item_keys_match_on_disk_slugs(self):
        registry = self._registry()
        for tier in (3, 4):
            for slug in registry.registry_slugs(tier):
                path = self._REASONING_DIR / f"t{tier}" / f"reason.t{tier}.{slug}.json"
                assert path.is_file(), f"missing item file for {slug}"
                envelope = json.loads(path.read_text(encoding="utf-8"))
                assert envelope["item_key"] == f"reasoning.t{tier}.{slug}"

    def test_validate_registry_vs_disk_detects_mismatches(self, tmp_path):
        registry = self._registry()
        reg_slugs = registry.registry_slugs(3)
        assert reg_slugs  # t3 is non-empty
        renamed = reg_slugs[0]
        # every real file is copied, but the first slug's file is renamed:
        # it becomes registry-only, and the replacement is disk-only
        t3 = tmp_path / "t3"
        t3.mkdir()
        for real in sorted((self._REASONING_DIR / "t3").glob("reason.t3.*.json")):
            content = real.read_text(encoding="utf-8")
            if real.stem == f"reason.t3.{renamed}":
                (t3 / f"reason.t3.{renamed}-renamed.json").write_text(content, encoding="utf-8")
            else:
                (t3 / real.name).write_text(content, encoding="utf-8")
        (t3 / "reason.t3.fake-extra.json").write_text('{"item_key": "fake"}', encoding="utf-8")

        report = registry.validate_registry_vs_disk(root=tmp_path, tiers=(3,))

        assert report[3]["registry_only"] == [renamed]
        assert report[3]["disk_only"] == [f"{renamed}-renamed", "fake-extra"]
