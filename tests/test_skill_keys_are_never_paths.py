"""A skill's name or key never reaches the filesystem as a path.

Every key-taking operation (write's ``replaces``, delete, restore, read, customize, the
page's skillRead) goes through ``FolderSkillStore``'s one resolver: a single safe path
component naming an existing entry inside the folder, else an unknown skill. Checked
at every layer a caller can reach it from: the store, ``T2VSkills``, the
``save_skill`` handler through ``execute_tool_call``, and the router through the real
dispatch path (``wire.parse_command`` then ``execute``)."""

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from talk2view.skill_format import from_form
from talk2view.skill_store import FolderSkillStore, SkillExistsError
from talk2view.skill_tool import SAVE_SKILL, SAVE_SKILL_SCHEMA, save_skill_handler
from talk2view.skills import T2VSkills
from talk2view.storage import MemoryStorage
from talk2view.tools import T2VTools
from talk2view.ui.wire import parse_command

from .ui_router_harness import make_router


class NoClient:
    def request(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return {"registered": [], "count": 0, "replaced": True}


class _NoTimer:
    def cancel(self) -> None:
        pass


def skills() -> T2VSkills:
    return T2VSkills(NoClient(), MemoryStorage(), start_timer=lambda d, f: _NoTimer())


def doc(name: str = "ok-one", body: str = "Body.") -> Any:
    return from_form(name, "Does x. Use when y.", body)


@pytest.fixture
def world(tmp_path: Path) -> Dict[str, Path]:
    """A store at ``tmp_path/skills`` and, next to it, things it must never touch: a
    file, a folder with a file in it, and a folder holding a SKILL.md."""
    (tmp_path / "skills").mkdir()
    (tmp_path / "victim.md").write_text("victim file")
    (tmp_path / "abs-victim.txt").write_text("absolute victim")
    (tmp_path / "victimdir").mkdir()
    (tmp_path / "victimdir" / "thesis.docx").write_text("thesis")
    (tmp_path / "outside-skill").mkdir()
    (tmp_path / "outside-skill" / "SKILL.md").write_text(
        "---\nname: outside-skill\ndescription: Out. Use never.\n---\nB\n"
    )
    return {"root": tmp_path, "skills": tmp_path / "skills"}


def snapshot(root: Path) -> List[str]:
    """Every path under ``root`` outside the store, with its contents: what must not
    change."""
    out = []
    for p in sorted(root.rglob("*")):
        if "skills" in p.relative_to(root).parts[:1]:
            continue
        out.append("{}:{}".format(p.relative_to(root), p.read_text() if p.is_file() else "/"))
    return out


def hostile_keys(root: Path) -> List[str]:
    return [
        "../victim",
        "../victim.md",
        "../victimdir",
        "../outside-skill",
        "..",
        ".",
        "",
        ".trash",
        ".t2v-skills.json",
        "a/b",
        "a\\b",
        "/",
        str(root / "abs-victim.txt"),
        str(root / "victimdir"),
        str(root / "outside-skill"),
        "C:",
        "C:\\Windows",
        "c:victim",
        "ok-one/../../victim",
        "ok-one\x00",
        " ok-one",
    ]


# ── the store ─────────────────────────────────────────────────────────────────────


def test_store_write_replaces_a_hostile_key_is_unknown_and_writes_nothing(world):
    root, store = world["root"], FolderSkillStore(world["skills"])
    before = snapshot(root)
    for key in hostile_keys(root):
        with pytest.raises(KeyError):
            store.write(doc(), replaces=key)
    assert snapshot(root) == before
    assert os.listdir(world["skills"]) == []


def test_store_delete_restore_read_refuse_every_hostile_key(world):
    root, store = world["root"], FolderSkillStore(world["skills"])
    store.write(doc())
    before = snapshot(root)
    for key in hostile_keys(root):
        with pytest.raises(KeyError):
            store.delete(key)
        with pytest.raises(KeyError):
            store.restore(key)
        assert store.read(key) is None
    store.close()
    assert snapshot(root) == before
    assert [s.key for s in store.list()] == ["ok-one"]


def test_a_symlinked_entry_pointing_outside_is_unknown(world):
    root, store = world["root"], FolderSkillStore(world["skills"])
    (world["skills"] / "linked").symlink_to(root / "outside-skill", target_is_directory=True)
    (world["skills"] / "flat-link.md").symlink_to(root / "victim.md")
    before = snapshot(root)
    for key in ("linked", "flat-link"):
        with pytest.raises(KeyError):
            store.delete(key)
        with pytest.raises(KeyError):
            store.write(doc(), replaces=key)
    assert snapshot(root) == before


def test_replaces_must_name_an_existing_skill(world):
    store = FolderSkillStore(world["skills"])
    with pytest.raises(KeyError):
        store.write(doc(), replaces="never-was")
    assert os.listdir(world["skills"]) == []


def test_hostile_frontmatter_name_is_listed_by_its_folder_and_deleted_there(world):
    root = world["root"]
    junk = world["skills"] / "junk"
    junk.mkdir()
    (junk / "SKILL.md").write_text(
        "---\nname: {}\ndescription: Junk. Use never.\n---\nbody\n".format(root / "victimdir")
    )
    s = skills()
    s.use_store(FolderSkillStore(world["skills"]))
    [row] = [i for i in s.state()["local"] if i["origin"] == "user"]
    assert row["name"] == str(root / "victimdir") and row["key"] == "junk"
    assert row["problems"]  # shown with its problems
    before = snapshot(root)
    s.delete_user(row["key"])  # what the page sends
    assert snapshot(root) == before
    assert not junk.exists()
    s.restore_user("junk")
    s.delete_user(row["name"])  # the name maps to its key; never a path
    assert snapshot(root) == before
    assert not junk.exists()


# ── T2VSkills ─────────────────────────────────────────────────────────────────────


def test_t2vskills_operations_refuse_hostile_keys(world, tmp_path):
    root = world["root"]
    bundled = tmp_path / "bundled" / "ct-window-presets"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(
        "---\nname: ct-window-presets\ndescription: CT. Use for CT.\n---\nB\n"
    )
    s = skills()
    s.use_store(FolderSkillStore(world["skills"]), bundled=[str(tmp_path / "bundled")])
    s.save_doc(doc())
    before = snapshot(root)
    for key in hostile_keys(root):
        with pytest.raises(KeyError):
            s.save_doc(doc("ok-two"), replaces=key)
        with pytest.raises(KeyError):
            s.delete_user(key)
        with pytest.raises(KeyError):
            s.restore_user(key)
        with pytest.raises(KeyError):
            s.customize(key)
        with pytest.raises(KeyError):
            s.read_markdown(key)
    assert snapshot(root) == before
    assert sorted(i["name"] for i in s.state()["local"]) == ["ct-window-presets", "ok-one"]


def test_replaces_naming_a_bundled_skill_is_refused(tmp_path):
    folder = tmp_path / "bundled" / "ct-window-presets"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: ct-window-presets\ndescription: CT. Use for CT.\n---\nB\n"
    )
    s = skills()
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[str(tmp_path / "bundled")])
    with pytest.raises(KeyError):
        s.save_doc(doc("ct-window-presets"), replaces="ct-window-presets")
    assert not (tmp_path / "user" / "ct-window-presets").exists()


