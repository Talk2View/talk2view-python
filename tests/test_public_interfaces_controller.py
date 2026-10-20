"""The client interfaces the chat controller is written against.

Every assertion names something `controller.py` or `test_contract.py` relies on. If one fails,
the client shipped a different name or signature: reconcile (usually by editing
`ChatController._open_chat_stream` / `_open_approval_stream`, the only two call sites).
"""

from __future__ import annotations

import dataclasses
import inspect
import threading

import pytest

import talk2view
from talk2view import Talk2View
from talk2view.cancel import Cancelled, CancelToken
from talk2view.dispatch import InlineDispatcher, QueueDispatcher, run_on_main
from talk2view.errors import (
    SESSION_LOST_AFTER_DENY_MESSAGE,
    SESSION_LOST_MESSAGE,
    SessionError,
    T2VError,
    error_for_type,
    is_session_gone,
)
from talk2view.sessions import build_user_content
from talk2view.tools import strip_null_args
from talk2view.types import Capabilities, ChatEvent, Decision, ToolEvent


def _params(fn: object) -> "dict[str, inspect.Parameter]":
    return dict(inspect.signature(fn).parameters)  # type: ignore[arg-type]


class TestCancel:
    def test_cancel_token(self) -> None:
        token = CancelToken()
        closed = []
        unregister = token.on_cancel(lambda: closed.append("a"))
        assert callable(unregister)
        assert token.cancelled is False
        token.cancel()
        assert token.cancelled is True
        assert closed == ["a"]
        assert issubclass(Cancelled, Exception)


class TestDispatch:
    def test_inline_dispatcher_runs_immediately(self) -> None:
        ran = []
        dispatcher = InlineDispatcher()
        dispatcher.post(lambda: ran.append(1))
        assert ran == [1]
        assert dispatcher.is_main_thread() is True
        assert run_on_main(dispatcher, lambda: 41 + 1) == 42

    def test_queue_dispatcher_runs_on_pump_and_run_on_main_blocks_the_caller(self) -> None:
        dispatcher = QueueDispatcher()  # owner = this thread
        ran = []
        dispatcher.post(lambda: ran.append(threading.get_ident()))
        assert ran == []  # queued, even when posted from the owner thread
        assert dispatcher.pump() == 1
        assert ran == [threading.get_ident()]

        result = []
        worker = threading.Thread(
            target=lambda: result.append(run_on_main(dispatcher, threading.get_ident))
        )
        worker.start()
        while worker.is_alive():
            dispatcher.pump()
            worker.join(0.01)
        assert result == [threading.get_ident()]

    def test_run_on_main_accepts_a_cancel_token(self) -> None:
        assert "cancel" in _params(run_on_main)


class TestTypes:
    def test_decision(self) -> None:
        decision = Decision(action="deny", feedback="no", updated_input={"a": 1})
        assert (decision.action, decision.feedback, decision.updated_input) == (
            "deny",
            "no",
            {"a": 1},
        )
        assert Decision(action="once") == Decision(action="once")  # a value object
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.action = "once"  # type: ignore[misc]

    def test_capabilities_default_off(self) -> None:
        caps = Capabilities()
        assert (caps.approval, caps.tool_events, caps.reasoning, caps.image_generation) == (
            False,
            False,
            False,
            False,
        )
        assert (caps.timezone, caps.surface) == (None, None)

    def test_chat_event_fields(self) -> None:
        names = {f.name for f in dataclasses.fields(ChatEvent)}
        assert names >= {
            "type",
            "content",
            "tool_name",
            "tool_call_id",
            "arguments",
            "thread_id",
            "message",
            "status",
            "todos",
            "kind",
            "description",
            "details",
            "decision",
            "error_type",
            "detail",
            "tool_event",
        }
        assert ChatEvent(type="text", content="hi").tool_name is None  # all optional but type

    def test_tool_event_and_strip_null_args(self) -> None:
        event = ToolEvent(type="tool_end", tool_name="t", tool_call_id="c1", status="ok")
        assert (event.arguments, event.display) == (None, None)
        assert strip_null_args({"a": 1, "b": None, "c": {}, "d": []}) == {"a": 1, "d": []}


class TestErrors:
    def test_t2v_error_attributes(self) -> None:
        error = T2VError("msg", "not_found", 404, "some_code", "debug detail")
        assert (error.type, error.status_code, error.code, error.detail) == (
            "not_found",
            404,
            "some_code",
            "debug detail",
        )
        assert str(T2VError("single-arg form still works")) == "single-arg form still works"

    def test_is_session_gone_and_error_for_type(self) -> None:
        assert is_session_gone(T2VError("x", "not_found", 404)) is True
        assert is_session_gone(T2VError("x", "session_not_found", 404)) is True
        assert is_session_gone(T2VError("x", "tool_call_id_mismatch", 409)) is False
        assert is_session_gone(RuntimeError("x")) is False
        assert type(error_for_type("x", "session_not_found", 404)) is SessionError
        assert type(error_for_type("x", "not_found", 404)) is T2VError  # deliberately unmapped

    def test_session_lost_messages(self) -> None:
        assert SESSION_LOST_MESSAGE.startswith("The chat session ended before that step")
        assert SESSION_LOST_AFTER_DENY_MESSAGE.endswith("Send your message again.")


class TestTalk2View:
    def test_chat_signature(self) -> None:
        params = _params(Talk2View.chat)
        assert list(params) == [
            "self",
            "message",
            "system_prompt",
            "history",
            "attachments",
            "cancel",
        ]
        assert params["cancel"].default is None

    def test_respond_to_approval_signature(self) -> None:
        params = _params(Talk2View.respond_to_approval)
        assert list(params) == ["self", "approval", "decision", "cancel"]
        assert params["cancel"].default is None

    def test_constructor_takes_dispatcher_and_capabilities(self) -> None:
        dispatcher = QueueDispatcher()
        with Talk2View(
            partner_key="pk_test_gate",
            dispatcher=dispatcher,
            capabilities=Capabilities(tool_events=True),
        ) as t2v:
            assert t2v.tools.dispatcher is dispatcher
            assert callable(t2v.clear_session)
            assert callable(t2v.stop)
            assert callable(t2v.tools.handle)
        with Talk2View(partner_key="pk_test_gate") as plain:
            assert isinstance(plain.tools.dispatcher, InlineDispatcher)
        assert build_user_content("hi") == "hi"
        for name in ("Decision", "Capabilities", "CancelToken", "Cancelled"):
            assert hasattr(talk2view, name), name
