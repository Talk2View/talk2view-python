"""0.2.0 release surface: version, MIT licence, public exports, README/CHANGELOG sections."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

import talk2view

PACKAGE = Path(__file__).resolve().parents[1]


def test_version_is_0_2_0_everywhere() -> None:
    assert talk2view.__version__ == "0.2.0"
    assert importlib.metadata.version("talk2view") == "0.2.0"
    pyproject = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.2.0"' in pyproject
    from talk2view.client import SDK_VERSION

    assert SDK_VERSION == "0.2.0"


def test_mit_licence_matches_the_typescript_sdk() -> None:
    pyproject = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert 'license = "MIT"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject
    assert "Proprietary" not in pyproject
    licence = (PACKAGE / "LICENSE").read_text(encoding="utf-8")
    assert licence.startswith("Copyright 2025 A2B Technology Corporation Pty Ltd.")
    assert "Permission is hereby granted, free of charge" in licence
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in licence


def test_public_exports() -> None:
    expected = {
        "Talk2View",
        "__version__",
        "ChatController",
        "T2VSkills",
        "Skill",
        "PlatformSkill",
        "SkillValidationError",
        "SkillsState",
        "FileStorage",
        "MemoryStorage",
        "TokenStorage",
        "T2VPreferences",
        "UserPreferences",
        "SignupOutcome",
        "AnonymousUnavailableError",
        "ANONYMOUS_UNAVAILABLE_TYPES",
        "ProviderSignInError",
        "PartnerConfig",
        "Model",
        "ModelsResponse",
        "AttachmentContent",
        "ALLOWED_ATTACHMENT_MIME",
        "ATTACHMENT_ACCEPT",
        "ATTACHMENT_TYPES_LABEL",
        "is_allowed_attachment_type",
    }
    missing = expected - set(talk2view.__all__)
    assert not missing, missing
    for name in talk2view.__all__:
        assert hasattr(talk2view, name), name


def test_readme_documents_the_new_surface() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    for heading in (
        "### `t2v.skills`",
        "### Anonymous demo sessions",
        "### `t2v.preferences`",
        "### `FileStorage`",
        "### Partner config, models, completions, warm-up",
    ):
        assert heading in readme, heading
    assert "SKILL.md" in readme
    assert "requires partner configuration" in readme
    assert "MIT" in readme
    # Stated for hosts: replace is last-writer-wins; list has its own limit.
    assert "last-writer-wins" in readme
    assert "60 calls a minute" in readme


def test_changelog_has_the_0_2_0_entry_with_breaking_changes() -> None:
    changelog = (PACKAGE / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"^## 0\.2\.0", changelog, re.MULTILINE)
    assert "`signup()` returns a `SignupOutcome`" in changelog
    assert "`ChatEvent`" in changelog
    assert "MIT" in changelog
