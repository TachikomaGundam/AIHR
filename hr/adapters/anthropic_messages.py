from __future__ import annotations

from typing import Any


def attach_images(
    messages: list[dict[str, Any]],
    images: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not images:
        return list(messages)
    translated = list(messages)
    user_index = next(
        (
            index
            for index in range(len(translated) - 1, -1, -1)
            if translated[index].get("role") == "user"
        ),
        -1,
    )
    if user_index < 0:
        return translated
    message = dict(translated[user_index])
    existing = message.get("content")
    content: list[dict[str, Any]] = []
    if isinstance(existing, str):
        content.append({"type": "text", "text": existing})
    elif isinstance(existing, list):
        content.extend(existing)
    content.extend(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.get("media_type", "image/png"),
                "data": image["data"],
            },
        }
        for image in images
    )
    message["content"] = content
    translated[user_index] = message
    return translated
