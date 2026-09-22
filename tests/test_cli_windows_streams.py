"""Windows stream hardening (release run 35728085524 regression).

The frozen bundle's ``hr --help`` aborted on the CI windows runner:
rich's box-drawing help frame hits codecs (cp1252) that cannot encode
U+2500-class glyphs -> UnicodeEncodeError. _harden_windows_streams must
downgrade the streams to errors="replace" so output degrades, never crashes.
"""

from __future__ import annotations

import io
import sys

from hr import cli_app


def test_harden_windows_streams_replaces_unencodable(
    monkeypatch: object,
) -> None:
    mp = monkeypatch  # type: ignore[assignment]
    mp.setattr(sys, "platform", "win32")
    out_buf = io.BytesIO()
    err_buf = io.BytesIO()
    out = io.TextIOWrapper(out_buf, encoding="cp1252")
    err = io.TextIOWrapper(err_buf, encoding="cp1252")
    mp.setattr(sys, "stdout", out)
    mp.setattr(sys, "stderr", err)

    cli_app._harden_windows_streams()

    out.write("╭─ Options ─╮ 人事")
    out.flush()
    assert b"Options" in out_buf.getvalue()


def test_harden_windows_streams_noop_off_windows(
    monkeypatch: object,
) -> None:
    mp = monkeypatch  # type: ignore[assignment]
    mp.setattr(sys, "platform", "linux")
    buf = io.BytesIO()
    out = io.TextIOWrapper(buf, encoding="cp1252")
    mp.setattr(sys, "stdout", out)

    cli_app._harden_windows_streams()

    assert out.errors == "strict"
