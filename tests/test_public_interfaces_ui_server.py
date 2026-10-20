"""The core names ``talk2view.ui`` is written against.

If one of these fails, the core shipped a different shape: reconcile ``talk2view.ui``
against it. Besides the ``talk2view.ui`` package itself, the UI server needs one read-only
property on the core, ``ChatController.t2v``.
"""

from __future__ import annotations

import dataclasses
import inspect

from talk2view import (
    AnonymousUnavailableError,
    Attachment,
    ChatController,
    ControllerBusyError,
    ControllerClosedError,
    Decision,
    Model,
    PartnerConfig,
    SignupOutcome,
    SkillValidationError,
    T2VError,
    Talk2View,
    is_allowed_attachment_type,
)
from talk2view.dispatch import QueueDispatcher
from talk2view.preferences import T2VPreferences, UserPreferences
from talk2view.skills import T2VSkills
from talk2view.state import OpRecorder, _apply_op, apply_batch, initial_snapshot


def _params(fn: object) -> dict:  # type: ignore[type-arg]
    return dict(inspect.signature(fn).parameters)  # type: ignore[arg-type]


def test_controller_surface_the_router_and_hub_call() -> None:
    for name in (
        "snapshot",
        "subscribe",
        "send",
        "cancel",
        "approve",
        "retry",
        "edit",
        "clear",
        "clear_error",
        "close",
        "wait_idle",
        "set_skills_state",
    ):
        assert callable(getattr(ChatController, name, None)), name
    keyword_only = inspect.Parameter.KEYWORD_ONLY
    assert _params(ChatController.subscribe)["deliver_on_main"].kind is keyword_only
    assert _params(ChatController.approve)["tool_call_id"].kind is keyword_only
    assert list(_params(ChatController.send)) == ["self", "text", "attachments"]
    assert list(_params(ChatController.edit)) == ["self", "message_id", "text"]
    assert issubclass(ControllerBusyError, RuntimeError)
    assert issubclass(ControllerClosedError, RuntimeError)


def test_state_helpers_the_hub_applies_ops_with() -> None:
    recorder = OpRecorder(initial_snapshot())
    recorder.set(["messages", 0], {"id": "m1", "role": "user", "content": "hi", "timestamp": "t"})
    recorder.append_text(["messages", 0, "content"], "!")
    batch = recorder.flush()
    assert batch is not None and batch["seq"] == 1
    state = apply_batch(initial_snapshot(), batch)
    assert state["messages"][0]["content"] == "hi!"
    root: dict = {"a": []}  # type: ignore[type-arg]
    _apply_op(root, {"type": "set", "path": ["a", 0], "value": 1})
    assert root == {"a": [1]}


def test_talk2view_surface_app_state_and_router_call() -> None:
    for name in (
        "get_config",
        "list_models",
        "set_chat_defaults",
        "upload_attachment",
        "warm_up",
        "close",
    ):
        assert callable(getattr(Talk2View, name, None)), name
    auth = Talk2View("pk_gate").auth
    for name in (
        "login",
        "signup",
        "logout",
        "start_anonymous",
        "get_user",
        "is_authenticated",
        "is_anonymous",
        "on_auth_state_change",
    ):
        assert callable(getattr(auth, name, None)), name
    for name in ("get", "update", "on_change"):
        assert callable(getattr(T2VPreferences, name, None)), name
    for name in ("set_enabled", "list_platform", "state", "on_change"):
        assert callable(getattr(T2VSkills, name, None)), name
    assert set(_params(T2VPreferences.update)) >= {"model", "font_size"}


def test_types_the_wire_is_built_from() -> None:
    assert [f.name for f in dataclasses.fields(Attachment)] == [
        "id",
        "filename",
        "mime_type",
        "size_bytes",
    ]
    assert {f.name for f in dataclasses.fields(PartnerConfig)} == {
        "default_llm_model",
        "default_stt_model",
        "default_image_model",
        "system_prompt",
        "allowed_llm_models",
        "allowed_stt_models",
        "gated_llm_models",
        "gated_stt_models",
        "web_search_enabled",
    }
    assert {f.name for f in dataclasses.fields(Model)} == {
        "id",
        "object",
        "created",
        "owned_by",
        "name",
    }
    assert {f.name for f in dataclasses.fields(UserPreferences)} == {
        "model",
        "stt_model",
        "stt_language",
        "font_size",
    }
    assert Decision("once").approved and not Decision("deny").approved
    assert {f.name for f in dataclasses.fields(SignupOutcome)} == {
        "user",
        "confirmation_required",
    }
    assert issubclass(SkillValidationError, ValueError)
    assert issubclass(AnonymousUnavailableError, T2VError)
    assert is_allowed_attachment_type("image/png")
    assert not is_allowed_attachment_type("text/html")
    dispatcher = QueueDispatcher()
    ran: list = []  # type: ignore[type-arg]
    dispatcher.post(lambda: ran.append(1))
    assert dispatcher.pump() == 1 and ran == [1]
