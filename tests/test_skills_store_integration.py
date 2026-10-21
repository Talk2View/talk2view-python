"""T2VSkills over a FolderSkillStore and bundled folders."""

from pathlib import Path
from typing import Any, Dict, List

from talk2view.skill_format import from_form
from talk2view.skill_store import FolderSkillStore
from talk2view.skills import T2VSkills
from talk2view.storage import MemoryStorage

from .test_skills_core import ManualTimer


class NoClient:
    def request(self, *a: Any, **k: Any) -> Dict[str, Any]:
        return {"registered": [], "count": 0, "replaced": True}


class RecordingClient:
    """Records the names sent on every ``/v1/skills/register`` call, so a test can assert
    what actually went into the register payload (not just what ``enabled()`` reports)."""

    def __init__(self) -> None:
        self.sent: List[List[str]] = []

    def request(self, *a: Any, **k: Any) -> Dict[str, Any]:
        body = k.get("body") or (a[-1] if a else {})
        names = [s["name"] for s in body.get("skills", [])]
        self.sent.append(names)
        return {"registered": names, "count": len(names), "replaced": True}


class _NoTimer:
    def cancel(self) -> None:
        pass


def _no_timer(delay: float, fn: Any) -> _NoTimer:
    return _NoTimer()


def skills() -> T2VSkills:
    return T2VSkills(NoClient(), MemoryStorage(), start_timer=_no_timer)


def bundled(tmp_path):
    # exist_ok=True: some tests call this twice for the same tmp_path (a second
    # T2VSkills reading the same bundled folder a first one already set up).
    folder = tmp_path / "bundled"
    (folder / "ct-window-presets").mkdir(parents=True, exist_ok=True)
    (folder / "ct-window-presets" / "SKILL.md").write_text(
        "---\nname: ct-window-presets\ndescription: CT presets. Use for CT windows.\n---\nB\n")
    return str(folder)


def names(state: Dict[str, Any], origin: str) -> List[str]:
    return [s["name"] for s in state["local"] if s["origin"] == origin]


def test_store_and_bundled_load_with_origins(tmp_path):
    s = skills()
    store = FolderSkillStore(tmp_path / "user")
    store.write(from_form("report-in-ml", "Reports mL. Use when measuring.", "B"))
    s.use_store(store, bundled=[bundled(tmp_path)])
    state = s.state()
    assert names(state, "bundled") == ["ct-window-presets"]
    assert names(state, "user") == ["report-in-ml"]
    user = next(i for i in state["local"] if i["name"] == "report-in-ml")
    assert user["editable"] is True and user["hasExtraFiles"] is False and user["problems"] == []
    assert next(i for i in state["local"] if i["origin"] == "bundled")["editable"] is False
    assert state["store"] == {"writable": True, "canOpenFolder": True}


def test_user_skill_overrides_bundled_by_name(tmp_path):
    s = skills()
    store = FolderSkillStore(tmp_path / "user")
    store.write(from_form("ct-window-presets", "Mine. Use always.", "Mine"))
    s.use_store(store, bundled=[bundled(tmp_path)])
    rows = [i for i in s.state()["local"] if i["name"] == "ct-window-presets"]
    # The user's copy is the one that counts; the bundled one stays listed as replaced.
    [mine] = [i for i in rows if i["origin"] == "user"]
    [replaced] = [i for i in rows if i["origin"] == "bundled"]
    assert mine.get("overridden") is None and mine["enabled"] is True
    assert replaced["overridden"] is True and replaced["enabled"] is False
    assert s.get("ct-window-presets").content == "Mine"
    assert [sk.name for sk in s.enabled()] == ["ct-window-presets"]


def test_disable_persists_across_a_new_skills_object(tmp_path):
    s = skills()
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[bundled(tmp_path)])
    s.set_enabled("ct-window-presets", False)
    again = skills()
    again.use_store(FolderSkillStore(tmp_path / "user"), bundled=[bundled(tmp_path)])
    assert [i["enabled"] for i in again.state()["local"]] == [False]


