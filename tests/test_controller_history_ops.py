"""retry / edit / clear (ported from chat-state.test.ts › retryLastMessage, clearMessages)."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import pytest

from talk2view.controller import ControllerBusyError, ControllerClosedError, Decision
from talk2view.types import Attachment, ChatMessage

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    Gate,
    error_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
    todos_chunk,
    tool_call_chunk,
)


def thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class SessionDeleteGate:
    """A ``clear_session`` that parks until the test releases it, so the window where
    ``clear()`` holds the controller busy is observable without a sleep."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        self.entered.set()
        self.release.wait(5.0)


class TestRetryLastMessage:
    def test_removes_failed_messages_and_resends(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("partial"), error_chunk("Server error")],
            [text_chunk("Success"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("test")
        assert controller.snapshot()["error"] == "Server error"
        assert len(controller.snapshot()["messages"]) == 2

        controller.retry()

        snap = controller.snapshot()
        assert snap["error"] is None
        assert len(snap["messages"]) == 2  # user + assistant from the retry
        assistant = next(m for m in snap["messages"] if m["role"] == "assistant")
        assert assistant["content"] == "Success"
        assert t2v.chat_calls[1]["message"] == "test"
        assert t2v.chat_calls[1]["history"] == []  # the failed pair left the LLM history too

    def test_does_nothing_if_no_previous_message(self) -> None:
        t2v = FakeT2V()
        controller = make_controller(t2v)
        controller.retry()
        assert controller.snapshot()["messages"] == []
        assert t2v.chat_calls == []

    def test_retry_keeps_earlier_turns_and_the_attachments(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("first reply"), stop_chunk()],
            [text_chunk("meh"), stop_chunk()],
            [text_chunk("better"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        scan = Attachment(id="att_1", filename="scan.png", mime_type="image/png", size_bytes=12)
        controller.send("first")
        controller.send("second", [scan])

        controller.retry()

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == [
            "first",
            "first reply",
            "second",
            "better",
        ]
        assert t2v.chat_calls[2]["attachments"] == [scan]
        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="first reply"),
        ]

    def test_retry_shrinks_with_a_whole_list_set(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("boom")], [text_chunk("ok"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("q")
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.retry()

        first_ops = log.batches[0]["ops"]
        assert first_ops[0] == {"type": "set", "path": ["messages"], "value": []}
        assert log.replay(start) == controller.snapshot()

    def test_retry_after_a_textless_failure_resends_exactly_one_user_message(self) -> None:
        """A rolled-back turn's mark points ONE PAST THE END of ``_history``: the cut must
        neither raise nor double-pop, and the mark must not outlive the message it keyed."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [error_chunk("boom")],  # no assistant text at all: the user entry is rolled back
            [text_chunk("ok"), stop_chunk()],
            [text_chunk("next"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("q")
        assert [m["content"] for m in controller.snapshot()["messages"]] == ["q"]

        controller.retry()

        snap = controller.snapshot()
        assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]
        assert [m["content"] for m in snap["messages"]] == ["q", "ok"]
        assert t2v.chat_calls[1]["history"] == []
        # No stale mark survived: the turn after the retry sees exactly one prior pair.
        controller.send("after")
        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="q"),
            ChatMessage(role="assistant", content="ok"),
        ]

    def test_retry_after_a_tools_only_turn_keeps_the_earlier_turn(self) -> None:
        """A successful tools-only turn adds NO assistant entry, so its user entry is left
        dangling in ``_history``; the cut has to take that one too or the resend replays
        ``[..., user, user]``."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("first reply"), stop_chunk()],
            [tool_call_chunk("list_files", "call_1", {}), stop_chunk()],  # tools only, no text
            [text_chunk("second reply"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("first")
        controller.send("second")
        assert [m["role"] for m in controller.snapshot()["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",  # kept by _finalize: it has steps, even with no text
        ]

        controller.retry()

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == [
            "first",
            "first reply",
            "second",
            "second reply",
        ]
        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="first reply"),
        ]

    def test_retry_pops_every_trailing_assistant_segment(self) -> None:
        """Text after a tool call opens a second assistant segment, so one turn can leave
        more than one assistant message on screen."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("a"), tool_call_chunk("t", "call_1", {}), text_chunk("b"), stop_chunk()],
            [text_chunk("again"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("q")
        assert [m["role"] for m in controller.snapshot()["messages"]] == [
            "user",
            "assistant",
            "assistant",
        ]

        controller.retry()

        snap = controller.snapshot()
        assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]
        assert [m["content"] for m in snap["messages"]] == ["q", "again"]
        assert t2v.chat_calls[1]["history"] == []

    def test_retry_after_two_textless_failures_drops_both_bubbles(self) -> None:
        """Two rolled-back turns in a row leave two consecutive user messages on screen. As in
        TS (``index.ts`` ``retryLastMessage``) the trailing-user loop pops them all, and the
        mark it keeps is the EARLIEST one's, so the history cut matches the display cut."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [error_chunk("one boom")],
            [error_chunk("two boom")],
            [text_chunk("ok"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("first")
        controller.send("second")
        assert [m["content"] for m in controller.snapshot()["messages"]] == ["first", "second"]

        controller.retry()

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == ["second", "ok"]
        assert t2v.chat_calls[2]["message"] == "second"
        assert t2v.chat_calls[2]["history"] == []

    def test_retry_cuts_history_at_the_earliest_popped_user_turn(self) -> None:
        """Two consecutive user bubbles can carry DIFFERENT marks: a turn that ends with
        nothing at all keeps its own ``_history`` user entry (nothing failed, so nothing was
        rolled back) while its empty bubble is dropped. The cut has to use the earliest popped
        mark, or the resend replays the deleted turn as ``[..., user, user]``."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [stop_chunk()],  # done with nothing: bubble dropped, `_history` user entry kept
            [error_chunk("boom")],  # failed with nothing: bubble kept, user entry rolled back
            [text_chunk("ok"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("a")
        controller.send("b")
        assert [m["content"] for m in controller.snapshot()["messages"]] == ["a", "b"]

        controller.retry()

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == ["b", "ok"]
        assert t2v.chat_calls[2]["history"] == []  # "a" went too: its bubble is gone


class TestEdit:
    def test_edit_truncates_and_resends(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("reply one"), stop_chunk()],
            [text_chunk("reply two"), stop_chunk()],
            [text_chunk("reply three"), stop_chunk()],
            [text_chunk("reply two, edited"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("one")
        controller.send("two")
        controller.send("three")
        second_user = controller.snapshot()["messages"][2]
        assert second_user["content"] == "two"

        controller.edit(second_user["id"], "two (edited)")

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == [
            "one",
            "reply one",
            "two (edited)",
            "reply two, edited",
        ]
        assert t2v.chat_calls[3]["message"] == "two (edited)"
        assert t2v.chat_calls[3]["history"] == [
            ChatMessage(role="user", content="one"),
            ChatMessage(role="assistant", content="reply one"),
        ]

    def test_edit_after_a_rolled_back_turn_cuts_history_at_the_right_place(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [error_chunk("boom")],  # rolled back: the display keeps it, the history does not
            [text_chunk("reply two"), stop_chunk()],
            [text_chunk("reply two, edited"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("one")
        controller.send("two")
        second_user = next(m for m in controller.snapshot()["messages"] if m["content"] == "two")

        controller.edit(second_user["id"], "two (edited)")

        assert t2v.chat_calls[2]["history"] == []

    def test_edit_unknown_or_assistant_id_raises(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("reply"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("one")
        assistant_id = controller.snapshot()["messages"][1]["id"]
        with pytest.raises(ValueError, match="No user message"):
            controller.edit("msg_missing", "x")
        with pytest.raises(ValueError, match="No user message"):
            controller.edit(assistant_id, "x")
        assert len(controller.snapshot()["messages"]) == 2

    def test_retry_and_edit_raise_while_busy(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        controller = make_controller(t2v)
        controller.send("send email")
        user_id = controller.snapshot()["messages"][0]["id"]
        with pytest.raises(ControllerBusyError):
            controller.retry()
        with pytest.raises(ControllerBusyError):
            controller.edit(user_id, "x")

    def test_edit_after_a_textless_failure_resends_exactly_one_user_message(self) -> None:
        """The mark of the very turn being edited points one past the end of ``_history``."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [error_chunk("boom")],
            [text_chunk("ok"), stop_chunk()],
            [text_chunk("next"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("q")
        failed_user = controller.snapshot()["messages"][0]

        controller.edit(failed_user["id"], "q (edited)")

        snap = controller.snapshot()
        assert [m["role"] for m in snap["messages"]] == ["user", "assistant"]
        assert [m["content"] for m in snap["messages"]] == ["q (edited)", "ok"]
        assert t2v.chat_calls[1]["history"] == []
        controller.send("after")
        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="q (edited)"),
            ChatMessage(role="assistant", content="ok"),
        ]

    def test_edit_after_a_plan_only_turn_keeps_the_earlier_turn(self) -> None:
        """Same as the tools-only case: a plan-only turn leaves a dangling user entry."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("first reply"), stop_chunk()],
            [todos_chunk("- think about it"), stop_chunk()],  # plan only, no text
            [text_chunk("second reply"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("first")
        controller.send("second")
        second_user = controller.snapshot()["messages"][2]
        assert second_user["content"] == "second"

        controller.edit(second_user["id"], "second (edited)")

        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == [
            "first",
            "first reply",
            "second (edited)",
            "second reply",
        ]
        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="first"),
            ChatMessage(role="assistant", content="first reply"),
        ]

    def test_edit_keeps_the_attachments_of_the_message_it_rewrites(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("reply one"), stop_chunk()],
            [text_chunk("reply two"), stop_chunk()],
            [text_chunk("edited reply"), stop_chunk()],
            [text_chunk("retried reply"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        scan = Attachment(id="att_1", filename="scan.png", mime_type="image/png", size_bytes=12)
        controller.send("look at this", [scan])
        controller.send("and this")
        first_user = controller.snapshot()["messages"][0]

        controller.edit(first_user["id"], "look at this instead")

        assert t2v.chat_calls[2]["message"] == "look at this instead"
        assert t2v.chat_calls[2]["attachments"] == [scan]
        assert controller.snapshot()["messages"][0]["attachments"] == [
            {"id": "att_1", "filename": "scan.png", "mime_type": "image/png", "size_bytes": 12}
        ]
        # The edit also becomes the retry target, attachments included.
        controller.retry()
        assert t2v.chat_calls[3]["attachments"] == [scan]

    def test_edit_shrinks_with_a_whole_list_set(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("one reply"), stop_chunk()],
            [text_chunk("edited reply"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("one")
        first_user = controller.snapshot()["messages"][0]
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.edit(first_user["id"], "one (edited)")

        first_ops = log.batches[0]["ops"]
        assert first_ops[0] == {"type": "set", "path": ["messages"], "value": []}
        assert log.replay(start) == controller.snapshot()


class TestClearMessages:
    def test_resets_all_state(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hello"), stop_chunk("thread_x")]]
        controller = make_controller(t2v)
        controller.send("Hi")

        controller.clear()

        snap = controller.snapshot()
        assert snap["messages"] == []
        assert snap["isLoading"] is False
        assert snap["error"] is None
        assert snap["pendingApproval"] is None
        assert snap["agentStatus"] is None
        assert snap["threadId"] is None
        assert snap["alwaysAllowedTools"] == []
        assert t2v.cleared == 1  # the engine session was deleted

    def test_clear_forgets_history_always_allowed_tools_and_the_retry_target(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")], [stop_chunk()]]
        t2v.approval_scripts = [[text_chunk("Sent"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("send email")
        controller.approve(Decision(action="always"))

        controller.clear()
        controller.retry()  # nothing to retry any more
        controller.send("fresh")

        assert len(t2v.chat_calls) == 2
        assert t2v.chat_calls[1]["history"] == []
        assert controller.snapshot()["alwaysAllowedTools"] == []

    def test_clear_with_a_pending_approval_drops_it(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        controller = make_controller(t2v)
        controller.send("send email")
        controller.clear()
        assert controller.snapshot()["pendingApproval"] is None
        assert t2v.approval_calls == []

    def test_clear_during_a_run_abandons_it(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("Partial"), gate, text_chunk(" late text"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert gate.entered.wait(2.0)

        controller.clear()
        assert controller.wait_idle(2.0) is True
        gate.release.set()  # (the token was cancelled, so the gate has already raised)

        snap = controller.snapshot()
        assert snap["messages"] == []
        assert snap["isLoading"] is False
        assert snap["error"] is None
        assert t2v.chat_calls[0]["cancel"].cancelled is True

    def test_clear_survives_a_failing_session_delete(self) -> None:
        t2v = FakeT2V()

        def boom() -> None:
            raise RuntimeError("network down")

        t2v.clear_session = boom  # type: ignore[method-assign]
        controller = make_controller(t2v)
        controller.clear()
        assert controller.wait_idle(0.5) is True
        snap = controller.snapshot()
        assert snap["isLoading"] is False
        # A failed DELETE is logged, not shown: nothing the end-user did went wrong, and the
        # conversation really is gone as far as this client is concerned.
        assert snap["error"] is None
        assert snap["messages"] == []
        controller.send("after")  # the controller is not left busy

    def test_clear_holds_the_controller_busy_until_the_session_delete_returns(self) -> None:
        t2v = FakeT2V()
        delete = SessionDeleteGate()
        t2v.clear_session = delete  # type: ignore[method-assign]
        controller = make_controller(t2v, executor=thread_executor)

        controller.clear()

        assert delete.entered.wait(2.0)
        assert controller.snapshot()["isLoading"] is True
        assert controller.wait_idle(0.0) is False
        with pytest.raises(ControllerBusyError):
            controller.send("too soon")  # cannot race a turn onto the session being deleted

        delete.release.set()
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["isLoading"] is False
        controller.send("now")
        assert len(t2v.chat_calls) == 1

    def test_every_history_op_refuses_a_closed_controller(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("reply"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("one")
        user_id = controller.snapshot()["messages"][0]["id"]
        snapshot = controller.export_conversation()
        controller.close()
        with pytest.raises(ControllerClosedError):
            controller.retry()
        with pytest.raises(ControllerClosedError):
            controller.edit(user_id, "x")
        with pytest.raises(ControllerClosedError):
            controller.clear()
        with pytest.raises(ControllerClosedError):
            controller.restore_conversation(snapshot)
        assert t2v.cleared == 0 and t2v.detached == 0

    def test_clear_resets_the_internal_turn_bookkeeping(self) -> None:
        """``_turn_failed`` and the ``_history_marks`` keys are the two resets no public
        sequence can distinguish — ``_prepare_send`` clears ``_turn_failed`` on every send, and
        a mark keyed by a discarded message id is only ever a leak — so they are read directly.
        """
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("boom")]]
        controller = make_controller(t2v)
        controller.send("q")
        assert controller._turn_failed is True
        assert controller._history_marks != {}

        controller.clear()

        assert controller._turn_failed is False
        assert controller._history_marks == {}
        assert controller._history == []
        assert controller._last_user_text is None
        assert controller._last_user_attachments is None

    def test_clear_shrinks_with_a_whole_list_set(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hello"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("Hi")
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.clear()

        first_ops = log.batches[0]["ops"]
        assert first_ops[0] == {"type": "set", "path": ["messages"], "value": []}
        assert log.replay(start) == controller.snapshot()


class TestCancelRacingTheHistoryOperations:
    """``cancel()`` swallows ``ControllerBusyError``/``ControllerClosedError`` from its own
    ``approve()`` call, so every new ``_start_run`` caller has to be checked for a sequence
    that could leave a ``pendingApproval`` set with no run able to answer it."""

    def test_cancel_that_read_a_pending_approval_before_clear_wins_is_inert(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")], [stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("send email")
        assert controller.snapshot()["pendingApproval"] is not None

        real_approve = controller.approve

        def approve_after_clear_landed(
            decision: Decision, *, tool_call_id: Optional[str] = None
        ) -> None:
            # The window: cancel() has read (pendingApproval set, token None) under the lock
            # and released it; clear() runs to completion before approve() retakes the lock.
            controller.clear()
            real_approve(decision, tool_call_id=tool_call_id)

        controller.approve = approve_after_clear_landed  # type: ignore[method-assign]

        controller.cancel()  # must not raise out of a Stop button

        snap = controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert snap["messages"] == []
        assert t2v.approval_calls == []  # clear() dropped it; the deny never reached the engine
        assert t2v.cleared == 1
        assert controller.wait_idle(1.0) is True
        controller.send("fresh")  # usable, not stranded
        assert len(t2v.chat_calls) == 2

    def test_cancel_after_clear_dropped_the_approval_is_a_no_op(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        controller = make_controller(t2v)
        controller.send("send email")
        controller.clear()
        seq_before = controller.snapshot()["seq"]

        controller.cancel()

        assert controller.snapshot()["seq"] == seq_before
        assert t2v.approval_calls == []

    def test_cancel_during_the_session_delete_leaves_the_controller_idle(self) -> None:
        t2v = FakeT2V()
        delete = SessionDeleteGate()
        t2v.clear_session = delete  # type: ignore[method-assign]
        controller = make_controller(t2v, executor=thread_executor)
        controller.clear()
        assert delete.entered.wait(2.0)

        controller.cancel()  # cancels clear()'s own token; clear_session takes none

        delete.release.set()
        assert controller.wait_idle(2.0) is True
        snap = controller.snapshot()
        assert snap["isLoading"] is False
        assert snap["error"] is None
        assert snap["pendingApproval"] is None
        assert delete.calls == 1

    def test_cancel_stops_a_retried_run(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [
            [error_chunk("boom")],
            [text_chunk("Partial"), gate, text_chunk(" late text"), stop_chunk()],
        ]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("q")
        assert controller.wait_idle(2.0) is True

        controller.retry()
        assert gate.entered.wait(2.0)
        controller.cancel()

        assert controller.wait_idle(2.0) is True
        gate.release.set()
        snap = controller.snapshot()
        assert [m["content"] for m in snap["messages"]] == ["q", "Partial"]
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert t2v.chat_calls[1]["cancel"].cancelled is True
