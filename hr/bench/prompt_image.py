from __future__ import annotations

import base64
import zlib


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return len(data).to_bytes(4, "big") + tag + data + crc.to_bytes(4, "big")


def build_test_image_png() -> str:
    """Build a base64 180x180 PNG containing four colored squares."""
    width = height = 180
    squares = (
        (20, 60, 20, 60, b"\xff\x00\x00"),
        (120, 160, 20, 60, b"\x00\x00\xff"),
        (20, 60, 120, 160, b"\x00\x80\x00"),
        (120, 160, 120, 160, b"\xff\xff\x00"),
    )
    rows: list[bytes] = []
    for y_coordinate in range(height):
        row = bytearray()
        for x_coordinate in range(width):
            color = next(
                (
                    rgb
                    for x_start, x_end, y_start, y_end, rgb in squares
                    if x_start <= x_coordinate < x_end
                    and y_start <= y_coordinate < y_end
                ),
                b"\xff\xff\xff",
            )
            row.extend(color)
        rows.append(b"\x00" + bytes(row))
    ihdr = (
        width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + _png_chunk(b"IEND", b"")
    )
    return base64.b64encode(png).decode("ascii")
