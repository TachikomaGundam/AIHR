"""Publish the latest canonical verdict to an optional Wiki.js target."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

from hr.config import wiki_config
from hr.db import connect
from hr.keyscan import scan_outbound


_DEFAULT_GRAPHQL_URL = "http://localhost:3000/graphql"
_SINGLE_BY_PATH = """
query SingleByPath($path: String!, $locale: String!) {
    pages { singleByPath(path: $path, locale: $locale) { id } }
}
"""
_CREATE_PAGE = """
mutation CreatePage(
    $title: String!, $path: String!, $description: String!, $content: String!,
    $tags: [String!]!, $editor: String!, $locale: String!,
    $isPublished: Boolean!, $isPrivate: Boolean!
) {
    pages { create(
        title: $title, path: $path, description: $description, content: $content,
        tags: $tags, editor: $editor, locale: $locale,
        isPublished: $isPublished, isPrivate: $isPrivate
    ) { responseResult { succeeded message errorCode } } }
}
"""
_UPDATE_PAGE = """
mutation UpdatePage(
    $id: Int!, $title: String!, $description: String!, $content: String!,
    $tags: [String!]!, $editor: String!, $locale: String!,
    $isPublished: Boolean!, $isPrivate: Boolean!
) {
    pages { update(
        id: $id, title: $title, description: $description, content: $content,
        tags: $tags, editor: $editor, locale: $locale,
        isPublished: $isPublished, isPrivate: $isPrivate
    ) { responseResult { succeeded message errorCode } } }
}
"""


def wiki_target() -> dict[str, Any] | None:
    """Return the optional Wiki.js target from hr.toml."""
    return wiki_config()


def _slugify(segment: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", segment.lower()).strip("-")


class WikiPublisher:
    """Small GraphQL client for idempotent Wiki.js page publication."""

    def __init__(self, graphql_url: str, api_key_file: Path) -> None:
        if not api_key_file.is_file():
            raise RuntimeError(f"Wiki.js API key file not found: {api_key_file}")
        api_key = api_key_file.read_text(encoding="utf-8").strip()
        if not api_key:
            raise RuntimeError(f"Wiki.js API key file is empty: {api_key_file}")
        self._url = graphql_url
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

    def __enter__(self) -> "WikiPublisher":
        return self

    def __exit__(self, *_: object) -> None:
        self._client.close()

    def _graphql(
        self,
        query: str,
        variables: dict[str, object],
    ) -> dict[str, Any]:
        response = self._client.post(
            self._url,
            json={"query": query, "variables": variables},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Wiki.js returned a non-object GraphQL response")
        if payload.get("errors"):
            raise RuntimeError(f"Wiki.js GraphQL error: {payload['errors']}")
        return payload

    def publish_verdict(self, sweep_id: str, content: str) -> None:
        """Create or update the stable latest-verdict page."""
        scan_outbound(content)
        path = "hr-agents/latest-verdict"
        found = self._graphql(_SINGLE_BY_PATH, {"path": path, "locale": "en"})
        page = found.get("data", {}).get("pages", {}).get("singleByPath")
        variables: dict[str, object] = {
            "title": f"HR Verdict: {sweep_id}",
            "description": f"Latest model seating verdict from sweep {sweep_id}",
            "content": content,
            "tags": ["hr-agent", "verdict", _slugify(sweep_id)],
            "editor": "markdown",
            "locale": "en",
            "isPublished": True,
            "isPrivate": False,
        }
        if page is None:
            result = self._graphql(_CREATE_PAGE, {"path": path, **variables})
            operation = result.get("data", {}).get("pages", {}).get("create", {})
        else:
            result = self._graphql(_UPDATE_PAGE, {"id": int(page["id"]), **variables})
            operation = result.get("data", {}).get("pages", {}).get("update", {})
        status = operation.get("responseResult", {})
        if not status.get("succeeded"):
            raise RuntimeError(
                f"Wiki.js rejected verdict publication: {status.get('message', 'unknown error')}"
            )


def publish_from_target(target: dict[str, Any]) -> None:
    """Build the latest verdict from canonical measurements and publish it."""
    from hr.cli import build_verdict_report, latest_sweep_id

    url = str(target.get("graphql_url") or _DEFAULT_GRAPHQL_URL)
    key_value = target.get("api_key_file") or os.environ.get("WIKI_API_KEY_FILE")
    key_file = (
        Path(str(key_value)).expanduser()
        if key_value
        else Path.home() / ".wikijs-api-key"
    )
    conn = connect()
    try:
        sweep_id = latest_sweep_id(conn)
        verdict = build_verdict_report(conn, sweep_id)
    finally:
        conn.close()
    try:
        with WikiPublisher(url, key_file) as publisher:
            publisher.publish_verdict(sweep_id, verdict)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"Wiki.js request failed: {exc}") from exc
