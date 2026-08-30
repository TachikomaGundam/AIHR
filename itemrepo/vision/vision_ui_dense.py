from __future__ import annotations



from vision_draw import (
    font_cjk,
    font_bold,
    font_regular,
    text_size,
    draw_title_bar,
)

def _imaging():
    """Load Pillow drawing primitives on first use.

    Pillow is an optional dependency (the ``[vision]`` extra): item
    registries import every generator up front, but drawing happens only
    when an item is regenerated, so PIL loads lazily inside the generator.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise RuntimeError(
            "vision item generation requires the 'vision' extra (pillow)"
        ) from exc
    return Image, ImageDraw


UI08_DATA = {
    "title": "Build Pipeline",
    "buttons": [
        ["Deploy", "Rollback", "Configure"],
        ["Export", "Roll back", "Debug"],
    ],
    "target": "Roll back",
}

def ui08_generate(path):
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
