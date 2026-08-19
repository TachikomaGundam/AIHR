"""hr2.items.loader — load a directory tree of item JSONs into a pool.

Spec §5.1 enforcement:
  - each item validates against hr2.items.schema.ItemEnvelope,
  - item_key format enforced by the Pydantic model,
  - meta.seats non-empty enforced by the Pydantic model,
  - factuality_qa requires meta.knowledge_after,
  - content_hash computed via SHA-256(canonical JSON) when absent; otherwise
    verified to match,
  - canary fraction >= 2% per pool (by item count — items whose item_key
    starts with "canary."),
  - pool_hash = SHA-256(sorted content_hashes joined with '\\n').

The loader does NOT perform model-API calls. It accepts a `db` handle conforming
to `LoaderDB` (mockable for tests). Real db.py lives in hr.db; we do NOT
import hr.db here to stay decoupled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from hr.items.schema import ItemEnvelope, ItemMeta, content_hash

log = logging.getLogger(__name__)

# Canary items have keys starting with "canary.". Spec §5.1 rule 2: ≥2%.
CANARY_PREFIX = "canary."
MIN_CANARY_FRAC = 0.02


class LoaderError(ValueError):
    """Raised when loader validation fails (bad item, canary, pool_hash...)."""


# ---------------------------------------------------------------------------
# Minimal DB protocol — mockable. hr2.db implements more; we only call these.
# ---------------------------------------------------------------------------
@runtime_checkable
class LoaderDB(Protocol):
    """Tiny persistence protocol used by ItemLoader.

    A mockable handle is sufficient for tests; a real psycopg2-backed
    implementation can be plugged in later.
    """

    def upsert_item(
        self,
        *,
        pool_id: str,
        item_key: str,
        type_: str,
        tier: int,
        payload_json: str,
        grading_json: str,
        meta_json: str,
        content_hash: str,
    ) -> None: ...

    def mark_pool_ready(
        self,
        *,
        pool_id: str,
        pool_hash: str,
        item_count: int,
        canary_count: int,
    ) -> None: ...


@dataclass
class _LoadedItem:
    envelope: ItemEnvelope
    source_path: Path
    content_hash_: str


# ---------------------------------------------------------------------------
# pool_hash — §5.1 rule 4
# ---------------------------------------------------------------------------
def pool_hash(content_hashes: Sequence[str]) -> str:
    """pool_hash = SHA-256(sorted content_hashes joined by LF)."""
    if not content_hashes:
        raise LoaderError("pool_hash called on empty content_hashes")
    joined = "\n".join(sorted(content_hashes))
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
@dataclass
class ItemLoader:
    """Walk a directory tree, validate each item JSON, register in DB.

    Parameters
    ----------
    db:
        A loader-compliant DB handle (see LoaderDB). May be a mock for tests.
    pool_id:
        Identifier of the item pool being loaded (e.g., "v0.2-phase0").
    require_canary:
        If True (default), enforce the ≥2% canary fraction.
    seat_cutoff_registry:
        Optional mapping seat_name -> ISO cutoff date. When present, time-
        sensitive items (currently only factuality_qa) are rejected unless
        meta.knowledge_after is later than the EARLIEST cutoff of the item's
        seats (spec §5.1 rule 3).
    """

    db: LoaderDB
    pool_id: str
    require_canary: bool = True
    seat_cutoff_registry: dict[str, str] = field(default_factory=dict)

    # -- public API --
    def load_directory(self, root: str | Path) -> list[_LoadedItem]:
        """Walk `root`, load every `.json`, validate, register in the DB."""
        root = Path(root)
        if not root.is_dir():
            raise LoaderError(f"not a directory: {root}")

        items: list[_LoadedItem] = []
        for path in sorted(root.rglob("*.json")):
            if path.is_file():
                items.append(self._load_one(path))

        self._check_canary_fraction(items)
        self._check_pool_hash(items)

        hashes = [it.content_hash_ for it in items]
        self.db.mark_pool_ready(
            pool_id=self.pool_id,
            pool_hash=pool_hash(hashes),
            item_count=len(items),
            canary_count=sum(self._is_canary(it) for it in items),
        )
        return items

    def load_dicts(self, items: Iterable[dict[str, Any]]) -> list[_LoadedItem]:
        """Load from raw item dicts (convenience for tests)."""
        loaded: list[_LoadedItem] = []
        for n, data in enumerate(items):
            try:
                envelope = self._validate_one(data)
            except Exception as exc:  # pragma: no cover - error path
                raise LoaderError(f"item {n}: {exc}") from exc
            chash = self._verify_or_compute_hash(envelope)
            loaded.append(
                _LoadedItem(
                    envelope=envelope,
                    source_path=Path("<dict>"),
                    content_hash_=chash,
                )
            )
            self._persist(envelope, chash)
        self._check_canary_fraction(loaded)
        hashes = [it.content_hash_ for it in loaded]
        self.db.mark_pool_ready(
            pool_id=self.pool_id,
            pool_hash=pool_hash(hashes),
            item_count=len(loaded),
            canary_count=sum(self._is_canary(it) for it in loaded),
        )
        return loaded

    # -- internal --
    def _load_one(self, path: Path) -> _LoadedItem:
        try:
            text = path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except (OSError, json.JSONDecodeError) as exc:
            raise LoaderError(f"{path}: {exc}") from exc
        envelope = self._validate_one(raw)
        chash = self._verify_or_compute_hash(envelope)
        self._check_seat_cutoff(envelope, path)
        self._persist(envelope, chash)
        return _LoadedItem(envelope=envelope, source_path=path, content_hash_=chash)

    def _validate_one(self, raw: dict[str, Any]) -> ItemEnvelope:
        try:
            return ItemEnvelope.model_validate(raw)
        except Exception as exc:
            raise LoaderError(f"validate failed: {exc}") from exc

    def _verify_or_compute_hash(self, envelope: ItemEnvelope) -> str:
        computed = content_hash(envelope)
        if envelope.content_hash is None:
            return computed
        if envelope.content_hash != computed:
            raise LoaderError(
                f"content_hash mismatch for {envelope.item_key}: "
                f"file={envelope.content_hash} computed={computed}"
            )
        return computed

    def _persist(self, envelope: ItemEnvelope, chash: str) -> None:
        self.db.upsert_item(
            pool_id=self.pool_id,
            item_key=envelope.item_key,
            type_=envelope.type.value,
            tier=envelope.tier,
            payload_json=json.dumps(envelope.payload, ensure_ascii=False,
                                    sort_keys=True),
            grading_json=envelope.grading.model_dump_json(by_alias=True),
            meta_json=envelope.meta.model_dump_json(by_alias=True),
            content_hash=chash,
        )

    @staticmethod
    def _is_canary(item: _LoadedItem) -> bool:
        return item.envelope.item_key.startswith(CANARY_PREFIX)

    def _check_canary_fraction(self, items: list[_LoadedItem]) -> None:
        if not self.require_canary:
            return
        total = len(items)
        if total == 0:
            raise LoaderError("pool is empty")
        canary_count = sum(self._is_canary(it) for it in items)
        frac = canary_count / total
        if frac < MIN_CANARY_FRAC:
            raise LoaderError(
                f"canary fraction {frac:.2%} below required {MIN_CANARY_FRAC:.2%} "
                f"({canary_count}/{total})"
            )

    def _check_pool_hash(self, items: list[_LoadedItem]) -> None:
        if not items:
            return  # already raised by _check_canary_fraction if canary=True
        # No explicit pool_hash to verify yet — the loader only computes it.
        _ = pool_hash([it.content_hash_ for it in items])

    def _check_seat_cutoff(
        self, envelope: ItemEnvelope, path: Path
    ) -> None:
        if not self.seat_cutoff_registry:
            return
        # Time-sensitive check (factuality_qa): meta.knowledge_after must be
        # later than the earliest cutoff across the item's seats.
        if envelope.type.value != "factuality_qa":
            return
        cutoffs: list[str] = []
        for seat in envelope.meta.seats:
            if seat in self.seat_cutoff_registry:
                cutoffs.append(self.seat_cutoff_registry[seat])
        if not cutoffs:
            return
        earliest = min(cutoffs)
        knowledge_after = envelope.meta.knowledge_after
        if knowledge_after is None:
            raise LoaderError(
                f"{path}: factuality_qa requires meta.knowledge_after"
            )
        # Compare ISO strings to avoid timezone drift.
        ka_iso = knowledge_after.strftime("%Y-%m-%d")
        if ka_iso <= earliest:
            raise LoaderError(
                f"{path}: meta.knowledge_after={ka_iso} must be later than "
                f"earliest seat cutoff {earliest}"
            )


# ---------------------------------------------------------------------------
# Canonical helpers exposed for tests / sibling modules
# ---------------------------------------------------------------------------
def compute_pool_hash_for_files(paths: Iterable[str | Path]) -> str:
    """Convenience: load files, compute content hashes, return pool_hash."""
    hashes: list[str] = []
    for p in paths:
        raw = json.loads(Path(p).read_text(encoding="utf-8"))
        env = ItemEnvelope.model_validate(raw)
        hashes.append(env.compute_content_hash())
    return pool_hash(hashes)


# Silence unused-import warning for ItemMeta (exported for convenience).
_ = (ItemMeta, os)
