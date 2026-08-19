"""B4 vision-lite item generators (hardened).

Each item exposes:
    DATA     - immutable dict of generator parameters (ground-truth source of truth)
    QUESTION - the question string
    KIND     - one of ui_read / schematic / chart_extract
    generate(path) - renders the PNG into `path` and returns the DATA dict
    answer()       - returns the ground-truth answer derived from DATA

No hand-typed ground truths. All answers computed from DATA at verification time.

Hardened version: 18 items targeting discrimination of strong vision models.
  Keep 5 easy baselines (tiers 2-3) + 13 hard items (tiers 4-5).
  Hardening mechanisms: density, fine discrimination, multi-step extraction,
  cross-region correlation, complex topology.
"""

from __future__ import annotations
import math
import os
from dataclasses import dataclass
from typing import Callable, Dict, Tuple

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
_FONT_LATIN_REGULAR = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf"
_FONT_LATIN_BOLD = "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf"
_FONT_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"


def font_cjk(size):
    try:
        return ImageFont.truetype(_FONT_CJK, size, index=0)
    except Exception:
        return ImageFont.load_default()


def font_bold(size):
    try:
        return ImageFont.truetype(_FONT_LATIN_BOLD, size)
    except Exception:
        return font_cjk(size)


def font_regular(size):
    try:
        return ImageFont.truetype(_FONT_LATIN_REGULAR, size)
    except Exception:
        return font_cjk(size)


# ---------------------------------------------------------------------------
# Drawing utilities
# ---------------------------------------------------------------------------
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


# =========================================================================
# Item 1: ui_read.sidebar-count (EASY BASELINE, tier 2)
# =========================================================================
UI01_DATA = {
    "title": "Project Center",
    "sidebar": ["Home", "Projects", "Settings", "Help", "Logs"],
    "active": 1,
}


def ui01_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_window(draw, img, title=UI01_DATA["title"],
                sidebar_items=UI01_DATA["sidebar"],
                active_sidebar_idx=UI01_DATA["active"])
    img.save(path)
    return UI01_DATA


def ui01_answer():
    return str(len(UI01_DATA["sidebar"]))


UI01_QUESTION = "How many navigation items are shown in the left sidebar?"


# =========================================================================
# Item 2: ui_read.window-title-cta (EASY BASELINE, tier 2)
# =========================================================================
UI04_DATA = {
    "title": "Analytics Dashboard",
    "primary_action": "Export Report",
}


def ui04_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_window(draw, img, title=UI04_DATA["title"],
                primary_action_button=UI04_DATA["primary_action"])
    img.save(path)
    return UI04_DATA


def ui04_answer():
    return UI04_DATA["primary_action"]


UI04_QUESTION = "What is the label of the blue primary action button in the top-right of the window?"


# =========================================================================
# Item 3: ui_read.dense-sidebar (HARD, tier 4)
# =========================================================================
UI06_DATA = {
    "title": "Admin Panel",
    "sidebar": ["Dashboard", "Projects", "Analytics", "Reports", "Users",
                "Teams", "Billing", "Settings", "Help"],
    "active": 2,
    "query_idx": 6,  # 7th item (0-indexed)
}


def ui06_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_window(draw, img, title=UI06_DATA["title"],
                sidebar_items=UI06_DATA["sidebar"],
                active_sidebar_idx=UI06_DATA["active"])
    img.save(path)
    return UI06_DATA


def ui06_answer():
    return UI06_DATA["sidebar"][UI06_DATA["query_idx"]]


UI06_QUESTION = "What is the label of the 7th navigation item in the left sidebar?"


# =========================================================================
# Item 4: ui_read.multi-state-form (HARD, tier 5)
# =========================================================================
UI07_DATA = {
    "title": "Account Settings",
    "fields": ["Username", "Email", "Password", "Confirm Password",
               "Phone", "Address", "Department"],
    "disabled_field": "Password",
    "error_field": "Phone",
    "error_message": "invalid format",
}


def ui07_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_title_bar(draw, 800, 600, UI07_DATA["title"])

    fields = UI07_DATA["fields"]
    disabled = UI07_DATA["disabled_field"]
    error = UI07_DATA["error_field"]
    fx, fy = 40, 60
    for fname in fields:
        is_disabled = (fname == disabled)
        is_error = (fname == error)

        label_color = "#9ca3af" if is_disabled else "#374151"
        draw.text((fx, fy), fname, font=font_regular(16), fill=label_color)
        fy += 22

        box = [(fx, fy), (fx + 500, fy + 28)]
        if is_disabled:
            # Disabled: gray background box, no content
            draw.rectangle(box, fill="#f3f4f6", outline="#d1d5db", width=1)
        elif is_error:
            draw.rectangle(box, fill="#ffffff", outline="#ef4444", width=2)
            draw.text((fx + 8, fy + 4), "••••••••", font=font_regular(14), fill="#c0c0c0")
        else:
            draw.rectangle(box, fill="#ffffff", outline="#9ca3af", width=1)
            draw.text((fx + 8, fy + 4), "••••••••", font=font_regular(14), fill="#c0c0c0")

        fy += 30
        if is_error:
            draw.text((fx, fy), "⚠ " + UI07_DATA["error_message"],
                      font=font_regular(13), fill="#dc2626")
            fy += 16
        fy += 6

    img.save(path)
    return UI07_DATA


def ui07_answer():
    fields = UI07_DATA["fields"]
    disabled_idx = fields.index(UI07_DATA["disabled_field"])
    return fields[disabled_idx + 1]


UI07_QUESTION = "What is the label of the form field directly below the disabled (gray, unfocusable) field?"


# =========================================================================
# Item 5: ui_read.near-label-buttons (HARD, tier 4)
# =========================================================================
UI08_DATA = {
    "title": "Build Pipeline",
    "buttons": [
        ["Deploy", "Rollback", "Configure"],
        ["Export", "Roll back", "Debug"],
    ],
    "target": "Roll back",
}


def ui08_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_title_bar(draw, 800, 600, UI08_DATA["title"])

    btn_font = font_regular(17)
    btn_w, btn_h = 170, 44
    col_x = [80, 310, 540]
    row_y = [180, 260]

    for r, row in enumerate(UI08_DATA["buttons"]):
        for c, label in enumerate(row):
            x, y = col_x[c], row_y[r]
            draw.rectangle([(x, y), (x + btn_w, y + btn_h)],
                           fill="#ffffff", outline="#6b7280", width=2)
            tw, th = text_size(draw, label, btn_font)
            draw.text((x + (btn_w - tw) / 2, y + (btn_h - th) / 2),
                      label, font=btn_font, fill="#111827")

    # Instruction hint
    draw.text((80, 360), "Select an action:", font=font_regular(15), fill="#6b7280")

    img.save(path)
    return UI08_DATA


