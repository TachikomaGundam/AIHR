from __future__ import annotations

import math

def _font():
    """Load Pillow's font API on first use.

    Pillow is an optional dependency (the ``[vision]`` extra): fonts are
    only needed when an item is actually rendered, so PIL loads lazily
    inside the font helpers instead of at module import.
    """
    try:
        from PIL import ImageFont
    except ImportError as exc:
        raise RuntimeError(
            "vision item generation requires the 'vision' extra (pillow)"
        ) from exc
    return ImageFont



_FONT_LATIN_REGULAR = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf"

_FONT_LATIN_BOLD = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf"

_FONT_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

def font_cjk(size):
    ImageFont = _font()
    try:
        return ImageFont.truetype(_FONT_CJK, size, index=0)
    except Exception:
        return ImageFont.load_default()

def font_bold(size):
    ImageFont = _font()
    try:
        return ImageFont.truetype(_FONT_LATIN_BOLD, size)
    except Exception:
        return font_cjk(size)

def font_regular(size):
    ImageFont = _font()
    try:
        return ImageFont.truetype(_FONT_LATIN_REGULAR, size)
    except Exception:
        return font_cjk(size)

def draw_text_center(draw, xy, text, font, fill):
    x, y = xy
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x - tw / 2 - bbox[0], y - th / 2 - bbox[1]), text, font=font, fill=fill)

def text_size(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]

def draw_window(draw, img, *, title, width=800, height=600, bg="#ffffff",
                sidebar_items=None, sidebar_width=180, active_sidebar_idx=None,
                title_font=None, body_font=None,
                disabled_button_label=None, buttons=None,
                form_fields=None, form_error_field=None, form_error_message=None,
                primary_action_button=None, window_size=(800, 600),
                font_cjk_loader=False):
    """Draw a full application window with optional sidebar, fields, action button."""
    title_font = title_font or font_bold(26)
    body_font = body_font or font_regular(18)

    # Outer window border
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline="#8a8a8a", fill=bg)

    # Title bar
    title_h = 40
    draw.rectangle([(0, 0), (width - 1, title_h)], fill="#1f2937")
    _title_font = font_cjk_loader and font_cjk(22) or font_bold(22)
    draw.text((16, 10), title, font=_title_font, fill="#ffffff")

    # Close button (decorative)
    draw.rectangle([(width - 44, 10), (width - 12, 30)], outline="#9ca3af", width=1)
    draw.line([(width - 40, 14), (width - 16, 26)], fill="#e5e7eb", width=1)
    draw.line([(width - 16, 14), (width - 40, 26)], fill="#e5e7eb", width=1)

    content_top = title_h

    # Sidebar
    if sidebar_items:
        draw.rectangle([(0, content_top), (sidebar_width - 1, height - 1)], fill="#f3f4f6", outline="#d1d5db")
        y = content_top + 18
        _sfont = font_cjk(18) if font_cjk_loader else body_font
        for i, item in enumerate(sidebar_items):
            if i == active_sidebar_idx:
                draw.rectangle([(4, y - 4), (sidebar_width - 4, y + 22)], fill="#3b82f6")
                draw.text((16, y), item, font=_sfont, fill="#ffffff")
            else:
                draw.text((16, y), item, font=_sfont, fill="#111827")
            y += 34
        content_left = sidebar_width
    else:
        content_left = 0

    # Form fields
    if form_fields:
        fx = content_left + 30
        fy = content_top + 30
        for i, fname in enumerate(form_fields):
            # Label
            draw.text((fx, fy), fname, font=font_cjk(16) if font_cjk_loader else body_font, fill="#374151")
            fy += 22
            # Text box
            box = [(fx, fy), (fx + 420, fy + 28)]
            err = form_error_field == fname
            draw.rectangle(box, fill="#ffffff", outline="#ef4444" if err else "#9ca3af", width=2 if err else 1)
            # Placeholder dots
            draw.text((fx + 8, fy + 4), ("• " * 8).strip(), font=font_regular(14), fill="#c0c0c0")
            fy += 30
            if err and form_error_message:
                draw.text((fx, fy), "⚠ " + form_error_message, font=font_cjk(13) if font_cjk_loader else font_regular(13), fill="#dc2626")
                fy += 18
            fy += 12

    # Action buttons (bottom-right of main area)
    if buttons:
        btn_y = height - 70
        btn_x = width - 40
        _btn_font = font_cjk(16) if font_cjk_loader else body_font
        for b in reversed(buttons):
            tw, th = text_size(draw, b, _btn_font)
            box = [(btn_x - tw - 28, btn_y), (btn_x, btn_y + 34)]
            if b == disabled_button_label:
                draw.rectangle(box, fill="#e5e7eb", outline="#b4b4b4")
                draw.text((btn_x - tw - 14, btn_y + 7), b, font=_btn_font, fill="#9ca3af")
            elif b == primary_action_button:
                draw.rectangle(box, fill="#2563eb", outline="#1e40af")
                draw.text((btn_x - tw - 14, btn_y + 7), b, font=_btn_font, fill="#ffffff")
            else:
                draw.rectangle(box, fill="#ffffff", outline="#6b7280")
                draw.text((btn_x - tw - 14, btn_y + 7), b, font=_btn_font, fill="#111827")
            btn_x = box[0][0] - 14

    elif primary_action_button:
        _pfont = font_cjk(16) if font_cjk_loader else body_font
        tw, th = text_size(draw, primary_action_button, _pfont)
        box = [(width - tw - 50, content_top + 14), (width - 20, content_top + 14 + 28)]
        draw.rectangle(box, fill="#2563eb", outline="#1e40af")
        draw.text((box[0][0] + 14, box[0][1] + 4), primary_action_button, font=_pfont, fill="#ffffff")

