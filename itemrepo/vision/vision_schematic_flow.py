from __future__ import annotations



from vision_draw import (
    font_bold,
    draw_arrow,
    draw_block,
    connect_arrow,
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
    Image, ImageDraw = _imaging()
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