def ui08_answer():
    return UI08_DATA["target"]


UI08_QUESTION = "Among the action buttons, which label is written as two separate words instead of a single compound word?"


# =========================================================================
# Item 6: ui_read.dashboard-cross-region (HARD, tier 5)
# =========================================================================
UI09_DATA = {
    "title": "Service Monitor",
    "sidebar": ["Auth Service", "API Gateway", "Database",
                "Cache Layer", "Worker Pool", "Scheduler"],
    "active": 3,  # Cache Layer
    "table_headers": ["Service Name", "Status", "Uptime"],
    "table_rows": [
        ("Auth Service", "ACTIVE", "99.9%"),
        ("API Gateway", "DEGRADED", "97.2%"),
        ("Database", "ACTIVE", "99.8%"),
        ("Cache Layer", "WARNING", "95.1%"),
        ("Worker Pool", "ACTIVE", "99.5%"),
        ("Scheduler", "MAINTENANCE", "—"),
    ],
}

_STATUS_COLORS = {
    "ACTIVE": "#16a34a",
    "DEGRADED": "#f59e0b",
    "WARNING": "#ef4444",
    "MAINTENANCE": "#6b7280",
}


def ui09_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_title_bar(draw, 800, 600, UI09_DATA["title"])

    # Sidebar
    sidebar_w = 180
    draw.rectangle([(0, 40), (sidebar_w - 1, 599)], fill="#f3f4f6", outline="#d1d5db")
    y = 58
    for i, item in enumerate(UI09_DATA["sidebar"]):
        if i == UI09_DATA["active"]:
            draw.rectangle([(4, y - 4), (sidebar_w - 4, y + 22)], fill="#3b82f6")
            draw.text((16, y), item, font=font_regular(16), fill="#ffffff")
        else:
            draw.text((16, y), item, font=font_regular(16), fill="#111827")
        y += 34

    # Data table
    tx = 200
    ty = 70
    col_w = [220, 150, 120]
    rh = 38
    total_w = sum(col_w)

    # Header
    draw.rectangle([(tx, ty), (tx + total_w, ty + rh)], fill="#e5e7eb", outline="#d1d5db")
    cx = tx + 10
    for h, w in zip(UI09_DATA["table_headers"], col_w):
        draw.text((cx, ty + 10), h, font=font_bold(14), fill="#374151")
        cx += w
    ty += rh

    # Rows
    for row in UI09_DATA["table_rows"]:
        draw.rectangle([(tx, ty), (tx + total_w, ty + rh)],
                       outline="#d1d5db", fill="#ffffff")
        cx = tx + 10
        for i, (cell, w) in enumerate(zip(row, col_w)):
            color = _STATUS_COLORS.get(cell, "#111827") if i == 1 else "#111827"
            draw.text((cx, ty + 10), str(cell), font=font_regular(15), fill=color)
            cx += w
        ty += rh

    img.save(path)
    return UI09_DATA


def ui09_answer():
    active = UI09_DATA["sidebar"][UI09_DATA["active"]]
    for name, status, _ in UI09_DATA["table_rows"]:
        if name == active:
            return status
    return ""


UI09_QUESTION = "What status indicator is shown in the table row matching the highlighted sidebar item?"


# =========================================================================
# Item 7: ui_read.cjk-dense-table (HARD, tier 4)
# =========================================================================
UI10_DATA = {
    "title": "管理面板",
    "sidebar": ["概览", "用户管理", "数据分析", "系统配置",
                "日志查看", "权限设置", "帮助文档"],
    "active": 5,  # 权限设置
    "table_rows": [
        ("概览", "12"),
        ("用户管理", "248"),
        ("数据分析", "45"),
        ("系统配置", "7"),
        ("日志查看", "1024"),
        ("权限设置", "32"),
        ("帮助文档", "15"),
    ],
}


def ui10_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_title_bar(draw, 800, 600, UI10_DATA["title"], use_cjk=True)

    # Sidebar
    sidebar_w = 160
    draw.rectangle([(0, 40), (sidebar_w - 1, 599)], fill="#f3f4f6", outline="#d1d5db")
    y = 58
    cjk_font = font_cjk(16)
    for i, item in enumerate(UI10_DATA["sidebar"]):
        if i == UI10_DATA["active"]:
            draw.rectangle([(4, y - 4), (sidebar_w - 4, y + 22)], fill="#3b82f6")
            draw.text((12, y), item, font=cjk_font, fill="#ffffff")
        else:
            draw.text((12, y), item, font=cjk_font, fill="#111827")
        y += 34

    # Table
    tx = 180
    ty = 70
    col_w = [200, 130]
    rh = 36
    total_w = sum(col_w)

    # Header
    draw.rectangle([(tx, ty), (tx + total_w, ty + rh)], fill="#e5e7eb", outline="#d1d5db")
    draw.text((tx + 10, ty + 8), "模块", font=font_cjk(14), fill="#374151")
    draw.text((tx + col_w[0] + 10, ty + 8), "数量", font=font_cjk(14), fill="#374151")
    ty += rh

    for name, count in UI10_DATA["table_rows"]:
        draw.rectangle([(tx, ty), (tx + total_w, ty + rh)],
                       outline="#d1d5db", fill="#ffffff")
        draw.text((tx + 10, ty + 8), name, font=font_cjk(14), fill="#111827")
        draw.text((tx + col_w[0] + 10, ty + 8), str(count),
                  font=font_regular(14), fill="#111827")
        ty += rh

    img.save(path)
    return UI10_DATA


def ui10_answer():
    active = UI10_DATA["sidebar"][UI10_DATA["active"]]
    for name, count in UI10_DATA["table_rows"]:
        if name == active:
            return count
    return ""


UI10_QUESTION = "In the data table, what numeric value is displayed in the row for the section highlighted in the sidebar?"


# =========================================================================
# Item 8: schematic.signal-flow (EASY BASELINE, tier 3)
# =========================================================================
SCH03_DATA = {
    "nodes": [
        ("Controller", (150, 300)),
        ("Valve", (370, 300)),
        ("Reactor", (590, 300)),
        ("Sensor", (740, 480)),
    ],
    "forward_edges": [
        ("Controller", "Valve"),
        ("Valve", "Reactor"),
        ("Reactor", "Sensor"),
    ],
    "feedback_edges": [
        ("Sensor", "Controller"),
    ],
}


