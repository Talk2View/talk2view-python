"""The skills commands through CommandRouter, with a real FolderSkillStore."""

import time as time_module
from typing import Any, Dict, List, Optional

import pytest

from talk2view import Talk2View
from talk2view.storage import MemoryStorage
from talk2view.ui import GenericHost, UIServer
from talk2view.ui import router as router_module
from talk2view.ui.options import ChatFeatures, ChatOptions
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import CommandFailure, parse_command

from .test_ui_server import STUB, Rig, inline
from .ui_router_harness import RecordingHost, make_router


def save(router, **args: Any) -> Dict[str, Any]:
    return router.run("skillSave", args)


def dispatch(router, **body: Any) -> Dict[str, Any]:
    """A command through the real path: ``wire.parse_command`` (every declared field
    present in ``args``, ``None`` for one the page left out) then ``execute`` -- not
    ``router.run``, which calls a handler with exactly the raw dict a test wrote."""
    return router.execute(parse_command(dict(body, id="c")))


def test_save_from_the_form_then_edit_and_rename(tmp_path):
    router, t2v = make_router(skills_dir=tmp_path)
    assert save(router, name="report-in-ml", description="Reports mL. Use when measuring.",
                instructions="Use mL.") == {"name": "report-in-ml"}
    renamed = save(router, name="report-in-millilitres",
                   description="Reports mL. Use when measuring.",
                   instructions="Use mL.", replaces="report-in-ml")
    assert renamed == {"name": "report-in-millilitres"}
    assert [s["name"] for s in t2v.skills.state()["local"]] == ["report-in-millilitres"]


def test_save_markdown(tmp_path):
    router, _ = make_router(skills_dir=tmp_path)
    md = "---\nname: md-skill\ndescription: From markdown. Use to test.\nlicense: MIT\n---\nBody\n"
    assert save(router, markdown=md) == {"name": "md-skill"}
    assert "license: MIT" in (tmp_path / "md-skill" / "SKILL.md").read_text()


def test_invalid_skill_names_the_field(tmp_path):
    router, _ = make_router(skills_dir=tmp_path)
    with pytest.raises(CommandFailure) as info:
        save(router, name="Bad Name", description="", instructions="x")
    assert info.value.type == "invalid_skill"
    assert info.value.message.splitlines()[0].startswith("name: ")
    assert any(line.startswith("description: ") for line in info.value.message.splitlines())


def test_taken_name_is_skill_exists(tmp_path):
    router, _ = make_router(skills_dir=tmp_path)
    save(router, name="a-b", description="D. Use it.", instructions="x")
    with pytest.raises(CommandFailure) as info:
        save(router, name="a-b", description="D. Use it.", instructions="y")
    assert (info.value.type, info.value.message) == ("skill_exists", "a-b")


def test_delete_restore_customize(tmp_path, bundled_dir):
    router, t2v = make_router(skills_dir=tmp_path / "user", bundled=[bundled_dir])
    save(router, name="a-b", description="D. Use it.", instructions="x")
    router.run("skillDelete", {"name": "a-b"})
    router.run("skillRestore", {"name": "a-b"})
    customized = router.run("skillCustomize", {"name": "ct-window-presets"})
    assert customized == {"name": "ct-window-presets"}
    origins = sorted(
        (s["name"], s["origin"], s.get("overridden", False))
        for s in t2v.skills.state()["local"]
    )
    assert origins == [
        ("a-b", "user", False),
        ("ct-window-presets", "bundled", True),
        ("ct-window-presets", "user", False),
    ]


def test_without_a_store_every_write_is_skills_unavailable():
    router, _ = make_router(skills_dir=None)
    for command, args in (("skillSave", {"name": "a", "description": "d", "instructions": "i"}),
                          ("skillDelete", {"name": "a"}), ("skillImport", {}),
                          ("skillOpenFolder", {})):
        with pytest.raises(CommandFailure) as info:
            router.run(command, args)
        assert info.value.type == "skills_unavailable"


def test_import_resolve_uses_the_picked_path_only(tmp_path):
    src = tmp_path / "src"
    (src / "ct-helper").mkdir(parents=True)
    (src / "ct-helper" / "SKILL.md").write_text(
        "---\nname: ct-helper\ndescription: CT. Use for CT.\n---\nB\n"
    )
    picks: List[Optional[str]] = [str(src / "ct-helper"), str(src / "ct-helper")]
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: picks.pop(0))
    assert router.run("skillImport", {}) == {"name": "ct-helper"}
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {})
    assert info.value.type == "skill_exists"
    # The page answers the conflict; it never names a path. The router uses the one it picked.
    assert router.run("skillImport", {"resolve": "keepBoth"}) == {"name": "ct-helper-2"}
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {"resolve": "replace"})  # nothing pending any more
    assert info.value.type == "import_unavailable"


