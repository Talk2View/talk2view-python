"""Behaviour worth pinning that no other test file covers: the default executor, the busy
checks on retry/edit, the launch-failure path of clear(), and the small branches no other
test reaches (a stream whose close() raises, a stream that raises Cancelled itself, the
defensive busy guard in _start_run, a reducer outcome that does not pause, and approve() on a
conversation with no assistant segment).
"""

from __future__ import annotations

import threading
from typing import Callable, Iterator, List, Optional, Tuple

import pytest

from talk2view._reducer import _Consumed, _Cursor
from talk2view.cancel import Cancelled, CancelToken
from talk2view.controller import ChatController, ControllerBusyError, Decision
from talk2view.state import PendingApproval
from talk2view.types import ChatEvent

from .controller_fakes import FakeT2V, Gate, make_controller, stop_chunk, text_chunk


def thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class TestDefaultExecutor:
    def test_the_default_executor_is_a_daemon_thread_named_t2v_chat(self) -> None:
        """Every other test injects an executor; this one exercises the one hosts get."""
        t2v = FakeT2V()
        gate = Gate()
        seen: List[threading.Thread] = []

        def note_thread(cancel: Optional[CancelToken]) -> None:
            seen.append(threading.current_thread())

        t2v.chat_scripts = [[note_thread, gate, text_chunk("hi"), stop_chunk()]]
        controller = ChatController(t2v)  # type: ignore[arg-type]  # no executor= at all

        controller.send("x")  # returns before the gate is released: the run is elsewhere
        assert gate.entered.wait(2.0)
        assert controller.wait_idle(0) is False
        gate.release.set()
        assert controller.wait_idle(2.0) is True

        assert seen[0] is not threading.current_thread()
        assert seen[0].name == "t2v-chat"
        assert seen[0].daemon is True
        assert controller.snapshot()["messages"][1]["content"] == "hi"


class TestBusyWhileARunIsActive:
    """``send()`` has this test already; ``retry()`` and ``edit()`` go through the same
    ``_require_idle`` and must refuse for the same reason, without touching the state."""

    def _running(self) -> Tuple[FakeT2V, ChatController, Gate]:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("first"), stop_chunk()], [text_chunk("partial"), gate]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("one")
        assert controller.wait_idle(2.0) is True
        controller.send("two")
        assert gate.entered.wait(2.0)
        return t2v, controller, gate

    def test_retry_while_a_run_is_active_raises_busy(self) -> None:
        t2v, controller, gate = self._running()
        before = controller.snapshot()
        with pytest.raises(ControllerBusyError, match="in progress"):
            controller.retry()
        assert controller.snapshot() == before  # nothing was truncated or resent
        gate.release.set()
        assert controller.wait_idle(2.0) is True
        assert len(t2v.chat_calls) == 2

    def test_edit_while_a_run_is_active_raises_busy(self) -> None:
        t2v, controller, gate = self._running()
        user_id = controller.snapshot()["messages"][0]["id"]
        before = controller.snapshot()
        with pytest.raises(ControllerBusyError, match="in progress"):
            controller.edit(user_id, "changed")
        assert controller.snapshot() == before
        gate.release.set()
        assert controller.wait_idle(2.0) is True
        assert len(t2v.chat_calls) == 2