def test_save_delete_restore_customize(tmp_path):
    s = skills()
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[bundled(tmp_path)])
    doc = from_form("report-in-ml", "Reports mL. Use when measuring.", "B")
    assert s.save_doc(doc) == "report-in-ml"
    assert "report-in-ml" in names(s.state(), "user")
    s.delete_user("report-in-ml")
    assert "report-in-ml" not in names(s.state(), "user")
    s.restore_user("report-in-ml")
    assert "report-in-ml" in names(s.state(), "user")
    assert s.customize("ct-window-presets") == "ct-window-presets"
    assert "ct-window-presets" in names(s.state(), "user")


def test_customized_bundled_skill_comes_back_after_the_user_copy_is_deleted(tmp_path):
    """Deleting a user copy of a customized bundled skill must not leave the name gone
    entirely: the bundled skill of the same name comes back, with origin "bundled"."""
    s = skills()
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[bundled(tmp_path)])
    assert s.customize("ct-window-presets") == "ct-window-presets"
    assert names(s.state(), "user") == ["ct-window-presets"]
    [replaced] = [i for i in s.state()["local"] if i["origin"] == "bundled"]
    assert replaced["overridden"] is True

    s.delete_user("ct-window-presets")

    state = s.state()
    assert names(state, "user") == []
    assert names(state, "bundled") == ["ct-window-presets"]
    [only] = [i for i in state["local"] if i["name"] == "ct-window-presets"]
    assert only["origin"] == "bundled" and only["editable"] is False


def test_nonconforming_folder_skill_still_loads_with_problems(tmp_path):
    user = tmp_path / "user"
    user.mkdir()
    (user / "Old_Skill.md").write_text("---\nname: Old_Skill\n---\nStill works.\n")
    s = skills()
    s.use_store(FolderSkillStore(user))
    [item] = s.state()["local"]
    assert item["name"] == "Old_Skill" and item["enabled"] is True
    assert {p["field"] for p in item["problems"]} == {"name", "description"}
    assert s.get("Old_Skill").content == "Still works."


def test_load_directory_skips_dot_entries(tmp_path):
    folder = tmp_path / "d"
    (folder / ".trash").mkdir(parents=True)
    (folder / ".trash" / "x.md").write_text("---\nname: x\ndescription: d\n---\nB\n")
    s = skills()
    assert s.load_directory(folder) == []


def test_code_and_dir_origins_unchanged(tmp_path):
    from talk2view.skills import Skill
    s = skills()
    s.add(Skill(name="from-code", description="d", content="c"))
    item = s.state()["local"][0]
    assert item["origin"] == "code" and item["editable"] is False


# ── Engine-invalid skills stay listed, never registered ─────────────────────


def test_engine_invalid_user_skill_stays_listed_but_never_registered(tmp_path):
    user = tmp_path / "user"
    (user / "empty-body").mkdir(parents=True)
    (user / "empty-body" / "SKILL.md").write_text(
        "---\nname: empty-body\ndescription: Does nothing. Use never.\n---\n\n"
    )
    client = RecordingClient()
    timer = ManualTimer()
    s = T2VSkills(client, MemoryStorage(), start_timer=timer)
    s.use_store(FolderSkillStore(user))

    [item] = s.state()["local"]
    assert item["name"] == "empty-body" and item["origin"] == "user"
    assert item["enabled"] is True  # never silently disabled either
    # de-duplicated: the store's own spec problems already say this; no second copy.
    assert item["problems"] == [{"field": "body", "message": "the instructions are empty"}]

    assert "empty-body" not in [sk.name for sk in s.enabled()]

    # The debounced path (_sync, scheduled by use_store's own load) must exclude it.
    timer.fire()
    assert client.sent[-1] == []  # never made it into the register payload

    # The immediate register() path must too.
    s.register()
    assert client.sent[-1] == []


