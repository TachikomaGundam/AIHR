from __future__ import annotations



from vision_draw import (
    font_bold,
    font_regular,
    draw_block,
    connect_arrow,
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
