"""talk2view.ui.testing.FakeController: the ChatController contract, scripted."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, Callable, List

import pytest

from talk2view import (
    CANCELLED_APPROVAL_FEEDBACK,
    Attachment,
    ControllerBusyError,
    ControllerClosedError,
    Decision,
    QueueDispatcher,
)
from talk2view.state import OpBatch, initial_snapshot
from talk2view.ui.hub import EventHub
from talk2view.ui.testing import (
    _REFLUSH_LIMIT,
    DEFAULT_REPLY,
    DEMO_PLAN,
    DEMO_PLAN_NOTES,
    FakeController,
    words,
)

from .controller_fakes import BatchLog


class ManualExecutor:
    """Holds jobs until the test runs them: a run is 'in flight' in between."""

    def __init__(self) -> None:
        self.jobs: List[Callable[[], None]] = []

    def __call__(self, job: Callable[[], None]) -> None:
        self.jobs.append(job)

    def run_all(self) -> None:
        while self.jobs:
            self.jobs.pop(0)()


def inline(job: Callable[[], None]) -> None:
    job()


def fake(**kwargs: Any) -> FakeController:
    kwargs.setdefault("executor", inline)
    kwargs.setdefault("reply", "Hello there world")
    return FakeController(**kwargs)


def test_words_keep_their_whitespace() -> None:
    assert words("a b\nc") == ["a ", "b\n", "c"]
    assert "".join(words(DEFAULT_REPLY)) == DEFAULT_REPLY


def test_a_turn_streams_one_batch_per_word_and_replays_exactly() -> None:
    controller = fake()
    log = BatchLog()
    controller.subscribe(log)
    start = controller.snapshot()
    controller.send("hi")
    snap = controller.snapshot()
    assert [(m["role"], m["content"]) for m in snap["messages"]] == [
        ("user", "hi"),
        ("assistant", "Hello there world"),
    ]
    assert snap["isLoading"] is False and snap["messages"][1]["isStreaming"] is False
    assert snap["threadId"] == "fake-thread" and snap["error"] is None
    deltas = [b for b in log.batches if b["ops"][0]["type"] == "append-text"]
    assert len(deltas) == 3
    assert log.seqs() == list(range(1, snap["seq"] + 1))
    assert log.replay(start) == snap
    assert controller.calls == [("send", ("hi", None))]


def test_attachments_land_on_the_user_message() -> None:
    controller = fake()
    attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    controller.send("see", [attachment])
    assert controller.snapshot()["messages"][0]["attachments"] == [
        {"id": "a1", "filename": "x.png", "mime_type": "image/png", "size_bytes": 3}
    ]


def test_approve_prefix_pauses_on_a_client_approval_and_always_is_remembered() -> None:
    controller = fake()
    controller.send("/approve load it")
    snap = controller.snapshot()
    pending = snap["pendingApproval"]
    assert pending is not None and pending["toolName"] == "load_sample_data"
    assert snap["isLoading"] is False and snap["messages"][1]["isStreaming"] is True
    assert snap["messages"][1]["steps"] == [
        {"name": "load_sample_data", "status": "running", "args": {"name": "MRHead"}}
    ]
    with pytest.raises(ControllerBusyError):
        controller.send("again")
    controller.approve(Decision("once"), tool_call_id="stale")
    assert controller.snapshot()["pendingApproval"] is not None  # a stale click answers nothing
    controller.approve(Decision("always"), tool_call_id=pending["toolCallId"])
    snap = controller.snapshot()
    assert snap["pendingApproval"] is None and snap["alwaysAllowedTools"] == ["load_sample_data"]
    assert snap["messages"][1]["steps"][0]["status"] == "used"
    assert snap["messages"][1]["content"].endswith("MRHead is loaded.\n")
    assert snap["isLoading"] is False


def test_cancel_with_an_approval_pending_denies_it() -> None:
    controller = fake(ask_approval=True)
    controller.send("anything")
    controller.cancel()
    step = controller.snapshot()["messages"][1]["steps"][0]
    assert step["status"] == "denied" and step["result"] == CANCELLED_APPROVAL_FEEDBACK
    assert ("approve", (Decision("deny", feedback=CANCELLED_APPROVAL_FEEDBACK), "call-2")) in (
        controller.calls
    )


def test_cancel_mid_stream_keeps_the_partial_reply_without_an_error() -> None:
    executor = ManualExecutor()
    controller = fake(executor=executor)
    controller.send("go")
    assert controller.snapshot()["isLoading"] is True
    with pytest.raises(ControllerBusyError):
        controller.send("again")
    assert not controller.wait_idle(0.0)
    controller.cancel()
    executor.run_all()
    snap = controller.snapshot()
    assert snap["isLoading"] is False and snap["error"] is None
    assert snap["messages"][1]["content"] == "" and snap["messages"][1]["isStreaming"] is False
    assert controller.wait_idle(0.0)


def test_fail_prefix_ends_in_an_error_and_clear_error_clears_it() -> None:
    controller = fake()
    controller.send("/fail please")
    assert controller.snapshot()["error"] == "Scripted failure"
    controller.clear_error()
    assert controller.snapshot()["error"] is None


@pytest.mark.parametrize("text, plan", [("/plan", DEMO_PLAN), ("/plan-notes", DEMO_PLAN_NOTES)])
def test_plan_prefix_gives_the_reply_a_plan_and_finishes(text: str, plan: str) -> None:
    controller = fake()
    controller.send(text)
    snap = controller.snapshot()
    reply = snap["messages"][-1]
    assert reply["plan"] == plan
    assert reply["content"] == "Hello there world"
    assert reply["isStreaming"] is False and snap["isLoading"] is False


def test_an_ordinary_turn_has_no_plan() -> None:
    controller = fake()
    controller.send("hi")
    assert "plan" not in controller.snapshot()["messages"][-1]


def test_retry_edit_and_clear() -> None:
    controller = fake()
    controller.send("first")
    controller.send("second")
    controller.retry()
    assert [m["content"] for m in controller.snapshot()["messages"] if m["role"] == "user"] == [
        "first",
        "second",
    ]
    first_id = controller.snapshot()["messages"][0]["id"]
    controller.edit(first_id, "edited")
    assert [m["content"] for m in controller.snapshot()["messages"]] == [
        "edited",
        "Hello there world",
    ]
    with pytest.raises(ValueError):
        controller.edit("nope", "x")
    with pytest.raises(ValueError):
        controller.edit(controller.snapshot()["messages"][1]["id"], "x")  # an assistant id
    controller.clear()
    snap = controller.snapshot()
    expected = dict(initial_snapshot(), seq=snap["seq"], threadId=None)
    assert snap == expected


def test_conversation_replaced_fires_for_restore_and_a_snapshot_carrying_exchange() -> None:
    """FakeController mirrors the real ChatController's contract
    exactly (this module's own docstring), so a host built and tested against the fake
    (the runtime guard, the 3D Slicer extension's own e2e backend) sees the same notice."""
    controller = fake()
    seen: List[str] = []
    controller.on("conversationReplaced", lambda: seen.append("replaced"))
    empty = {"messages": [], "history": [], "threadId": None}

    controller.restore_conversation(empty)  # type: ignore[arg-type]
    assert seen == ["replaced"]

    controller.exchange_conversation(empty, epoch=0)  # type: ignore[arg-type]
    assert seen == ["replaced", "replaced"]

    controller.exchange_conversation(None, epoch=0)  # export-only: nothing replaced
    assert seen == ["replaced", "replaced"]


def test_clear_is_allowed_while_busy_and_abandons_the_run() -> None:
    executor = ManualExecutor()
    controller = fake(executor=executor)
    controller.send("go")
    controller.clear()
    executor.run_all()  # the abandoned run writes nothing
    assert controller.snapshot()["messages"] == []
    assert controller.wait_idle(0.0)


def test_closed_controller_raises_like_the_real_one() -> None:
    controller = fake()
    controller.close()
    for call in (lambda: controller.send("x"), controller.clear, controller.retry):
        with pytest.raises(ControllerClosedError):
            call()
    with pytest.raises(ControllerClosedError):
        controller.subscribe(lambda batch: None)


def test_raise_on_and_call_log() -> None:
    controller = fake()
    controller.raise_on["send"] = RuntimeError("executor refused")
    with pytest.raises(RuntimeError):
        controller.send("x")
    assert controller.calls == [("send", ("x", None))]
    assert controller.snapshot()["messages"] == []


def test_a_subscriber_may_call_back_in_and_snapshot_without_deadlock() -> None:
    controller = fake()
    seen: List[int] = []

    def on_batch(batch: OpBatch) -> None:
        seen.append(len(controller.snapshot()["messages"]))  # a plain copy in here
        if batch["seq"] == 1:
            controller.clear_error()  # records nothing new (error is None): no loop

    controller.subscribe(on_batch)
    controller.send("hi")
    assert seen and seen[-1] == 2


def test_a_raising_subscriber_does_not_stop_the_run() -> None:
    controller = fake()

    def boom(_batch: OpBatch) -> None:
        raise RuntimeError("subscriber bug")

    controller.subscribe(boom)
    controller.send("hi")
    assert controller.snapshot()["messages"][1]["content"] == "Hello there world"


def test_deliver_on_main_posts_through_the_dispatcher_and_unsubscribe_stops() -> None:
    dispatcher = QueueDispatcher()
    controller = fake(dispatcher=dispatcher)
    log = BatchLog()
    unsubscribe = controller.subscribe(log, deliver_on_main=True)
    controller.send("hi")
    assert log.batches == []
    dispatcher.pump()
    assert log.batches and log.seqs()[-1] == controller.snapshot()["seq"]
    unsubscribe()
    controller.send("again")
    dispatcher.pump()
    assert log.seqs()[-1] < controller.snapshot()["seq"]


def test_it_joins_an_event_hub_like_the_real_controller() -> None:
    controller = fake()
    hub = EventHub()
    hub.join(controller)
    controller.send("/approve hi")
    state = hub.snapshot()["state"]
    expected = dict(controller.snapshot())
    expected.pop("seq")
    assert state == expected


# ── Crash- and close-safety parity with the real ChatController ──


def test_a_raising_step_ends_the_run_and_leaves_the_controller_usable() -> None:
    """Mirrors ``ChatController._run``'s own except/finally: a bug in a scripted step must
    not leave the controller busy forever. On the DEFAULT (threaded) executor, with a
    bounded ``wait_idle`` -- an unguarded raise would otherwise hang this test."""
    controller = FakeController(reply="Hello there world")

    def broken_stream(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("scripted step blew up")

    controller._stream = broken_stream  # type: ignore[method-assign]
    controller.send("go")
    assert controller.wait_idle(2.0)
    snap = controller.snapshot()
    assert snap["error"] == "scripted step blew up"
    assert snap["isLoading"] is False

    del controller._stream  # restore the real bound method for the next turn
    controller.send("again")  # not left busy forever
    assert controller.wait_idle(2.0)
    assert controller.snapshot()["messages"][-1]["content"] == "Hello there world"


def test_a_malformed_attachment_ends_the_run_like_the_real_one() -> None:
    """Mirrors ``ChatController._prepare_send``: a failure after the run started and before
    it launched used to leave it active for good (``wait_idle`` never True, every later
    send busy)."""
    controller = fake()
    log = BatchLog()
    controller.subscribe(log)
    start = controller.snapshot()
    with pytest.raises(AttributeError, match="filename"):
        controller.send("look", [SimpleNamespace(id="a1")])  # type: ignore[list-item]
    assert controller.wait_idle(1.0)
    snap = controller.snapshot()
    assert snap["isLoading"] is False and "filename" in (snap["error"] or "")
    assert log.replay(start) == snap  # the run's end was delivered
    controller.send("again")
    assert [m["content"] for m in controller.snapshot()["messages"]] == [
        "again", "Hello there world",
    ]


@pytest.mark.parametrize("start", ["send", "retry", "edit"])
def test_a_failure_while_a_turn_is_recorded_ends_the_run_like_the_real_one(
    monkeypatch: pytest.MonkeyPatch, start: str
) -> None:
    controller = fake()
    controller.send("first")
    user_id = controller.snapshot()["messages"][0]["id"]
    log = BatchLog()
    controller.subscribe(log)
    before = controller.snapshot()

    def exploded() -> str:
        raise RuntimeError("clock exploded")

    monkeypatch.setattr("talk2view.ui.testing._now", exploded)
    with pytest.raises(RuntimeError, match="clock exploded"):
        if start == "send":
            controller.send("more")
        elif start == "retry":
            controller.retry()
        else:
            controller.edit(user_id, "edited")
    assert controller.wait_idle(1.0)
    snap = controller.snapshot()
    assert snap["isLoading"] is False and snap["error"] == "clock exploded"
    assert log.replay(before) == snap
    monkeypatch.undo()
    controller.send("third")
    assert controller.snapshot()["messages"][-1]["content"] == "Hello there world"


def test_a_callback_that_records_on_every_batch_cannot_spin_forever() -> None:
    """A subscriber that records an op every time it is called would re-flush forever,
    holding the emit lock: a silent wedge. The loop gives up after ``_REFLUSH_LIMIT``
    rounds instead — same bound as ``ChatController``'s."""
    controller = fake()
    deliveries: List[int] = []

    def greedy(batch: OpBatch) -> None:
        deliveries.append(batch["seq"])
        with controller._lock:
            controller._rec.set(["error"], "e{}".format(batch["seq"]))
        controller._flush()  # nested: only asks the outer loop for another round

    controller.subscribe(greedy)
    with controller._lock:
        controller._rec.set(["error"], "seed")

    flusher = threading.Thread(target=controller._flush, daemon=True)
    flusher.start()
    flusher.join(5.0)

    assert not flusher.is_alive(), "the re-flush loop never terminated"
    assert deliveries == list(range(1, _REFLUSH_LIMIT + 1))
    assert controller.wait_idle(2.0)


def test_flushing_while_holding_the_state_lock_is_refused() -> None:
    """Lock-order guard: ``_flush`` takes ``_emit_lock``, so a caller already holding the
    state lock would invert the order and can deadlock against any other emitter."""
    controller = fake()
    if controller._lock_is_owned is None:  # pragma: no cover -- CPython always has it
        pytest.skip("this interpreter's RLock does not expose _is_owned")
    with controller._lock:
        with pytest.raises(RuntimeError):
            controller._flush()


def test_approve_after_close_with_no_pending_approval_does_nothing() -> None:
    controller = fake()
    controller.close()
    controller.approve(Decision("once"))  # no-op: nothing pending, whether closed or not
    assert ("approve", (Decision("once"), None)) in controller.calls


def test_approve_after_close_with_a_matching_pending_approval_raises_closed() -> None:
    controller = fake()
    controller.send("/approve go")
    pending = controller.snapshot()["pendingApproval"]
    assert pending is not None
    controller.close()
    with pytest.raises(ControllerClosedError):
        controller.approve(Decision("once"), tool_call_id=pending["toolCallId"])


def test_cancel_after_close_with_a_leftover_pending_approval_does_not_raise() -> None:
    controller = fake()
    controller.send("/approve go")
    assert controller.snapshot()["pendingApproval"] is not None
    controller.close()
    controller.cancel()  # never raises, even with a leftover pending approval
    assert controller.snapshot()["pendingApproval"] is not None  # closed: untouched, no-op


def test_retry_keeps_the_original_attachments() -> None:
    controller = fake()
    attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    controller.send("see", [attachment])
    controller.retry()
    assert controller.snapshot()["messages"][0]["attachments"] == [
        {"id": "a1", "filename": "x.png", "mime_type": "image/png", "size_bytes": 3}
    ]


def test_edit_keeps_its_own_attachments() -> None:
    controller = fake()
    attachment = Attachment(id="a1", filename="x.png", mime_type="image/png", size_bytes=3)
    controller.send("see", [attachment])
    message_id = controller.snapshot()["messages"][0]["id"]
    controller.edit(message_id, "changed")
    assert controller.snapshot()["messages"][0]["attachments"] == [
        {"id": "a1", "filename": "x.png", "mime_type": "image/png", "size_bytes": 3}
    ]


def test_clear_runs_as_a_job_so_isloading_tracks_the_executor() -> None:
    """Like the real ``clear()``'s DELETE: ``isLoading`` stays True until the job (here, no
    network work) has actually run on the executor."""
    executor = ManualExecutor()
    controller = fake(executor=executor)
    controller.send("go")
    controller.clear()
    assert controller.snapshot()["messages"] == []  # the reset itself is synchronous
    assert controller.snapshot()["isLoading"] is True  # but the run it started is not done
    assert not controller.wait_idle(0.0)
    executor.run_all()
    assert controller.snapshot()["isLoading"] is False
    assert controller.wait_idle(0.0)


def test_close_deactivates_every_subscriber_so_an_in_flight_delivery_is_suppressed() -> None:
    controller = fake()
    calls: List[str] = []

    def first(_batch: OpBatch) -> None:
        calls.append("first")
        controller.close()  # a second subscriber, already queued for this same batch, must
        # not be called after this: close() deactivates it before this delivery moves on.

    def second(_batch: OpBatch) -> None:
        calls.append("second")

    controller.subscribe(first)
    controller.subscribe(second)
    controller.send("hi")
    assert calls == ["first"]


# ── The default (threaded) executor ──


def test_send_streams_end_to_end_on_the_default_thread_executor() -> None:
    controller = FakeController(reply="Hello there world")
    controller.send("hi")
    assert controller.wait_idle(2.0)
    snap = controller.snapshot()
    assert snap["messages"][1]["content"] == "Hello there world"
    assert snap["isLoading"] is False


def test_a_pending_approval_resolves_on_the_default_thread_executor() -> None:
    controller = FakeController(reply="Hello there world")
    controller.send("/approve go")
    assert controller.wait_idle(2.0)
    pending = controller.snapshot()["pendingApproval"]
    assert pending is not None
    controller.approve(Decision("once"), tool_call_id=pending["toolCallId"])
    assert controller.wait_idle(2.0)
    assert controller.snapshot()["pendingApproval"] is None


def test_cancel_during_a_stream_on_the_default_thread_executor() -> None:
    controller = FakeController(reply="one two three four five", chunk_delay_s=0.05)
    controller.send("go")
    controller.cancel()
    assert controller.wait_idle(2.0)
    assert controller.snapshot()["error"] is None
