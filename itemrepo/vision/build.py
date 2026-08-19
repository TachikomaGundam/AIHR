#!/usr/bin/env python3
"""Build B4 vision-lite items: render PNGs and write envelope JSONs."""
from __future__ import annotations
import hashlib
import json
import os
import sys

import generators as G

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.join(HERE, "img")
os.makedirs(IMG_DIR, exist_ok=True)


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def content_hash(payload: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def render_and_write(item) -> dict:
    slug = item["slug"]
    image_path = os.path.join(IMG_DIR, f"{slug}.png")
    item["generate"](image_path)

    # Check file was created with non-trivial size
    size = os.path.getsize(image_path)
    assert size > 1000, f"{slug}: image too small ({size} bytes)"

    answer = str(item["answer_fn"]())
    assert answer.strip(), f"{slug}: empty answer from generator"

    # Image ref is relative to item JSON dir -> img/<slug>.png
    payload = {
        "image_ref": f"img/{slug}.png",
        "kind": item["kind"],
        "question": item["question"],
        "answer": answer,
    }
    grading = {
        "grader": "exact_match@1.0",
    }
    envelope = {
        "item_key": item["item_key"],
        "type": "vision",
        "tier": item["tier"],
        "payload": payload,
        "grading": grading,
        "meta": {
            "source": "generated",
            "generated_by": "hr2-itemgen-b4@0.1",
            "seats": item["seats"],
        },
    }
    envelope["content_hash"] = content_hash(payload)

    json_path = os.path.join(HERE, f"{item['item_key']}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)

    return {
        "item_key": item["item_key"],
        "kind": item["kind"],
        "tier": item["tier"],
        "image": image_path,
        "image_bytes": size,
        "json": json_path,
        "answer": answer,
    }


def main() -> int:
    results = []
    for item in G.ITEMS:
        r = render_and_write(item)
        results.append(r)
        print(f"[build] {r['item_key']} kind={r['kind']} tier={r['tier']} answer={r['answer']!r} img={r['image_bytes']}B")
    print(f"\n{len(results)} items written under {HERE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