def sch03_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Schematic: control loop", font=font_bold(20), fill="#0f172a")
    positions = {n[0]: n[1] for n in SCH03_DATA["nodes"]}
    for src, dst in SCH03_DATA["forward_edges"]:
        a, b = positions[src], positions[dst]
        draw_arrow(draw, (a[0] + 60, a[1]), (b[0] - 60, b[1]), color="#1f2937", width=2)
    for src, dst in SCH03_DATA["feedback_edges"]:
        a, b = positions[src], positions[dst]
        mid_y = max(a[1], b[1]) + 60
        pts = [
            (a[0], a[1] + 30),
            (a[0], mid_y),
            (b[0], mid_y),
            (b[0], b[1] + 30),
        ]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill="#b91c1c", width=2)
        ex, ey = pts[-1]
        draw.polygon([(ex, ey), (ex - 7, ey + 10), (ex + 7, ey + 10)], fill="#b91c1c")
        draw.text(((a[0] + b[0]) / 2 - 30, mid_y + 4), "feedback",
                  font=font_bold(14), fill="#b91c1c")
    for name, (cx, cy) in SCH03_DATA["nodes"]:
        draw_block(draw, (cx, cy), name, fill="#fef3c7", outline="#92400e")
    img.save(path)
    return SCH03_DATA


def sch03_answer():
    return "yes" if SCH03_DATA["feedback_edges"] else "no"


SCH03_QUESTION = "Does a signal path exist from the output of the Sensor back into the Controller (answer 'yes' or 'no')?"


# =========================================================================
# Item 9: schematic.dense-path (HARD, tier 5)
# =========================================================================
SCH06_DATA = {
    "nodes": [
        ("Input", (90, 180)),
        ("Preamp", (240, 180)),
        ("EQ", (390, 180)),
        ("Compressor", (540, 180)),
        ("Limiter", (690, 180)),
        ("Splitter", (400, 420)),
        ("Recorder", (580, 420)),
        ("Output", (740, 330)),
    ],
    "edges": [
        ("Input", "Preamp"),
        ("Preamp", "EQ"),
        ("EQ", "Compressor"),
        ("Compressor", "Limiter"),
        ("Limiter", "Splitter"),
        ("Splitter", "Recorder"),
        ("Recorder", "Output"),
        ("Splitter", "Output"),
    ],
    "path_start": "Limiter",
    "path_end": "Output",
    "path_via": "Recorder",
}


def sch06_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Audio processing chain", font=font_bold(20), fill="#0f172a")
    positions = {n[0]: n[1] for n in SCH06_DATA["nodes"]}

    # Draw edges
    for src, dst in SCH06_DATA["edges"]:
        connect_arrow(draw, positions, src, dst)

    # Draw nodes (variable width for long names)
    for name, (cx, cy) in SCH06_DATA["nodes"]:
        w = max(110, len(name) * 14 + 20)
        draw_block(draw, (cx, cy), name, w=w)

    img.save(path)
    return SCH06_DATA


def sch06_answer():
    """Find path from start to end passing through via."""
    adj = {}
    for s, d in SCH06_DATA["edges"]:
        adj.setdefault(s, []).append(d)

    start = SCH06_DATA["path_start"]
    via = SCH06_DATA["path_via"]
    end = SCH06_DATA["path_end"]

    def all_paths(s, e, visited):
        if s == e:
            return [[s]]
        visited = visited | {s}
        result = []
        for n in adj.get(s, []):
            if n not in visited:
                for p in all_paths(n, e, visited):
                    result.append([s] + p)
        return result

    for p in all_paths(start, end, set()):
        if via in p:
            return ", ".join(p)
    return ""


SCH06_QUESTION = "List the components on the directed path from 'Limiter' to 'Output' that passes through 'Recorder', in order. Answer as comma-separated names."


# =========================================================================
# Item 10: schematic.bypass-path (HARD, tier 5)
# =========================================================================
SCH07_DATA = {
    "nodes": [
        ("Controller", (150, 180)),
        ("Actuator", (390, 180)),
        ("Plant", (600, 180)),
        ("Sensor", (600, 450)),
        ("Bypass", (150, 450)),
    ],
    "forward_edges": [
        ("Controller", "Actuator"),
        ("Actuator", "Plant"),
        ("Plant", "Sensor"),
    ],
    "bypass_edges": [
        ("Controller", "Bypass"),
        ("Bypass", "Sensor"),
    ],
    "feedback_edges": [
        ("Sensor", "Controller"),
    ],
    "check_start": "Controller",
    "check_end": "Sensor",
    "avoid_nodes": ["Actuator", "Plant"],
}


def sch07_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Control system with bypass", font=font_bold(20), fill="#0f172a")
    positions = {n[0]: n[1] for n in SCH07_DATA["nodes"]}

    # Forward edges
    for src, dst in SCH07_DATA["forward_edges"]:
        connect_arrow(draw, positions, src, dst)

    # Bypass edges (blue)
    for src, dst in SCH07_DATA["bypass_edges"]:
        connect_arrow(draw, positions, src, dst, color="#2563eb", width=2)

    # Feedback (red, curved)
    for src, dst in SCH07_DATA["feedback_edges"]:
        a, b = positions[src], positions[dst]
        # Curved path below
        mid_y = 550
        pts = [(a[0], a[1] + 30), (a[0], mid_y), (b[0], mid_y), (b[0], b[1] + 30)]
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill="#b91c1c", width=2)
        ex, ey = pts[-1]
        draw.polygon([(ex, ey), (ex - 7, ey + 10), (ex + 7, ey + 10)], fill="#b91c1c")
        draw.text((300, mid_y + 4), "feedback", font=font_bold(13), fill="#b91c1c")

    # Bypass label
    bx = (positions["Controller"][0] + positions["Bypass"][0]) / 2
    by = (positions["Controller"][1] + positions["Bypass"][1]) / 2
    draw.text((bx - 80, by), "bypass", font=font_bold(13), fill="#2563eb")

    # Nodes
    colors = {
        "Controller": ("#dbeafe", "#1e40af"),
        "Actuator": ("#fef3c7", "#92400e"),
        "Plant": ("#fef3c7", "#92400e"),
        "Sensor": ("#dcfce7", "#14532d"),
        "Bypass": ("#ede9fe", "#5b21b6"),
    }
    for name, (cx, cy) in SCH07_DATA["nodes"]:
        f, o = colors.get(name, ("#dbeafe", "#1e40af"))
        draw_block(draw, (cx, cy), name, fill=f, outline=o)

    img.save(path)
    return SCH07_DATA


def sch07_answer():
    """Check if path exists from start to end avoiding specified nodes."""
    adj = {}
    for e_list in [SCH07_DATA["forward_edges"], SCH07_DATA["bypass_edges"],
                   SCH07_DATA["feedback_edges"]]:
        for s, d in e_list:
            adj.setdefault(s, []).append(d)

    start = SCH07_DATA["check_start"]
    end = SCH07_DATA["check_end"]
    avoid = set(SCH07_DATA["avoid_nodes"])

    def can_reach(s, visited):
        if s == end:
            return True
        visited = visited | {s}
        for n in adj.get(s, []):
            if n not in visited and n not in avoid:
                if can_reach(n, visited):
                    return True
        return False

    # Check from start's neighbors (start itself is not avoided)
    for n in adj.get(start, []):
        if n not in avoid:
            if can_reach(n, {start}):
                return "yes"
    return "no"


