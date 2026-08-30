from __future__ import annotations



from vision_draw import (
    font_bold,
    draw_block,
    connect_arrow,
    draw_resistor,
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
