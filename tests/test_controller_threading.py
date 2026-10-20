"""Threading model: worker streams, main-thread handlers, seq-ordered delivery."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterator, List, Optional

import pytest

from talk2view.cancel import CancelToken
from talk2view.controller import _REFLUSH_LIMIT, ChatController, ControllerClosedError
from talk2view.dispatch import QueueDispatcher, run_on_main
from talk2view.state import ChatSnapshot, OpBatch, apply_batch
from talk2view.types import ChatEvent, Decision

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    Gate,
    RaisingDispatcher,
    error_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
)

#: How long a parked emit window waits for the batch that must NOT overtake it. A bounded
#: wait is the only way to assert a "must not happen" property: a correct controller
#: serialises the racing flush behind the park, so the event is never set and this expires.
#: A broken one delivers straight away, sets it, and the wait returns at once.
BLOCKED_GRACE = 0.2


def thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def pump_until(dispatcher: QueueDispatcher, done: Callable[[], bool], timeout: float = 3.0) -> None:
    """What a host's main-thread timer does: drain the queue until ``done()``."""
    deadline = time.monotonic() + timeout
    while not done():
        dispatcher.pump()
        if time.monotonic() > deadline:
            raise AssertionError("timed out pumping the dispatcher")
        time.sleep(0.005)
    dispatcher.pump()


