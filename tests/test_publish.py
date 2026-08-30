from __future__ import annotations

import pytest

from hr.keyscan import SecretLeakError
from hr.publish import WikiPublisher


def test_wiki_publish_rejects_secret_shaped_content_before_network(tmp_path, monkeypatch) -> None:
    # Given: a configured publisher and verdict containing synthetic key material.
    key_file = tmp_path / "wiki.key"
    key_file.write_text("synthetic-wiki-key", encoding="utf-8")
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    monkeypatch.setattr(
        publisher,
        "_graphql",
        lambda *_args, **_kwargs: pytest.fail("network boundary was reached"),
    )

    # When / Then: outbound scanning blocks publication before GraphQL.
    with publisher, pytest.raises(SecretLeakError):
        publisher.publish_verdict(
            "sweep",
            "token sk-ant-FFFFFFFFFFFFFFFFGGGG2222HHHHHHHH",
        )


def test_wiki_publisher_init_refuses_missing_or_empty_key_file(
    tmp_path,
) -> None:
    # Given: no key file, then an empty one
    missing = tmp_path / "nope.key"
    empty = tmp_path / "empty.key"
    empty.write_text("  \n", encoding="utf-8")
    # When / Then: both are refused before any client is built.
    with pytest.raises(RuntimeError, match="not found"):
        WikiPublisher("https://wiki.invalid/graphql", missing)
    with pytest.raises(RuntimeError, match="empty"):
        WikiPublisher("https://wiki.invalid/graphql", empty)


def test_wiki_publisher_attaches_bearer_auth(tmp_path) -> None:
    # Given: a key file with a real key
    key_file = tmp_path / "wiki.key"
    key_file.write_text("  secret-key  ", encoding="utf-8")
    # When: the publisher is constructed
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    # Then: the client sends the trimmed key as a Bearer header.
    assert publisher._client.headers["Authorization"] == "Bearer secret-key"
    assert publisher._client.headers["Content-Type"] == "application/json"
    publisher._client.close()


def test_graphql_raises_on_http_error_and_bad_payloads(
    tmp_path, monkeypatch
) -> None:
    # Given: a publisher with a mocked network boundary
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    import httpx

    # When: the HTTP layer fails / returns junk / reports GraphQL errors
    # Then: each failure is a loud RuntimeError (except HTTPError, raised
    # as-is so callers can wrap it).
    monkeypatch.setattr(
        publisher,
        "_client",
        type(
            "FakeClient",
            (),
            {
                "post": lambda self, *a, **kw: httpx.Response(
                    500, request=httpx.Request("POST", "https://x")
                )
            },
        )(),
    )
    with pytest.raises(httpx.HTTPError):
        publisher._graphql("query", {})
    monkeypatch.setattr(
        publisher,
        "_client",
        type("FakeClient", (), {"post": lambda self, *a, **kw: type(
            "R", (), {"raise_for_status": lambda self: None,
                      "json": lambda self: ["not", "a", "dict"]})()}),
    )
    with pytest.raises(RuntimeError, match="non-object"):
        publisher._graphql("query", {})
    monkeypatch.setattr(
        publisher,
        "_client",
        type("FakeClient", (), {"post": lambda self, *a, **kw: type(
            "R", (), {"raise_for_status": lambda self: None,
                      "json": lambda self: {"errors": ["boom"]}})()}),
    )
    with pytest.raises(RuntimeError, match="GraphQL error"):
        publisher._graphql("query", {})


def test_publish_verdict_creates_when_page_missing(tmp_path, monkeypatch) -> None:
    # Given: a publisher whose singleByPath lookup finds no page
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(publisher, "_graphql", lambda query, variables: (calls.append((query, variables)) or {"data": {"pages": {"singleByPath": None, "create": {"responseResult": {"succeeded": True}}}}}))

    # When: the verdict is published.
    publisher.publish_verdict("sw-1", "verdict content here")

    # Then: CREATE is used with the sweep-tagged metadata.
    assert len(calls) == 2
    create_query, variables = calls[1]
    assert "CreatePage" in create_query
    assert variables["path"] == "hr-agents/latest-verdict"
    assert variables["title"] == "HR Verdict: sw-1"
    assert variables["content"] == "verdict content here"
    assert "sw-1" in variables["tags"]


