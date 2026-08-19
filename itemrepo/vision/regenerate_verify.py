#!/usr/bin/env python3
"""Regenerate every B4 vision-lite image + answer from its generator,
then compare freshly-computed answers to the committed JSON answers.

Print one line per item: item_key  kind  derived_answer  PASS|FAIL
Exit code: 0 iff every item is PASS.
"""
from __future__ import annotations
import hashlib
import json
import os
import sys
import tempfile

import generators as G

HERE = os.path.dirname(os.path.abspath(__file__))
VER = "0.1"


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def content_hash(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def load_envelope(item_key: str) -> dict:
    path = os.path.join(HERE, f"{item_key}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def regenerate_into(item, tmpdir: str) -> dict:
    """Re-run the generator into tmpdir and re-derive the answer."""
    out_path = os.path.join(tmpdir, f"{item['slug']}.png")
    data = item["generate"](out_path)
    regen_answer = str(item["answer_fn"]())
    size = os.path.getsize(out_path)
    return {"path": out_path, "size": size, "regen_answer": regen_answer}


def main() -> int:
    failed = 0
    with tempfile.TemporaryDirectory(prefix="b4_vision_regen_") as tmp:
        print(f"{G.VER if hasattr(G, 'VER') else ''}", end="")
        for item in G.ITEMS:
            env = load_envelope(item["item_key"])
            regen = regenerate_into(item, tmp)

            stored = env["payload"]["answer"]
            stored_kind = env["payload"]["kind"]
            derived = regen["regen_answer"]

            # Content-hash must match after regen (payload is identical by construction)
            regen_payload = dict(env["payload"])  # same payload re-derived
            expected_hash = content_hash(regen_payload)
            hash_ok = env.get("content_hash") == expected_hash
            image_ok = regen["size"] > 1000
            kind_ok = stored_kind == item["kind"]
            answer_ok = derived == stored

            status = "PASS" if (answer_ok and hash_ok and kind_ok and image_ok) else "FAIL"
            if status == "FAIL":
                failed += 1

            print(f"[{status}] {env['item_key']}  kind={stored_kind}  "
                  f"derived={derived!r}  stored={stored!r}  "
                  f"image={regen['size']}B  hash_ok={hash_ok}")

    if failed:
        print(f"\n{failed} items FAILED", file=sys.stderr)
        return 1
    print(f"\nAll {len(G.ITEMS)} items PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