def test_import_cancelled_picker(tmp_path):
    router, _ = make_router(skills_dir=tmp_path, pick=lambda: None)
    assert router.run("skillImport", {}) == {"cancelled": True}


def test_import_without_a_picker_is_import_unavailable(tmp_path):
    router, _ = make_router(skills_dir=tmp_path, pick=None)
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {})
    assert info.value.type == "import_unavailable"


def test_open_folder_posts_a_file_url(tmp_path):
    router, _ = make_router(skills_dir=tmp_path)
    router.run("skillOpenFolder", {})
    assert router.host.opened == [tmp_path.resolve().as_uri()]


def test_features_skills_on_the_wire():
    from talk2view.ui.options import ChatFeatures
    assert ChatFeatures().to_wire()["skills"] is True
    assert ChatFeatures(skills=False).to_wire()["skills"] is False


# ── Dispatch, capabilities and the picker ────────────────────────────────────────────
#
# 1. Through the real dispatch path (wire.parse_command -> execute), not a handler called
#    directly: `router.run`'s raw dict only ever has the keys a test wrote, so it can't
#    pin the "every declared field is present in args, None for one the page left out"
#    shape a real command always has -- the shape `_skill_save`'s `is not None` checks
#    exist for.


def test_skill_save_form_fields_through_the_real_dispatch_path(tmp_path):
    router, t2v = make_router(skills_dir=tmp_path)
    result = dispatch(router, type="skillSave", name="report-in-ml",
                       description="Reports mL. Use when measuring.", instructions="Use mL.")
    assert result["ok"] is True
    assert result["data"] == {"name": "report-in-ml"}
    assert [s["name"] for s in t2v.skills.state()["local"]] == ["report-in-ml"]


def test_skill_save_markdown_through_the_real_dispatch_path(tmp_path):
    router, _ = make_router(skills_dir=tmp_path)
    md = "---\nname: md-skill\ndescription: From markdown. Use to test.\nlicense: MIT\n---\nBody\n"
    result = dispatch(router, type="skillSave", markdown=md)
    assert result["ok"] is True
    assert result["data"] == {"name": "md-skill"}
    assert "license: MIT" in (tmp_path / "md-skill" / "SKILL.md").read_text()


def test_skill_import_pick_conflict_resolve_through_the_real_dispatch_path(tmp_path):
    src = tmp_path / "src"
    (src / "ct-helper").mkdir(parents=True)
    (src / "ct-helper" / "SKILL.md").write_text(
        "---\nname: ct-helper\ndescription: CT. Use for CT.\n---\nB\n"
    )
    picks: List[Optional[str]] = [str(src / "ct-helper"), str(src / "ct-helper")]
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: picks.pop(0))
    first = dispatch(router, type="skillImport")
    assert first["ok"] is True and first["data"] == {"name": "ct-helper"}
    second = dispatch(router, type="skillImport")
    assert second["ok"] is False and second["error"]["type"] == "skill_exists"
    third = dispatch(router, type="skillImport", resolve="keepBoth")
    assert third["ok"] is True and third["data"] == {"name": "ct-helper-2"}


# 2. `capabilities.skillImport` must be true only once a picker was given AND the store
#    was actually wired up (skills_dir given, an account behind the controller, and
#    features.skills on) -- not just because a picker was given.


def test_capabilities_skill_import_needs_the_store_actually_wired(tmp_path):
    t2v = Talk2View(partner_key="pk_test", storage=MemoryStorage(), base_url="http://127.0.0.1:9")
    controller = FakeController(executor=inline)
    controller.t2v = t2v
    host = GenericHost()
    options = ChatOptions(features=ChatFeatures(skills=False))
    server = UIServer(controller, host, static_dir=STUB, options=options,
                       skills_dir=tmp_path, on_pick_skill_import=lambda: None)
    rig = Rig(server, controller, host)
    try:
        state = rig.server._hub.snapshot()["state"]
        assert state["capabilities"]["skillImport"] is False
        assert t2v.skills.store is None  # the store was never wired up either
    finally:
        rig.server.stop()
        t2v.close()


