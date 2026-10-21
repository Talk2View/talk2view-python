"""The controller and Talk2View names the skills and identity code is written against.

If one of these fails, the controller or Talk2View shipped a different shape: reconcile
the skills code against it. The event handlers live in ``_reducer.py`` (``TurnReducer``),
which the skills code never edits; it only adds ``set_skills_state`` to the controller
shell and reads ``auth.is_anonymous()``.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from talk2view import Talk2View
from talk2view.controller import ChatController
from talk2view.state import (
    ChatSnapshot,
    LocalSkillState,
    OpRecorder,
    SkillsState,
    empty_skills_state,
    initial_snapshot,
)


def test_controller_plumbing_used_by_set_skills_state() -> None:
    for name in (
        "_set_top",
        "_start_run",
        "_is_current",
        "_end_run",
        "_launch",
        "_flush",
        "_read_is_anonymous",
        "close",
        "snapshot",
        "subscribe",
        "wait_idle",
    ):
        assert name in dir(ChatController), name
    params = dict(inspect.signature(ChatController.__init__).parameters)
    assert set(params) >= {
        "t2v", "dispatcher", "system_prompt", "model", "capabilities", "executor"
    }
    assert inspect.signature(ChatController._launch).parameters.keys() >= {"run_id", "job"}
    assert inspect.signature(ChatController._set_top).parameters.keys() >= {"key", "value"}


def test_talk2view_hooks_used_by_skills() -> None:
    assert callable(getattr(Talk2View, "_after_session_created"))
    assert callable(getattr(Talk2View, "clear_session"))
    assert callable(getattr(Talk2View, "create_session"))
    assert callable(getattr(Talk2View, "on_session_recovered"))
    params = dict(inspect.signature(Talk2View.__init__).parameters)
    assert set(params) >= {
        "partner_key", "base_url", "storage", "model", "dispatcher", "capabilities"
    }


def test_state_skills_slice_shape() -> None:
    snapshot: ChatSnapshot = initial_snapshot()
    assert snapshot["skills"] == empty_skills_state()
    assert set(SkillsState.__annotations__) == {
        "local",
        "platform",
        "registered",
        "available",
        "needsSessionReset",
        "store",
    }
    assert LocalSkillState.__required_keys__ == {
        "name", "description", "enabled", "origin", "label",
        "editable", "hasExtraFiles", "problems",
    }
    assert LocalSkillState.__optional_keys__ == {
        "path", "overridesPlatform", "key", "overridden"
    }
    recorder = OpRecorder(snapshot)
    recorder.set(["skills"], empty_skills_state())
    batch = recorder.flush()
    assert batch is not None and batch["ops"][0]["path"] == ["skills"]


def test_reducer_and_golden_ops_exist() -> None:
    from talk2view._reducer import TurnReducer

    assert callable(getattr(TurnReducer, "reduce", None))
    # The skills slice is seeded into this golden file too.
    assert (Path(__file__).parent / "golden" / "controller_ops.json").is_file()