SCH07_QUESTION = "Is there a directed path from Controller to Sensor that does NOT pass through 'Actuator' or 'Plant'? (answer 'yes' or 'no')"


# =========================================================================
# Item 11: schematic.node-degree (HARD, tier 4)
# =========================================================================
SCH08_DATA = {
    "nodes": [
        ("Ingest", (90, 180)),
        ("Router", (280, 300)),
        ("ParserA", (480, 120)),
        ("ParserB", (480, 260)),
        ("ParserC", (480, 400)),
        ("Merger", (660, 260)),
        ("Output", (780, 260)),
    ],
    "edges": [
        ("Ingest", "Router"),
        ("Router", "ParserA"),
        ("Router", "ParserB"),
        ("Router", "ParserC"),
        ("ParserA", "Merger"),
        ("ParserB", "Merger"),
        ("ParserC", "Merger"),
        ("Merger", "Output"),
    ],
    "query_node": "Router",
}


def sch08_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Data pipeline topology", font=font_bold(20), fill="#0f172a")
    positions = {n[0]: n[1] for n in SCH08_DATA["nodes"]}

    for src, dst in SCH08_DATA["edges"]:
        connect_arrow(draw, positions, src, dst)

    for name, (cx, cy) in SCH08_DATA["nodes"]:
        w = max(110, len(name) * 14 + 20)
        fill = "#bfdbfe" if name == SCH08_DATA["query_node"] else "#dbeafe"
        outline = "#1e3a8a" if name == SCH08_DATA["query_node"] else "#1e40af"
        draw_block(draw, (cx, cy), name, w=w, fill=fill, outline=outline)

    img.save(path)
    return SCH08_DATA


def sch08_answer():
    q = SCH08_DATA["query_node"]
    count = sum(1 for s, d in SCH08_DATA["edges"] if s == q or d == q)
    return str(count)


SCH08_QUESTION = "How many directed edges connect to or from the highlighted 'Router' node (counting both incoming and outgoing)?"


# =========================================================================
# Item 12: schematic.dual-destination (HARD, tier 4)
# =========================================================================
SCH09_DATA = {
    "nodes": [
        ("Source", (80, 300)),
        ("Router", (250, 300)),
        ("TaskA", (450, 120)),
        ("TaskB", (450, 240)),
        ("TaskC", (450, 360)),
        ("TaskD", (450, 480)),
        ("Sink", (660, 240)),
    ],
    "edges": [
        ("Source", "Router"),
        ("Router", "TaskA"),
        ("Router", "TaskB"),
        ("Router", "TaskC"),
        ("Router", "TaskD"),
        ("TaskA", "Sink"),
        ("TaskB", "Sink"),
        ("TaskC", "Sink"),
    ],
}


def sch09_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Task distribution topology", font=font_bold(20), fill="#0f172a")
    positions = {n[0]: n[1] for n in SCH09_DATA["nodes"]}

    for src, dst in SCH09_DATA["edges"]:
        connect_arrow(draw, positions, src, dst)

    for name, (cx, cy) in SCH09_DATA["nodes"]:
        w = max(100, len(name) * 14 + 20)
        draw_block(draw, (cx, cy), name, w=w)

    img.save(path)
    return SCH09_DATA


def sch09_answer():
    """Find node downstream of Router that has no edge to Sink."""
    router_downstream = {d for s, d in SCH09_DATA["edges"] if s == "Router"}
    sink_inputs = {s for s, d in SCH09_DATA["edges"] if d == "Sink"}
    for node in sorted(router_downstream):
        if node not in sink_inputs:
            return node
    return ""


SCH09_QUESTION = "Among the four nodes downstream of 'Router', which one does NOT have an outgoing edge to 'Sink'?"


# =========================================================================
# Item 13: schematic.dense-resistor-net (HARD, tier 5)
# =========================================================================
SCH10_DATA = {
    "nodes": ["VCC", "A", "B", "C", "OUT", "GND"],
    "resistors": [
        ("R1", "VCC", "A"),
        ("R2", "A", "B"),
        ("R3", "A", "GND"),
        ("R4", "B", "C"),
        ("R5", "B", "GND"),
        ("R6", "C", "OUT"),
        ("R7", "C", "GND"),
        ("R8", "OUT", "GND"),
    ],
    "query_node": "GND",
}


def sch10_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Schematic: resistor ladder network", font=font_bold(20), fill="#0f172a")

    node_xy = {
        "VCC": (130, 100),
        "A": (300, 100),
        "B": (470, 100),
        "C": (640, 100),
        "OUT": (700, 260),
        "GND": (400, 490),
    }

    # Draw nodes
    for n, (x, y) in node_xy.items():
        draw.ellipse([(x - 7, y - 7), (x + 7, y + 7)], fill="#000000")
        draw.text((x + 12, y - 20), n, font=font_bold(14), fill="#0f172a")

    for name, a, b in SCH10_DATA["resistors"]:
        draw_resistor(draw, node_xy[a], node_xy[b], name)

    img.save(path)
    return SCH10_DATA


def sch10_answer():
    q = SCH10_DATA["query_node"]
    return str(sum(1 for _, a, b in SCH10_DATA["resistors"] if a == q or b == q))


SCH10_QUESTION = "How many resistors have one terminal connected to the 'GND' node?"


# =========================================================================
# Item 14: chart_extract.bar-max (EASY BASELINE, tier 2)
# =========================================================================
CRT01_DATA = {
    "categories": ["A", "B", "C", "D", "E"],
    "values": [45, 72, 58, 31, 63],
    "y_max": 80,
}


def crt01_generate(path):
    values = CRT01_DATA["values"]
    cats = CRT01_DATA["categories"]
    y_max = CRT01_DATA["y_max"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Quarterly Sales by Region", font=font_bold(22), fill="#0f172a")
    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    chart_top = ax_y - ax_h
    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)
    for v in range(0, y_max + 1, 20):
        y = ax_y - (v / y_max) * ax_h
        draw.line([(ax_x - 6, y), (ax_x, y)], fill="#111111", width=1)
        draw.text((ax_x - 40, y - 8), str(v), font=font_regular(14), fill="#374151")
    n = len(values)
    bar_w = int(ax_w * 0.7 / n)
    gap = int(ax_w * 0.3 / (n + 1))
    colors = ["#2563eb", "#dc2626", "#16a34a", "#f59e0b", "#7c3aed"]
    for i, (c, v) in enumerate(zip(cats, values)):
        left = ax_x + gap + i * (bar_w + gap)
        bh = (v / y_max) * ax_h
        top = ax_y - bh
        draw.rectangle([(left, top), (left + bar_w, ax_y)], fill=colors[i % len(colors)])
        draw.text((left + bar_w / 2 - 5, top - 20), str(v), font=font_bold(14), fill="#111111")
        draw.text((left + bar_w / 2 - 5, ax_y + 6), c, font=font_bold(16), fill="#111111")
    img.save(path)
    return CRT01_DATA


