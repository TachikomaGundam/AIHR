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
