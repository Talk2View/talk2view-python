"""ChatController.send and stream consumption (ported from chat-state.test.ts › sendMessage)."""

from __future__ import annotations

import re
import threading
from typing import Callable

import pytest

from talk2view.controller import Capabilities, ControllerBusyError, ControllerClosedError
from talk2view.types import Attachment, ChatEvent, ChatMessage

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    Gate,
    make_controller,
    status_chunk,
    stop_chunk,
    text_chunk,
    todos_chunk,
    tool_call_chunk,
)


class TestSendMessage:
    def test_adds_user_and_assistant_messages_streams_text(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hello "), text_chunk("world"), stop_chunk()]]
        controller = make_controller(t2v)

        controller.send("Hi")

        messages = controller.snapshot()["messages"]
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hi"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Hello world"

    def test_sets_is_loading_during_streaming_and_clears_after(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("response"), stop_chunk()]]
        controller = make_controller(t2v)
        log = BatchLog()
        controller.subscribe(log)

        controller.send("test")

        loading = log.values("isLoading")
        assert True in loading
        assert loading[-1] is False
        assert controller.snapshot()["isLoading"] is False

    def test_emits_messages_change_events(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hi"), stop_chunk()]]
        controller = make_controller(t2v)
        log = BatchLog()
        controller.subscribe(log)

        controller.send("Hello")

        touching = [b for b in log.batches if any(op["path"][0] == "messages" for op in b["ops"])]
        # At least: initial add (user + assistant), text delta, finalize (isStreaming False).
        assert len(touching) >= 2

    def test_sets_thread_id_from_done_event(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("ok"), stop_chunk("thread_abc")]]
        controller = make_controller(t2v)
        controller.send("test")
        assert controller.snapshot()["threadId"] == "thread_abc"

    def test_handles_status_events(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [status_chunk("thinking", "Processing..."), text_chunk("done"), stop_chunk()]
        ]
        controller = make_controller(t2v)
        log = BatchLog()
        controller.subscribe(log)

        controller.send("test")

        statuses = log.values("agentStatus")
        assert {"type": "thinking", "message": "Processing..."} in statuses
        assert statuses[-1] is None  # cleared at finalize

    def test_stores_todos_as_plan_on_assistant_message(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [todos_chunk("- [ ] Step 1\n- [ ] Step 2"), text_chunk("Working"), stop_chunk()]
        ]
        controller = make_controller(t2v)
        controller.send("plan")
        assistant = next(m for m in controller.snapshot()["messages"] if m["role"] == "assistant")
        assert assistant["plan"] == "- [ ] Step 1\n- [ ] Step 2"

    def test_records_tool_call_as_step_on_assistant_message(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_call_chunk("search", "call_1", {"q": "test"}),
                text_chunk("Found it"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)
        controller.send("search for something")

        assistant = next(m for m in controller.snapshot()["messages"] if m["role"] == "assistant")
        assert len(assistant["steps"]) == 1
        step = assistant["steps"][0]
        assert step["name"] == "search"
        assert step["status"] == "used"
        assert step["args"] == {"q": "test"}
        assert step["toolCallId"] == "call_1"
        assert step["source"] == "client"

    def test_sets_error_on_stream_exception(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("partial"), RuntimeError("Network failure")]]
        controller = make_controller(t2v)
        controller.send("test")
        snap = controller.snapshot()
        assert snap["error"] == "Network failure"
        assert snap["isLoading"] is False
        assert snap["messages"][1]["content"] == "partial"

    def test_passes_model_from_options_to_chat(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]
        # In Python the model is a constructor option, and Talk2View binds it to the session:
        # the controller hands it to Talk2View.set_chat_defaults instead of to chat().
        controller = make_controller(t2v, model="gpt-4o")
        controller.send("test")
        assert t2v.chat_calls[0]["message"] == "test"
        assert t2v.defaults == [{"model": "gpt-4o", "capabilities": None}]

    def test_passes_system_prompt_from_send_message_options(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]
        controller = make_controller(t2v, system_prompt="You are a helpful bot")
        controller.send("test")
        assert t2v.chat_calls[0]["system_prompt"] == "You are a helpful bot"

    def test_marks_assistant_message_as_not_streaming_after_completion(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hello"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("Hi")
        assistant = next(m for m in controller.snapshot()["messages"] if m["role"] == "assistant")
        assert assistant["isStreaming"] is False


class TestConversationHistoryIntegrity:
    def test_includes_prior_completed_turns_in_history_on_the_next_send(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("first reply"), stop_chunk()],
            [text_chunk("second reply"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("first")
        controller.send("second")

        assert t2v.chat_calls[0]["history"] == []
        assert t2v.chat_calls[1]["message"] == "second"
        assert t2v.chat_calls[1]["history"] == [
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="first reply"),
        ]


class TestEventUnsubscribe:
    def test_stops_receiving_events_after_unsubscribe(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("first"), stop_chunk()],
            [text_chunk("second"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        log = BatchLog()
        unsubscribe = controller.subscribe(log)

        controller.send("test")
        count_before = len(log.batches)
        assert count_before > 0

        unsubscribe()
        controller.send("test2")

        assert len(log.batches) == count_before


class TestSendDetails:
    def test_send_while_busy_raises(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("partial"), gate, stop_chunk()]]
        controller = make_controller(
            t2v, executor=lambda fn: threading.Thread(target=fn, daemon=True).start()
        )

        controller.send("first")
        assert gate.entered.wait(2.0)
        assert controller.snapshot()["isLoading"] is True
        with pytest.raises(ControllerBusyError):
            controller.send("second")

        gate.release.set()
        assert controller.wait_idle(2.0) is True
        assert len(controller.snapshot()["messages"]) == 2  # the rejected send left no trace

    def test_an_executor_that_refuses_the_job_does_not_leave_the_controller_busy(self) -> None:
        t2v = FakeT2V()

        def refusing_executor(fn: Callable[[], None]) -> None:
            raise RuntimeError("cannot schedule new futures after shutdown")

        controller = make_controller(t2v, executor=refusing_executor)

        with pytest.raises(RuntimeError, match="cannot schedule"):
            controller.send("hello")

        snap = controller.snapshot()
        assert snap["isLoading"] is False
        assert snap["error"] == "cannot schedule new futures after shutdown"
        assert [m["role"] for m in snap["messages"]] == ["user"]  # empty assistant dropped
        assert controller.wait_idle(0.1) is True
        with pytest.raises(RuntimeError, match="cannot schedule"):  # not ControllerBusyError
            controller.send("again")
        assert t2v.chat_calls == []

    def test_send_after_close_raises_closed(self) -> None:
        t2v = FakeT2V()
        controller = make_controller(t2v)
        controller.close()
        with pytest.raises(ControllerClosedError):
            controller.send("x")
        assert t2v.chat_calls == []

    def test_a_stream_that_raises_on_a_worker_thread_still_reaches_idle(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("partial"), RuntimeError("boom")], [stop_chunk()]]
        controller = make_controller(
            t2v, executor=lambda fn: threading.Thread(target=fn, daemon=True).start()
        )

        controller.send("first")

        assert controller.wait_idle(2.0) is True  # _flush() runs on the failure path too
        assert controller.snapshot()["error"] == "boom"
        controller.send("second")  # the run was released: not busy
        assert controller.wait_idle(2.0) is True

    def test_text_after_a_tool_call_starts_a_new_segment(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                text_chunk("Let me look. "),
                tool_call_chunk("search", "call_1", {"q": "x", "page": None, "filter": {}}),
                text_chunk("Found it"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)
        controller.send("go")

        messages = controller.snapshot()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant", "assistant"]
        assert messages[1]["content"] == "Let me look. "
        assert messages[1]["steps"][0]["args"] == {"q": "x"}  # None and {} are stripped
        assert messages[1]["isStreaming"] is False
        assert messages[2]["content"] == "Found it"
        # History gets ONE assistant entry per turn: the segments joined by a blank line.
        t2v.chat_scripts = [[stop_chunk()]]
        controller.send("next")
        assert t2v.chat_calls[1]["history"][1] == ChatMessage(
            role="assistant", content="Let me look. \n\nFound it"
        )

    def test_empty_trailing_segment_is_removed_with_a_whole_list_set(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]  # no text, no steps, no plan
        controller = make_controller(t2v)
        log = BatchLog()
        controller.subscribe(log)

        controller.send("hello?")

        messages = controller.snapshot()["messages"]
        assert [m["role"] for m in messages] == ["user"]
        shrink_ops = [op for b in log.batches for op in b["ops"] if op["path"] == ["messages"]]
        assert len(shrink_ops) == 1
        assert shrink_ops[0]["type"] == "set"
        assert shrink_ops[0]["value"] == messages

    def test_attachments_ride_on_the_user_message_and_in_history(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("ok"), stop_chunk()], [stop_chunk()]]
        controller = make_controller(t2v)
        scan = Attachment(id="att_1", filename="scan.png", mime_type="image/png", size_bytes=12)

        controller.send("look", [scan])
        controller.send("again")

        user = controller.snapshot()["messages"][0]
        assert user["attachments"] == [
            {"id": "att_1", "filename": "scan.png", "mime_type": "image/png", "size_bytes": 12}
        ]
        assert t2v.chat_calls[0]["attachments"] == [scan]
        assert t2v.chat_calls[1]["history"][0] == ChatMessage(
            role="user",
            content=[
                {"type": "text", "text": "look"},
                {"type": "attachment", "attachment_id": "att_1"},
            ],
        )

    def test_replaying_every_batch_reproduces_the_final_snapshot(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                status_chunk("thinking", "..."),
                todos_chunk("- [ ] a"),
                text_chunk("one "),
                tool_call_chunk("search", "call_1", {"q": "x"}),
                text_chunk("two"),
                stop_chunk("thread_z"),
            ]
        ]
        controller = make_controller(t2v)
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.send("go")

        assert log.replay(start) == controller.snapshot()
        assert log.seqs() == list(range(1, len(log.batches) + 1))
        deltas = [op for b in log.batches for op in b["ops"] if op["type"] == "append-text"]
        assert [op["value"] for op in deltas] == ["one ", "two"]
        assert deltas[0]["path"] == ["messages", 1, "content"]
        assert deltas[1]["path"] == ["messages", 2, "content"]

    def test_capabilities_become_chat_defaults_and_a_cancel_token_reaches_chat(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]
        capabilities = Capabilities(approval=True, tool_events=True)
        controller = make_controller(t2v, capabilities=capabilities)
        controller.send("x")
        assert t2v.defaults == [{"model": None, "capabilities": capabilities}]
        assert t2v.chat_calls[0]["cancel"] is not None
        assert t2v.chat_calls[0]["cancel"].cancelled is False

    def test_message_ids_are_unique_and_timestamps_are_iso_utc(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("a"), stop_chunk()], [text_chunk("b"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("1")
        controller.send("2")
        messages = controller.snapshot()["messages"]
        assert len({m["id"] for m in messages}) == 4
        for m in messages:
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", m["timestamp"])

    def test_is_anonymous_is_refreshed_on_send(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]
        controller = make_controller(t2v)
        t2v.auth.anonymous = True
        controller.send("x")
        assert controller.snapshot()["isAnonymous"] is True

    def test_unknown_event_types_are_ignored(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [ChatEvent(type="reasoning", content="hmm"), text_chunk("answer"), stop_chunk()]
        ]
        controller = make_controller(t2v)
        controller.send("x")
        assert controller.snapshot()["messages"][1]["content"] == "answer"