def test_publish_verdict_updates_existing_page(tmp_path, monkeypatch) -> None:
    # Given: a publisher whose lookup finds page id 42
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        publisher,
        "_graphql",
        lambda query, variables: (
            calls.append((query, variables))
            or {
                "data": {
                    "pages": {
                        "singleByPath": {"id": 42},
                        "update": {"responseResult": {"succeeded": True}},
                    }
                }
            }
        ),
    )

    # When: the verdict is published again.
    publisher.publish_verdict("sw-2", "updated content")

    # Then: UPDATE carries the existing page id.
    assert len(calls) == 2
    update_query, variables = calls[1]
    assert "UpdatePage" in update_query
    assert variables["id"] == 42


def test_publish_verdict_rejected_by_wiki_raises(tmp_path, monkeypatch) -> None:
    # Given: a page create that Wiki.js rejects
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    publisher = WikiPublisher("https://wiki.invalid/graphql", key_file)
    monkeypatch.setattr(
        publisher,
        "_graphql",
        lambda query, variables: {
            "data": {
                "pages": {
                    "singleByPath": None,
                    "create": {"responseResult": {"succeeded": False, "message": "bad path"}},
                }
            }
        },
    )
    # When / Then: the rejection becomes a loud RuntimeError.
    with pytest.raises(RuntimeError, match="bad path"):
        publisher.publish_verdict("sw-3", "content")


def test_publish_from_target_end_to_end(tmp_path, monkeypatch) -> None:
    # Given: a configured target, a fake DB connection and a fake publisher
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    target = {
        "graphql_url": "https://wiki.invalid/graphql",
        "api_key_file": str(key_file),
    }
    from hr.publish import publish_from_target

    conn = type("Conn", (), {"close": lambda self: None})()
    monkeypatch.setattr("hr.publish.connect", lambda: conn)
    monkeypatch.setattr("hr.cli.latest_sweep_id", lambda conn: "sw-9")
    monkeypatch.setattr("hr.cli.build_verdict_report", lambda conn, sweep_id: f"verdict {sweep_id}")
    published: list[tuple[str, str]] = []

    class _FakeWiki:
        def __init__(self, url: str, key_file_path) -> None:
            self.url = url
            self.key_file = key_file_path

        def __enter__(self) -> "_FakeWiki":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def publish_verdict(self, sweep_id: str, content: str) -> None:
            published.append((sweep_id, content))

    monkeypatch.setattr("hr.publish.WikiPublisher", _FakeWiki)

    # When: the verdict is published from the target.
    publish_from_target(target)

    # Then: the sweep verdict reached the Wiki publisher unchanged.
    assert published == [("sw-9", "verdict sw-9")]


def test_publish_from_target_wraps_http_errors(tmp_path, monkeypatch) -> None:
    # Given: a wiki endpoint that is unreachable
    key_file = tmp_path / "wiki.key"
    key_file.write_text("k", encoding="utf-8")
    target = {
        "graphql_url": "https://wiki.invalid/graphql",
        "api_key_file": str(key_file),
    }
    import httpx

    from hr.publish import publish_from_target

    monkeypatch.setattr("hr.publish.connect", lambda: type("Conn", (), {"close": lambda self: None})())
    monkeypatch.setattr("hr.cli.latest_sweep_id", lambda conn: "sw-9")
    monkeypatch.setattr("hr.cli.build_verdict_report", lambda conn, sweep_id: "v")

    class _BrokenWiki:
        def __init__(self, url: str, key_file_path) -> None:
            pass

        def __enter__(self) -> "_BrokenWiki":
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def publish_verdict(self, sweep_id: str, content: str) -> None:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("hr.publish.WikiPublisher", _BrokenWiki)

    # When / Then: transport failures surface as a clean RuntimeError.
    with pytest.raises(RuntimeError, match="Wiki.js request failed"):
        publish_from_target(target)
