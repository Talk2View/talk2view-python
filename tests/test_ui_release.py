"""talk2view.ui release surface: exports, README section, CHANGELOG entry."""

from __future__ import annotations

from pathlib import Path

import talk2view.ui as ui

PACKAGE = Path(__file__).resolve().parents[1]


def test_the_ui_package_exports() -> None:
    assert set(ui.__all__) == {
        "AppState",
        "ChatFeatures",
        "ChatOptions",
        "ChatWelcome",
        "ToolPresentation",
        "GenericHost",
        "HostAdapter",
        "StaticAssetsMissingError",
        "Theme",
        "UIServer",
    }
    for name in ui.__all__:
        assert hasattr(ui, name), name


def test_readme_documents_the_ui_server() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    assert "### Chat UI server (`talk2view.ui`)" in readme
    for needle in (
        "StaticAssetsMissingError",
        "python -m talk2view.ui.testing",
        "Access-Control-*",
        "never a query string",
        "HostAdapter",
        "supports_clipboard",
        "fresh snapshot",
    ):
        assert needle in readme, needle
    assert "(a later release)" not in readme


def test_changelog_announces_the_ui_server() -> None:
    changelog = (PACKAGE / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("\n## 0.2.0", 1)[0]
    assert "## Unreleased" in unreleased
    for needle in ("`talk2view.ui`", "`ChatController.t2v`", "check_wheel_assets.py"):
        assert needle in unreleased, needle
