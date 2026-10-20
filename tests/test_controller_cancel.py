"""ChatController.cancel: stop keeps the partial reply; with an approval pending it denies."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterator, List, Optional

import pytest

from talk2view import Talk2View
from talk2view import controller as controller_module
from talk2view.cancel import CancelToken
from talk2view.controller import CANCELLED_APPROVAL_FEEDBACK, ChatController, Decision
from talk2view.dispatch import QueueDispatcher, run_on_main
from talk2view.state import OpBatch
from talk2view.types import ChatEvent, ChatMessage

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    Gate,
    RaisingDispatcher,
    approval_result_chunk,
    engine_interrupt_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
    tool_call_chunk,
)


def thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


class TestCancel:
    def test_cancel_keeps_partial_and_no_error(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [
            [text_chunk("Partial "), text_chunk("answer"), gate, text_chunk(" never sent")],
            [stop_chunk()],
        ]
        controller = make_controller(t2v, executor=thread_executor)

        controller.send("hi")
        assert gate.entered.wait(2.0)
        assert controller.snapshot()["isLoading"] is True

        controller.cancel()
        assert controller.wait_idle(2.0) is True

        snap = controller.snapshot()
        assert snap["isLoading"] is False
        assert snap["error"] is None
        assistant = snap["messages"][1]
        assert assistant["content"] == "Partial answer"
        assert assistant["isStreaming"] is False
        assert t2v.chat_calls[0]["cancel"].cancelled is True
        # The partial reply is a real turn: it is replayed as history next time.
        controller.send("continue")
        assert controller.wait_idle(2.0) is True
        assert t2v.chat_calls[1]["history"] == [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="Partial answer"),
        ]

    def test_cancel_with_a_transport_error_after_cancel_sets_no_error(self) -> None:
        """Closing the response from another thread makes the read raise; that is not an error."""
        t2v = FakeT2V()
        entered = threading.Event()

        def explode_once_cancelled(cancel: Optional[CancelToken]) -> None:
            entered.set()
            assert cancel is not None
            cancelled = threading.Event()
            cancel.on_cancel(cancelled.set)  # fires at once if already cancelled: no lost wakeup
            assert cancelled.wait(2.0)
            raise RuntimeError("stream closed")

        t2v.chat_scripts = [[text_chunk("Partial"), explode_once_cancelled]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert entered.wait(2.0)
        controller.cancel()
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["error"] is None
        assert controller.snapshot()["messages"][1]["content"] == "Partial"

    def test_cancel_before_any_text_rolls_back_the_user_turn(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[gate], [stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("never mind")
        assert gate.entered.wait(2.0)
        controller.cancel()
        assert controller.wait_idle(2.0) is True
        assert [m["role"] for m in controller.snapshot()["messages"]] == ["user"]
        controller.send("new question")
        assert controller.wait_idle(2.0) is True
        assert t2v.chat_calls[1]["history"] == []

    def test_cancel_with_pending_approval_denies(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")]]
        t2v.approval_scripts = [
            [
                approval_result_chunk("delete_file", "deny", "call_1"),
                text_chunk("Ok."),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)
        controller.send("delete x")
        assert controller.snapshot()["pendingApproval"] is not None

        controller.cancel()

        assert t2v.approval_calls[0]["decision"] == Decision(
            action="deny", feedback=CANCELLED_APPROVAL_FEEDBACK
        )
        assert CANCELLED_APPROVAL_FEEDBACK == "Cancelled by the user"
        snap = controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert snap["messages"][1]["steps"][0]["status"] == "denied"

    def test_cancel_with_pending_engine_approval_denies(self) -> None:
        """Cancel denies a pending approval, and that applies to an engine-gated call exactly as it
        does to a client one -- cancel() never inspects `kind`."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [engine_interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")]
        ]
        t2v.approval_scripts = [
            [
                approval_result_chunk("delete_file", "deny", "call_1"),
                text_chunk("Ok."),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)
        controller.send("delete x")
        pending = controller.snapshot()["pendingApproval"]
        assert pending is not None
        assert pending["kind"] == "engine"

        controller.cancel()

        assert t2v.approval_calls[0]["decision"] == Decision(
            action="deny", feedback=CANCELLED_APPROVAL_FEEDBACK
        )
        snap = controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert snap["messages"][1]["steps"][0]["status"] == "denied"

    def test_cancel_when_idle_is_a_no_op(self) -> None:
        controller = make_controller()
        controller.cancel()
        assert controller.snapshot()["seq"] == 0

    def test_cancel_after_the_stream_ends_naturally_is_a_no_op(self) -> None:
        """Shape (i): cancel() lands well after the run finished. `_end_run` already cleared
        `_cancel_token`, so this is the same no-op as the idle case -- proving the token really
        was cleared (a stale token here would make `token.cancel()` run harmlessly too, but a
        true no-op is stronger: no flush at all, and the next send is unaffected)."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Hello"), stop_chunk()], [stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert controller.wait_idle(2.0) is True
        seq_before = controller.snapshot()["seq"]

        controller.cancel()

        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["seq"] == seq_before  # no batch was flushed: a true no-op

        controller.send("again")
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["error"] is None

    def test_cancel_called_from_within_the_stream_as_it_ends_naturally(self) -> None:
        """Shape (ii): the cancel is issued synchronously, on the SAME thread that is draining
        the stream, in between two script items -- so `token.cancelled` ends up True even
        though `done` is about to be (or was just) read. Robustness only: this turn has real
        text, so the history-rollback question doesn't arise here (see
        TestCancelHistoryIntegrity for the tools-only case, where it does)."""
        t2v = FakeT2V()
        controller = make_controller(t2v, executor=thread_executor)

        def cancel_from_within(cancel: Optional[CancelToken]) -> None:
            controller.cancel()

        t2v.chat_scripts = [[text_chunk("Hi"), cancel_from_within, stop_chunk()]]

        controller.send("hi")
        assert controller.wait_idle(2.0) is True

        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert snap["messages"][1]["content"] == "Hi"


class TestCancelWhenTheRunIsAlreadyEnding:
    """`cancel()` must never raise, even when it lands in a narrow window where
    `pendingApproval` and the run's own bookkeeping disagree. Since the reducer split the only such
    window left is `close()`'s -- it clears the run and leaves the card up on purpose -- and
    the tests below also pin the invariant that closed the others: a pending approval and an
    active run never coexist, whichever thread cuts the batch that carries the card."""

    def test_cancel_racing_the_approval_pause_does_not_raise(self) -> None:
        """`_on_approval_required` sets `pendingApproval` and `_consume` ends the run in the
        SAME locked section. A `cancel()` landing right after `_consume`
        returns -- the gap that used to show a pending approval AND a live token at once --
        now sees the approval alone, and takes the deny branch.

        To land there deterministically this patches the controller's own `_consume` to pause,
        on the worker thread, exactly after it returns the paused-for-manual-approval result
        and before `_drain` flushes.

        END STATE (changed by the hardening, deliberately): before it, the race took the
        token-cancel branch and left the card up for the host to answer again; now the token
        is already gone when `cancel()` reads under the lock, so Stop means what it means
        everywhere else with a card up -- the call is denied with
        `CANCELLED_APPROVAL_FEEDBACK` and the resume runs on a new run.
        """
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")]]
        t2v.approval_scripts = [
            [approval_result_chunk("delete_file", "deny", "call_1"), stop_chunk()]
        ]
        controller = make_controller(t2v, executor=thread_executor)

        paused = threading.Event()
        release = threading.Event()
        real_consume = controller._consume  # type: ignore[attr-defined]

        def gated_consume(*args: Any, **kwargs: Any) -> Any:
            result = real_consume(*args, **kwargs)
            if result.paused and result.auto_approval is None:
                paused.set()
                assert release.wait(2.0)
            return result

        controller._consume = gated_consume  # type: ignore[attr-defined]

        controller.send("delete x")
        assert paused.wait(2.0)
        # The window: the card is up and, with the run already ended, the token is gone.
        mid_snap = controller.snapshot()
        assert mid_snap["pendingApproval"] is not None
        assert controller._cancel_token is None
        assert controller._active_run is None

        controller.cancel()  # must not raise; takes the deny branch

        release.set()
        assert controller.wait_idle(2.0) is True

        assert t2v.approval_calls[0]["decision"] == Decision(
            action="deny", feedback=CANCELLED_APPROVAL_FEEDBACK
        )
        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert snap["pendingApproval"] is None
        assert snap["messages"][1]["steps"][0]["status"] == "denied"

    def test_the_card_is_never_published_on_an_active_run(self) -> None:
        """The hard guarantee behind the hardening: whoever cuts the batch that carries
        `pendingApproval`, the run it came from is already over. Same `_consume` seam as
        above; inside the window the run bookkeeping is checked directly, and the call that
        used to raise `ControllerBusyError` from it -- `approve()` -- is made and must succeed."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        t2v.approval_scripts = [[text_chunk("Sent!"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)

        paused = threading.Event()
        release = threading.Event()
        real_consume = controller._consume  # type: ignore[attr-defined]

        def gated_consume(*args: Any, **kwargs: Any) -> Any:
            result = real_consume(*args, **kwargs)
            if result.paused and result.auto_approval is None:
                paused.set()
                assert release.wait(2.0)
            return result

        controller._consume = gated_consume  # type: ignore[attr-defined]

        controller.send("send email")
        assert paused.wait(2.0)
        with controller._lock:
            assert controller._rec.state["pendingApproval"] is not None
            assert controller._active_run is None
            assert controller._cancel_token is None

        controller.approve(Decision(action="once"))  # used to be ControllerBusyError here

        release.set()
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["pendingApproval"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "Sent!"

    def test_no_flusher_on_any_thread_sees_the_card_on_an_active_run(self) -> None:
        """The same guarantee under contention, the way a host would hit it: other threads
        flush continuously (`snapshot()` cuts a batch) while the worker reaches the
        interrupt. Every batch that carries a pending approval is checked against the run
        bookkeeping at the moment it is delivered."""
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [
            [text_chunk("a"), gate, interrupt_chunk("send_email", "call_1", {}, "Send")]
        ]
        controller = make_controller(t2v, executor=thread_executor)
        stop = threading.Event()
        seen_active: List[Optional[int]] = []

        def watch(batch: OpBatch) -> None:
            for op in batch["ops"]:
                if op["path"] == ["pendingApproval"] and op["value"] is not None:
                    seen_active.append(controller._active_run)

        def hammer() -> None:
            while not stop.is_set():
                controller.snapshot()

        controller.subscribe(watch)
        hammers = [threading.Thread(target=hammer, daemon=True) for _ in range(3)]
        for h in hammers:
            h.start()
        controller.send("send email")
        assert gate.entered.wait(2.0)
        gate.release.set()
        assert controller.wait_idle(2.0) is True
        stop.set()
        for h in hammers:
            h.join(2.0)

        assert controller.snapshot()["pendingApproval"] is not None
        assert seen_active == [None]  # delivered exactly once, and the run was already over

    def test_cancel_after_close_with_pending_approval_does_not_raise(self) -> None:
        """`close()` clears `_cancel_token` but does not touch `pendingApproval` (there is
        nothing to stream anymore, so nothing to reconcile). A `cancel()` afterward must not
        raise `ControllerClosedError` out of `approve()`."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")]]
        controller = make_controller(t2v)
        controller.send("delete x")
        assert controller.snapshot()["pendingApproval"] is not None

        controller.close()
        controller.cancel()  # must not raise ControllerClosedError

        # Still closed, still (harmlessly) showing the stale approval -- cancel() did nothing,
        # which is correct: a closed controller has no run to stop and no host left to answer.
        assert controller.snapshot()["pendingApproval"] is not None

    def test_cancel_when_close_wins_the_race_with_approve_does_not_raise(self) -> None:
        """The residual gap: `cancel()` reads `closed=False` under the lock, then calls
        `approve()` after releasing it. If a `close()` (or another run starting and finishing)
        lands in that exact gap, `approve()`'s own `_start_run()` would raise
        `ControllerClosedError` (or `ControllerBusyError`) from inside `cancel()`. Patch
        `approve()` itself to close() first, so the race is deterministic instead of a coin
        flip on thread scheduling."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")]]
        controller = make_controller(t2v)
        controller.send("delete x")
        assert controller.snapshot()["pendingApproval"] is not None

        real_approve = controller.approve

        def approve_after_close(decision: Decision, *, tool_call_id: Optional[str] = None) -> None:
            controller.close()  # lands in the gap between cancel()'s read and this call
            real_approve(decision, tool_call_id=tool_call_id)

        controller.approve = approve_after_close  # type: ignore[method-assign]

        controller.cancel()  # must not raise ControllerClosedError

        assert controller.wait_idle(0) is True

    def test_cancel_never_denies_a_card_that_replaced_the_one_it_read(self) -> None:
        """The same gap, another way to lose it: `cancel()` reads card A under the lock, and
        before its `approve()` retakes the lock the host answers A and A's resume raises card
        B. Stop passes the `toolCallId` it read, so the denial meant for A is a no-op against
        B -- B stays up for the host. Same seam as above: `approve()` is patched so the
        host's click lands first."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("delete_file", "call_A", {"path": "/a"}, "Delete")]]
        t2v.approval_scripts = [
            [
                approval_result_chunk("delete_file", "once", "call_A"),
                interrupt_chunk("delete_file", "call_B", {"path": "/b"}, "Delete"),
            ]
        ]
        controller = make_controller(t2v)
        controller.send("delete a and b")
        pending = controller.snapshot()["pendingApproval"]
        assert pending is not None and pending["toolCallId"] == "call_A"

        real_approve = controller.approve

        def approve_after_the_host_answered(
            decision: Decision, *, tool_call_id: Optional[str] = None
        ) -> None:
            real_approve(Decision(action="once"), tool_call_id="call_A")  # the host's click
            real_approve(decision, tool_call_id=tool_call_id)  # then cancel()'s own call

        controller.approve = approve_after_the_host_answered  # type: ignore[method-assign]

        controller.cancel()

        assert [call["decision"] for call in t2v.approval_calls] == [Decision(action="once")]
        snap = controller.snapshot()
        assert snap["pendingApproval"] is not None
        assert snap["pendingApproval"]["toolCallId"] == "call_B"
        assert [(s["toolCallId"], s["status"]) for s in snap["messages"][1]["steps"]] == [
            ("call_A", "used"),
            ("call_B", "running"),
        ]
        assert controller.wait_idle(0) is True


class TestCancelWhenTheDenialCannotLaunch:
    """`cancel()` never raises -- not even for the failures `approve()` re-raises on purpose:
    the executor refusing the denial's resume, or the host's `Dispatcher.post` raising while
    the denial's first batch is posted. `_launch` has already undone that run and put the
    failure in `error`, where the host sees it; Stop logs it and returns, and the controller
    is left idle and usable, never busy for good."""

    @staticmethod
    def _assert_the_denial_failed_cleanly(
        controller: ChatController,
        t2v: FakeT2V,
        message: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        assert t2v.approval_calls == []  # the denial never reached the engine
        snap = controller.snapshot()
        assert snap["error"] == message
        assert snap["isLoading"] is False
        assert snap["pendingApproval"] is None
        assert snap["messages"][-1]["isStreaming"] is False
        assert controller.wait_idle(0) is True
        assert sum("could not deny" in r.message for r in caplog.records) == 1

    def test_an_executor_that_refuses_the_denial(self, caplog: pytest.LogCaptureFixture) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")],
            [text_chunk("Hello again"), stop_chunk()],
        ]
        launches: List[Callable[[], None]] = []

        def executor(fn: Callable[[], None]) -> None:
            launches.append(fn)
            if len(launches) == 2:  # the denial's resume
                raise RuntimeError("pool is shut down")
            fn()

        controller = make_controller(t2v, executor=executor)
        controller.send("delete x")
        assert controller.snapshot()["pendingApproval"] is not None

        with caplog.at_level(logging.ERROR, logger="talk2view.controller"):
            controller.cancel()  # must not raise

        self._assert_the_denial_failed_cleanly(controller, t2v, "pool is shut down", caplog)
        controller.send("hello")
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["error"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "Hello again"

    def test_a_dispatcher_that_raises_on_the_denial(self, caplog: pytest.LogCaptureFixture) -> None:
        dispatcher = RaisingDispatcher()
        dispatcher.failing = False
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [
            [interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")],
            [text_chunk("Hello again"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        log = BatchLog()
        controller.subscribe(log, deliver_on_main=True)
        controller.send("delete x")
        assert controller.snapshot()["pendingApproval"] is not None

        dispatcher.failing = True  # the host's main-thread queue goes away under the card
        with caplog.at_level(logging.ERROR, logger="talk2view.controller"):
            controller.cancel()  # must not raise

        self._assert_the_denial_failed_cleanly(controller, t2v, "dispatcher is shut down", caplog)
        dispatcher.failing = False
        controller.send("hello")
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["error"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "Hello again"
        assert log.batches[-1]["seq"] == controller.snapshot()["seq"]


class TestTheResumeOverlapsTheClosingResponse:
    """Belongs with the hardening tests above. Ending the run inside `_consume`'s locked
    section moves `_end_run` ahead of `_consume`'s `finally`, so the card can be published and
    answered while the worker is still closing the paused response -- an ordering that was
    impossible before. It is safe because nothing the resume needs is per-stream:
    `Talk2View._active_cancel` is cleared only `if self._active_cancel is token`, a session
    holds no per-stream slot, and the controller never calls `stop()`. The ordering can only be
    seen by parking the close itself, which is what the first test does; the second pins the
    `_active_cancel` identity guard at the `Talk2View` level.
    """

    def test_the_resume_runs_while_the_paused_response_is_still_closing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old worker is released only once the resume is MID-STREAM, and is then waited
        on until its whole `_run` has returned -- `finally` (`_end_run`, `_flush`) included.
        The resume's run must come through that untouched: still the active run, still
        loading, and then it finishes cleanly."""
        t2v = FakeT2V()
        resume_gate = Gate()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        t2v.approval_scripts = [[text_chunk("Sent"), resume_gate, text_chunk("!"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)

        closing = threading.Event()
        release = threading.Event()
        parked = threading.Event()  # only the FIRST close is parked, not the resume's own
        real_close = controller_module._close_stream

        def gated_close(stream: Any) -> None:
            if not parked.is_set():
                parked.set()
                closing.set()
                assert release.wait(2.0)
            real_close(stream)

        monkeypatch.setattr(controller_module, "_close_stream", gated_close)

        run_ids: List[int] = []  # in start order: the paused turn's run, then the resume's
        old_run_returned = threading.Event()
        real_run = controller._run

        def tracked_run(run_id: int, *args: Any) -> None:
            run_ids.append(run_id)
            try:
                real_run(run_id, *args)
            finally:
                if run_id == run_ids[0]:
                    old_run_returned.set()

        controller._run = tracked_run  # type: ignore[method-assign]
        try:
            controller.send("send email")
            assert closing.wait(2.0)  # the worker is inside `_consume`'s finally, response open

            # Any flusher publishes the card here (`snapshot()` cuts the batch), and a host
            # answering from it opens the resume with the paused response still unclosed.
            assert controller.snapshot()["pendingApproval"] is not None
            controller.approve(Decision(action="once"))
            assert resume_gate.entered.wait(2.0)  # the resume is mid-stream
            assert old_run_returned.is_set() is False

            release.set()  # the old response closes only now, with the resume still streaming
            assert old_run_returned.wait(2.0)

            old_run, resume_run = run_ids
            assert old_run != resume_run
            # The abandoned worker's `_end_run` is guarded on its own run id: it ended nothing.
            assert controller._active_run == resume_run
            assert controller.snapshot()["isLoading"] is True
            assert controller.wait_idle(0) is False
            assert controller.snapshot()["messages"][-1]["content"] == "Sent"

            resume_gate.release.set()
            assert controller.wait_idle(2.0) is True
        finally:
            release.set()  # on a failed assertion, never leave a worker parked
            resume_gate.release.set()
            del controller._run  # the class's own method again

        assert t2v.approval_calls[0]["decision"] == Decision(action="once")
        snap = controller.snapshot()
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert snap["pendingApproval"] is None
        assert snap["messages"][-1]["content"] == "Sent!"
        assert snap["messages"][-1]["isStreaming"] is False
        assert controller._active_run is None
        # The overlap cancelled nothing: the two tokens are independent.
        assert t2v.chat_calls[0]["cancel"].cancelled is False
        assert t2v.approval_calls[0]["cancel"].cancelled is False

    def test_closing_the_paused_stream_leaves_stop_aimed_at_the_resume(self) -> None:
        """Two overlapping generators each register their own token in
        `Talk2View._active_cancel`; the older one's `finally` clears the slot only while it
        still holds ITS token. So closing the paused `chat()` after `respond_to_approval()`
        has started leaves `stop()` aimed at the resume, not at nothing."""
        approval = ChatEvent(
            type="approval_required",
            kind="client",
            tool_name="send_email",
            tool_call_id="call_1",
            arguments={},
        )

        class _Session:
            def send_message(self, message: str, **kwargs: Any) -> Iterator[ChatEvent]:
                yield approval

            def respond_to_approval(
                self, request: ChatEvent, decision: Decision, cancel: Any = None
            ) -> Iterator[ChatEvent]:
                yield ChatEvent(type="text", content="Sent")
                yield ChatEvent(type="done", thread_id="thread_1")

        paused_token = CancelToken()
        resume_token = CancelToken()
        with Talk2View("pk_test", base_url="http://localhost:9999") as t2v:
            t2v._current_session = _Session()  # type: ignore[assignment]

            paused = t2v.chat("send email", cancel=paused_token)
            assert next(paused).type == "approval_required"
            assert t2v._active_cancel is paused_token

            resume = t2v.respond_to_approval(approval, Decision(action="once"), cancel=resume_token)
            assert next(resume).type == "text"
            assert t2v._active_cancel is resume_token

            paused.close()  # the paused turn's worker closes its response only now
            assert t2v._active_cancel is resume_token  # its `finally` left the resume's slot

            t2v.stop()
            assert resume_token.cancelled is True
            assert paused_token.cancelled is False
            resume.close()
            assert t2v._active_cancel is None


class TestCancelHistoryIntegrity:
    def test_a_completed_tools_only_turn_survives_a_late_cancel(self) -> None:
        """Correctness ruling: a turn that already received `done` is COMPLETE. A cancel
        landing after that (inside the natural end, shape (ii) above) must not roll the user
        turn out of `_history`, even though the turn produced no assistant TEXT (tools-only) --
        `_finalize`'s orphan-rollback is gated on `cancelled`, and without the fix
        `token.cancelled` reads True here purely because of when the cancel happened to land,
        not because the turn was actually interrupted."""
        t2v = FakeT2V()
        controller = make_controller(t2v, executor=thread_executor)

        def cancel_after_done(cancel: Optional[CancelToken]) -> None:
            controller.cancel()

        t2v.chat_scripts = [
            [tool_call_chunk("list_files", "call_1", {}), stop_chunk(), cancel_after_done],
            [stop_chunk()],
        ]

        controller.send("list files")
        assert controller.wait_idle(2.0) is True
        # Tools-only: kept on screen, but no text -> no assistant entry is added to `_history`
        # either way. The bug this pins is losing the USER entry too.
        assert [m["role"] for m in controller.snapshot()["messages"]] == ["user", "assistant"]

        controller.send("continue")
        assert controller.wait_idle(2.0) is True
        assert t2v.chat_calls[1]["history"] == [
            ChatMessage(role="user", content="list files"),
        ]


class TestCancelAndQueuedToolHandlers:
    def test_cancelling_while_a_handler_is_still_queued_never_runs_it(self) -> None:
        """A client tool handler queued for the host's main thread through
        `run_on_main` must never run once `cancel()` fires, and the controller must still
        reach idle with no error. `QueueDispatcher(owner_thread_id=-1)` never matches a real
        thread id, so `run_on_main` always queues instead of running inline; nothing pumps it,
        so the handler can only ever run if genuinely dispatched -- which it must not be."""
        t2v = FakeT2V()
        dispatcher = QueueDispatcher(owner_thread_id=-1)
        handler_ran = threading.Event()
        entered = threading.Event()

        def call_run_on_main(cancel: Optional[CancelToken]) -> None:
            entered.set()

            def fn() -> None:
                handler_ran.set()

            run_on_main(dispatcher, fn, cancel=cancel)  # blocks until cancel(); raises Cancelled

        t2v.chat_scripts = [[call_run_on_main]]
        controller = make_controller(t2v, executor=thread_executor)

        controller.send("run a tool")
        assert entered.wait(2.0)

        controller.cancel()

        assert controller.wait_idle(2.0) is True
        assert handler_ran.is_set() is False
        assert controller.snapshot()["error"] is None
