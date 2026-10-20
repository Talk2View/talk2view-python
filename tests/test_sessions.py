"""Tests for T2VSession."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from talk2view.cancel import CancelToken
from talk2view.sessions import T2VSession
from talk2view.tools import T2VTools
from talk2view.types import (
    AgentStatus,
    ApprovalDetail,
    ApprovalRequest,
    Capabilities,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatEvent,
    Decision,
    PermissionCheck,
    StreamError,
    ToolCallInterrupt,
    ToolEvent,
)


def _make_session(
    stream_chunks: List[ChatCompletionChunk],
    tool_handlers: Dict[str, callable] = None,  # type: ignore[type-arg]
) -> tuple:  # type: ignore[type-arg]
    """Create a T2VSession with mocked client and optional tool handlers."""
    client = MagicMock()
    tools = MagicMock(spec=T2VTools)

    tools.check_permission.return_value = PermissionCheck(action="allow")
    tools.get_description.return_value = ""
    if tool_handlers:
        tools.has_handler.side_effect = lambda name: name in tool_handlers
        tools.execute_tool_call.side_effect = lambda name, args, cancel=None: (
            tool_handlers[name](**args) if name in tool_handlers else ("unknown", True)
        )
    else:
        tools.has_handler.return_value = False

    # Make stream_request return an iterator of chunks
    client.stream_request.return_value = iter(stream_chunks)

    session = T2VSession(
        session_id="ses_123",
        thread_id="thr_456",
        model="gpt-4.1-mini",
        client=client,
        tools=tools,
    )
    return session, client, tools


class TestT2VSession:
    def test_send_text_message(self) -> None:
        chunks = [
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content="Hello"))],
            ),
            ChatCompletionChunk(
                id="c2",
                choices=[ChatCompletionChunkChoice(
                    delta=ChatCompletionChunkDelta(content=" world"),
                )],
            ),
            ChatCompletionChunk(
                id="c3",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="thr_456",
            ),
        ]

        session, client, _ = _make_session(chunks)
        events = list(session.send_message("Hi"))

        assert len(events) == 3
        assert events[0] == ChatEvent(type="text", content="Hello")
        assert events[1] == ChatEvent(type="text", content=" world")
        assert events[2].type == "done"
        assert events[2].thread_id == "thr_456"

    def test_send_with_system_prompt(self) -> None:
        chunks = [
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="t1",
            ),
        ]

        session, client, _ = _make_session(chunks)
        list(session.send_message("Hello", system_prompt="Be helpful"))

        # Check that system prompt was included in the request
        call_args = client.stream_request.call_args
        body = call_args[0][1]
        assert body["messages"][0] == {"role": "system", "content": "Be helpful"}
        assert body["messages"][1] == {"role": "user", "content": "Hello"}

    def test_status_event(self) -> None:
        chunks = [
            ChatCompletionChunk(
                status=AgentStatus(type="thinking", message="Planning..."),
            ),
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="t1",
            ),
        ]

        session, _, _ = _make_session(chunks)
        events = list(session.send_message("Hello"))

        assert events[0].type == "status"
        assert events[0].status == "thinking"
        assert events[0].message == "Planning..."

    def test_todos_event(self) -> None:
        chunks = [
            ChatCompletionChunk(todos="1. Do thing\n2. Do other"),
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="t1",
            ),
        ]

        session, _, _ = _make_session(chunks)
        events = list(session.send_message("Plan something"))

        assert events[0].type == "todos"
        assert events[0].todos == "1. Do thing\n2. Do other"

    def test_tool_call_no_handler(self) -> None:
        """When no handler is registered, emit tool_call event and stop."""
        chunks = [
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(finish_reason="tool_calls")],
                interrupt=ToolCallInterrupt(
                    type="tool_call",
                    tool_name="create_shape",
                    tool_call_id="call_123",
                    arguments={"shape": "cube"},
                ),
            ),
        ]

        session, _, _ = _make_session(chunks)
        events = list(session.send_message("Make a cube"))

        assert len(events) == 1
        assert events[0].type == "tool_call"
        assert events[0].tool_name == "create_shape"
        assert events[0].tool_call_id == "call_123"
        assert events[0].arguments == {"shape": "cube"}

    def test_tool_call_with_handler_auto_resume(self) -> None:
        """When handler exists, execute tool and resume automatically."""
        # First stream: text + interrupt
        first_chunks = [
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(
                    delta=ChatCompletionChunkDelta(content="Let me "),
                )],
            ),
            ChatCompletionChunk(
                id="c2",
                choices=[ChatCompletionChunkChoice(finish_reason="tool_calls")],
                interrupt=ToolCallInterrupt(
                    type="tool_call",
                    tool_name="create_shape",
                    tool_call_id="call_123",
                    arguments={"shape": "cube"},
                ),
            ),
        ]
        # Resume stream: more text + done
        resume_chunks = [
            ChatCompletionChunk(
                id="c3",
                choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content="Created!"))],
            ),
            ChatCompletionChunk(
                id="c4",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="t1",
            ),
        ]

        client = MagicMock()
        tools = MagicMock(spec=T2VTools)
        tools.check_permission.return_value = PermissionCheck(action="allow")
        tools.has_handler.return_value = True
        tools.execute_tool_call.return_value = ('{"success": true}', False)

        # First call returns first_chunks, second call returns resume_chunks
        client.stream_request.side_effect = [iter(first_chunks), iter(resume_chunks)]

        session = T2VSession("ses_1", "thr_1", "gpt-4.1-mini", client, tools)
        events = list(session.send_message("Make a cube"))

        # Should have: text, tool_call, text, done
        assert events[0].type == "text"
        assert events[0].content == "Let me "
        assert events[1].type == "tool_call"
        assert events[1].tool_name == "create_shape"
        assert events[2].type == "text"
        assert events[2].content == "Created!"
        assert events[3].type == "done"

        # Verify resume was called
        resume_call = client.stream_request.call_args_list[1]
        assert "/resume" in resume_call[0][0]
        resume_body = resume_call[0][1]
        assert resume_body["tool_call_id"] == "call_123"
        assert resume_body["result"] == '{"success": true}'
        assert resume_body["is_error"] is False

    def test_resume_tool_call_manual(self) -> None:
        chunks = [
            ChatCompletionChunk(
                id="c1",
                choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content="Done"))],
            ),
            ChatCompletionChunk(
                id="c2",
                choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                thread_id="t1",
            ),
        ]

        session, client, _ = _make_session(chunks)
        events = list(session.resume_tool_call("call_123", '{"ok":true}'))

        assert len(events) == 2
        assert events[0].type == "text"

        call_args = client.stream_request.call_args
        assert "/resume" in call_args[0][0]
        body = call_args[0][1]
        assert body["tool_call_id"] == "call_123"
        assert body["is_error"] is False


def _stop() -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="stop",
        choices=[ChatCompletionChunkChoice(finish_reason="stop")],
        thread_id="thr_456",
    )


def _interrupt(tool_name: str, tool_call_id: str, arguments: Dict[str, Any]) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="int",
        choices=[ChatCompletionChunkChoice(finish_reason="tool_calls")],
        interrupt=ToolCallInterrupt(
            type="tool_call",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
        ),
    )


def _flow_session(
    streams: List[List[ChatCompletionChunk]],
    capabilities: Optional[Capabilities] = None,
) -> tuple:  # type: ignore[type-arg]
    """A session whose client serves one chunk list per stream_request call."""
    client = MagicMock()
    client.stream_request.side_effect = [iter(chunks) for chunks in streams]
    tools = MagicMock(spec=T2VTools)
    tools.check_permission.return_value = PermissionCheck(action="allow")
    tools.get_description.return_value = "Delete a volume from the scene."
    tools.has_handler.return_value = True
    tools.execute_tool_call.return_value = ("deleted", False)
    session = T2VSession(
        session_id="ses_123",
        thread_id="thr_456",
        model="m",
        client=client,
        tools=tools,
        capabilities=capabilities,
    )
    return session, client, tools


def _body(client: MagicMock, call: int) -> Dict[str, Any]:
    return client.stream_request.call_args_list[call][0][1]  # type: ignore[no-any-return]


class TestCapabilityFlags:
    def test_flags_omitted_by_default(self) -> None:
        session, client, _ = _flow_session(
            [[_interrupt("delete_volume", "tc1", {"name": "CT"})], [_stop()]]
        )

        list(session.send_message("hi"))

        # Byte-compatible with SDK 0.1.0: same keys, same order, nothing added.
        assert json.dumps(_body(client, 0)) == (
            '{"messages": [{"role": "user", "content": "hi"}], "stream": true}'
        )
        assert json.dumps(_body(client, 1)) == (
            '{"tool_call_id": "tc1", "result": "deleted", "is_error": false}'
        )

    def test_flags_sent_on_messages_and_resume(self) -> None:
        caps = Capabilities(
            approval=True,
            tool_events=True,
            reasoning=True,
            image_generation=True,
            timezone="Australia/Perth",
            surface="app",
        )
        session, client, _ = _flow_session(
            [[_interrupt("delete_volume", "tc1", {"name": "CT"})], [_stop()]],
            capabilities=caps,
        )

        list(session.send_message("hi"))

        messages_body, resume_body = _body(client, 0), _body(client, 1)
        assert client.stream_request.call_args_list[1][0][0] == "/v1/sessions/ses_123/resume"
        shared = {
            "supports_tool_events": True,
            "supports_reasoning": True,
            "supports_image_generation": True,
            "timezone": "Australia/Perth",
            "surface": "app",
        }
        for key, value in shared.items():
            assert messages_body[key] == value, key
            assert resume_body[key] == value, key
        # supports_approval belongs to /messages only: the engine's
        # ResumeRequest has no such field (the run's state remembers it).
        assert messages_body["supports_approval"] is True
        assert "supports_approval" not in resume_body

    def test_only_the_enabled_flags_are_sent(self) -> None:
        session, client, _ = _flow_session([[_stop()]], capabilities=Capabilities(reasoning=True))

        list(session.send_message("hi"))

        assert _body(client, 0) == {
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "supports_reasoning": True,
        }


class TestClientToolApproval:
    def test_permission_true_pauses_with_approval_required(self) -> None:
        session, client, tools = _flow_session(
            [[_interrupt("delete_volume", "tc1", {"name": "CT"})]]
        )
        tools.check_permission.return_value = PermissionCheck(action="require_approval")

        events = list(session.send_message("delete CT"))

        assert events == [
            ChatEvent(
                type="approval_required",
                kind="client",
                tool_name="delete_volume",
                tool_call_id="tc1",
                arguments={"name": "CT"},
                description="Delete a volume from the scene.",
            )
        ]
        tools.execute_tool_call.assert_not_called()
        assert client.stream_request.call_count == 1  # paused: nothing resumed

    def test_approve_once_runs_the_tool_with_updated_input(self) -> None:
        session, client, tools = _flow_session([[_stop()]])
        approval = ChatEvent(
            type="approval_required",
            kind="client",
            tool_name="delete_volume",
            tool_call_id="tc1",
            arguments={"name": "CT"},
        )

        events = list(
            session.respond_to_approval(
                approval, Decision("once", updated_input={"name": "CT_copy"})
            )
        )

        assert [e.type for e in events] == ["approval_result", "done"]
        assert events[0] == ChatEvent(
            type="approval_result", tool_name="delete_volume", tool_call_id="tc1", decision="once"
        )
        tools.execute_tool_call.assert_called_once_with("delete_volume", {"name": "CT_copy"}, None)
        assert _body(client, 0) == {"tool_call_id": "tc1", "result": "deleted", "is_error": False}

    def test_deny_resumes_with_is_error_and_feedback(self) -> None:
        session, client, tools = _flow_session([[_stop()]])
        approval = ChatEvent(
            type="approval_required",
            kind="client",
            tool_name="delete_volume",
            tool_call_id="tc1",
            arguments={"name": "CT"},
        )

        events = list(
            session.respond_to_approval(approval, Decision("deny", feedback="Hide it instead."))
        )

        assert events[0] == ChatEvent(
            type="approval_result", tool_name="delete_volume", tool_call_id="tc1", decision="deny"
        )
        tools.execute_tool_call.assert_not_called()
        assert client.stream_request.call_args[0][0] == "/v1/sessions/ses_123/resume"
        assert _body(client, 0) == {
            "tool_call_id": "tc1",
            "result": "User denied this action. Hide it instead.",
            "is_error": True,
        }

    def test_deny_without_feedback_has_no_trailing_space(self) -> None:
        session, client, _ = _flow_session([[_stop()]])
        approval = ChatEvent(
            type="approval_required", kind="client", tool_name="t", tool_call_id="tc1"
        )

        list(session.respond_to_approval(approval, Decision("deny")))

        assert _body(client, 0)["result"] == "User denied this action."

    def test_programmatic_deny_message_format(self) -> None:
        session, client, tools = _flow_session(
            [
                [_interrupt("save_scene", "tc1", {})],
                [_interrupt("save_scene", "tc2", {})],
                [_stop()],
            ]
        )
        tools.check_permission.side_effect = [
            PermissionCheck(action="deny", message="read-only mode"),
            PermissionCheck(action="deny"),
        ]

        events = list(session.send_message("save"))

        # A programmatic denial runs nothing and emits no tool_call event.
        assert [e.type for e in events] == ["done"]
        tools.execute_tool_call.assert_not_called()
        assert _body(client, 1) == {
            "tool_call_id": "tc1",
            "result": "Permission denied: read-only mode",
            "is_error": True,
        }
        assert _body(client, 2)["result"] == "Permission denied: Tool not allowed"

    def test_an_unknown_check_action_denies_instead_of_running_the_tool(self) -> None:
        """Fail closed: only "allow" runs. A typo, a stale action
        name or an un-normalised UI string must deny, not execute."""
        session, client, tools = _flow_session(
            [[_interrupt("delete_volume", "tc1", {"name": "CT"})], [_stop()]]
        )
        tools.check_permission.return_value = PermissionCheck(action="Deny")

        events = list(session.send_message("delete CT"))

        assert [e.type for e in events] == ["done"]
        tools.execute_tool_call.assert_not_called()
        assert _body(client, 1) == {
            "tool_call_id": "tc1",
            "result": "Permission denied: Tool not allowed",
            "is_error": True,
        }

    def test_permission_callback_updated_input_reaches_the_handler(self) -> None:
        session, _, tools = _flow_session(
            [[_interrupt("open_file", "tc1", {"path": "a"})], [_stop()]]
        )
        tools.check_permission.return_value = PermissionCheck(
            action="allow", updated_input={"path": "a", "read_only": True}
        )

        events = list(session.send_message("open a"))

        assert events[0].type == "tool_call"
        assert events[0].arguments == {"path": "a", "read_only": True}
        tools.execute_tool_call.assert_called_once_with(
            "open_file", {"path": "a", "read_only": True}, None
        )

    def test_respond_to_approval_rejects_other_events(self) -> None:
        session, _, _ = _flow_session([])

        with pytest.raises(ValueError, match="approval_required"):
            list(session.respond_to_approval(ChatEvent(type="text", content="x"), Decision("once")))

    def test_unrecognised_action_denies_client_tool_and_does_not_run_handler(self) -> None:
        session, client, tools = _flow_session([[_stop()]])
        approval = ChatEvent(
            type="approval_required",
            kind="client",
            tool_name="delete_volume",
            tool_call_id="tc1",
            arguments={"name": "CT"},
        )

        events = list(
            session.respond_to_approval(approval, Decision(action="denied"))  # type: ignore[arg-type]
        )

        assert events[0] == ChatEvent(
            type="approval_result", tool_name="delete_volume", tool_call_id="tc1", decision="deny"
        )
        tools.execute_tool_call.assert_not_called()
        assert _body(client, 0)["is_error"] is True


class TestEngineApproval:
    def _approval_chunk(self) -> ChatCompletionChunk:
        return ChatCompletionChunk(
            id="apr",
            choices=[ChatCompletionChunkChoice(finish_reason="tool_calls")],
            approval=ApprovalRequest(
                type="approval_request",
                tool_name="send_email",
                tool_call_id="tc9",
                arguments={"to": "a@b.com"},
                summary="Send an email to a@b.com",
                details=[ApprovalDetail(label="To", value="a@b.com")],
            ),
        )

    def test_engine_approval_chunk_pauses_with_approval_required(self) -> None:
        session, client, tools = _flow_session([[self._approval_chunk()]])

        events = list(session.send_message("email them"))

        assert events == [
            ChatEvent(
                type="approval_required",
                kind="engine",
                tool_name="send_email",
                tool_call_id="tc9",
                arguments={"to": "a@b.com"},
                description="Send an email to a@b.com",
                details=[{"label": "To", "value": "a@b.com"}],
            )
        ]
        tools.check_permission.assert_not_called()
        assert client.stream_request.call_count == 1

    def test_engine_approval_resumes_with_approved_bool_only(self) -> None:
        session, client, tools = _flow_session([[self._approval_chunk()], [_stop()], [_stop()]])
        approval = list(session.send_message("email them"))[0]

        events = list(session.respond_to_approval(approval, Decision("once")))
        list(session.respond_to_approval(approval, Decision("deny", feedback="not now")))

        assert [e.type for e in events] == ["approval_result", "done"]
        assert client.stream_request.call_args_list[1][0][0] == "/v1/sessions/ses_123/resume"
        # Exactly one of result/approved — the engine's validator rejects both.
        assert _body(client, 1) == {"tool_call_id": "tc9", "approved": True}
        assert _body(client, 2) == {"tool_call_id": "tc9", "approved": False}
        tools.execute_tool_call.assert_not_called()  # the ENGINE runs platform tools

    def test_capability_flags_ride_on_the_approved_resume_body(self) -> None:
        session, client, _ = _flow_session(
            [[self._approval_chunk()], [_stop()]],
            capabilities=Capabilities(tool_events=True),
        )
        approval = list(session.send_message("email them"))[0]

        list(session.respond_to_approval(approval, Decision("once")))

        assert _body(client, 1) == {
            "tool_call_id": "tc9",
            "approved": True,
            "supports_tool_events": True,
        }

    def test_unrecognised_action_denies_engine_approval(self) -> None:
        session, client, _ = _flow_session([[self._approval_chunk()], [_stop()]])
        approval = list(session.send_message("email them"))[0]

        events = list(
            session.respond_to_approval(approval, Decision(action="denied"))  # type: ignore[arg-type]
        )

        assert events[0] == ChatEvent(
            type="approval_result", tool_name="send_email", tool_call_id="tc9", decision="deny"
        )
        assert _body(client, 1) == {"tool_call_id": "tc9", "approved": False}


class TestNewEventTypes:
    def test_reasoning_and_tool_event_are_surfaced(self) -> None:
        tool_event = ToolEvent(
            type="tool_end", tool_name="web_search", tool_call_id="call_1", status="ok"
        )
        session, _, _ = _flow_session(
            [
                [
                    ChatCompletionChunk(
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(reasoning_content="Thinking")
                            )
                        ]
                    ),
                    ChatCompletionChunk(
                        choices=[ChatCompletionChunkChoice()], tool_event=tool_event
                    ),
                    ChatCompletionChunk(
                        choices=[
                            ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content="Hi"))
                        ]
                    ),
                    _stop(),
                ]
            ]
        )

        events = list(session.send_message("hi"))

        assert [e.type for e in events] == ["reasoning", "tool_event", "text", "done"]
        assert events[0].content == "Thinking"
        assert events[1].tool_event is tool_event
        assert events[1].tool_name == "web_search"

    def test_error_chunk_is_an_error_event_then_done(self) -> None:
        session, _, _ = _flow_session(
            [
                [
                    ChatCompletionChunk(
                        choices=[
                            ChatCompletionChunkChoice(
                                delta=ChatCompletionChunkDelta(content="An error occurred."),
                                finish_reason="stop",
                            )
                        ],
                        error=StreamError(type="budget_exceeded", message="An error occurred."),
                    )
                ]
            ]
        )

        events = list(session.send_message("hi"))

        assert [e.type for e in events] == ["error", "done"]
        assert events[0].error_type == "budget_exceeded"

    def test_cancel_token_is_forwarded_to_stream_and_tools(self) -> None:
        session, client, tools = _flow_session(
            [[_interrupt("delete_volume", "tc1", {})], [_stop()]]
        )
        token = CancelToken()

        list(session.send_message("hi", cancel=token))

        assert all(c[1]["cancel"] is token for c in client.stream_request.call_args_list)
        tools.check_permission.assert_called_once_with("delete_volume", {}, token)
        tools.execute_tool_call.assert_called_once_with("delete_volume", {}, token)
