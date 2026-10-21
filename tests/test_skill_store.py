"""talk2view.skill_store.FolderSkillStore: SKILL.md folders on disk."""

import os

import pytest

from talk2view.skill_format import from_form, parse, render
from talk2view.skill_store import FolderSkillStore, InvalidSkillError, SkillExistsError


def doc(
    name="report-in-ml",
    description="Reports volumes in mL. Use when measuring.",
    body="Use mL.",
):
    return from_form(name, description, body)


def test_write_uses_the_spec_layout_and_lists_it(tmp_path):
    store = FolderSkillStore(tmp_path)
    stored = store.write(doc())
    assert stored.path == str(tmp_path / "report-in-ml" / "SKILL.md")
    assert (tmp_path / "report-in-ml" / "SKILL.md").read_text() == render(doc())
    assert [s.doc.name for s in store.list()] == ["report-in-ml"]


def test_write_is_atomic_no_temp_files_left(tmp_path):
    FolderSkillStore(tmp_path).write(doc())
    assert sorted(os.listdir(tmp_path / "report-in-ml")) == ["SKILL.md"]


def test_invalid_doc_writes_nothing(tmp_path):
    with pytest.raises(InvalidSkillError) as info:
        FolderSkillStore(tmp_path).write(doc(name="Bad Name"))
    assert [p.field for p in info.value.problems] == ["name"]
    assert os.listdir(tmp_path) == []


def test_taken_name_raises_unless_replacing_or_overwriting(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc())
    with pytest.raises(SkillExistsError):
        store.write(doc(body="Other."))
    store.write(doc(body="Other."), replaces="report-in-ml")
    store.write(doc(body="Third."), overwrite=True)
    assert store.read("report-in-ml").doc.body == "Third.\n"


def test_rename_leaves_one_copy(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc())
    store.write(doc(name="report-in-millilitres"), replaces="report-in-ml")
    assert [s.doc.name for s in store.list()] == ["report-in-millilitres"]
    store.delete("report-in-millilitres")
    assert store.restore("report-in-millilitres").doc.name == "report-in-millilitres"
    assert [s.doc.name for s in store.list()] == ["report-in-millilitres"]


def test_flat_files_are_read_and_an_edit_moves_them_to_a_folder(tmp_path):
    (tmp_path / "old-style.md").write_text("---\nname: old-style\ndescription: d\n---\nBody\n")
    store = FolderSkillStore(tmp_path)
    assert [s.doc.name for s in store.list()] == ["old-style"]
    store.write(parse((tmp_path / "old-style.md").read_text())[0], replaces="old-style")
    assert not (tmp_path / "old-style.md").exists()
    assert (tmp_path / "old-style" / "SKILL.md").is_file()


def test_dot_entries_are_never_skills(tmp_path):
    (tmp_path / ".trash").mkdir()
    (tmp_path / ".trash" / "x.md").write_text("---\nname: x\ndescription: d\n---\nB\n")
    (tmp_path / ".hidden.md").write_text("---\nname: hidden\ndescription: d\n---\nB\n")
    assert FolderSkillStore(tmp_path).list() == []


def test_delete_then_restore_then_close_empties_trash(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc())
    store.delete("report-in-ml")
    assert store.list() == []
    assert store.restore("report-in-ml").doc.name == "report-in-ml"
    store.delete("report-in-ml")
    store.close()
    assert not (tmp_path / ".trash").exists()


def test_restore_unknown_raises_key_error(tmp_path):
    with pytest.raises(KeyError):
        FolderSkillStore(tmp_path).restore("nope")


def test_disabled_survives_a_new_store(tmp_path):
    FolderSkillStore(tmp_path).set_enabled("ct-window-presets", False)
    store = FolderSkillStore(tmp_path)
    assert store.disabled() == {"ct-window-presets"}
    store.set_enabled("ct-window-presets", True)
    assert FolderSkillStore(tmp_path).disabled() == set()


def test_corrupt_state_file_means_nothing_disabled(tmp_path):
    (tmp_path / ".t2v-skills.json").write_text("{not json")
    assert FolderSkillStore(tmp_path).disabled() == set()


def test_extra_files_are_flagged_and_kept(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc())
    (tmp_path / "report-in-ml" / "references").mkdir()
    (tmp_path / "report-in-ml" / "references" / "units.md").write_text("mL")
    stored = store.read("report-in-ml")
    assert stored.has_extra_files
    store.write(doc(body="Edited."), replaces="report-in-ml")
    assert (tmp_path / "report-in-ml" / "references" / "units.md").is_file()


def test_nonconforming_file_lists_with_problems(tmp_path):
    (tmp_path / "Bad_Name.md").write_text("---\nname: Bad_Name\n---\nB\n")
    [stored] = FolderSkillStore(tmp_path).list()
    assert {p.field for p in stored.problems} == {"name", "description"}


def test_metadata_problem_is_listed_once(tmp_path):
    # parse() and validate() can both report a problem for
    # a scalar `metadata`. _read_entry must de-duplicate (same field + message once).
    folder = tmp_path / "bad-meta"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: bad-meta\ndescription: d\nmetadata: not-a-map\n---\nB\n"
    )
    [stored] = FolderSkillStore(tmp_path).list()
    assert [p.field for p in stored.problems] == ["metadata"]