def draw_arrow(draw, start, end, color="#111827", width=2):
    sx, sy = start
    ex, ey = end
    draw.line([start, end], fill=color, width=width)
    # Arrowhead
    angle = math.atan2(ey - sy, ex - sx)
    size = 10
    a1 = angle + math.pi - 0.45
    a2 = angle + math.pi + 0.45
    p1 = (ex + size * math.cos(a1), ey + size * math.sin(a1))
    p2 = (ex + size * math.cos(a2), ey + size * math.sin(a2))
    draw.polygon([(ex, ey), p1, p2], fill=color)

def draw_block(draw, center, label, *, w=110, h=56, fill="#dbeafe", outline="#1e40af", font=None):
    x, y = center
    font = font or font_bold(16)
    box = [(x - w / 2, y - h / 2), (x + w / 2, y + h / 2)]
    draw.rectangle(box, fill=fill, outline=outline, width=2)
    tw, th = text_size(draw, label, font)
    draw.text((x - tw / 2, y - th / 2), label, font=font, fill="#0f172a")
    return box

def connect_arrow(draw, positions, src, dst, color="#1f2937", width=2):
    """Draw arrow from src block edge to dst block edge based on relative position."""
    sx, sy = positions[src]
    dx, dy = positions[dst]
    w2, h2 = 55, 28  # half block dimensions
    if abs(dx - sx) >= abs(dy - sy):
        if dx > sx:
            start, end = (sx + w2, sy), (dx - w2, dy)
        else:
            start, end = (sx - w2, sy), (dx + w2, dy)
    else:
        if dy > sy:
            start, end = (sx, sy + h2), (dx, dy - h2)
        else:
            start, end = (sx, sy - h2), (dx, dy + h2)
    draw_arrow(draw, start, end, color=color, width=width)

def draw_resistor(draw, xy1, xy2, label):
    """Draw a resistor symbol between two points with a label."""
    x1, y1 = xy1; x2, y2 = xy2
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1:
        return
    ux, uy = dx / length, dy / length
    nx, ny = -uy, ux
    lead = 14
    zig = 10
    start_x = x1 + lead * ux
    start_y = y1 + lead * uy
    end_x = x2 - lead * ux
    end_y = y2 - lead * uy
    seg_len = math.hypot(end_x - start_x, end_y - start_y)
    n_zigs = 4
    seg = seg_len / (n_zigs * 2)
    pts = [(x1, y1), (start_x, start_y)]
    for i in range(n_zigs * 2):
        mx = start_x + ux * seg * (i + 0.5)
        my = start_y + uy * seg * (i + 0.5)
        sign = 1 if i % 2 == 0 else -1
        pts.append((mx + sign * nx * zig, my + sign * ny * zig))
    pts.append((end_x, end_y))
    pts.append((x2, y2))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill="#000000", width=2)
    mid_x = (x1 + x2) / 2 + nx * 18
    mid_y = (y1 + y2) / 2 + ny * 18
    draw.text((mid_x, mid_y), label, font=font_bold(15), fill="#7f1d1d")

def draw_title_bar(draw, width, height, title, use_cjk=False):
    """Draw standard window chrome: border + title bar + close button."""
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline="#8a8a8a", fill="#ffffff")
    draw.rectangle([(0, 0), (width - 1, 40)], fill="#1f2937")
    tfont = font_cjk(22) if use_cjk else font_bold(22)
    draw.text((16, 10), title, font=tfont, fill="#ffffff")
    draw.rectangle([(width - 44, 10), (width - 12, 30)], outline="#9ca3af", width=1)
    draw.line([(width - 40, 14), (width - 16, 26)], fill="#e5e7eb", width=1)
    draw.line([(width - 16, 14), (width - 40, 26)], fill="#e5e7eb", width=1)