def test_a_nonconforming_row_can_be_edited_and_deleted(tmp_path):
    """Frontmatter name != folder: the row shows its problems, and Edit (replaces = its
    key) and Delete (its key) both work on it."""
    store_dir = tmp_path / "user"
    (store_dir / "Weird Folder").mkdir(parents=True)
    (store_dir / "Weird Folder" / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: Weird. Use when odd.\n---\nB\n"
    )
    (store_dir / "Old Flat.md").write_text(
        "---\nname: flat-name\ndescription: Flat. Use when flat.\n---\nF\n"
    )
    s = skills()
    s.use_store(FolderSkillStore(store_dir))
    rows = {i["key"]: i for i in s.state()["local"]}
    assert rows["Weird Folder"]["name"] == "other-name"
    assert any(p["field"] == "name" for p in rows["Weird Folder"]["problems"])
    assert rows["Old Flat"]["name"] == "flat-name"

    # Edit, keeping the frontmatter name: the folder moves to the name, one copy left.
    assert s.save_doc(doc("other-name", "Edited."), replaces="Weird Folder") == "other-name"
    assert sorted(os.listdir(store_dir)) == ["Old Flat.md", "other-name"]
    [edited] = [i for i in s.state()["local"] if i["name"] == "other-name"]
    assert edited["key"] == "other-name" and edited["problems"] == []

    # Delete the flat one by its key.
    s.delete_user("Old Flat")
    assert sorted(os.listdir(store_dir)) == [".trash", "other-name"]
    s.restore_user("Old Flat")
    assert (store_dir / "Old Flat.md").is_file()


