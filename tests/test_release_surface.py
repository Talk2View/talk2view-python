"""Release surface: version, MIT licence, public exports, README/CHANGELOG sections."""

from __future__ import annotations

import importlib.metadata
import inspect
import re
from pathlib import Path

import talk2view

PACKAGE = Path(__file__).resolve().parents[1]

# What a host imports to use on-device memory, from talk2view.memory.
MEMORY_NAMES = ("T2VMemory", "MemoryStore", "MemoryEntry", "Fact", "SearchHit", "AppliedBatch")
MEMORY_HEADING = "### On-device memory (`memory_path`)"


def _memory_section(readme: str) -> str:
    """The README's memory section: its heading up to the next ``###``."""
    start = readme.index(MEMORY_HEADING)
    return readme[start : readme.index("\n### ", start + 1)]


def test_version_is_the_same_everywhere() -> None:
    assert talk2view.__version__ == "0.4.0"
    assert importlib.metadata.version("talk2view") == "0.4.0"
    pyproject = (PACKAGE / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "0.4.0"' in pyproject
    from talk2view.client import SDK_VERSION

    assert SDK_VERSION == "0.4.0"


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


def test_memory_public_exports() -> None:
    from talk2view import memory

    missing = set(MEMORY_NAMES) - set(memory.__all__)
    assert not missing, missing
    for name in MEMORY_NAMES:
        assert hasattr(memory, name), name
    # The search_memory bounds handle_search documents, which the evals import.
    from talk2view.memory import client

    assert {"QUERY_MAX", "SEARCH_LIMIT"} <= set(client.__all__)


def test_talk2view_takes_memory_path() -> None:
    from talk2view.memory import T2VMemory

    parameter = inspect.signature(talk2view.Talk2View.__init__).parameters["memory_path"]
    assert parameter.default is None
    # Both are read-only: a user switch closes the memory, nothing assigns it.
    assert isinstance(talk2view.Talk2View.memory, property)
    assert talk2view.Talk2View.memory.fset is None
    assert isinstance(T2VMemory.enabled, property) and T2VMemory.enabled.fset is None
    assert "inline" in inspect.signature(T2VMemory.__init__).parameters
    # A host's "Memory updated" notice reads the batch and takes it back; only the memory sets it.
    assert isinstance(T2VMemory.last_applied, property) and T2VMemory.last_applied.fset is None
    assert list(inspect.signature(T2VMemory.undo).parameters) == ["self", "batch_id"]


def test_readme_documents_memory() -> None:
    readme = (PACKAGE / "README.md").read_text(encoding="utf-8")
    section = _memory_section(readme)
    for needle in (
        "`<memory_path>/<user id>.sqlite3`",
        "SHA-256",
        "`0600`",
        "`0700`",
        "pip install 'talk2view[memory]'",
        "Python 3.11",
        "start_background(interval_s=60.0)",
        "`t2v.clear_session()`",
        "`run_due()`",
        "`incognito`",
        "`paused`",
        "export_markdown(",
        "export_json()",
        "`import_text`",
        "reset()",
        "memory.enabled",
        "`403 memory_disabled`",
        "`402 memory_budget_low`",
        "inline=True",
        "observe_turn",
        "observe_approval",
        "`talk2view.memory`",
        "keeps no memory content",
        "no sync",
        "`t2v.memory.last_applied`",
        "t2v.memory.undo(batch_id)",
        "under one that already exists",
    ):
        assert needle in section, needle
    # The pointers in the sections around it.
    assert "memory_path=None," in readme
    assert '| `"memory"` |' in readme
    assert "or `memory_path` is unset" in readme
    assert "`apsw >= 3.54`" in readme


def test_readme_names_only_memory_calls_that_exist(tmp_path: Path) -> None:
    """Every ``t2v.memory.<name>`` and ``store.<name>`` the memory section mentions is real."""
    from talk2view import MemoryStorage
    from talk2view.client import T2VClient
    from talk2view.memory import MemoryStore, T2VMemory

    section = _memory_section((PACKAGE / "README.md").read_text(encoding="utf-8"))
    client = T2VClient(partner_key="pk_test", storage=MemoryStorage())
    memory = T2VMemory(client, MemoryStore(tmp_path / "memory.sqlite3"))
    try:
        for name in sorted(set(re.findall(r"t2v\.memory\.(\w+)", section))):
            assert hasattr(memory, name), "t2v.memory." + name
        for name in sorted(set(re.findall(r"\bstore\.(\w+)", section))):
            assert hasattr(memory.store, name), "store." + name
    finally:
        memory.close()
        client.close()


def test_changelog_unreleased_announces_memory() -> None:
    changelog = (PACKAGE / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = changelog.split("## Unreleased", 1)[1].split("\n## ", 1)[0]
    assert "### Added" in unreleased
    for needle in (
        "`Talk2View(memory_path=...)`",
        "`t2v.memory`",
        "`talk2view.memory`",
        "`Capabilities.memory`",
        "`T2VSession.send_message(memory=...)`",
        "`t2v.memory.last_applied`",
        "`t2v.memory.undo(batch_id)`",
        "under one that already exists",
        "pip install 'talk2view[memory]'",
    ):
        assert needle in unreleased, needle
