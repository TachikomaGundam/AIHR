"""Wiki.js publisher for HR evaluation reports.

Publishes per-model evaluation pages and a team-overview page to a Wiki.js
instance via its GraphQL endpoint.  The publish target is OPTIONAL: it comes
from ``hr.toml`` ``[wiki]`` at the monorepo root (``graphql_url`` /
``api_key_file``), falling back to the legacy env-based defaults
(``WIKI_API_KEY_FILE``, default ``~/.wikijs-api-key``).  Without a ``[wiki]``
section, ``hr publish`` skips cleanly — a missing target is not an error.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import psycopg2.extras

from hr.config import wiki_config
from hr.database import (
    get_assignments,
    get_connection,
    get_model,
    list_models,
)
from hr.models import EvaluationReport, ModelProfile, RoleAssignment

logger = logging.getLogger(__name__)

_DEFAULT_GRAPHQL_URL = "http://localhost:3000/graphql"
_WIKI_KEY_FILE = Path(os.environ.get("WIKI_API_KEY_FILE", str(Path.home() / ".wikijs-api-key")))

_SINGLE_BY_PATH_GQL = """
query SingleByPath($path: String!, $locale: String!) {
    pages { singleByPath(path: $path, locale: $locale) { id path title } }
}
"""


def _slugify(segment: str) -> str:
    """Wiki.js-safe path slug: lowercase, collapse runs of chars outside [a-z0-9_-] to '-'.

    Wiki.js rejects page paths containing '.' or other illegal characters
    (errorCode 6005, "Page path cannot contains illegal characters"). The
    original model_id is preserved in the page title/content and tags; only the
    URL path segment is slugified.
    """
    return re.sub(r"[^a-z0-9_-]+", "-", segment.lower()).strip("-")


def wiki_target() -> dict[str, Any] | None:
    """The optional ``[wiki]`` publish target from root ``hr.toml`` (None = skip)."""
    return wiki_config()

_CREATE_PAGE_GQL = """
mutation CreatePage(
    $title: String!, $path: String!, $description: String!,
    $editor: String!, $locale: String!, $isPublished: Boolean!,
    $isPrivate: Boolean!, $content: String!, $tags: [String!]!
) {
    pages {
        create(
            title: $title, path: $path, description: $description,
            editor: $editor, locale: $locale,
            isPublished: $isPublished, isPrivate: $isPrivate,
            content: $content, tags: $tags
        ) {
            responseResult { succeeded message errorCode }
            page { id title path }
        }
    }
}
"""

_UPDATE_PAGE_GQL = """
mutation UpdatePage(
    $id: Int!, $content: String!, $title: String!, $description: String!,
    $tags: [String!]!, $editor: String!, $locale: String!,
    $isPublished: Boolean!, $isPrivate: Boolean!
) {
    pages {
        update(
            id: $id, content: $content, title: $title, description: $description,
            tags: $tags, editor: $editor, locale: $locale,
            isPublished: $isPublished, isPrivate: $isPrivate
        ) { responseResult { succeeded message errorCode } }
    }
}
"""


# ---------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------


class WikiPublisher:
    """Publishes HR evaluation reports to Wiki.js via GraphQL."""

    def __init__(
        self,
        *,
        graphql_url: str = _DEFAULT_GRAPHQL_URL,
        api_key_file: Path = _WIKI_KEY_FILE,
    ) -> None:
        self._url = graphql_url
        if not api_key_file.exists():
            raise RuntimeError(f"Wiki.js API key file not found: {api_key_file}")
        self._api_key = api_key_file.read_text(encoding="utf-8").strip()
        if not self._api_key:
            raise RuntimeError(f"Wiki.js API key file is empty: {api_key_file}")
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )

    # --- lifecycle ---

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WikiPublisher":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # --- core GraphQL ---

    def _graphql(
        self, mutation_str: str, variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": mutation_str}
        if variables is not None:
            payload["variables"] = variables
        resp = self._client.post(self._url, json=payload)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    # --- page primitives ---

    def find_page(self, path: str) -> int | None:
        """Return the page id at exact `path` (locale 'en'), or None if absent."""
        try:
            data = self._graphql(
                _SINGLE_BY_PATH_GQL, {"path": path, "locale": "en"}
            )
        except httpx.HTTPError as exc:
            logger.warning("Wiki.js find_page(%s) failed: %s", path, exc)
            return None
        page = data.get("data", {}).get("pages", {}).get("singleByPath")
        if not page:
            return None
        return int(page["id"])

    def create_page(
        self,
        title: str,
        path: str,
        content: str,
        description: str = "",
        tags: list[str] | None = None,
    ) -> bool:
        variables = {
            "title": title,
            "path": path,
            "description": description,
            "editor": "markdown",
            "locale": "en",
            "isPublished": True,
            "isPrivate": False,
            "content": content,
            "tags": list(tags or []),
        }
        try:
            data = self._graphql(_CREATE_PAGE_GQL, variables)
        except httpx.HTTPError as exc:
            logger.warning("Wiki.js create_page(%s) failed: %s", path, exc)
            return False
        result = _result_of(data, "pages", "create")
        return bool(result.get("succeeded"))

    def update_page(
        self,
        page_id: int,
        title: str,
        description: str,
        tags: list[str],
        content: str,
    ) -> bool:
        variables = {
            "id": int(page_id),
            "title": title,
            "description": description,
            "tags": list(tags),
            "editor": "markdown",
            "locale": "en",
            "isPublished": True,
            "isPrivate": False,
            "content": content,
        }
        try:
            data = self._graphql(_UPDATE_PAGE_GQL, variables)
        except httpx.HTTPError as exc:
            logger.warning("Wiki.js update_page(%d) failed: %s", page_id, exc)
            return False
        result = _result_of(data, "pages", "update")
        return bool(result.get("succeeded"))

    # --- composite publishing ---

    def publish_model_report(
        self,
        model_profile: ModelProfile,
        report: EvaluationReport,
        benchmarks: list[dict],
    ) -> bool:
        """Create or update a per-model evaluation page at
        ``hr-agents/{provider}/{model_id}``."""
        display = model_profile.display_name or model_profile.model_id
        title = f"HR: {display} Evaluation"
        path = f"hr-agents/{_slugify(model_profile.provider)}/{_slugify(model_profile.model_id)}"
        tags = ["hr-agent", model_profile.provider, model_profile.model_id]
        description = report.summary[:200] if report.summary else title
        content = _render_model_markdown(model_profile, report, benchmarks)
        page_id = self.find_page(path)
        if page_id is None:
            return self.create_page(
                title=title, path=path, content=content,
                description=description, tags=tags,
            )
        return self.update_page(page_id, title, description, tags, content)

    def publish_team_overview(
        self,
        assignments: list[RoleAssignment],
        models: list[ModelProfile],
    ) -> bool:
        """Create or update the team overview page at ``hr-agents/team-overview``."""
        path = "hr-agents/team-overview"
        title = "HR: Team Overview"
        description = "Active HR role assignments across models."
        tags = ["hr-agent", "team"]
        content = _render_team_markdown(assignments, models)
        page_id = self.find_page(path)
        if page_id is None:
            return self.create_page(
                title=title, path=path, content=content,
                description=description, tags=tags,
            )
        return self.update_page(page_id, title, description, tags, content)

    def publish_all(self) -> None:
        """Publish every model report and the team overview, skipping models
        that do not yet have an EvaluationReport."""
        models = list_models()
        assignments = get_assignments()

        for profile in models:
            model_pk = get_model(profile.provider, profile.model_id)
            if model_pk is None:
                continue
            report = _load_report_for_model(model_pk)
            if report is None:
                logger.info("Skipping %s (no report yet)", profile.model_id)
                continue
            benchmarks = _load_benchmarks_for_model(model_pk)
            try:
                self.publish_model_report(profile, report, benchmarks)
            except httpx.HTTPError as exc:
                logger.warning(
                    "publish_model_report(%s) HTTP error: %s",
                    profile.model_id, exc,
                )

        try:
            self.publish_team_overview(assignments, models)
        except httpx.HTTPError as exc:
            logger.warning("publish_team_overview HTTP error: %s", exc)


def publish_from_target(target: dict[str, Any]) -> None:
    """Run a full publish against a resolved ``[wiki]`` target dict.

    ``graphql_url`` and ``api_key_file`` fall back to the legacy env/default
    values when the target does not carry them.
    """
    url = target.get("graphql_url") or _DEFAULT_GRAPHQL_URL
    key_file = target.get("api_key_file") or os.environ.get("WIKI_API_KEY_FILE")
    api_key_file = Path(key_file).expanduser() if key_file else _WIKI_KEY_FILE
    with WikiPublisher(graphql_url=url, api_key_file=api_key_file) as publisher:
        publisher.publish_all()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _result_of(data: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Drill into ``data.pages.<op>.responseResult`` safely."""
    cur: Any = data.get("data", {})
    for k in keys:
        cur = (cur or {}).get(k, {})
    return (cur or {}).get("responseResult", {}) or {}