def test_on_off_follows_the_name(tmp_path):
    store_dir = tmp_path / "user"
    s = skills()
    s.use_store(FolderSkillStore(store_dir))
    s.save_doc(doc("first"))
    s.set_enabled("first", False)
    # A rename carries the off state to the new name.
    s.save_doc(doc("second"), replaces="first")
    assert FolderSkillStore(store_dir).disabled() == {"second"}
    assert [(i["name"], i["enabled"]) for i in s.state()["local"]] == [("second", False)]
    # A delete forgets it: a new skill of that name starts on.
    s.delete_user("second")
    assert FolderSkillStore(store_dir).disabled() == set()
    s.save_doc(doc("second"))
    assert [(i["name"], i["enabled"]) for i in s.state()["local"]] == [("second", True)]


# ── save_skill, the way a live tool call runs it ──────────────────────────────────


def test_save_skill_with_a_hostile_replaces_is_not_saved(world):
    root = world["root"]
    s = skills()
    s.use_store(FolderSkillStore(world["skills"]))
    tools = T2VTools(NoClient())
    tools.add_builtin(SAVE_SKILL_SCHEMA, save_skill_handler(s), permission=True)
    before = snapshot(root)
    for key in hostile_keys(root) + ["never-was"]:
        if not key:
            continue  # an empty replaces is "no replaces": a plain new save
        result, is_error = tools.execute_tool_call(
            SAVE_SKILL,
            {
                "name": "ok-two",
                "description": "Does x. Use when y.",
                "instructions": "B",
                "replaces": key,
            },
        )
        assert is_error is False
        assert result.startswith("Not saved."), (key, result)
        assert "no skill named" in result
    assert snapshot(root) == before
    assert os.listdir(world["skills"]) == []


# ── the router, through the real dispatch path ────────────────────────────────────


def dispatch(router: Any, **body: Any) -> Dict[str, Any]:
    return router.execute(parse_command(dict(body, id="c")))


def test_router_commands_refuse_hostile_keys(world):
    root = world["root"]
    router, t2v = make_router(skills_dir=world["skills"])
    t2v.skills.save_doc(doc())
    before = snapshot(root)
    for key in hostile_keys(root):
        if not key:
            continue  # the wire refuses an empty name before any handler
        for body in (
            {"type": "skillDelete", "name": key},
            {"type": "skillRestore", "name": key},
            {"type": "skillCustomize", "name": key},
            {"type": "skillRead", "name": key},
            {
                "type": "skillSave",
                "name": "ok-two",
                "description": "Does x. Use when y.",
                "instructions": "B",
                "replaces": key,
            },
        ):
            result = dispatch(router, **body)
            assert result["ok"] is False, (body, result)
            assert result["error"]["type"] in ("unknown_skill", "invalid_command"), (body, result)
    assert snapshot(root) == before
    assert sorted(os.listdir(world["skills"])) == ["ok-one"]


def test_router_edits_and_deletes_a_nonconforming_row_by_its_key(tmp_path):
    store_dir = tmp_path / "user"
    (store_dir / "Weird").mkdir(parents=True)
    (store_dir / "Weird" / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: Weird. Use when odd.\n---\nB\n"
    )
    router, t2v = make_router(skills_dir=store_dir)
    [row] = t2v.skills.state()["local"]
    assert (row["name"], row["key"]) == ("other-name", "Weird")
    read = dispatch(router, type="skillRead", name="Weird")
    assert read["ok"] is True and "other-name" in read["data"]["markdown"]
    saved = dispatch(
        router, type="skillSave", name="other-name", description="Weird. Use when odd.",
        instructions="Edited", replaces="Weird",
    )
    assert saved["ok"] is True and saved["data"] == {"name": "other-name"}
    assert os.listdir(store_dir) == ["other-name"]
    assert dispatch(router, type="skillDelete", name="other-name")["ok"] is True
    assert [i for i in t2v.skills.state()["local"] if i["origin"] == "user"] == []


def test_customize_refuses_when_the_user_already_has_that_name(tmp_path):
    folder = tmp_path / "bundled" / "ct-window-presets"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: ct-window-presets\ndescription: CT. Use for CT.\n---\nB\n"
    )
    s = skills()
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[str(tmp_path / "bundled")])
    s.customize("ct-window-presets")
    s.save_doc(doc("ct-window-presets", "Mine."), replaces="ct-window-presets")
    with pytest.raises(SkillExistsError):
        s.customize("ct-window-presets")
    assert "Mine." in (tmp_path / "user" / "ct-window-presets" / "SKILL.md").read_text()