def test_import_a_file_and_a_folder(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "notes.md").write_text("# no frontmatter\n")
    folder = src / "ct-helper"
    folder.mkdir()
    (folder / "SKILL.md").write_text(
        "---\nname: ct-helper\ndescription: Helps with CT. Use for CT.\n---\nB\n"
    )
    (folder / "assets").mkdir()
    (folder / "assets" / "table.csv").write_text("a,b")
    store = FolderSkillStore(tmp_path / "skills")
    with pytest.raises(InvalidSkillError) as info:
        store.import_path(src / "notes.md")  # no description: ask for one, never guess
    assert [p.field for p in info.value.problems] == ["description"]
    stored = store.import_path(folder)
    assert stored.has_extra_files
    assert (tmp_path / "skills" / "ct-helper" / "assets" / "table.csv").is_file()
    with pytest.raises(SkillExistsError):
        store.import_path(folder)
    assert store.import_path(folder, on_conflict="keep_both").doc.name == "ct-helper-2"
    assert store.import_path(folder, on_conflict="replace").doc.name == "ct-helper"



def test_import_takes_a_missing_description_from_the_caller(tmp_path):
    (tmp_path / "notes.md").write_text("---\nname: notes\n---\nBody\n")
    store = FolderSkillStore(tmp_path / "skills")
    stored = store.import_path(tmp_path / "notes.md", description="Takes notes. Use for notes.")
    assert stored.doc.description == "Takes notes. Use for notes."
    text = (tmp_path / "skills" / "notes" / "SKILL.md").read_text()
    assert "description: Takes notes. Use for notes.\n" in text
    # The given description is validated like any other.
    with pytest.raises(InvalidSkillError) as info:
        store.import_path(tmp_path / "notes.md", description="<b>x</b>", on_conflict="replace")
    assert [p.field for p in info.value.problems] == ["description"]