class TestLaunchFailureOnClear:
    def test_a_refusing_executor_on_clear_leaves_is_loading_false(self) -> None:
        """``_launch``'s ``isLoading: False`` matters only for a job launched without an
        assistant segment (with one, ``_finalize`` has already cleared it). ``clear()``'s
        session delete is such a job: it is refused, and the controller is left neither busy
        nor loading."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        launches: List[Callable[[], None]] = []

        def executor(fn: Callable[[], None]) -> None:
            launches.append(fn)
            if len(launches) == 2:  # clear()'s session delete
                raise RuntimeError("pool is shut down")
            fn()

        controller = make_controller(t2v, executor=executor)
        controller.send("x")

        with pytest.raises(RuntimeError, match="pool is shut down"):
            controller.clear()

        snap = controller.snapshot()
        assert snap["messages"] == []  # the local reset happened before the job was refused
        assert snap["isLoading"] is False
        assert snap["error"] == "pool is shut down"
        assert controller.wait_idle(0) is True
        assert t2v.cleared == 0  # the DELETE never ran
        controller.send("again")  # not busy: the run was undone
        assert controller.wait_idle(0) is True


class _RaisingClose:
    """An iterator whose ``close()`` raises, as a generator can when its ``finally`` does."""

    def __init__(self, events: List[ChatEvent]) -> None:
        self._events = iter(events)

    def __iter__(self) -> Iterator[ChatEvent]:
        return self

    def __next__(self) -> ChatEvent:
        return next(self._events)

    def close(self) -> None:
        raise RuntimeError("close exploded")


class TestSmallBranches:
    def test_a_stream_whose_close_raises_does_not_fail_the_turn(self) -> None:
        t2v = FakeT2V()
        events = [text_chunk("done"), stop_chunk()]
        t2v.chat = lambda *a, **k: _RaisingClose(events)  # type: ignore[method-assign]
        controller = make_controller(t2v)
        controller.send("x")
        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["messages"][1]["content"] == "done"

    def test_a_stream_that_raises_cancelled_itself_keeps_the_partial_text(self) -> None:
        """``Talk2View.chat()`` swallows ``Cancelled``; the lower-level session iterators
        raise it. The controller accepts either shape."""
        t2v = FakeT2V()

        def raising(*a: object, **k: object) -> Iterator[ChatEvent]:
            yield text_chunk("partial")
            raise Cancelled()

        t2v.chat = raising  # type: ignore[method-assign]
        controller = make_controller(t2v)
        controller.send("x")
        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert snap["messages"][1]["content"] == "partial"

    def test_start_run_refuses_a_second_run(self) -> None:
        """An internal-invariant guard: a caller starts a run only once it has made sure none
        is active, so no public path reaches it and it is pinned directly."""
        controller = make_controller()
        with controller._lock:
            run_id, _token = controller._start_run()
            with pytest.raises(ControllerBusyError, match="in progress"):
                controller._start_run()
            assert controller._active_run == run_id  # the refused call started nothing
            controller._end_run(run_id)
        controller._flush()  # an ended run is flushed, which is what sets idle
        assert controller.wait_idle(0) is True

    def test_an_outcome_that_does_not_pause_leaves_the_run_to_drain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_consume`` ends the run itself only for a manual-approval PAUSE. No handler hands
        back a ``_Consumed`` without pausing today; one that did must leave the run to
        ``_drain``, which finalizes it. Ending it early would skip that finalize and leave
        the reply streaming for good."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v)
        real_reduce = controller._reducer.reduce

        def reduce(event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
            outcome = real_reduce(event, cursor)
            if event.type == "done":
                return _Consumed(False, None, cursor.assistant_id, cursor.completed)
            return outcome

        monkeypatch.setattr(controller._reducer, "reduce", reduce)
        controller.send("x")

        snap = controller.snapshot()
        assert snap["messages"][1]["content"] == "hi"
        assert snap["messages"][1]["isStreaming"] is False
        assert snap["isLoading"] is False
        assert controller.wait_idle(0) is True

    def test_approve_with_no_assistant_segment_opens_one(self) -> None:
        """``_last_assistant_id`` finds nothing to continue into (the card is up but the
        conversation shows no assistant message): the resume gets a fresh segment."""
        t2v = FakeT2V()
        t2v.approval_scripts = [[text_chunk("Sent!"), stop_chunk()]]
        controller = make_controller(t2v)
        pending: PendingApproval = {
            "toolCallId": "call_1",
            "toolName": "send_email",
            "arguments": {},
            "description": "Send",
        }
        with controller._lock:
            controller._rec.set(["pendingApproval"], pending)

        controller.approve(Decision(action="once"))

        messages = controller.snapshot()["messages"]
        assert [m["role"] for m in messages] == ["assistant"]
        assert messages[0]["content"] == "Sent!"
        assert messages[0]["isStreaming"] is False
