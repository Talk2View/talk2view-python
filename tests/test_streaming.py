"""Tests for SSE stream parser."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import httpx

from talk2view.sessions import T2VSession
from talk2view.streaming import parse_sse_stream
from talk2view.tools import T2VTools
from talk2view.types import ChatCompletionChunk, ChatEvent


def _sse_line(data: Any) -> str:
    """Build a single SSE data line."""
    return "data: " + json.dumps(data) + "\n\n"


def _make_response(sse_text: str) -> MagicMock:
    """Create a mock httpx.Response that yields sse_text as strings."""
    response = MagicMock()
    response.iter_text.return_value = [sse_text]
    return response


def _collect(sse_text: str) -> List[ChatCompletionChunk]:
    response = _make_response(sse_text)
    return list(parse_sse_stream(response))


def _text_chunk(
    chunk_id: str, content: str, thread_id: str = "t1"
) -> Dict[str, Any]:
    return {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "gpt-4.1-mini",
        "choices": [
            {"index": 0, "delta": {"content": content}, "finish_reason": None}
        ],
        "thread_id": thread_id,
    }


class TestParseSSEStream:
    def test_text_chunks(self) -> None:
        sse = (
            _sse_line(_text_chunk("c1", "Hello"))
            + _sse_line(_text_chunk("c2", " world"))
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 2
        assert chunks[0].choices[0].delta.content == "Hello"
        assert chunks[1].choices[0].delta.content == " world"
        assert chunks[0].thread_id == "t1"

    def test_done_terminates(self) -> None:
        sse = (
            _sse_line(_text_chunk("c1", "hi"))
            + "data: [DONE]\n\n"
            + _sse_line(_text_chunk("c2", "should not appear"))
        )
        chunks = _collect(sse)
        assert len(chunks) == 1

    def test_tool_call_interrupt(self) -> None:
        chunk = {
            "id": "c1",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
            ],
            "interrupt": {
                "type": "tool_call",
                "tool_name": "create_shape",
                "tool_call_id": "call_123",
                "arguments": {"shape": "cube", "size": 10},
            },
        }
        sse = _sse_line(chunk) + "data: [DONE]\n\n"
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].interrupt is not None
        assert chunks[0].interrupt.tool_name == "create_shape"
        assert chunks[0].interrupt.tool_call_id == "call_123"
        assert chunks[0].interrupt.arguments == {"shape": "cube", "size": 10}

    def test_status_chunk(self) -> None:
        chunk = {
            "status": {
                "type": "thinking",
                "message": "Planning shape creation...",
            }
        }
        sse = _sse_line(chunk) + "data: [DONE]\n\n"
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].status is not None
        assert chunks[0].status.type == "thinking"
        assert chunks[0].status.message == "Planning shape creation..."

    def test_todos_chunk(self) -> None:
        chunk = {"todos": "1. Create cube\n2. Apply color"}
        sse = _sse_line(chunk) + "data: [DONE]\n\n"
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].todos == "1. Create cube\n2. Apply color"

    def test_stop_finish_reason(self) -> None:
        chunk = {
            "id": "c1",
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ],
            "thread_id": "t1",
        }
        sse = _sse_line(chunk) + "data: [DONE]\n\n"
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].choices[0].finish_reason == "stop"

    def test_malformed_json_skipped(self) -> None:
        sse = (
            "data: {bad json}\n\n"
            + _sse_line({"id": "c1", "choices": []})
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].id == "c1"

    def test_empty_lines_ignored(self) -> None:
        sse = (
            "\n\n\n"
            + _sse_line({"id": "c1", "choices": []})
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 1

    def test_split_text_chunks(self) -> None:
        """Test parser handles data split across text chunks."""
        response = MagicMock()
        full = _sse_line(_text_chunk("c1", "hi")) + "data: [DONE]\n\n"
        mid = len(full) // 2
        response.iter_text.return_value = [full[:mid], full[mid:]]

        chunks = list(parse_sse_stream(response))
        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content == "hi"


def _real_response(parts: List[bytes]) -> httpx.Response:
    """A genuine streaming httpx.Response fed from ``parts``, one read each."""
    return httpx.Response(200, content=iter(parts))


def _heartbeat_chunk() -> Dict[str, Any]:
    # format_heartbeat_chunk(): exclude_none, so delta is {} and nothing else rides along.
    return {
        "id": "hb",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {}}],
        "thread_id": "t1",
    }


def _stop_chunk() -> Dict[str, Any]:
    return {
        "id": "c9",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "thread_id": "t1",
    }


def _session_events(sse_text: str) -> List[ChatEvent]:
    """Run parsed chunks through a real T2VSession with a stubbed client."""
    client = MagicMock()
    client.stream_request.return_value = iter(_collect(sse_text))
    tools = MagicMock(spec=T2VTools)
    tools.has_handler.return_value = False
    session = T2VSession("ses_1", "thr_1", "m", client, tools)
    return list(session.send_message("hi"))


class TestSSEHardening:
    def test_data_prefix_without_space(self) -> None:
        sse = (
            "data:" + json.dumps(_text_chunk("c1", "Hello")) + "\n\n"
            + "data:[DONE]\n\n"
            + "data:" + json.dumps(_text_chunk("c2", "after done")) + "\n\n"
        )
        chunks = _collect(sse)
        assert [c.choices[0].delta.content for c in chunks] == ["Hello"]

    def test_preamble_comment_ignored(self) -> None:
        sse = (
            ": t2v stream open\n\n"
            + "event: message\nid: 7\n"
            + _sse_line(_text_chunk("c1", "Hello"))
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content == "Hello"

    def test_heartbeat_chunk_yields_no_event(self) -> None:
        sse = (
            ": t2v stream open\n\n"
            + _sse_line(_heartbeat_chunk())
            + _sse_line(_heartbeat_chunk())
            + _sse_line(_stop_chunk())
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 3
        assert chunks[0].choices[0].delta.content is None
        assert chunks[0].choices[0].finish_reason is None

        events = _session_events(sse)
        assert [e.type for e in events] == ["done"]

    def test_error_chunk_parsed_and_not_text(self) -> None:
        # format_error_chunk(): message mirrored into delta.content + error field.
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [
                {"index": 0, "delta": {"content": "An error occurred."}, "finish_reason": "stop"}
            ],
            "thread_id": "t1",
            "error": {
                "type": "budget_exceeded",
                "message": "An error occurred.",
                "detail": "BudgetExceededError('over')",
            },
        }
        sse = _sse_line(chunk) + "data: [DONE]\n\n"

        chunks = _collect(sse)
        assert chunks[0].error is not None
        assert chunks[0].error.type == "budget_exceeded"
        assert chunks[0].error.message == "An error occurred."
        assert chunks[0].error.detail == "BudgetExceededError('over')"

        events = _session_events(sse)
        assert [e.type for e in events] == ["error", "done"]
        assert events[0].message == "An error occurred."
        assert events[0].error_type == "budget_exceeded"
        assert events[0].detail == "BudgetExceededError('over')"
        assert all(e.type != "text" for e in events)

    def test_approval_chunk(self) -> None:
        chunk = {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "thread_id": "t1",
            "interrupt": None,
            "approval": {
                "type": "approval_request",
                "tool_name": "send_email",
                "tool_call_id": "call_9",
                "arguments": {"to": "a@b.com"},
                "summary": "Send an email to a@b.com",
                "details": [{"label": "To", "value": "a@b.com"}],
            },
        }
        chunks = _collect(_sse_line(chunk))
        assert len(chunks) == 1
        approval = chunks[0].approval
        assert approval is not None
        assert approval.type == "approval_request"
        assert approval.tool_name == "send_email"
        assert approval.tool_call_id == "call_9"
        assert approval.arguments == {"to": "a@b.com"}
        assert approval.summary == "Send an email to a@b.com"
        assert [(d.label, d.value) for d in approval.details] == [("To", "a@b.com")]
        assert chunks[0].interrupt is None

    def test_tool_event_absent_fields_omitted(self) -> None:
        start = {
            "id": "c1",
            "choices": [{"index": 0, "delta": {}}],
            "tool_event": {
                "type": "tool_start",
                "tool_name": "web_search",
                "tool_call_id": "call_1",
                "arguments": {"query": "slicer"},
            },
        }
        end = {
            "id": "c2",
            "choices": [{"index": 0, "delta": {}}],
            "tool_event": {
                "type": "tool_end",
                "tool_name": "web_search",
                "tool_call_id": "call_1",
                "status": "ok",
            },
        }
        chunks = _collect(_sse_line(start) + _sse_line(end) + "data: [DONE]\n\n")

        first, second = chunks[0].tool_event, chunks[1].tool_event
        assert first is not None and second is not None
        assert first.status is None and first.display is None
        assert second.arguments is None and second.display is None
        assert first.to_dict() == {
            "type": "tool_start",
            "tool_name": "web_search",
            "tool_call_id": "call_1",
            "arguments": {"query": "slicer"},
        }
        assert second.to_dict() == {
            "type": "tool_end",
            "tool_name": "web_search",
            "tool_call_id": "call_1",
            "status": "ok",
        }

    def test_stream_ends_without_DONE_after_interrupt(self) -> None:  # noqa: N802
        chunk = {
            "id": "c1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "interrupt": {
                "type": "tool_call",
                "tool_name": "create_shape",
                "tool_call_id": "call_123",
                "arguments": {"shape": "cube"},
            },
        }
        response = _real_response([(": t2v stream open\n\n" + _sse_line(chunk)).encode()])

        chunks = list(parse_sse_stream(response))

        assert len(chunks) == 1
        assert chunks[0].interrupt is not None
        assert chunks[0].interrupt.tool_call_id == "call_123"

    def test_multibyte_split_across_reads(self) -> None:
        text = "café — \U0001f9e0"
        # json.dumps escapes non-ASCII by default; put real UTF-8 bytes on the wire.
        raw = (
            "data: "
            + json.dumps(_text_chunk("c1", text), ensure_ascii=False)
            + "\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        cut = raw.index("\U0001f9e0".encode("utf-8")) + 2  # inside the 4-byte character
        response = _real_response([raw[:cut], raw[cut:]])

        chunks = list(parse_sse_stream(response))

        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content == text

    def test_trailing_line_without_newline_flushed(self) -> None:
        sse = _sse_line(_text_chunk("c1", "one")) + "data: " + json.dumps(
            _text_chunk("c2", "two")
        )
        chunks = _collect(sse)
        assert [c.choices[0].delta.content for c in chunks] == ["one", "two"]

    def test_trailing_done_without_newline_is_not_a_chunk(self) -> None:
        chunks = _collect(_sse_line(_text_chunk("c1", "one")) + "data: [DONE]")
        assert [c.choices[0].delta.content for c in chunks] == ["one"]

    def test_non_object_json_and_null_delta_are_tolerated(self) -> None:
        sse = (
            "data: 5\n\n"
            + "data: [1, 2]\n\n"
            + _sse_line({"id": "c1", "choices": [{"index": 0, "delta": None}]})
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert len(chunks) == 1
        assert chunks[0].choices[0].delta.content is None

    def test_a_chunk_with_no_choices_key_is_read_for_its_other_fields(self) -> None:
        # TS guards a missing `choices` (sessions.ts:236); a status-only chunk
        # without the key must neither end the turn nor lose the text around it.
        status_only = {"id": "s1", "status": {"type": "thinking", "message": "Working"}}
        sse = (
            _sse_line(_text_chunk("c1", "Hel"))
            + _sse_line(status_only)
            + _sse_line(_text_chunk("c2", "lo"))
            + _sse_line(_stop_chunk())
            + "data: [DONE]\n\n"
        )
        chunks = _collect(sse)
        assert chunks[1].choices == [] and chunks[1].status is not None
        events = _session_events(sse)
        assert [(e.type, e.content, e.status, e.message) for e in events] == [
            ("text", "Hel", None, None),
            ("status", None, "thinking", "Working"),
            ("text", "lo", None, None),
            ("done", None, None, None),
        ]

    def test_terminal_finish_reason_superset_kept(self) -> None:
        for reason in ("stop", "length", "content_filter"):
            chunk = {
                "id": "c1",
                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                "thread_id": "t1",
            }
            events = _session_events(_sse_line(chunk))
            assert [e.type for e in events] == ["done"], reason
