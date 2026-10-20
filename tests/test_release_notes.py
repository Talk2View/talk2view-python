"""The parity refresh's release surface: exports, README and CHANGELOG."""

from __future__ import annotations

from pathlib import Path

import talk2view

PACKAGE = Path(__file__).resolve().parents[1]


def test_the_new_core_exports() -> None:
    assert {"AudioModel", "DEFAULT_MODEL"} <= set(talk2view.__all__)


def test_readme_documents_the_parity_refresh() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    assert "### `t2v.voice`" in readme
    for needle in (
        "request_timeout=30.0",
        "upload_timeout=120.0",
        "get_valid_access_token",
        "DEFAULT_MODEL",
        "supported_languages",
        "One process per token file",
        "match on `err.type`",
        "ChatOptions",
        "ToolPresentation",
        "`limits`",
        "`reload`",
    ):
        assert needle in readme, needle


def test_changelog_announces_the_parity_refresh() -> None:
    changelog = (PACKAGE / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## 0.2.0", 1)[0]
    assert "### Changed" in unreleased
    for needle in (
        "`T2VAuth.get_valid_access_token",
        "`DEFAULT_MODEL`",
        "`AudioModel`",
        "`ChatOptions`",
        "`limits`",
        "120 s",
    ):
        assert needle in unreleased, needle

