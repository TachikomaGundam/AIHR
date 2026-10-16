"""Seating-chain read-back reconciliation (apply → re-parse → per-seat check).

The envelope (schema gate, backup, auto-restore) already existed; these
lock the missing link: what was WRITTEN must read back as what was
INTENDED, seat by seat, or apply fails loudly (and the envelope restores).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hr import apply as apply_mod

_SEATING = [
    {"seat_code": "oracle", "primary": "prov/model-a"},
    {"seat_code": "quick", "primary": "prov/model-b"},
]


def _fake_latest(conn, *, deployable=None):
    return list(_SEATING), "sweep-test"


@pytest.fixture()
def patched(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "latest_assignments", _fake_latest)
    return tmp_path


def test_apply_readback_receipt_ok(patched):
    msg = apply_mod.apply(None, config_dir=patched)
    assert "sha256 " in msg
    assert "  verified: oracle -> prov/model-a" in msg
    assert "  verified: quick -> prov/model-b" in msg


def test_apply_raises_when_written_preset_lies(patched, monkeypatch):
    real = apply_mod.write_preset

    def lying(agents, name, config_dir, **kw):
        path = real(agents, name, config_dir, **kw)
        store = json.loads(path.read_text())
        store["presets"][name]["agents"]["oracle"] = "evil/other"
        path.write_text(json.dumps(store))
        return path

    monkeypatch.setattr(apply_mod, "write_preset", lying)
    with pytest.raises(RuntimeError, match="read-back failed.*oracle.*intended 'prov/model-a', read-back 'evil/other'"):
        apply_mod.apply(None, config_dir=patched)


def test_apply_raises_on_phantom_seat(patched, monkeypatch):
    real = apply_mod.write_preset

    def extra(agents, name, config_dir, **kw):
        agents = {**agents, "ghost": "prov/haunt"}
        return real(agents, name, config_dir, **kw)

    monkeypatch.setattr(apply_mod, "write_preset", extra)
    with pytest.raises(RuntimeError, match="ghost.*not in the intended seating"):
        apply_mod.apply(None, config_dir=patched)


def test_apply_state_readback_ok_and_divergence_raises(patched, monkeypatch):
    msg = apply_mod.apply(None, config_dir=patched, set_state=True)
    assert "state -> " in msg and "(read-back verified)" in msg
    assert "restart" in msg

    real = apply_mod.write_state

    def lying(agents, config_dir):
        path = real(agents, config_dir)
        raw = json.loads(path.read_text())
        raw["agents"] = {}
        path.write_text(json.dumps(raw))
        return path

    monkeypatch.setattr(apply_mod, "write_state", lying)
    with pytest.raises(RuntimeError, match="state file agents map diverges"):
        apply_mod.apply(None, config_dir=patched, set_state=True)
