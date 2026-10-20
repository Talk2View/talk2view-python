"""TurnReducer: one event in, ops out. No controller, no threads, no locks.

The reducer is the half of the controller that knows what an event means. These tests drive
it directly on an ``OpRecorder`` and read the ops it records, so each rule is pinned without a
run loop around it. The end-to-end behaviour of the same rules is in ``test_controller_*``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from talk2view._reducer import DEMO_LIMIT_REACHED, TurnReducer, _Cursor, set_top
from talk2view.state import Op, OpRecorder, initial_snapshot
from talk2view.types import ChatEvent, ToolEvent

from .controller_fakes import (
    approval_result_chunk,
    engine_interrupt_chunk,
    error_chunk,
    interrupt_chunk,
    status_chunk,
    stop_chunk,
    text_chunk,
    todos_chunk,
    tool_call_chunk,
)


def _fresh(*, is_anonymous: bool = False) -> tuple[OpRecorder, TurnReducer, _Cursor]:
    rec = OpRecorder(initial_snapshot(is_anonymous=is_anonymous))
    reducer = TurnReducer(rec)
    cursor = _Cursor(assistant_id=reducer.append_assistant())
    rec.flush()  # the segment itself is not what these tests look at
    return rec, reducer, cursor


def _ops(rec: OpRecorder) -> List[Op]:
    batch = rec.flush()
    return [] if batch is None else batch["ops"]


def _tool_event(kind: str, call_id: Optional[str], **fields: Any) -> ChatEvent:
    return ChatEvent(
        type="tool_event",
        tool_name="web_search",
        tool_call_id=call_id,
        tool_event=ToolEvent(
            type=kind, tool_name="web_search", tool_call_id=call_id or "", **fields
        ),
    )


class TestText:
    def test_text_is_an_append_text_op_on_the_cursor_segment(self) -> None:
        rec, reducer, cursor = _fresh()
        assert reducer.reduce(text_chunk("Hel"), cursor) is None
        assert reducer.reduce(text_chunk("lo"), cursor) is None
        assert _ops(rec) == [
            {"type": "append-text", "path": ["messages", 0, "content"], "value": "Hel"},
            {"type": "append-text", "path": ["messages", 0, "content"], "value": "lo"},
        ]
        assert rec.state["messages"][0]["content"] == "Hello"

    def test_text_after_a_tool_call_opens_a_new_segment(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(tool_call_chunk("search", "call_1", {"q": "x"}), cursor)
        assert cursor.had_tool is True
        first = cursor.assistant_id
        rec.flush()

        reducer.reduce(text_chunk("Found"), cursor)

        assert cursor.had_tool is False
        assert cursor.assistant_id != first
        ops = _ops(rec)
        assert ops[0] == {"type": "set", "path": ["messages", 0, "isStreaming"], "value": False}
        assert ops[1]["path"] == ["messages", 1]  # the new segment, appended at len
        assert ops[1]["value"]["isStreaming"] is True
        assert ops[2] == {
            "type": "append-text",
            "path": ["messages", 1, "content"],
            "value": "Found",
        }

    def test_a_stale_cursor_records_nothing(self) -> None:
        """The segment a cursor names can be gone (a whole-list shrink from another path).
        Every handler that writes into it is then a no-op rather than an OpError."""
        rec, reducer, cursor = _fresh()
        cursor.assistant_id = "msg_gone"
        reducer.reduce(text_chunk("lost"), cursor)
        reducer.reduce(todos_chunk("- [ ] a"), cursor)
        reducer.reduce(tool_call_chunk("search", "call_1", {}), cursor)
        assert _ops(rec) == []


class TestTopLevelEvents:
    def test_status_todos_and_done(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(status_chunk("thinking", "..."), cursor)
        reducer.reduce(todos_chunk("- [ ] a"), cursor)
        reducer.reduce(stop_chunk("thread_9"), cursor)
        assert cursor.completed is True
        assert _ops(rec) == [
            {
                "type": "set",
                "path": ["agentStatus"],
                "value": {"type": "thinking", "message": "..."},
            },
            {"type": "set", "path": ["messages", 0, "plan"], "value": "- [ ] a"},
            {"type": "set", "path": ["threadId"], "value": "thread_9"},
        ]

    def test_an_unknown_event_type_is_ignored(self) -> None:
        rec, reducer, cursor = _fresh()
        assert reducer.reduce(ChatEvent(type="reasoning", content="hmm"), cursor) is None
        assert reducer.reduce(ChatEvent(type="reduce", content="x"), cursor) is None
        assert _ops(rec) == []

    def test_set_top_records_only_a_change(self) -> None:
        rec = OpRecorder(initial_snapshot())
        set_top(rec, "error", None)  # already None
        assert rec.pending is False
        set_top(rec, "error", "boom")
        assert _ops(rec) == [{"type": "set", "path": ["error"], "value": "boom"}]


class TestErrors:
    def test_an_error_marks_the_turn_failed_and_sets_error(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(error_chunk("Something went wrong"), cursor)
        assert cursor.turn_failed is True
        assert cursor.notices == []
        assert _ops(rec) == [{"type": "set", "path": ["error"], "value": "Something went wrong"}]

    def test_an_anonymous_budget_error_is_a_notice_not_an_error(self) -> None:
        rec, reducer, cursor = _fresh(is_anonymous=True)
        reducer.reduce(error_chunk("Budget exceeded", "budget_exceeded"), cursor)
        assert cursor.turn_failed is True
        assert cursor.notices == [DEMO_LIMIT_REACHED]
        assert _ops(rec) == []  # no raw error for the signup prompt to fight with

    def test_a_signed_in_budget_error_is_an_ordinary_error(self) -> None:
        rec, reducer, cursor = _fresh(is_anonymous=False)
        reducer.reduce(error_chunk("Budget exceeded", "budget_exceeded"), cursor)
        assert cursor.notices == []
        assert _ops(rec) == [{"type": "set", "path": ["error"], "value": "Budget exceeded"}]


class TestApprovals:
    def test_a_client_approval_pauses_with_the_card_set(self) -> None:
        rec, reducer, cursor = _fresh()
        event = interrupt_chunk("send_email", "call_1", {"to": "a"}, "Send")
        outcome = reducer.reduce(event, cursor)
        assert outcome is not None
        assert outcome.paused is True
        assert outcome.auto_approval is None
        assert outcome.assistant_id == cursor.assistant_id
        ops = _ops(rec)
        assert ops[0]["path"] == ["messages", 0, "steps"]
        assert ops[0]["value"] == [
            {
                "name": "send_email",
                "status": "running",
                "args": {"to": "a"},
                "toolCallId": "call_1",
                "source": "client",
            }
        ]
        assert ops[1] == {
            "type": "set",
            "path": ["pendingApproval"],
            "value": {
                "toolCallId": "call_1",
                "toolName": "send_email",
                "arguments": {"to": "a"},
                "description": "Send",
            },
        }

    def test_an_always_allowed_client_tool_is_handed_back_not_set(self) -> None:
        rec, reducer, cursor = _fresh()
        rec.set(["alwaysAllowedTools"], ["send_email"])
        rec.flush()
        outcome = reducer.reduce(interrupt_chunk("send_email", "call_1", {}, "Send"), cursor)
        assert outcome is not None
        assert outcome.auto_approval == {
            "toolCallId": "call_1",
            "toolName": "send_email",
            "arguments": {},
            "description": "Send",
        }
        assert [op["path"] for op in _ops(rec)] == [["messages", 0, "steps"]]  # no card

    def test_an_engine_approval_is_never_auto_approved_and_keeps_kind(self) -> None:
        rec, reducer, cursor = _fresh()
        rec.set(["alwaysAllowedTools"], ["send_email"])
        rec.flush()
        outcome = reducer.reduce(engine_interrupt_chunk("send_email", "call_2", {}, "Send"), cursor)
        assert outcome is not None
        assert outcome.auto_approval is None
        assert rec.state["pendingApproval"] == {
            "toolCallId": "call_2",
            "toolName": "send_email",
            "arguments": {},
            "description": "Send",
            "kind": "engine",
        }
        assert rec.state["messages"][0]["steps"][0]["source"] == "platform"

    def test_an_approval_result_marks_the_step_by_id(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(tool_call_chunk("write", "call_1", {}), cursor)
        reducer.reduce(interrupt_chunk("write", "call_2", {}, "Write"), cursor)
        rec.flush()

        reducer.reduce(approval_result_chunk("write", "deny", "call_2"), cursor)

        assert _ops(rec) == [
            {
                "type": "set",
                "path": ["messages", 0, "steps", 1],
                "value": {
                    "name": "write",
                    "status": "denied",
                    "args": {},
                    "toolCallId": "call_2",
                    "source": "client",
                },
            }
        ]
        assert cursor.had_tool is True

    def test_an_approval_result_without_an_id_falls_back_to_the_name(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(interrupt_chunk("write", "call_1", {}, "Write"), cursor)
        rec.flush()
        reducer.reduce(approval_result_chunk("write", "once"), cursor)
        assert rec.state["messages"][0]["steps"][0]["status"] == "used"

    @pytest.mark.parametrize("decision", ["denied", None, "", "approve"])
    def test_a_decision_this_sdk_cannot_read_marks_the_step_denied(
        self, decision: Optional[str]
    ) -> None:
        """Fail closed: only the literal ``once`` / ``always`` ran the call. Any
        other decision -- a near-miss spelling, an empty one, none at all -- is shown as
        denied, never as a call that ran."""
        rec, reducer, cursor = _fresh()
        reducer.reduce(interrupt_chunk("write", "call_1", {}, "Write"), cursor)
        rec.flush()
        event = ChatEvent(
            type="approval_result", tool_name="write", tool_call_id="call_1", decision=decision
        )
        reducer.reduce(event, cursor)
        assert rec.state["messages"][0]["steps"][0]["status"] == "denied"


class TestToolEvents:
    def test_start_then_end_is_one_step(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(_tool_event("tool_start", "call_1", arguments={"q": "x"}), cursor)
        reducer.reduce(
            _tool_event("tool_end", "call_1", status="ok", display={"kind": "results"}), cursor
        )
        steps = rec.state["messages"][0]["steps"]
        assert steps == [
            {
                "name": "web_search",
                "status": "used",
                "args": {"q": "x"},
                "toolCallId": "call_1",
                "source": "platform",
                "display": {"kind": "results"},
            }
        ]

    def test_an_end_without_a_start_is_recorded_as_finished(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(_tool_event("tool_end", "call_1", status="error"), cursor)
        assert rec.state["messages"][0]["steps"] == [
            {
                "name": "web_search",
                "toolCallId": "call_1",
                "source": "platform",
                "status": "denied",
                "result": "Tool failed",
            }
        ]

    def test_other_tool_event_kinds_and_a_missing_payload_are_ignored(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(_tool_event("tool_progress", "call_1"), cursor)
        reducer.reduce(ChatEvent(type="tool_event", tool_name="x", tool_call_id="c"), cursor)
        assert _ops(rec) == []


class TestFinalize:
    def test_finalize_returns_the_trailing_segments_text_oldest_first(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(text_chunk("one"), cursor)
        reducer.reduce(tool_call_chunk("search", "call_1", {}), cursor)
        reducer.reduce(text_chunk("two"), cursor)
        rec.set(["isLoading"], True)
        rec.set(["agentStatus"], {"type": "thinking", "message": "..."})
        rec.flush()

        parts = reducer.finalize(cursor.assistant_id)

        assert parts == ["one", "two"]
        assert _ops(rec) == [
            {"type": "set", "path": ["messages", 1, "isStreaming"], "value": False},
            {"type": "set", "path": ["agentStatus"], "value": None},
            {"type": "set", "path": ["isLoading"], "value": False},
        ]

    def test_finalize_drops_an_empty_trailing_segment_with_a_whole_list_set(self) -> None:
        rec, reducer, cursor = _fresh()
        parts = reducer.finalize(cursor.assistant_id)
        assert parts == []
        assert rec.state["messages"] == []
        ops = _ops(rec)
        assert ops[0] == {"type": "set", "path": ["messages", 0, "isStreaming"], "value": False}
        assert ops[1] == {"type": "set", "path": ["messages"], "value": []}

    def test_finalize_keeps_a_tools_only_segment(self) -> None:
        rec, reducer, cursor = _fresh()
        reducer.reduce(tool_call_chunk("search", "call_1", {}), cursor)
        assert reducer.finalize(cursor.assistant_id) == []
        assert len(rec.state["messages"]) == 1
        assert rec.state["messages"][0]["isStreaming"] is False


class TestIds:
    def test_ids_come_off_one_counter(self) -> None:
        rec = OpRecorder(initial_snapshot())
        reducer = TurnReducer(rec)
        a, b = reducer.next_message_id(), reducer.next_message_id()
        c = reducer.append_assistant()
        assert [x.split("_")[1] for x in (a, b, c)] == ["1", "2", "3"]
        message: Dict[str, Any] = rec.state["messages"][0]
        assert message["id"] == c
        assert message["isStreaming"] is True