def test_capabilities_skill_import_true_once_the_store_is_wired(tmp_path):
    t2v = Talk2View(partner_key="pk_test", storage=MemoryStorage(), base_url="http://127.0.0.1:9")
    controller = FakeController(executor=inline)
    controller.t2v = t2v
    host = GenericHost()
    server = UIServer(controller, host, static_dir=STUB,
                       skills_dir=tmp_path, on_pick_skill_import=lambda: None)
    rig = Rig(server, controller, host)
    try:
        state = rig.server._hub.snapshot()["state"]
        assert state["capabilities"]["skillImport"] is True
        assert t2v.skills.store is not None
    finally:
        rig.server.stop()
        t2v.close()


# 3. `_on_host` must not hang forever when the host never runs the posted callable: a
#    host whose `post` drops it, and `PICKER_WAIT_S` patched small.


class DroppingHost(RecordingHost):
    """A host whose ``post`` drops the callable, as if the host never got around to it."""

    def post(self, fn):
        pass


def test_picker_timeout_ends_in_import_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(router_module, "PICKER_WAIT_S", 0.05)
    router, _ = make_router(skills_dir=tmp_path, pick=lambda: str(tmp_path), host=DroppingHost())
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {})
    assert info.value.type == "import_unavailable"
    assert info.value.message == "The file picker did not answer."


# 4. `_pending_import` expires after `PENDING_IMPORT_S`, and any new pick clears it.


def test_pending_import_expires(tmp_path, monkeypatch):
    src = tmp_path / "src"
    (src / "ct-helper").mkdir(parents=True)
    (src / "ct-helper" / "SKILL.md").write_text(
        "---\nname: ct-helper\ndescription: CT. Use for CT.\n---\nB\n"
    )
    picks: List[Optional[str]] = [str(src / "ct-helper"), str(src / "ct-helper")]
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: picks.pop(0))
    clock = [1_000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
    assert router.run("skillImport", {}) == {"name": "ct-helper"}
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {})
    assert info.value.type == "skill_exists"
    clock[0] += router_module.PENDING_IMPORT_S + 1
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {"resolve": "keepBoth"})
    assert (info.value.type, info.value.message) == (
        "import_unavailable", "There is no import waiting for an answer.",
    )


def test_a_fresh_pick_clears_an_earlier_unresolved_conflict(tmp_path):
    src = tmp_path / "src"
    (src / "ct-helper").mkdir(parents=True)
    (src / "ct-helper" / "SKILL.md").write_text(
        "---\nname: ct-helper\ndescription: CT. Use for CT.\n---\nB\n"
    )
    picks: List[Optional[str]] = [str(src / "ct-helper"), str(src / "ct-helper"), None]
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: picks.pop(0))
    assert router.run("skillImport", {}) == {"name": "ct-helper"}
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {})
    assert info.value.type == "skill_exists"
    # A fresh pick -- even a cancelled one -- abandons that still-live conflict.
    assert router.run("skillImport", {}) == {"cancelled": True}
    with pytest.raises(CommandFailure) as info:
        router.run("skillImport", {"resolve": "keepBoth"})
    assert info.value.type == "import_unavailable"


# ── skillRead: the file the form's Edit / Customize / Edit as markdown opens ──


