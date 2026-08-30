from __future__ import annotations



from vision_draw import (
    font_regular,
    draw_window,
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


UI01_DATA = {
    "title": "Project Center",
    "sidebar": ["Home", "Projects", "Settings", "Help", "Logs"],
    "active": 1,
}

def ui01_generate(path):
    Image, ImageDraw = _imaging()
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

UI04_DATA = {
    "title": "Analytics Dashboard",
    "primary_action": "Export Report",
}

def ui04_generate(path):
    Image, ImageDraw = _imaging()
    img = Image.new("RGB", (800, 600), "#ffffff")
    draw = ImageDraw.Draw(img)
    draw_window(draw, img, title=UI04_DATA["title"],
                primary_action_button=UI04_DATA["primary_action"])
    img.save(path)
    return UI04_DATA

def ui04_answer():
    return UI04_DATA["primary_action"]

UI04_QUESTION = "What is the label of the blue primary action button in the top-right of the window?"

UI06_DATA = {
    "title": "Admin Panel",
    "sidebar": ["Dashboard", "Projects", "Analytics", "Reports", "Users",
                "Teams", "Billing", "Settings", "Help"],
    "active": 2,
    "query_idx": 6,  # 7th item (0-indexed)
}

def ui06_generate(path):
    Image, ImageDraw = _imaging()
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

UI07_DATA = {
    "title": "Account Settings",
    "fields": ["Username", "Email", "Password", "Confirm Password",
               "Phone", "Address", "Department"],
    "disabled_field": "Password",
    "error_field": "Phone",
    "error_message": "invalid format",
}

def ui07_generate(path):
    Image, ImageDraw = _imaging()
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