def test_engine_invalid_bundled_skill_stays_listed_but_never_registered(tmp_path):
    folder = tmp_path / "bundled"
    (folder / "empty-body").mkdir(parents=True)
    (folder / "empty-body" / "SKILL.md").write_text(
        "---\nname: empty-body\ndescription: Does nothing. Use never.\n---\n\n"
    )
    client = RecordingClient()
    s = T2VSkills(client, MemoryStorage(), start_timer=_no_timer)
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[str(folder)])

    [item] = s.state()["local"]
    assert item["name"] == "empty-body" and item["origin"] == "bundled"
    assert item["enabled"] is True
    assert item["problems"] == [{"field": "body", "message": "the instructions are empty"}]

    assert "empty-body" not in [sk.name for sk in s.enabled()]
    s.register()
    assert client.sent[-1] == []


# ── rescan() schedules a register only when something changed ──


def test_rescan_with_no_change_schedules_no_register(tmp_path):
    timer = ManualTimer()
    client = RecordingClient()
    s = T2VSkills(client, MemoryStorage(), start_timer=timer)
    folder = bundled(tmp_path)
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[folder])
    scheduled = timer.started
    assert scheduled > 0  # the initial load did have something to register

    s.rescan()
    assert timer.started == scheduled  # nothing on disk changed: no new schedule
    assert client.sent == []  # the timer never fired, so definitely no POST was sent

    skill_file = Path(folder) / "ct-window-presets" / "SKILL.md"
    skill_file.write_text(
        "---\nname: ct-window-presets\ndescription: CT presets, updated. Use for windows.\n"
        "---\nB\n"
    )
    s.rescan()
    assert timer.started == scheduled + 1  # exactly one new schedule for the real edit


# ── A bundled skill a user skill replaces ─────────────────────────────────────────────


def test_rescans_after_an_edited_customize_schedule_no_register(tmp_path):
    timer = ManualTimer()
    s = T2VSkills(NoClient(), MemoryStorage(), start_timer=timer)
    s.use_store(FolderSkillStore(tmp_path / "user"), bundled=[bundled(tmp_path)])
    s.customize("ct-window-presets")
    s.save_doc(
        from_form("ct-window-presets", "CT presets, mine. Use for CT windows.", "Use W350"),
        replaces="ct-window-presets",
    )
    seen: List[Any] = []
    s.on_change(lambda st: seen.append(
        [(i["name"], i["origin"], i.get("overridden", False)) for i in st["local"]]
    ))
    started = timer.started
    s.rescan()
    s.rescan()
    assert timer.started == started  # no register call scheduled
    # Never a flicker: every state the page was sent has the user's copy registered and
    # the bundled one listed as replaced.
    assert seen and all(
        state == [("ct-window-presets", "user", False), ("ct-window-presets", "bundled", True)]
        for state in seen
    )
    assert s.get("ct-window-presets").content == "Use W350"
    assert [sk.content for sk in s.enabled()] == ["Use W350"]


def test_the_replaced_bundled_skill_is_never_registered(tmp_path):
    client = RecordingClient()
    s = T2VSkills(client, MemoryStorage(), start_timer=_no_timer)
    store = FolderSkillStore(tmp_path / "user")
    store.write(from_form("ct-window-presets", "Mine. Use always.", "Mine"))
    s.use_store(store, bundled=[bundled(tmp_path)])
    s.register()
    assert client.sent[-1] == ["ct-window-presets"]
    assert [sk.content for sk in s.enabled()] == ["Mine"]


def test_customize_never_overwrites_a_hand_made_skill_in_the_same_folder(tmp_path):
    import pytest

    from talk2view.skill_store import SkillExistsError

    user = tmp_path / "user"
    (user / "ct-window-presets").mkdir(parents=True)
    (user / "ct-window-presets" / "SKILL.md").write_text(
        "---\nname: mine-other\ndescription: Mine. Use always.\n---\nMY OWN WORDS\n"
    )
    s = skills()
    s.use_store(FolderSkillStore(user), bundled=[bundled(tmp_path)])
    with pytest.raises(SkillExistsError):
        s.customize("ct-window-presets")
    assert (user / "ct-window-presets" / "SKILL.md").read_text().endswith("MY OWN WORDS\n")