def test_read_returns_the_file_verbatim(tmp_path, bundled_dir):
    router, _ = make_router(skills_dir=tmp_path / "user", bundled=[bundled_dir])
    md = "---\nname: md-skill\ndescription: From markdown. Use to test.\nlicense: MIT\n---\nBody\n"
    save(router, markdown=md)
    # A user skill: the store's own file, byte for byte (frontmatter it doesn't model too).
    user_text = (tmp_path / "user" / "md-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert dispatch(router, type="skillRead", name="md-skill")["data"] == {"markdown": user_text}
    assert "license: MIT" in user_text
    # A bundled skill (what Customize opens): its folder's SKILL.md.
    bundled_text = (tmp_path / "bundled" / "ct-window-presets" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    result = dispatch(router, type="skillRead", name="ct-window-presets")
    assert result["ok"] is True and result["data"] == {"markdown": bundled_text}


def test_read_a_flat_bundled_file(tmp_path):
    bundled = tmp_path / "flat"
    bundled.mkdir()
    text = "---\nname: flat-one\ndescription: Flat. Use it.\n---\nFlat body\n"
    (bundled / "flat-one.md").write_text(text, encoding="utf-8")
    router, _ = make_router(skills_dir=tmp_path / "user", bundled=[str(bundled)])
    assert dispatch(router, type="skillRead", name="flat-one")["data"] == {"markdown": text}


@pytest.mark.parametrize("name", ["nope", "../user/md-skill", "scan-report"])
def test_read_refuses_anything_not_listed_as_a_user_or_bundled_skill(tmp_path, bundled_dir, name):
    router, t2v = make_router(skills_dir=tmp_path / "user", bundled=[bundled_dir])
    from talk2view.skills import Skill

    # A code-origin skill has no file: never read, even though it is listed.
    t2v.skills.add(Skill(name="scan-report", description="d", content="c"))
    save(router, name="md-skill", description="D. Use it.", instructions="x")
    result = dispatch(router, type="skillRead", name=name)
    assert result["ok"] is False and result["error"]["type"] == "unknown_skill"


def test_read_without_a_store_is_skills_unavailable():
    router, _ = make_router(skills_dir=None)
    result = dispatch(router, type="skillRead", name="a")
    assert result["ok"] is False and result["error"]["type"] == "skills_unavailable"



# ── An import with no description asks for one ──────────────────────────────────────


def _no_description(tmp_path):
    src = tmp_path / "src" / "notes"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: notes\n---\nTake notes.\n")
    return str(src)


def test_import_missing_description_waits_for_one_through_the_real_dispatch_path(tmp_path):
    path = _no_description(tmp_path)
    router, t2v = make_router(skills_dir=tmp_path / "user", pick=lambda: path)
    first = dispatch(router, type="skillImport")
    assert first["ok"] is False and first["error"]["type"] == "invalid_skill"
    assert all(line.startswith("description: ") for line in first["error"]["message"].splitlines())
    second = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert second["ok"] is True and second["data"] == {"name": "notes"}
    assert [s["description"] for s in t2v.skills.state()["local"]] == [
        "Takes notes. Use for notes.",
    ]
    # Answered: nothing is pending any more.
    third = dispatch(router, type="skillImport", description="Again. Use again.")
    assert third["ok"] is False and third["error"]["type"] == "import_unavailable"


def test_import_description_then_a_clash_resolves_with_that_description(tmp_path):
    path = _no_description(tmp_path)
    router, t2v = make_router(skills_dir=tmp_path / "user", pick=lambda: path)
    save(router, name="notes", description="Existing. Use it.", instructions="x")
    assert dispatch(router, type="skillImport")["error"]["type"] == "invalid_skill"
    clash = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert clash["ok"] is False and clash["error"] == {"type": "skill_exists", "message": "notes"}
    kept = dispatch(router, type="skillImport", resolve="keepBoth")
    assert kept["ok"] is True and kept["data"] == {"name": "notes-2"}
    described = {s["name"]: s["description"] for s in t2v.skills.state()["local"]}
    assert described["notes-2"] == "Takes notes. Use for notes."


def test_a_still_invalid_description_keeps_the_import_waiting(tmp_path):
    path = _no_description(tmp_path)
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: path)
    dispatch(router, type="skillImport")
    bad = dispatch(router, type="skillImport", description="<b>tags</b>")
    assert bad["ok"] is False and bad["error"]["type"] == "invalid_skill"
    good = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert good["ok"] is True


def test_other_problems_do_not_keep_an_import_waiting(tmp_path):
    src = tmp_path / "src" / "bad"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: Bad Name\n---\nB\n")
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: str(src))
    assert dispatch(router, type="skillImport")["error"]["type"] == "invalid_skill"
    after = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert after["ok"] is False and after["error"]["type"] == "import_unavailable"


def test_a_description_with_nothing_pending_or_expired_is_import_unavailable(tmp_path, monkeypatch):
    path = _no_description(tmp_path)
    router, _ = make_router(skills_dir=tmp_path / "user", pick=lambda: path)
    absent = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert absent["ok"] is False and absent["error"]["type"] == "import_unavailable"
    clock = [1_000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: clock[0])
    assert dispatch(router, type="skillImport")["error"]["type"] == "invalid_skill"
    clock[0] += router_module.PENDING_IMPORT_S + 1
    expired = dispatch(router, type="skillImport", description="Takes notes. Use for notes.")
    assert expired["ok"] is False and expired["error"]["type"] == "import_unavailable"
