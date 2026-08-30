from __future__ import annotations



from vision_draw import (
    font_bold,
    font_regular,
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
    Image, ImageDraw = _imaging()
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

CRT09_DATA = {
    "categories": ["Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta"],
    "values": [18, 24, 36, 12, 42, 27, 15],
    "y_max": 50,
}

def crt09_generate(path):
    cats = CRT09_DATA["categories"]
    vals = CRT09_DATA["values"]
    y_max = CRT09_DATA["y_max"]
    Image, ImageDraw = _imaging()
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
