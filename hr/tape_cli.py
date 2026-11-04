"""S2b CLI wiring: build tape-backed adapter factories for --record/--replay.

One seam, two directions (SIM-LADDER-v2 §S2, design S2b-MINI-DESIGN-20261103):
replay answers ONLY from the tape — a miss aborts by name (the re-record
procedure), it never quietly falls through to live calls; record tees the
live run and prints a manifest receipt (path + sha256 + endpoints) so the
provenance of every future replay is on the record.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

from .adapters import Adapter, adapter_for
from .adapters import fleet as _fleet_mod
from .tape import TapeError, TapeReader, TapeStore
from .tape_transports import (
    RecordingHttpxClient,
    RecordingRequestsPost,
    replay_post,
)

TapeFactory = Callable[[str], Adapter]


class TapeWiringError(RuntimeError):
    """Tape CLI misuse: unusable direction, missing manifest, wrong seam."""


def make_tape_factory(
    *,
    record_dir: Path | str | None = None,
    replay_dir: Path | str | None = None,
) -> tuple[TapeFactory | None, Any]:
    """Return (adapter_factory, tape_handle). Exactly one direction, always.

    The handle is the TapeStore (record: call finalize() at the end of the
    run) or the TapeReader (replay: preflighted here — a tape that fails its
    manifest check never starts the sweep).
    """
    if record_dir is not None and replay_dir is not None:
        raise TapeWiringError(
            "--tape-record and --tape-replay are mutually exclusive"
        )
    if record_dir is None and replay_dir is None:
        return None, None
    if replay_dir is not None:
        try:
            reader: Any = TapeReader(replay_dir)
        except TapeError as exc:
            raise TapeWiringError(str(exc)) from exc
        # No handle returned for replay: there is nothing to finalize —
        # only a recording run closes with a manifest receipt.
        return _replay_factory(reader), None
    store = TapeStore(record_dir if record_dir is not None else Path.cwd())
    return _record_factory(store), store


def _replay_factory(reader: Any) -> TapeFactory:
    def factory(model_id: str) -> Adapter:
        wire = _fleet_mod.provider_type(_provider_of(model_id))
        if wire == "openai-compat":
            return adapter_for(model_id, transport_post=_guard(reader, "requests"))
        if wire == "anthropic-compat":
            return adapter_for(
                model_id, http_client=_guard_httpx(_HttpxReplayClient(reader))
            )
        raise TapeWiringError(
            f"no tape-capable wire for model {model_id!r} (wire={wire!r})"
        )

    return factory


def _guard(reader: Any, stack: str) -> Any:
    """A replay miss aborts the sweep by name — it never becomes an
    inconclusive row and never falls through to a live call. The engine's
    per-battery except-Exception would swallow TapeError into a measurement,
    so the abort rides SystemExit (a BaseException) past it."""
    post = replay_post(reader)

    def guarded(*args: Any, **kw: Any) -> Any:
        try:
            return post(*args, **kw)
        except TapeError as exc:
            _abort(str(exc))

    return guarded


def _guard_httpx(client: Any) -> Any:
    outer = client

    class _Guarded:
        def stream(self, *args: Any, **kw: Any) -> Any:
            try:
                return outer.stream(*args, **kw)
            except TapeError as exc:
                _abort(str(exc))

        def __getattr__(self, name: str) -> Any:
            return getattr(outer, name)

    return _Guarded()


def _abort(reason: str) -> None:
    import sys

    sys.stderr.write(
        f"tape replay aborted: {reason}\n"
        "re-record procedure: run the same sweep once with --tape-record "
        "<dir> on a network-authorized machine, then copy the tape back.\n"
    )
    raise SystemExit(2)


def _HttpxReplayClient(reader: Any) -> Any:
    from .tape_transports import ReplayHttpxClient

    return ReplayHttpxClient(reader)


def _record_factory(store: TapeStore) -> TapeFactory:
    import httpx
    import requests

    def factory(model_id: str) -> Adapter:
        wire = _fleet_mod.provider_type(_provider_of(model_id))
        if wire == "openai-compat":
            return adapter_for(
                model_id,
                transport_post=RecordingRequestsPost(store, requests.post),
            )
        if wire == "anthropic-compat":
            return adapter_for(
                model_id,
                http_client=RecordingHttpxClient(store, httpx.Client()),
            )
        raise TapeWiringError(
            f"no tape-capable wire for model {model_id!r} (wire={wire!r})"
        )

    return factory


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


def finalize_recording(store: Any) -> Path:
    """Close a recording run: manifest endpoints come from what was seen."""
    paths = sorted({str(meta.get("path", "")) for meta in store._entries.values()})
    return store.finalize(endpoints=paths)


def format_receipt(manifest_path: Path) -> str:
    """One-line-per-fact receipt for a finished recording run."""
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    lines = [f"tape manifest: {manifest_path}", f"tape sha256:   {digest}"]
    for ep in _endpoints_of(manifest_path):
        lines.append(f"tape endpoint: {ep}")
    return "\n".join(lines)


def _endpoints_of(manifest_path: Path) -> list[str]:
    import json

    return list(json.loads(manifest_path.read_text(encoding="utf-8")).get("endpoints", []))