def test_symlink_out_of_the_folder_is_refused(tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: outside\ndescription: d\n---\nB\n")
    root = tmp_path / "skills"
    root.mkdir()
    (root / "outside.md").symlink_to(outside)
    assert FolderSkillStore(root).list() == []


# ── Renames, restores and imports that must be refused ──


def test_rename_onto_a_name_another_skill_holds_is_refused(tmp_path):
    # A rename (replaces set, differing from the new name) must never merge into or
    # clobber a different skill, even with overwrite=True.
    store = FolderSkillStore(tmp_path)
    store.write(doc(name="foo", body="Foo body."))
    store.write(doc(name="bar", body="Bar body."))
    with pytest.raises(SkillExistsError):
        store.write(doc(name="bar", body="Clobbered?"), replaces="foo", overwrite=True)
    # Nothing was touched: both skills are still there, unchanged.
    assert {s.doc.name for s in store.list()} == {"foo", "bar"}
    assert store.read("foo").doc.body == "Foo body.\n"
    assert store.read("bar").doc.body == "Bar body.\n"


def test_restore_onto_a_reused_name_is_refused_and_keeps_undo(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc(name="foo"))
    store.delete("foo")
    store.write(doc(name="foo", body="New."))
    with pytest.raises(SkillExistsError):
        store.restore("foo")
    # The trashed copy was not lost: it is still there, and still restorable once the
    # skill now using the name is out of the way.
    assert store.read("foo").doc.body == "New.\n"
    store.delete("foo")
    assert store.restore("foo").doc.name == "foo"


def test_import_from_inside_the_store_is_refused(tmp_path):
    store = FolderSkillStore(tmp_path)
    store.write(doc(name="ct-helper"))
    with pytest.raises(InvalidSkillError) as info:
        store.import_path(tmp_path / "ct-helper", on_conflict="replace")
    assert [p.field for p in info.value.problems] == ["path"]
    # Nothing was touched: the skill is exactly as it was.
    assert store.read("ct-helper").doc.name == "ct-helper"


def test_import_never_follows_a_symlink(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not copy me")

    src = tmp_path / "src-folder"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: linky\ndescription: d. Use for d.\n---\nB\n")
    (src / "link.txt").symlink_to(secret)
    nested = src / "nested"
    nested.mkdir()
    (nested / "inner-link.txt").symlink_to(secret)

    store = FolderSkillStore(tmp_path / "skills")
    store.import_path(src)
    assert not (tmp_path / "skills" / "linky" / "link.txt").exists()
    assert not (tmp_path / "skills" / "linky" / "nested" / "inner-link.txt").exists()

    # A SKILL.md that is itself a link is refused outright, before anything is read.
    linked_folder = tmp_path / "linked-folder"
    linked_folder.mkdir()
    (linked_folder / "SKILL.md").symlink_to(
        tmp_path / "src-folder" / "SKILL.md"
    )
    with pytest.raises(InvalidSkillError) as info:
        FolderSkillStore(tmp_path / "skills2").import_path(linked_folder)
    assert [p.field for p in info.value.problems] == ["path"]


# ── Engine caps, unreadable imports and the store lock ──────────────────────────────


def test_a_skill_over_the_engine_cap_is_refused_before_anything_is_written(tmp_path):
    from talk2view.skills import MAX_REGISTER_BODY_BYTES

    store = FolderSkillStore(tmp_path)
    with pytest.raises(InvalidSkillError) as info:
        store.write(doc(body="x" * (MAX_REGISTER_BODY_BYTES + 1)))
    assert [p.field for p in info.value.problems] == ["size"]
    assert os.listdir(tmp_path) == []


def test_import_a_file_that_is_not_utf8(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"---\nname: bad\ndescription: \xff\xfe. Use.\n---\nB\n")
    with pytest.raises(InvalidSkillError) as info:
        FolderSkillStore(tmp_path / "skills").import_path(bad)
    assert [(p.field, p.message) for p in info.value.problems] == [
        ("path", "the file is not UTF-8 text")
    ]


def test_import_a_folder_without_skill_md(tmp_path):
    (tmp_path / "empty-folder").mkdir()
    with pytest.raises(InvalidSkillError) as info:
        FolderSkillStore(tmp_path / "skills").import_path(tmp_path / "empty-folder")
    assert [(p.field, p.message) for p in info.value.problems] == [
        ("path", "there is no SKILL.md in that folder")
    ]


def test_mutating_operations_run_under_the_store_lock(tmp_path):
    """write, import_path, delete, restore, set_enabled and close each hold the one lock
    (the router thread and the tool thread can both reach the store)."""
    store = FolderSkillStore(tmp_path / "skills")
    held: list = []
    real = store._lock

    class Recording:
        def __enter__(self):
            real.acquire()
            held.append(True)

        def __exit__(self, *exc):
            real.release()

    store._lock = Recording()  # type: ignore[assignment]
    src = tmp_path / "src.md"
    src.write_text("---\nname: imported\ndescription: I. Use it.\n---\nB\n")
    calls = [
        lambda: store.write(doc()),
        lambda: store.import_path(src),
        lambda: store.delete("report-in-ml"),
        lambda: store.restore("report-in-ml"),
        lambda: store.set_enabled("report-in-ml", False),
        lambda: store.close(),
    ]
    for call in calls:
        before = len(held)
        call()
        assert len(held) == before + 1


def test_concurrent_writes_leave_one_consistent_copy(tmp_path):
    import threading

    store = FolderSkillStore(tmp_path)
    store.write(doc())
    errors: list = []

    def edit(i: int) -> None:
        try:
            store.write(doc(body="Edit {}.".format(i)), replaces="report-in-ml")
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=edit, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert sorted(os.listdir(tmp_path)) == ["report-in-ml"]
    assert os.listdir(tmp_path / "report-in-ml") == ["SKILL.md"]


# ── A link planted inside the skills folder never lets a write escape it ──


@pytest.mark.parametrize("link_name", ["csv-helper", "csv-helper.md"])
def test_a_write_never_goes_through_a_link_out_of_the_folder(tmp_path, link_name):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("mine")
    root = tmp_path / "skills"
    root.mkdir()
    target = outside if not link_name.endswith(".md") else outside / "keep.txt"
    (root / link_name).symlink_to(target)
    store = FolderSkillStore(root)
    with pytest.raises(InvalidSkillError) as info:
        store.write(doc(name="csv-helper"))
    assert [p.field for p in info.value.problems] == ["name"]
    assert sorted(p.name for p in outside.iterdir()) == ["keep.txt"]
    assert (outside / "keep.txt").read_text() == "mine"


def test_a_write_never_goes_through_a_link_to_an_outside_skill_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: outside\ndescription: d\n---\nTHEIRS\n")
    root = tmp_path / "skills"
    root.mkdir()
    (root / "outside").symlink_to(outside)
    store = FolderSkillStore(root)
    for kwargs in ({}, {"overwrite": True}):
        with pytest.raises(InvalidSkillError):
            store.write(doc(name="outside", body="MODEL WROTE THIS"), **kwargs)
    assert (outside / "SKILL.md").read_text().endswith("THEIRS\n")


def test_an_import_never_goes_through_a_link_out_of_the_folder(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "skills"
    root.mkdir()
    (root / "ct-helper").symlink_to(outside)
    src = tmp_path / "src" / "ct-helper"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: ct-helper\ndescription: CT. Use for CT.\n---\nB\n")
    store = FolderSkillStore(root)
    for on_conflict in ("ask", "replace"):
        with pytest.raises(InvalidSkillError):
            store.import_path(src, on_conflict=on_conflict)
    assert list(outside.iterdir()) == []


def test_delete_never_moves_a_skill_into_a_trash_that_is_a_link(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "skills"
    store = FolderSkillStore(root)
    store.write(doc())
    (root / ".trash").symlink_to(outside)
    with pytest.raises(OSError):
        store.delete("report-in-ml")
    assert list(outside.iterdir()) == []
    assert store.read("report-in-ml") is not None