def _load_benchmarks_for_model(model_pk: int) -> list[dict]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT DISTINCT ON (benchmark_name)"
                " benchmark_name, score, passed FROM hr_benchmarks "
                "WHERE model_fk = %s ORDER BY benchmark_name, created_at DESC",
                (model_pk,),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _load_report_for_model(model_pk: int) -> EvaluationReport | None:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT model_fk, overall_score, pros, cons, "
                "recommended_roles, summary "
                "FROM hr_reports WHERE model_fk = %s ORDER BY id DESC LIMIT 1",
                (model_pk,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return EvaluationReport(
            model_fk=row["model_fk"],
            overall_score=row["overall_score"],
            pros=list(row["pros"] or []),
            cons=list(row["cons"] or []),
            recommended_roles=list(row["recommended_roles"] or []),
            summary=row["summary"] or "",
        )
    finally:
        conn.close()


def _render_model_markdown(
    profile: ModelProfile,
    report: EvaluationReport,
    benchmarks: list[dict],
) -> str:
    display = profile.display_name or profile.model_id
    vision = "Yes" if profile.supports_vision else "No"
    thinking = "Yes" if profile.supports_thinking else "No"
    ctx = str(profile.context_window) if profile.context_window else "n/a"
    out = str(profile.max_output) if profile.max_output else "n/a"

    lines = [
        f"## HR Evaluation: {display}",
        "",
        f"**Provider**: {profile.provider}",
        f"**Model ID**: {profile.model_id}",
        f"**Vision**: {vision} | **Thinking**: {thinking}",
        f"**Context**: {ctx} | **Max Output**: {out}",
        "",
        "### Benchmark Scores",
        "| Category | Score | Passed |",
        "|----------|-------|--------|",
    ]
    if benchmarks:
        for b in benchmarks:
            name = b.get("benchmark_name", "?")
            score = b.get("score", 0)
            mark = "✅" if b.get("passed") else "❌"
            lines.append(f"| {name} | {score}/100 | {mark} |")
    else:
        lines.append("| *no benchmarks yet* | — | — |")

    lines += ["", "### Pros"]
    lines += [f"- {p}" for p in report.pros] or ["- (none)"]
    lines += ["", "### Cons"]
    lines += [f"- {c}" for c in report.cons] or ["- (none)"]

    lines += ["", "### Recommended Roles"]
    if report.recommended_roles:
        lines += [f"- **{r}**" for r in report.recommended_roles]
    else:
        lines.append("- *(no roles yet)*")

    lines += ["", "### HR Verdict", report.summary or "(no verdict yet)", ""]
    lines.append("---")
    lines.append(f"*Generated by HR Agent (人事) on {date.today().isoformat()}*")
    return "\n".join(lines)


def _render_team_markdown(
    assignments: list[RoleAssignment],
    models: list[ModelProfile],
) -> str:
    fk_to_display: dict[int, str] = {}
    try:
        conn = get_connection()
    except Exception as exc:  # pragma: no cover - DB must be up
        logger.warning("Could not fetch models for team overview: %s", exc)
    else:
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id, provider, model_id, display_name FROM hr_models")
                for row in cur.fetchall():
                    match = next(
                        (m for m in models
                         if m.provider == row["provider"] and m.model_id == row["model_id"]),
                        None,
                    )
                    if match is not None:
                        fk_to_display[row["id"]] = match.display_name or match.model_id
        finally:
            conn.close()

    lines = [
        "## HR: Team Overview",
        "",
        "| Role | Model | Fit | Notes |",
        "|------|-------|-----|-------|",
    ]
    if not assignments:
        lines.append("| *(no assignments yet)* | — | — | — |")
    else:
        for a in assignments:
            name = fk_to_display.get(a.model_fk, f"model#{a.model_fk}")
            fit = f"{int(a.fit_score)}/100" if a.fit_score is not None else "—"
            notes = (a.rationale or "").replace("\n", " ")
            lines.append(f"| **{a.role}** | {name} | {fit} | {notes} |")
    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by HR Agent (人事) on {date.today().isoformat()}*")
    return "\n".join(lines)
