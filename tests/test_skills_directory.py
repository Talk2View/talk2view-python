"""SKILL.md parsing (the engine's rules) and T2VSkills.load_directory."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pytest

from talk2view.skills import (
    FRONTMATTER_RE,
    Skill,
    T2VSkills,
    check_body_size,
    is_filesystem_safe_name,
    parse_allowed_tools,
    parse_frontmatter,
    parse_skill_markdown,
    register_payload,
)
from talk2view.storage import MemoryStorage

from .test_skills_core import ManualTimer

ENGINE_REGEX = r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$"  # the engine's own front-matter regex


def _skills() -> Tuple[T2VSkills, MagicMock, ManualTimer]:
    client = MagicMock()
    client.request.return_value = {"registered": [], "count": 0}
    timer = ManualTimer()
    return T2VSkills(client, MemoryStorage(), start_timer=timer), client, timer


def _write(root: Path, rel: str, text: str) -> Path:
    file = root / rel
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(text.encode("utf-8"))
    return file


def _lock_free(skills: T2VSkills) -> bool:
    """Whether ANOTHER thread can take ``skills``' entries lock right now (bounded wait):
    mirrors ``test_skills_core._post_lock_free`` but for ``_lock``, the one ``load_directory``
    holds while mutating entries. ``_notify()`` must run with neither lock held so a
    controller-style listener taking ``_lock`` from its own thread never deadlocks."""
    acquired: List[bool] = []

    def probe() -> None:
        got = skills._lock.acquire(timeout=1)
        if got:
            skills._lock.release()
        acquired.append(got)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    thread.join(5)
    return acquired == [True]


# ── parse_skill_markdown (engine rules) ─────────────────────────────────────


class TestParse:
    def test_regex_is_the_engines(self) -> None:
        assert FRONTMATTER_RE.pattern == ENGINE_REGEX
        assert FRONTMATTER_RE.flags & re.DOTALL

    def test_frontmatter_table_dashes_not_split(self) -> None:
        text = (
            "---\nname: tables\ndescription: Has a table\n---\n"
            "# Heading\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n---\n\nAfter a rule.\n"
        )
        skill = parse_skill_markdown(text, "fallback")
        assert skill.name == "tables"
        assert skill.description == "Has a table"
        assert skill.content.startswith("# Heading")
        assert "|---|---|" in skill.content
        assert skill.content.endswith("After a rule.")

    def test_crlf(self) -> None:
        text = "---\r\nname: crlf\r\ndescription: Windows\r\n---\r\nBody line 1\r\nline 2\r\n"
        skill = parse_skill_markdown(text, "fallback")
        assert skill == Skill(name="crlf", description="Windows", content="Body line 1\r\nline 2")

    def test_allowed_tools_both_separators(self) -> None:
        spaces = parse_skill_markdown("---\nallowed-tools: a b  c\n---\nx\n", "n")
        commas = parse_skill_markdown("---\nallowed-tools: a,b, c\n---\nx\n", "n")
        underscore = parse_skill_markdown("---\nallowed_tools: a\n---\nx\n", "n")
        assert spaces.allowed_tools == ["a", "b", "c"]
        assert commas.allowed_tools == ["a", "b", "c"]
        assert underscore.allowed_tools == ["a"]
        assert parse_allowed_tools({"allowed-tools": ""}) is None
        assert parse_allowed_tools({}) is None
        assert parse_allowed_tools({"allowed-tools": " , "}) is None

    def test_missing_frontmatter_uses_the_fallback_name(self) -> None:
        skill = parse_skill_markdown("# Just markdown\n\nNo frontmatter.\n", "my-skill")
        assert skill.name == "my-skill"
        assert skill.description == ""
        assert skill.content == "# Just markdown\n\nNo frontmatter."
        assert skill.allowed_tools is None

    def test_empty_name_field_uses_the_fallback(self) -> None:
        skill = parse_skill_markdown("---\nname:\ndescription: d\n---\nbody\n", "stem")
        assert skill.name == "stem"

    def test_closing_dashes_need_a_trailing_newline_like_the_engine(self) -> None:
        # No newline after the closing ``---``: the engine's regex does not match, so the
        # whole file is the body. Mirrored, not "fixed" — the engine is the oracle.
        meta, body, _warnings = parse_frontmatter("---\nname: x\n---")
        assert meta == {} and body == "---\nname: x\n---"

    def test_values_may_contain_colons(self) -> None:
        meta, _body, _warnings = parse_frontmatter("---\ndescription: a: b: c\n---\n\n")
        assert meta == {"description": "a: b: c"}

    def test_multi_line_yaml_values_warn(self) -> None:
        text = "---\nname: n\ndescription: >\n  folded line\nallowed-tools:\n  - a\n---\nbody\n"
        meta, _body, warnings = parse_frontmatter(text)
        assert meta == {"name": "n", "description": ">", "allowed-tools": ""}
        assert len(warnings) == 3
        assert "folded line" in warnings[0]
        assert "- a" in warnings[1]
        assert "multi-line YAML scalar" in warnings[2]

    @pytest.mark.parametrize(
        "name, safe",
        [
            ("measure-volume", True),
            ("Measure Volume 2", True),
            ("a/b", False),
            ("a\\b", False),
            (".hidden", False),
            ("..", False),
            ("", False),
            ("bad:name", False),
            ("tab\there", False),
            (" padded", False),
        ],
    )
    def test_is_filesystem_safe_name(self, name: str, safe: bool) -> None:
        assert is_filesystem_safe_name(name) is safe


# ── load_directory ──────────────────────────────────────────────────────────


class TestLoadDirectory:
    def test_folder_vs_flat(self, tmp_path: Path) -> None:
        _write(tmp_path, "folder-skill/SKILL.md", "---\ndescription: In a folder\n---\nF\n")
        _write(tmp_path, "flat-skill.md", "---\ndescription: Flat file\n---\nG\n")
        _write(tmp_path, "notes.txt", "ignored")
        _write(tmp_path, "empty-folder/readme.txt", "no SKILL.md here")
        skills, _client, _timer = _skills()
        loaded = skills.load_directory(tmp_path, label="bundled")
        assert loaded == [
            Skill(name="flat-skill", description="Flat file", content="G"),
            Skill(name="folder-skill", description="In a folder", content="F"),
        ]
        state = skills.state()
        assert [s["origin"] for s in state["local"]] == ["dir", "dir"]
        assert [s["label"] for s in state["local"]] == ["bundled", "bundled"]
        assert state["local"][0]["path"] == str(tmp_path / "flat-skill.md")
        assert state["local"][1]["path"] == str(tmp_path / "folder-skill")

    def test_precedence_code_over_later_dir_over_earlier_dir(self, tmp_path: Path) -> None:
        early = tmp_path / "early"
        late = tmp_path / "late"
        _write(early, "shared.md", "early body")
        _write(early, "only-early.md", "e")
        _write(late, "shared.md", "late body")
        _write(late, "coded.md", "dir version")
        skills, _client, _timer = _skills()
        skills.add(Skill(name="coded", content="code version"))
        skills.load_directory(early, label="bundled")
        skills.load_directory(late, label="user")
        by_name = {s.name: s for s in skills.get_all()}
        assert by_name["shared"].content == "late body"
        assert by_name["coded"].content == "code version"
        assert by_name["only-early"].content == "e"
        labels = {s["name"]: s["label"] for s in skills.state()["local"]}
        assert labels == {"coded": "code", "only-early": "bundled", "shared": "user"}
        # Code added AFTER a directory still wins.
        skills.add(Skill(name="shared", content="from code"))
        assert skills.get("shared") == Skill(name="shared", content="from code")

    def test_rescan_drops_deleted_files_and_flags_a_reset(self, tmp_path: Path) -> None:
        a = _write(tmp_path, "a.md", "A")
        _write(tmp_path, "b.md", "B")
        skills, client, _timer = _skills()
        client.request.return_value = {"registered": ["a", "b"], "count": 2}
        skills.load_directory(tmp_path, label="user")
        skills.register()
        a.unlink()
        _write(tmp_path, "c.md", "C")
        loaded = skills.load_directory(tmp_path, label="user")
        assert [s.name for s in loaded] == ["b", "c"]
        assert [s.name for s in skills.get_all()] == ["b", "c"]
        assert skills.needs_session_reset() is True  # this engine merges (no `replaced`)

    def test_rescan_on_a_replacing_engine_is_one_replace_not_a_reset(
        self, tmp_path: Path
    ) -> None:
        a = _write(tmp_path, "a.md", "A")
        _write(tmp_path, "b.md", "B")
        skills, client, timer = _skills()
        client.request.return_value = {"registered": ["a", "b"], "count": 2, "replaced": True}
        skills.load_directory(tmp_path, label="user")
        skills.register()
        a.unlink()
        skills.load_directory(tmp_path, label="user")
        assert skills.needs_session_reset() is False
        client.request.return_value = {"registered": ["b"], "count": 1, "replaced": True}
        timer.fire()
        assert skills.state()["registered"] == ["b"]

    def test_rescan_keeps_the_enabled_flag(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "A")
        skills, _client, _timer = _skills()
        skills.load_directory(tmp_path)
        skills.set_enabled("a", False)
        skills.load_directory(tmp_path)
        assert skills.state()["local"][0]["enabled"] is False

    def test_loads_are_batched_into_one_debounced_register(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "A")
        _write(tmp_path, "b.md", "B")
        skills, client, timer = _skills()
        skills.load_directory(tmp_path)
        client.request.assert_not_called()
        timer.fire()
        body: Dict[str, Any] = client.request.call_args.kwargs["body"]
        assert [s["name"] for s in body["skills"]] == ["a", "b"]

    def test_missing_directory_loads_nothing(self, tmp_path: Path) -> None:
        skills, _client, _timer = _skills()
        assert skills.load_directory(tmp_path / "nope") == []
        assert skills.get_all() == []

    def test_unsafe_names_and_invalid_skills_are_skipped_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path, "one.md", "---\nname: bad/name\n---\nx\n")
        _write(tmp_path, "two.md", "---\nname: ok\n---\n\n")  # empty body
        _write(tmp_path, "three.md", "---\nname: " + "x" * 256 + "\n---\nbody\n")
        _write(tmp_path, "four.md", "fine")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert [s.name for s in loaded] == ["four"]
        assert "not a safe file name" in caplog.text
        assert "has no content" in caplog.text
        assert "256 code points" in caplog.text

    def test_sibling_files_and_multi_line_values_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path, "s/SKILL.md", "---\nname: s\ndescription: >\n  folded\n---\nbody\n")
        _write(tmp_path, "s/helper.py", "print()")
        _write(tmp_path, "s/scripts/run.sh", "")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert [s.name for s in loaded] == ["s"]
        assert "ignoring helper.py, scripts" in caplog.text
        assert "multi-line YAML" in caplog.text

    def test_unreadable_file_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        (tmp_path / "bin.md").write_bytes(b"\xff\xfe\x00broken")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            assert skills.load_directory(tmp_path) == []
        assert "Could not read skill file" in caplog.text

    def test_a_directory_skill_over_the_body_cap_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import talk2view.skills as skills_module

        monkeypatch.setattr(skills_module, "MAX_REGISTER_BODY_BYTES", 200)
        _write(tmp_path, "small.md", "ok")
        _write(tmp_path, "big.md", "x" * 300)
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded: List[Skill] = skills.load_directory(tmp_path)
        assert [s.name for s in loaded] == ["small"]
        assert "bytes" in caplog.text

    def test_notify_runs_with_no_lock_held(self, tmp_path: Path) -> None:
        # The controller takes its own lock inside the on_change callback; if
        # load_directory notified while still holding `_lock`, a callback that (like the
        # controller's) reaches back into `skills` would deadlock against itself.
        _write(tmp_path, "a.md", "A")
        skills, _client, _timer = _skills()
        free: List[bool] = []
        skills.on_change(lambda _state: free.append(_lock_free(skills)))
        skills.load_directory(tmp_path)
        assert free == [True]

    def test_a_skill_that_moves_between_labels_follows_the_later_directory(
        self, tmp_path: Path
    ) -> None:
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        _write(a_dir, "x.md", "from a")
        skills, _client, _timer = _skills()
        skills.load_directory(a_dir, label="a")
        assert skills.get("x") == Skill(name="x", content="from a")
        _write(b_dir, "x.md", "from b")
        skills.load_directory(b_dir, label="b")
        assert skills.get("x") == Skill(name="x", content="from b")
        labels = {s["name"]: s["label"] for s in skills.state()["local"]}
        assert labels == {"x": "b"}
        # Rescanning "a" without "x" must not drop the copy "b" now owns.
        (a_dir / "x.md").unlink()
        skills.load_directory(a_dir, label="a")
        assert skills.get("x") == Skill(name="x", content="from b")
        local = skills.state()["local"]
        assert len(local) == 1
        assert local[0]["label"] == "b"
        assert local[0]["path"] == str(b_dir / "x.md")

    def test_an_oversized_file_is_skipped_before_being_read(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import talk2view.skills as skills_module

        big = tmp_path / "big.md"
        with open(big, "wb") as f:
            f.truncate(skills_module.MAX_REGISTER_BODY_BYTES + 1)  # sparse: cheap to create
        read_calls: List[Path] = []
        original_read_text = Path.read_text

        def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self == big:
                read_calls.append(self)
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read_text)
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert loaded == []
        assert read_calls == []  # skipped by size before the file was ever opened
        assert "bytes" in caplog.text

    def test_a_name_twice_in_one_directory_keeps_the_first(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path, "alpha/SKILL.md", "---\nname: dup\n---\nfrom the folder")
        _write(tmp_path, "zeta.md", "---\nname: dup\n---\nfrom the file")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert loaded == [Skill(name="dup", content="from the folder")]
        assert [(s["name"], s["path"]) for s in skills.state()["local"]] == [
            ("dup", str(tmp_path / "alpha"))
        ]
        assert "appears twice" in caplog.text
        assert str(tmp_path / "alpha") in caplog.text  # names the copy that was kept

    def test_skills_that_fit_one_by_one_but_not_together_skip_the_overflow(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import talk2view.skills as skills_module

        three = [Skill(name=n, content="x" * 60) for n in ("a", "b", "c")]
        fits_two = check_body_size(three[:2])
        monkeypatch.setattr(skills_module, "MAX_REGISTER_BODY_BYTES", fits_two)
        for skill in three:
            _write(tmp_path, skill.name + ".md", skill.content)
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert [s.name for s in loaded] == ["a", "b"]
        assert "Skipping {}".format(tmp_path / "c.md") in caplog.text

    def test_a_same_named_skill_is_sized_as_a_replacement(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # "x" from b/ replaces "x" from a/: only one of them is ever sent, so the two
        # together over the cap must not stop the second.
        import talk2view.skills as skills_module

        x = Skill(name="x", content="y" * 80)
        monkeypatch.setattr(skills_module, "MAX_REGISTER_BODY_BYTES", check_body_size([x]))
        _write(tmp_path / "a", "x.md", x.content)
        _write(tmp_path / "b", "x.md", x.content)
        skills, _client, _timer = _skills()
        assert skills.load_directory(tmp_path / "a", label="a") == [x]
        assert skills.load_directory(tmp_path / "b", label="b") == [x]
        assert [s["label"] for s in skills.state()["local"]] == ["b"]

    def test_each_skill_is_serialised_once_not_once_per_later_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The body cap is checked with a running byte total. Re-serialising every loaded
        # skill for each new one is quadratic: at the 8 MB cap, 1000 x 7 KB skills held
        # the skills lock for about 9 s.
        for n in range(40):
            _write(tmp_path, "s{:02d}.md".format(n), "body {}".format(n))
        skills, _client, _timer = _skills()
        skills.add(Skill(name="from-code", content="c"))
        calls: List[str] = []
        real = Skill.to_wire

        def counting(self: Skill) -> Dict[str, Any]:
            calls.append(self.name)
            return real(self)

        monkeypatch.setattr(Skill, "to_wire", counting)
        assert len(skills.load_directory(tmp_path)) == 40
        assert len(calls) <= 41  # each directory skill once, the code skill once


class TestBodySizeRunningTotal:
    @pytest.mark.parametrize(
        "names",
        [[], ["a"], ["a", "b"], ["a", "b", "c", "d", "e"]],
        ids=["empty", "one", "two", "five"],
    )
    def test_equals_check_body_size(self, names: List[str]) -> None:
        from talk2view.skills import _body_bytes, _wire_bytes

        skills = [
            Skill(
                name=name,
                description='quotes " and \\ and\nnewlines',
                content="é ✓ 漢字 \U0001f600 " * (i + 1),
                allowed_tools=["t1", "t2"] if i % 2 else None,
            )
            for i, name in enumerate(names)
        ]
        exact = len(json.dumps(register_payload(skills)).encode("utf-8"))
        assert _body_bytes(sum(_wire_bytes(s) for s in skills), len(skills)) == exact
        assert check_body_size(skills) == exact


@pytest.mark.skipif(os.name == "nt", reason="symlinks need admin privileges on Windows")
class TestLoadDirectorySymlinks:
    def test_an_outside_pointing_md_symlink_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        secret = _write(tmp_path, "secret.md", "---\ndescription: leaked\n---\nleaked\n")
        os.symlink(secret, root / "leaky.md")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(root)
        assert loaded == []
        assert skills.get_all() == []
        assert "resolves outside" in caplog.text

    def test_an_outside_pointing_skill_folder_symlink_is_skipped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside_skill = tmp_path / "outside-skill"
        _write(outside_skill, "SKILL.md", "---\ndescription: leaked\n---\nleaked\n")
        os.symlink(outside_skill, root / "leaky-skill")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(root)
        assert loaded == []
        assert skills.get_all() == []
        assert "resolves outside" in caplog.text

    def test_an_inside_pointing_symlink_still_loads(self, tmp_path: Path) -> None:
        _write(tmp_path, "sub/actual-skill/SKILL.md", "---\ndescription: real\n---\nR\n")
        os.symlink(tmp_path / "sub" / "actual-skill", tmp_path / "linked")
        skills, _client, _timer = _skills()
        loaded = skills.load_directory(tmp_path)
        assert [s.name for s in loaded] == ["linked"]
        assert skills.get("linked") == Skill(name="linked", description="real", content="R")

    def test_a_broken_symlink_is_skipped_with_a_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        os.symlink(tmp_path / "does-not-exist.md", tmp_path / "broken.md")
        skills, _client, _timer = _skills()
        with caplog.at_level(logging.WARNING, logger="talk2view.skills"):
            loaded = skills.load_directory(tmp_path)
        assert loaded == []
        assert "could not be resolved" in caplog.text