def drain(dispatcher: QueueDispatcher) -> None:
    """Run everything queued, including callables the queued callbacks post themselves."""
    while dispatcher.pump():
        pass


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    """Wait for a condition another thread establishes, the way ``pump_until`` waits for one.

    The condition is the signal -- the bound only turns a hang into a failing assertion. Used
    where the state to wait for is a controller invariant (ops recorded but not yet cut) that
    no ``Event`` in the fakes can announce.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.001)
    return True


def assert_wait_idle_untimed(controller: ChatController, timeout: float = 5.0) -> None:
    """``wait_idle(None)`` must return on every exit path. It runs on a helper thread so a
    controller that never goes idle fails this assertion instead of hanging the suite."""
    results: List[bool] = []

    def wait() -> None:
        results.append(controller.wait_idle(None))

    waiter = threading.Thread(target=wait, daemon=True)
    waiter.start()
    waiter.join(timeout)
    assert not waiter.is_alive(), "wait_idle(None) never returned"
    assert results == [True]


class ToolRunningT2V(FakeT2V):
    """chat() does what the real session does on a client-tool interrupt: it runs the
    handler through ``run_on_main`` from inside the stream, i.e. from the worker thread."""

    def __init__(self, dispatcher: QueueDispatcher, handler: Callable[[], str]) -> None:
        super().__init__(dispatcher)
        self._handler = handler
        self.stream_thread: Optional[int] = None

    def chat(self, message: str, *args: object, **kwargs: object) -> Iterator[ChatEvent]:  # type: ignore[override]
        cancel = kwargs.get("cancel")
        assert isinstance(cancel, CancelToken)
        dispatcher = self.tools.dispatcher

        def stream() -> Iterator[ChatEvent]:
            self.stream_thread = threading.get_ident()
            yield ChatEvent(
                type="tool_call", tool_name="set_layout", tool_call_id="c1", arguments={}
            )
            result = run_on_main(dispatcher, self._handler, cancel)
            yield ChatEvent(type="text", content=result)
            yield ChatEvent(type="done", thread_id="thread_1")

        return stream()


class TestThreads:
    def test_handler_runs_on_pump_thread_while_stream_on_worker(self) -> None:
        main = threading.get_ident()
        dispatcher = QueueDispatcher()  # owned by this (the "host main") thread
        handler_threads: List[int] = []

        def handler() -> str:
            handler_threads.append(threading.get_ident())
            return "layout changed"

        t2v = ToolRunningT2V(dispatcher, handler)
        controller = make_controller(t2v, executor=thread_executor)

        controller.send("four-up please")  # returns at once; this thread is free to pump
        pump_until(dispatcher, lambda: controller.wait_idle(0))

        assert handler_threads == [main]
        assert t2v.stream_thread is not None
        assert t2v.stream_thread != main
        snap = controller.snapshot()
        assert snap["messages"][1]["steps"][0]["name"] == "set_layout"
        assert snap["messages"][2]["content"] == "layout changed"
        assert snap["isLoading"] is False

    def test_deliver_on_main_posts_batches_through_the_dispatcher(self) -> None:
        main = threading.get_ident()
        dispatcher = QueueDispatcher()
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [[text_chunk("a"), text_chunk("b"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        start = controller.snapshot()
        on_main = BatchLog()
        on_worker = BatchLog()
        controller.subscribe(on_main, deliver_on_main=True)
        controller.subscribe(on_worker)

        controller.send("hi")
        assert controller.wait_idle(3.0) is True
        assert on_main.batches == []  # posted, not run: nothing arrives until the host pumps
        dispatcher.pump()

        assert set(on_main.threads) == {main}
        assert main not in set(on_worker.threads[1:])  # batch 1 is emitted by send() itself
        assert on_main.seqs() == on_worker.seqs() == list(range(1, len(on_main.batches) + 1))
        assert on_main.replay(start) == controller.snapshot()

    def test_a_never_pumped_main_thread_subscriber_does_not_block_the_run(self) -> None:
        """Delivery to the main thread is post-and-forget: a host that stops pumping stalls
        its own batches and nothing else. ``run_on_main`` waits untimed and must not be used."""
        dispatcher = QueueDispatcher()
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        log = BatchLog()
        controller.subscribe(log, deliver_on_main=True)

        controller.send("x")

        assert controller.wait_idle(3.0) is True  # the worker never waited for the pump
        assert log.batches == []
        assert controller.snapshot()["isLoading"] is False
        assert dispatcher.pump() > 0  # the batches were waiting in the queue all along
        assert log.seqs() == list(range(1, len(log.batches) + 1))

    def test_unsubscribing_drops_batches_already_queued_for_the_main_thread(self) -> None:
        """Delivery to the main thread is deferred, so a batch can be waiting in the queue
        when the host unsubscribes. It must not be handed over after that."""
        dispatcher = QueueDispatcher()
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        log = BatchLog()
        unsubscribe = controller.subscribe(log, deliver_on_main=True)

        controller.send("x")
        assert controller.wait_idle(3.0) is True
        unsubscribe()
        assert dispatcher.pump() > 0  # the queued batches ran

        assert log.batches == []  # ...but the unsubscribed callback was not called

    def test_a_main_thread_subscriber_may_mutate_state_while_the_worker_streams(self) -> None:
        """Cross-thread contention: the pump thread re-enters the controller from inside a
        delivery while the worker and a third thread are emitting. Nothing deadlocks (the
        emit lock is only ever taken BEFORE the state lock) and every batch keeps its order."""
        main = threading.get_ident()
        dispatcher = QueueDispatcher()
        t2v = FakeT2V(dispatcher)
        words = ["w{} ".format(i) for i in range(50)]
        t2v.chat_scripts = [[text_chunk(w) for w in words] + [stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        start = controller.snapshot()
        on_main = BatchLog()
        on_worker = BatchLog()

        def reactive(batch: OpBatch) -> None:
            on_main(batch)
            if controller.snapshot()["error"] is not None:
                controller.clear_error()  # mutates state from inside the delivery

        controller.subscribe(reactive, deliver_on_main=True)
        controller.subscribe(on_worker)

        def hammer() -> None:
            for _ in range(50):
                with controller._lock:
                    controller._rec.set(["error"], "x")
                controller._flush()

        noise = threading.Thread(target=hammer, daemon=True)
        controller.send("go")
        noise.start()
        pump_until(dispatcher, lambda: controller.wait_idle(0) and not noise.is_alive(), 10.0)
        noise.join(10.0)
        assert controller.wait_idle(10.0) is True
        drain(dispatcher)

        assert set(on_main.threads) == {main}
        assert on_worker.seqs() == list(range(1, len(on_worker.batches) + 1))
        assert on_main.seqs() == on_worker.seqs()
        assert on_main.replay(start) == controller.snapshot()
        assert controller.snapshot()["messages"][1]["content"] == "".join(words)


class TestOrdering:
    def test_batches_delivered_in_seq_order_under_concurrency(self) -> None:
        t2v = FakeT2V()
        words = ["w{} ".format(i) for i in range(200)]
        t2v.chat_scripts = [[text_chunk(w) for w in words] + [stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        def hammer() -> None:  # other threads mutating state while the worker streams
            for _ in range(150):
                with controller._lock:
                    controller._rec.set(["error"], "x")
                controller._flush()
                controller.clear_error()

        hammers = [threading.Thread(target=hammer, daemon=True) for _ in range(4)]
        for h in hammers:
            h.start()
        controller.send("go")
        for h in hammers:
            h.join(10.0)
        assert controller.wait_idle(10.0) is True
        controller.clear_error()

        assert log.seqs() == list(range(1, len(log.batches) + 1))  # no gap, no reordering
        assert log.replay(start) == controller.snapshot()
        assert controller.snapshot()["messages"][1]["content"] == "".join(words)

    def test_a_subscriber_that_calls_back_in_keeps_the_order_for_everyone(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[RuntimeError("boom")]]
        controller = make_controller(t2v)
        late = BatchLog()

        def reactive(batch: OpBatch) -> None:
            # Clears the error from inside the delivery callback.
            if any(op["path"] == ["error"] and op["value"] for op in batch["ops"]):
                controller.clear_error()

        controller.subscribe(reactive)
        controller.subscribe(late)  # registered after: must still see batches in seq order

        controller.send("x")

        assert late.seqs() == list(range(1, len(late.batches) + 1))
        assert controller.snapshot()["error"] is None

    def test_a_subscriber_that_approves_from_its_callback_keeps_the_order(self) -> None:
        """The heaviest re-entrant call a host makes: answering the approval from inside the
        batch that announced it, which starts a whole new run on the delivering thread."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("set_layout", "c1", {"layout": "four-up"})]]
        t2v.approval_scripts = [[text_chunk("done"), stop_chunk()]]
        controller = make_controller(t2v)  # inline executor: the nested run is this thread's
        start = controller.snapshot()
        late = BatchLog()

        def approver(batch: OpBatch) -> None:
            if controller.snapshot()["pendingApproval"] is not None:
                controller.approve(Decision(action="once"))

        controller.subscribe(approver)
        controller.subscribe(late)

        controller.send("four-up please")

        assert late.seqs() == list(range(1, len(late.batches) + 1))
        assert late.replay(start) == controller.snapshot()
        assert controller.snapshot()["pendingApproval"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "done"
        assert controller.wait_idle(0) is True

    def test_clear_and_the_abandoned_worker_never_deliver_out_of_order(self) -> None:
        """The hazard left open earlier: ``clear()`` abandons a live run but keeps the
        subscribers, so the unwinding worker, ``clear()``'s own delete job and the next
        ``send()`` all deliver batches. Cutting and delivering under one emit lock is what
        stops a subscriber seeing batch n+1 before n (and raising ``SeqGapError``)."""
        main = threading.get_ident()
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("Partial"), gate]]
        controller = make_controller(t2v, executor=thread_executor)
        start = controller.snapshot()
        clearing = threading.Event()
        parked = threading.Event()
        overtaken = threading.Event()
        parked_seq: List[int] = []
        overtook_the_park: List[int] = []
        late = BatchLog()

        def blocker(batch: OpBatch) -> None:
            # Hold the emit window open on the first batch a background thread delivers once
            # the abandoned run is over -- the moment the next send() can race it.
            if (
                clearing.is_set()
                and threading.get_ident() != main
                and controller._active_run is None
                and not parked.is_set()
            ):
                parked_seq.append(batch["seq"])
                parked.set()
                if overtaken.wait(BLOCKED_GRACE):
                    overtook_the_park.append(batch["seq"])

        def watcher(batch: OpBatch) -> None:
            late(batch)
            if parked_seq and batch["seq"] > parked_seq[0]:
                overtaken.set()

        controller.subscribe(blocker)
        controller.subscribe(watcher)  # registered after: it is the one that sees a reorder

        controller.send("hi")
        assert gate.entered.wait(3.0)
        clearing.set()
        controller.clear()  # abandons the gated worker, starts the session-delete job
        assert parked.wait(3.0)
        controller.send("again")  # its batch must queue behind the parked delivery
        overtaken.set()  # release the park; a no-op if the wait already returned
        assert controller.wait_idle(5.0) is True

        assert overtook_the_park == []  # nothing was delivered while the window was open
        assert late.seqs() == list(range(1, len(late.batches) + 1))  # no gap, no reordering
        assert late.replay(start) == controller.snapshot()

    def test_wait_idle_returns_only_after_the_last_batch_was_delivered(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        seen: List[int] = []

        def slow(batch: OpBatch) -> None:
            time.sleep(0.05)
            seen.append(batch["seq"])

        controller.subscribe(slow)
        controller.send("x")
        assert controller.wait_idle(5.0) is True
        assert seen[-1] == controller.snapshot()["seq"]


class TestSnapshotOnTheBatchLine:
    """``snapshot()`` is the late-join protocol's replay base (``subscribe``'s docstring, and
    what the UI server does on every SSE connect and every gap recovery). It only works if the copy
    is taken ON a batch boundary: a state carrying ops that have not been cut yet still has
    the previous ``seq``, so the consumer replays those ops a second time."""

    def test_a_snapshot_taken_while_ops_are_uncut_is_still_a_replay_base(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [
            [error_chunk("boom"), gate, text_chunk("w0"), text_chunk("w1"), stop_chunk()]
        ]
        controller = make_controller(t2v, executor=thread_executor)
        log = BatchLog()
        armed = threading.Event()
        parked = threading.Event()
        release = threading.Event()

        def blocker(batch: OpBatch) -> None:
            # Holds the emit window open on the holder thread, so the ops the worker records
            # next sit recorded-but-uncut for as long as the park lasts.
            if armed.is_set() and not parked.is_set():
                parked.set()
                release.wait(BLOCKED_GRACE)

        controller.subscribe(blocker)
        controller.subscribe(log)

        controller.send("go")
        assert gate.entered.wait(3.0)  # the worker is parked mid-stream; nothing is pending
        armed.set()
        holder = threading.Thread(target=controller.clear_error, daemon=True)
        holder.start()
        assert parked.wait(3.0)  # the emit window is held open by the holder
        gate.release.set()  # ...and the worker records text it cannot cut
        assert wait_until(lambda: controller._rec.pending), "the worker recorded nothing"

        taken: List[ChatSnapshot] = []
        snapper = threading.Thread(target=lambda: taken.append(controller.snapshot()), daemon=True)
        snapper.start()
        snapper.join(5.0)
        assert not snapper.is_alive(), "snapshot() never returned"
        release.set()  # a no-op once the park's own grace has expired
        holder.join(5.0)
        assert controller.wait_idle(5.0) is True

        snap = taken[0]
        replayed = snap
        for batch in log.batches:
            if batch["seq"] > snap["seq"]:
                replayed = apply_batch(replayed, batch)
        assert replayed == controller.snapshot()

    def test_snapshot_from_an_emitting_thread_callback_does_not_deadlock(self) -> None:
        """A ``deliver_on_main=False`` callback runs inside the emit window, on the emitting
        thread. Cutting a batch there would put batch n+1 in front of n for every later
        subscriber, so ``snapshot()`` takes the plain copy instead of waiting on itself."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        seen: List[ChatSnapshot] = []

        controller.subscribe(lambda _batch: seen.append(controller.snapshot()))
        controller.send("x")

        assert controller.wait_idle(3.0) is True
        assert seen, "no batch was delivered"
        assert seen[-1]["messages"][1]["content"] == "hi"

    def test_snapshot_from_a_main_thread_callback_does_not_deadlock(self) -> None:
        """A ``deliver_on_main=True`` callback runs on the pump thread, OUTSIDE the window the
        emitting thread holds, so its ``snapshot()`` goes through the flush like any other
        caller. Delivery there is post-and-forget, so the two can never wait on each other."""
        dispatcher = QueueDispatcher()
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [[text_chunk("a"), text_chunk("b"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        seen: List[ChatSnapshot] = []

        def watch(_batch: OpBatch) -> None:
            seen.append(controller.snapshot())

        controller.subscribe(watch, deliver_on_main=True)
        controller.send("x")
        assert controller.wait_idle(3.0) is True
        drain(dispatcher)

        assert seen, "no batch reached the pump thread"
        assert seen[-1]["messages"][1]["content"] == "ab"

    def test_a_snapshot_waits_for_the_open_emit_window(self) -> None:
        """The same hazard stated as the rule it rests on, with the ``BLOCKED_GRACE`` pattern
        of ``TestOrdering``: while another thread holds the emit window, ``snapshot()`` has to
        queue behind it like every other emitter, and cannot read state cut out from under it.
        A bounded park is the only way to assert a "must not happen": a correct controller
        leaves the snapshot waiting until the park expires on its own."""
        main = threading.get_ident()
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[error_chunk("boom"), gate, text_chunk("w0"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        armed = threading.Event()
        parked = threading.Event()
        snapshot_done = threading.Event()
        returned_inside_the_window: List[bool] = []
        log = BatchLog()

        def blocker(batch: OpBatch) -> None:
            if armed.is_set() and threading.get_ident() != main and not parked.is_set():
                parked.set()
                snapshot_done.wait(BLOCKED_GRACE)  # nothing releases this early
                returned_inside_the_window.append(snapshot_done.is_set())

        def snap() -> None:
            controller.snapshot()
            snapshot_done.set()

        controller.subscribe(blocker)
        controller.subscribe(log)

        controller.send("go")
        assert gate.entered.wait(3.0)
        armed.set()
        holder = threading.Thread(target=controller.clear_error, daemon=True)
        holder.start()
        assert parked.wait(3.0)
        gate.release.set()
        assert wait_until(lambda: controller._rec.pending), "the worker recorded nothing"

        snapper = threading.Thread(target=snap, daemon=True)
        snapper.start()
        snapper.join(5.0)
        assert not snapper.is_alive(), "snapshot() never returned"
        holder.join(5.0)
        assert controller.wait_idle(5.0) is True

        assert returned_inside_the_window == [False]
        assert log.seqs() == list(range(1, len(log.batches) + 1))  # no gap, no reordering


class TestIdle:
    def test_a_flush_with_nothing_to_deliver_still_sets_idle(self) -> None:
        """Under the emit lock another thread may have already taken the last ops, so the
        flush that follows the end of a run can find nothing to cut. It must still go idle."""
        controller = make_controller()
        controller.send("x")
        assert controller.wait_idle(0) is True
        controller._idle.clear()

        controller._flush()  # nothing pending, nothing to deliver

        assert controller.wait_idle(0) is True

    def test_wait_idle_untimed_returns_after_a_run(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("hi"), stop_chunk()]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("x")
        assert_wait_idle_untimed(controller)

    def test_wait_idle_untimed_returns_after_close_during_a_live_run(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("Partial"), gate]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert gate.entered.wait(3.0)

        controller.close()

        assert_wait_idle_untimed(controller)

    def test_wait_idle_untimed_returns_after_clear_abandons_a_run(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("Partial"), gate]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert gate.entered.wait(3.0)

        controller.clear()

        assert_wait_idle_untimed(controller)
        assert t2v.cleared == 1


class TestHostMisbehaviour:
    """A host can break the delivery window three ways. None may wedge the controller."""

    def test_a_raising_host_dispatcher_does_not_leave_the_controller_busy(self) -> None:
        dispatcher = RaisingDispatcher()
        t2v = FakeT2V(dispatcher)
        t2v.chat_scripts = [[text_chunk("recovered"), stop_chunk()]]
        controller = make_controller(t2v)  # inline executor: send() runs the whole turn
        log = BatchLog()
        controller.subscribe(log, deliver_on_main=True)

        with pytest.raises(RuntimeError, match="dispatcher is shut down"):
            controller.send("x")

        # Surfaced exactly as a refusing executor is: the run is undone, not left in flight.
        # The turn died in the flush that precedes the job, so it never reached the network.
        assert t2v.chat_calls == []
        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["isLoading"] is False
        assert controller.snapshot()["error"] == "dispatcher is shut down"

        dispatcher.failing = False
        controller.send("again")

        assert controller.wait_idle(2.0) is True
        assert controller.snapshot()["messages"][-1]["content"] == "recovered"
        assert log.seqs() == list(range(min(log.seqs()), max(log.seqs()) + 1))

    def test_a_dispatcher_that_fails_twice_surfaces_the_first_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After undoing the run, `_launch` flushes again -- through the same broken
        dispatcher. `send()` and `approve()` promise the original failure re-raised unchanged,
        so the caller gets the FIRST error, and the second is logged, not raised over it."""
        first = RuntimeError("first post failed")
        second = RuntimeError("second post failed")

        class TwiceFailingDispatcher:
            def __init__(self) -> None:
                self.failures = [first, second]

            def post(self, fn: Callable[[], None]) -> None:
                if self.failures:
                    raise self.failures.pop(0)
                fn()

            def is_main_thread(self) -> bool:
                return False

        t2v = FakeT2V(TwiceFailingDispatcher())
        t2v.chat_scripts = [[text_chunk("recovered"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.subscribe(BatchLog(), deliver_on_main=True)

        with caplog.at_level(logging.ERROR, logger="talk2view.controller"):
            with pytest.raises(RuntimeError) as raised:
                controller.send("x")

        assert raised.value is first
        assert [r.exc_info[1] for r in caplog.records if r.exc_info] == [second]
        assert t2v.chat_calls == []
        assert controller.wait_idle(0) is True
        snap = controller.snapshot()
        assert snap["isLoading"] is False
        assert snap["error"] == "first post failed"

        controller.send("again")  # both failures are spent: the dispatcher works again
        assert controller.snapshot()["messages"][-1]["content"] == "recovered"

    def test_a_callback_that_records_on_every_batch_cannot_spin_forever(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A subscriber that records an op every time it is called would re-flush forever,
        holding the emit lock: a silent wedge. The loop gives up and says so instead."""
        controller = make_controller()
        deliveries: List[int] = []

        def greedy(batch: OpBatch) -> None:
            deliveries.append(batch["seq"])
            with controller._lock:
                controller._rec.set(["error"], "e{}".format(batch["seq"]))
            controller._flush()  # nested: only asks the outer loop for another round

        controller.subscribe(greedy)
        with controller._lock:
            controller._rec.set(["error"], "seed")

        with caplog.at_level(logging.ERROR, logger="talk2view.controller"):
            flusher = threading.Thread(target=controller._flush, daemon=True)
            flusher.start()
            flusher.join(5.0)

        assert not flusher.is_alive(), "the re-flush loop never terminated"
        assert deliveries == list(range(1, _REFLUSH_LIMIT + 1))
        assert sum("feedback loop" in r.message for r in caplog.records) == 1
        assert controller.wait_idle(2.0) is True
        # `_reflush` was left clear, so the ops the last callback recorded are simply pending
        # and the next external flush delivers them.
        assert controller._rec.pending is True
        assert controller._reflush is False

    def test_the_ops_a_capped_reflush_left_pending_arrive_on_the_next_flush(self) -> None:
        """The cap gives the emit lock back with the last callback's ops still recorded. They
        are not lost: the next flush from outside (any public call, or the run's own) cuts
        them as the batch after the capped one, in order."""
        controller = make_controller()
        log = BatchLog()
        greedy_batches: List[int] = []

        def greedy(batch: OpBatch) -> None:
            greedy_batches.append(batch["seq"])
            with controller._lock:
                controller._rec.set(["error"], "e{}".format(batch["seq"]))
            controller._flush()

        unsubscribe = controller.subscribe(greedy)
        controller.subscribe(log)
        with controller._lock:
            controller._rec.set(["error"], "seed")
        controller._flush()  # spins to the cap, leaves `e100` pending
        assert controller._rec.pending is True
        unsubscribe()  # the runaway host stops; whatever flushes next delivers the rest

        controller.clear_error()  # an ordinary public call: records `None`, flushes

        assert greedy_batches == list(range(1, _REFLUSH_LIMIT + 1))  # none after unsubscribe
        assert log.seqs() == list(range(1, _REFLUSH_LIMIT + 2))
        last = log.batches[-1]
        assert last["seq"] == _REFLUSH_LIMIT + 1
        assert last["ops"] == [
            {"type": "set", "path": ["error"], "value": "e{}".format(_REFLUSH_LIMIT)},
            {"type": "set", "path": ["error"], "value": None},
        ]
        assert controller._rec.pending is False
        assert controller.wait_idle(0) is True

    def test_flushing_while_holding_the_state_lock_is_refused(self) -> None:
        """Lock-order guard: ``_flush`` takes ``_emit_lock``, so a caller already holding the
        state lock would invert the order and can deadlock against any other emitter."""
        controller = make_controller()
        if controller._lock_is_owned is None:  # pragma: no cover — CPython always has it
            pytest.skip("this interpreter's RLock does not expose _is_owned")

        with controller._lock:
            with pytest.raises(RuntimeError, match="state lock"):
                controller._flush()

        controller._flush()  # and it is fine the moment the lock is released
        assert controller.wait_idle(0) is True


class TestClose:
    def test_close_cancels_the_run_and_refuses_further_sends(self) -> None:
        t2v = FakeT2V()
        gate = Gate()
        t2v.chat_scripts = [[text_chunk("Partial"), gate]]
        controller = make_controller(t2v, executor=thread_executor)
        controller.send("hi")
        assert gate.entered.wait(2.0)

        controller.close()

        assert t2v.chat_calls[0]["cancel"].cancelled is True
        assert controller.wait_idle(1.0) is True
        with pytest.raises(ControllerClosedError):
            controller.send("again")