def crt01_answer():
    vals = CRT01_DATA["values"]
    cats = CRT01_DATA["categories"]
    i = max(range(len(vals)), key=lambda k: vals[k])
    return f"{cats[i]}, {vals[i]}"


CRT01_QUESTION = "Which category has the tallest bar, and what is its exact value? Answer as 'category, value' (e.g. 'A, 45')."


# =========================================================================
# Item 15: chart_extract.trend-direction (EASY BASELINE, tier 3)
# =========================================================================
CRT04_DATA = {
    "series": [
        (0, 10), (1, 12), (2, 15), (3, 20), (4, 25),
        (5, 28), (6, 26), (7, 22), (8, 18), (9, 14), (10, 10),
    ],
    "query_interval": (4, 8),
    "trend": "decreasing",
}


def crt04_generate(path):
    s = CRT04_DATA["series"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Sensor reading over time", font=font_bold(22), fill="#0f172a")
    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    xs = [p[0] for p in s]
    ys = [p[1] for p in s]
    x_lo, x_hi = min(xs), max(xs)
    y_lo = min(ys) - 2
    y_hi = max(ys) + 2
    draw.line([(ax_x, ax_y - ax_h), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)
    for xi in range(x_lo, x_hi + 1):
        px = ax_x + (xi - x_lo) / (x_hi - x_lo) * ax_w
        draw.line([(px, ax_y), (px, ax_y + 6)], fill="#111111", width=1)
        draw.text((px - 4, ax_y + 10), str(xi), font=font_regular(13), fill="#374151")
    for yi in range(int(y_lo), int(y_hi) + 1, 4):
        py = ax_y - (yi - y_lo) / (y_hi - y_lo) * ax_h
        draw.line([(ax_x - 6, py), (ax_x, py)], fill="#111111", width=1)
        draw.text((ax_x - 38, py - 8), str(yi), font=font_regular(13), fill="#374151")
    q_lo, q_hi = CRT04_DATA["query_interval"]
    qpx_lo = ax_x + (q_lo - x_lo) / (x_hi - x_lo) * ax_w
    qpx_hi = ax_x + (q_hi - x_lo) / (x_hi - x_lo) * ax_w
    draw.rectangle([(qpx_lo, ax_y - ax_h), (qpx_hi, ax_y)], fill="#fde68a")
    pts = []
    for xi, yi in s:
        px = ax_x + (xi - x_lo) / (x_hi - x_lo) * ax_w
        py = ax_y - (yi - y_lo) / (y_hi - y_lo) * ax_h
        pts.append((px, py))
    for i in range(len(pts) - 1):
        draw.line([pts[i], pts[i + 1]], fill="#7c3aed", width=3)
    for p in pts:
        draw.ellipse([(p[0] - 3, p[1] - 3), (p[0] + 3, p[1] + 3)], fill="#7c3aed")
    img.save(path)
    return CRT04_DATA


def crt04_answer():
    a, b = CRT04_DATA["query_interval"]
    series = CRT04_DATA["series"]
    ya = next(v for (x, v) in series if x == a)
    yb = next(v for (x, v) in series if x == b)
    return "increasing" if yb > ya else "decreasing" if yb < ya else "flat"


CRT04_QUESTION = "Looking at the highlighted yellow interval (x = 4 to x = 8), what is the overall trend direction of the line (answer 'increasing' or 'decreasing')?"


# =========================================================================
# Item 16: chart_extract.eight-near-bars (HARD, tier 4)
# =========================================================================
CRT06_DATA = {
    "categories": ["A", "B", "C", "D", "E", "F", "G", "H"],
    "values": [42, 44, 46, 47, 43, 48, 45, 41],
    "y_max": 55,
    "focus_idx": 5,  # F
}


def crt06_generate(path):
    cats = CRT06_DATA["categories"]
    vals = CRT06_DATA["values"]
    y_max = CRT06_DATA["y_max"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Processing throughput by stage", font=font_bold(22), fill="#0f172a")

    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    chart_top = ax_y - ax_h

    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)

    for v in range(0, y_max + 1, 10):
        y = ax_y - (v / y_max) * ax_h
        draw.line([(ax_x - 6, y), (ax_x, y)], fill="#111111", width=1)
        draw.text((ax_x - 36, y - 8), str(v), font=font_regular(14), fill="#374151")

    n = len(vals)
    bar_w = int(ax_w * 0.75 / n)
    gap = int(ax_w * 0.25 / (n + 1))
    for i, (c, v) in enumerate(zip(cats, vals)):
        left = ax_x + gap + i * (bar_w + gap)
        bh = (v / y_max) * ax_h
        top = ax_y - bh
        draw.rectangle([(left, top), (left + bar_w, ax_y)], fill="#2563eb", outline="#1e3a8a")
        draw.text((left + bar_w / 2 - 8, top - 20), str(v), font=font_bold(14), fill="#111111")
        draw.text((left + bar_w / 2 - 5, ax_y + 6), c, font=font_bold(15), fill="#111111")
    img.save(path)
    return CRT06_DATA


def crt06_answer():
    return str(CRT06_DATA["values"][CRT06_DATA["focus_idx"]])


CRT06_QUESTION = "What is the exact numeric value shown above the bar for category 'F'?"


# =========================================================================
# Item 17: chart_extract.smallest-gap (HARD, tier 5)
# =========================================================================
CRT07_DATA = {
    "categories": ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6"],
    "series_a": [50, 62, 45, 73, 58, 66],
    "series_b": [48, 59, 43, 70, 57, 58],
    "y_max": 80,
}


def crt07_generate(path):
    cats = CRT07_DATA["categories"]
    sa = CRT07_DATA["series_a"]
    sb = CRT07_DATA["series_b"]
    y_max = CRT07_DATA["y_max"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Performance comparison (Series A vs B)", font=font_bold(22), fill="#0f172a")

    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    chart_top = ax_y - ax_h

    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)

    for v in range(0, y_max + 1, 20):
        y = ax_y - (v / y_max) * ax_h
        draw.line([(ax_x - 6, y), (ax_x, y)], fill="#111111", width=1)
        draw.text((ax_x - 36, y - 8), str(v), font=font_regular(14), fill="#374151")

    n = len(cats)
    group_w = int(ax_w * 0.8 / n)
    gap = int(ax_w * 0.2 / (n + 1))
    bar_w = int(group_w * 0.4)

    for i, c in enumerate(cats):
        left_base = ax_x + gap + i * (group_w + gap)
        # Series A (blue)
        left_a = left_base
        bh_a = (sa[i] / y_max) * ax_h
        top_a = ax_y - bh_a
        draw.rectangle([(left_a, top_a), (left_a + bar_w, ax_y)], fill="#2563eb", outline="#1e3a8a")
        draw.text((left_a + bar_w / 2 - 8, top_a - 20), str(sa[i]),
                  font=font_bold(12), fill="#1e40af")
        # Series B (red)
        left_b = left_base + bar_w + 4
        bh_b = (sb[i] / y_max) * ax_h
        top_b = ax_y - bh_b
        draw.rectangle([(left_b, top_b), (left_b + bar_w, ax_y)], fill="#dc2626", outline="#991b1b")
        draw.text((left_b + bar_w / 2 - 8, top_b - 20), str(sb[i]),
                  font=font_bold(12), fill="#991b1b")
        # Category label
        cx = left_base + group_w / 2
        draw.text((cx - 8, ax_y + 6), c, font=font_bold(15), fill="#111111")

    # Legend
    lx, ly = ax_x + ax_w - 160, chart_top + 10
    draw.rectangle([(lx, ly), (lx + 18, ly + 12)], fill="#2563eb")
    draw.text((lx + 24, ly - 2), "Series A", font=font_regular(14), fill="#111111")
    draw.rectangle([(lx, ly + 22), (lx + 18, ly + 34)], fill="#dc2626")
    draw.text((lx + 24, ly + 20), "Series B", font=font_regular(14), fill="#111111")

    img.save(path)
    return CRT07_DATA


def crt07_answer():
    diffs = [abs(a - b) for a, b in zip(CRT07_DATA["series_a"], CRT07_DATA["series_b"])]
    min_idx = min(range(len(diffs)), key=lambda k: diffs[k])
    return CRT07_DATA["categories"][min_idx]


CRT07_QUESTION = "For which category is the absolute difference between Series A (blue) and Series B (red) the smallest? Answer with the category label."


# =========================================================================
# Item 18: chart_extract.three-lines-middle (HARD, tier 5)
# =========================================================================
CRT08_DATA = {
    "lines": [
        {"name": "Alpha", "color": "#2563eb",
         "values": [45, 47, 49, 51, 53, 55, 57, 59, 61, 63, 65]},
        {"name": "Beta", "color": "#dc2626",
         "values": [28, 33, 38, 43, 48, 53, 58, 63, 68, 73, 78]},
        {"name": "Gamma", "color": "#16a34a",
         "values": [50, 51, 52, 53, 53, 54, 55, 56, 56, 57, 58]},
    ],
    "x_range": (0, 10),
    "query_x": 5,
}


def crt08_generate(path):
    lines = CRT08_DATA["lines"]
    x0, x1 = CRT08_DATA["x_range"]
    qx = CRT08_DATA["query_x"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Multi-sensor readings over time", font=font_bold(22), fill="#0f172a")

    ax_x, ax_y = 90, 530
    ax_w, ax_h = 600, 400
    chart_top = ax_y - ax_h

    # Compute y range
    all_vals = [v for l in lines for v in l["values"]]
    y_lo = min(all_vals) - 3
    y_hi = max(all_vals) + 3

    # Axes
    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)

    # Y ticks
    for yi in range(int(y_lo), int(y_hi) + 1, 5):
        py = ax_y - (yi - y_lo) / (y_hi - y_lo) * ax_h
        draw.line([(ax_x - 6, py), (ax_x, py)], fill="#111111", width=1)
        draw.text((ax_x - 38, py - 8), str(yi), font=font_regular(13), fill="#374151")

    # X ticks
    for xi in range(x0, x1 + 1):
        px = ax_x + (xi - x0) / (x1 - x0) * ax_w
        draw.line([(px, ax_y), (px, ax_y + 6)], fill="#111111", width=1)
        draw.text((px - 4, ax_y + 10), str(xi), font=font_regular(13), fill="#374151")

    # Draw lines with dots
    for l in lines:
        pts = []
        for i, v in enumerate(l["values"]):
            px = ax_x + (i - x0) / (x1 - x0) * ax_w
            py = ax_y - (v - y_lo) / (y_hi - y_lo) * ax_h
            pts.append((px, py))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill=l["color"], width=3)
        for p in pts:
            draw.ellipse([(p[0] - 3, p[1] - 3), (p[0] + 3, p[1] + 3)], fill=l["color"])

    qpx = ax_x + (qx - x0) / (x1 - x0) * ax_w
    draw.line([(qpx, chart_top), (qpx, ax_y)], fill="#888888", width=1)

    label_data = []
    for l in lines:
        v = l["values"][qx]
        py = ax_y - (v - y_lo) / (y_hi - y_lo) * ax_h
        label_data.append((py, v, l["name"], l["color"]))
    label_data.sort(key=lambda t: t[0])

    min_gap = 18
    placed = []
    for py, v, name, color in label_data:
        ly = py - 8
        for prev_y in placed:
            if abs(ly - prev_y) < min_gap:
                ly = prev_y + min_gap
        placed.append(ly)
        draw.text((qpx + 8, ly), f"{name}: {v}", font=font_bold(13), fill=color)

    # Legend
    lx, ly = ax_x + ax_w - 140, chart_top + 10
    for i, l in enumerate(lines):
        draw.rectangle([(lx, ly + i * 24), (lx + 24, ly + i * 24 + 14)], fill=l["color"])
        draw.text((lx + 30, ly + i * 24), l["name"], font=font_regular(14), fill="#111111")

    img.save(path)
    return CRT08_DATA


def crt08_answer():
    qx = CRT08_DATA["query_x"]
    values_at_qx = [(l["name"], l["values"][qx]) for l in CRT08_DATA["lines"]]
    values_at_qx.sort(key=lambda x: x[1])
    # Median = middle of 3
    return values_at_qx[1][0]


CRT08_QUESTION = "At x = 5, which of the three lines has the median (middle) value? Answer with the line name."


# =========================================================================
# Item 19: chart_extract.exact-double (HARD, tier 4)
# =========================================================================
CRT09_DATA = {
    "categories": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"],
    "values": [18, 24, 36, 12, 42, 27, 15],
    "y_max": 50,
}


def crt09_generate(path):
    cats = CRT09_DATA["categories"]
    vals = CRT09_DATA["values"]
    y_max = CRT09_DATA["y_max"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Resource usage by service", font=font_bold(22), fill="#0f172a")

    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    chart_top = ax_y - ax_h

    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)

    for v in range(0, y_max + 1, 10):
        y = ax_y - (v / y_max) * ax_h
        draw.line([(ax_x - 6, y), (ax_x, y)], fill="#111111", width=1)
        draw.text((ax_x - 36, y - 8), str(v), font=font_regular(14), fill="#374151")

    n = len(vals)
    bar_w = int(ax_w * 0.7 / n)
    gap = int(ax_w * 0.3 / (n + 1))
    colors = ["#2563eb", "#7c3aed", "#16a34a", "#dc2626", "#f59e0b", "#0891b2", "#be185d"]
    for i, (c, v) in enumerate(zip(cats, vals)):
        left = ax_x + gap + i * (bar_w + gap)
        bh = (v / y_max) * ax_h
        top = ax_y - bh
        draw.rectangle([(left, top), (left + bar_w, ax_y)], fill=colors[i % len(colors)])
        draw.text((left + bar_w / 2 - 8, top - 20), str(v), font=font_bold(14), fill="#111111")
        draw.text((left + bar_w / 2 - 14, ax_y + 6), c, font=font_regular(13), fill="#111111")
    img.save(path)
    return CRT09_DATA


def crt09_answer():
    vals = CRT09_DATA["values"]
    cats = CRT09_DATA["categories"]
    min_val = min(vals)
    target = min_val * 2
    for i, v in enumerate(vals):
        if v == target:
            return cats[i]
    return ""


CRT09_QUESTION = "Which category has a value that is exactly twice the smallest value in the chart? Answer with the category name."


# =========================================================================
# Item 20: ui_read.tier3-toggles (tier 3, NON-BINARY count)
# =========================================================================
UI_T3_DATA = {
    "title": "Application Settings",
    "toggles": [
        ("Notifications", True),
        ("Auto-save", True),
        ("Dark Mode", False),
        ("Analytics", True),
        ("Two-factor Auth", False),
        ("Cloud Sync", False),
    ],
}


def ui_t3_generate(path):
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_title_bar(draw, 800, 600, UI_T3_DATA["title"])

    y = 80
    for label, is_on in UI_T3_DATA["toggles"]:
        # Label
        draw.text((60, y), label, font=font_regular(18), fill="#111827")

        # Toggle switch (rounded rectangle)
        toggle_x = 500
        toggle_w, toggle_h = 60, 30
        toggle_y = y - 5

        bg_color = "#22c55e" if is_on else "#d1d5db"
        draw.rounded_rectangle(
            [(toggle_x, toggle_y), (toggle_x + toggle_w, toggle_y + toggle_h)],
            radius=15, fill=bg_color
        )

        # Toggle circle
        circle_x = toggle_x + toggle_w - 18 if is_on else toggle_x + 18
        circle_y = toggle_y + toggle_h / 2
        draw.ellipse(
            [(circle_x - 10, circle_y - 10), (circle_x + 10, circle_y + 10)],
            fill="#ffffff"
        )

        y += 70

    img.save(path)
    return UI_T3_DATA


def ui_t3_answer():
    return str(sum(1 for _, is_on in UI_T3_DATA["toggles"] if is_on))


UI_T3_QUESTION = "In the settings panel, how many toggle switches are in the ON (green) state?"


# =========================================================================
# Item 21: schematic.tier3-next-component (tier 3, NON-BINARY name)
# =========================================================================
SCH_T3_DATA = {
    "nodes": [
        ("Input", (80, 300)),
        ("Filter", (240, 300)),
        ("Processor", (400, 300)),
        ("Analyzer", (560, 300)),
        ("Output", (720, 300)),
    ],
    "edges": [
        ("Input", "Filter"),
        ("Filter", "Processor"),
        ("Processor", "Analyzer"),
        ("Analyzer", "Output"),
    ],
    "query_from": "Filter",
}


def sch_t3_generate(path):
    img = Image.new("RGB", (800, 600), "#f8fafc")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Data processing pipeline", font=font_bold(20), fill="#0f172a")

    positions = {n[0]: n[1] for n in SCH_T3_DATA["nodes"]}

    # Draw arrows
    for src, dst in SCH_T3_DATA["edges"]:
        connect_arrow(draw, positions, src, dst)

    # Draw nodes
    query_from = SCH_T3_DATA["query_from"]
    for name, (cx, cy) in SCH_T3_DATA["nodes"]:
        fill = "#fef3c7" if name == query_from else "#dbeafe"
        outline = "#92400e" if name == query_from else "#1e40af"
        draw_block(draw, (cx, cy), name, fill=fill, outline=outline)

    img.save(path)
    return SCH_T3_DATA


def sch_t3_answer():
    query_from = SCH_T3_DATA["query_from"]
    for src, dst in SCH_T3_DATA["edges"]:
        if src == query_from:
            return dst
    return ""


SCH_T3_QUESTION = "In the processing pipeline, which component comes immediately after the highlighted 'Filter' block?"


# =========================================================================
# Item 22: chart_extract.tier3-bar-value (tier 3, NON-BINARY number)
# =========================================================================
CRT_T3_DATA = {
    "categories": ["Jan", "Feb", "Mar", "Apr", "May"],
    "values": [35, 58, 42, 67, 49],
    "y_max": 80,
    "query_idx": 3,  # Apr (index 3)
    "query_color": "#dc2626",  # red
}


def crt_t3_generate(path):
    cats = CRT_T3_DATA["categories"]
    vals = CRT_T3_DATA["values"]
    y_max = CRT_T3_DATA["y_max"]
    query_idx = CRT_T3_DATA["query_idx"]
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw.text((20, 18), "Monthly Revenue ($K)", font=font_bold(22), fill="#0f172a")

    ax_x, ax_y = 90, 540
    ax_w, ax_h = 640, 440
    chart_top = ax_y - ax_h

    draw.line([(ax_x, chart_top), (ax_x, ax_y)], fill="#111111", width=2)
    draw.line([(ax_x, ax_y), (ax_x + ax_w, ax_y)], fill="#111111", width=2)

    # Y-axis ticks
    for v in range(0, y_max + 1, 20):
        y = ax_y - (v / y_max) * ax_h
        draw.line([(ax_x - 6, y), (ax_x, y)], fill="#111111", width=1)
        draw.text((ax_x - 40, y - 8), str(v), font=font_regular(14), fill="#374151")

    n = len(vals)
    bar_w = int(ax_w * 0.7 / n)
    gap = int(ax_w * 0.3 / (n + 1))
    colors = ["#2563eb", "#16a34a", "#dc2626", "#dc2626", "#7c3aed"]

    for i, (c, v) in enumerate(zip(cats, vals)):
        left = ax_x + gap + i * (bar_w + gap)
        bh = (v / y_max) * ax_h
        top = ax_y - bh
        draw.rectangle([(left, top), (left + bar_w, ax_y)], fill=colors[i])
        # Value label above bar
        draw.text((left + bar_w / 2 - 8, top - 20), str(v), font=font_bold(14), fill="#111111")
        # Category label below bar
        draw.text((left + bar_w / 2 - 10, ax_y + 6), c, font=font_bold(15), fill="#111111")

    img.save(path)
    return CRT_T3_DATA


def crt_t3_answer():
    return str(CRT_T3_DATA["values"][CRT_T3_DATA["query_idx"]])


CRT_T3_QUESTION = "What is the exact value (in $K) shown on the red bar (April)?"


# =========================================================================
# Item registry (22 items)
# =========================================================================
ITEMS = [
    # --- ui_read (6 items) ---
    {
        "item_key": "vision.ui_read.sidebar-count",
        "slug": "ui_01_sidebar_count",
        "kind": "ui_read",
        "tier": 2,
        "question": UI01_QUESTION,
        "generate": ui01_generate,
        "answer_fn": ui01_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.window-title-cta",
        "slug": "ui_04_window_title_cta",
        "kind": "ui_read",
        "tier": 2,
        "question": UI04_QUESTION,
        "generate": ui04_generate,
        "answer_fn": ui04_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.dense-sidebar",
        "slug": "ui_06_dense_sidebar",
        "kind": "ui_read",
        "tier": 4,
        "question": UI06_QUESTION,
        "generate": ui06_generate,
        "answer_fn": ui06_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.multi-state-form",
        "slug": "ui_07_multi_state_form",
        "kind": "ui_read",
        "tier": 5,
        "question": UI07_QUESTION,
        "generate": ui07_generate,
        "answer_fn": ui07_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.near-label-buttons",
        "slug": "ui_08_near_label_buttons",
        "kind": "ui_read",
        "tier": 4,
        "question": UI08_QUESTION,
        "generate": ui08_generate,
        "answer_fn": ui08_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.dashboard-cross-region",
        "slug": "ui_09_dashboard_cross_region",
        "kind": "ui_read",
        "tier": 5,
        "question": UI09_QUESTION,
        "generate": ui09_generate,
        "answer_fn": ui09_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.ui_read.cjk-dense-table",
        "slug": "ui_10_cjk_dense_table",
        "kind": "ui_read",
        "tier": 4,
        "question": UI10_QUESTION,
        "generate": ui10_generate,
        "answer_fn": ui10_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    # --- schematic (6 items) ---
    {
        "item_key": "vision.schematic.signal-flow",
        "slug": "sch_03_signal_flow",
        "kind": "schematic",
        "tier": 3,
        "question": SCH03_QUESTION,
        "generate": sch03_generate,
        "answer_fn": sch03_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.schematic.dense-path",
        "slug": "sch_06_dense_path",
        "kind": "schematic",
        "tier": 5,
        "question": SCH06_QUESTION,
        "generate": sch06_generate,
        "answer_fn": sch06_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.schematic.bypass-path",
        "slug": "sch_07_bypass_path",
        "kind": "schematic",
        "tier": 5,
        "question": SCH07_QUESTION,
        "generate": sch07_generate,
        "answer_fn": sch07_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.schematic.node-degree",
        "slug": "sch_08_node_degree",
        "kind": "schematic",
        "tier": 4,
        "question": SCH08_QUESTION,
        "generate": sch08_generate,
        "answer_fn": sch08_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.schematic.dual-destination",
        "slug": "sch_09_dual_destination",
        "kind": "schematic",
        "tier": 4,
        "question": SCH09_QUESTION,
        "generate": sch09_generate,
        "answer_fn": sch09_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.schematic.dense-resistor-net",
        "slug": "sch_10_dense_resistor_net",
        "kind": "schematic",
        "tier": 5,
        "question": SCH10_QUESTION,
        "generate": sch10_generate,
        "answer_fn": sch10_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    # --- chart_extract (5 items) ---
    {
        "item_key": "vision.chart_extract.bar-max",
        "slug": "crt_01_bar_max",
        "kind": "chart_extract",
        "tier": 2,
        "question": CRT01_QUESTION,
        "generate": crt01_generate,
        "answer_fn": crt01_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    {
        "item_key": "vision.chart_extract.trend-direction",
        "slug": "crt_04_trend_direction",
        "kind": "chart_extract",
        "tier": 3,
        "question": CRT04_QUESTION,
        "generate": crt04_generate,
        "answer_fn": crt04_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    {
        "item_key": "vision.chart_extract.eight-near-bars",
        "slug": "crt_06_eight_near_bars",
        "kind": "chart_extract",
        "tier": 4,
        "question": CRT06_QUESTION,
        "generate": crt06_generate,
        "answer_fn": crt06_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    {
        "item_key": "vision.chart_extract.smallest-gap",
        "slug": "crt_07_smallest_gap",
        "kind": "chart_extract",
        "tier": 5,
        "question": CRT07_QUESTION,
        "generate": crt07_generate,
        "answer_fn": crt07_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    {
        "item_key": "vision.chart_extract.three-lines-middle",
        "slug": "crt_08_three_lines_middle",
        "kind": "chart_extract",
        "tier": 5,
        "question": CRT08_QUESTION,
        "generate": crt08_generate,
        "answer_fn": crt08_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    {
        "item_key": "vision.chart_extract.exact-double",
        "slug": "crt_09_exact_double",
        "kind": "chart_extract",
        "tier": 4,
        "question": CRT09_QUESTION,
        "generate": crt09_generate,
        "answer_fn": crt09_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
    # --- tier 3 coverage (non-binary) ---
    {
        "item_key": "vision.ui_read.tier3-toggles",
        "slug": "ui_t3_toggles",
        "kind": "ui_read",
        "tier": 3,
        "question": UI_T3_QUESTION,
        "generate": ui_t3_generate,
        "answer_fn": ui_t3_answer,
        "seats": ["multimodal_looker", "visual_engineering"],
    },
    {
        "item_key": "vision.schematic.tier3-next-component",
        "slug": "sch_t3_next_component",
        "kind": "schematic",
        "tier": 3,
        "question": SCH_T3_QUESTION,
        "generate": sch_t3_generate,
        "answer_fn": sch_t3_answer,
        "seats": ["multimodal_looker", "circuit_engineer"],
    },
    {
        "item_key": "vision.chart_extract.tier3-bar-value",
        "slug": "crt_t3_bar_value",
        "kind": "chart_extract",
        "tier": 3,
        "question": CRT_T3_QUESTION,
        "generate": crt_t3_generate,
        "answer_fn": crt_t3_answer,
        "seats": ["multimodal_looker", "artistry"],
    },
]
