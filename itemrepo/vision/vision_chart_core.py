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


CRT01_DATA = {
    "categories": ["A", "B", "C", "D", "E"],
    "values": [45, 72, 58, 31, 63],
    "y_max": 80,
}

def crt01_generate(path):
    values = CRT01_DATA["values"]
    cats = CRT01_DATA["categories"]
    y_max = CRT01_DATA["y_max"]
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
