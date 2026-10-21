"""Turn timings (``talk2view.metrics``): a real ``Talk2View`` + ``ChatController`` against
the stateful fake engine, with the listener API ``t2v.metrics.on_turn``.

Each turn ends in exactly one ``TurnTiming``; its marks are in order, its durations add
up, and it holds nothing the end-user or the model said.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, List, Optional

import pytest

from talk2view import (
    MemoryStorage,
    QueueDispatcher,
    Talk2View,
    TurnMetrics,
    TurnTiming,
    tool,
)
from talk2view.controller import ChatController
from talk2view.types import Decision

from .fake_engine import TOOL, FakeEngine

PRIVATE_TEXT = "SECRET-TEXT-4711"
PRIVATE_MESSAGE = "please-delete-" + PRIVATE_TEXT  # the fake engine calls the tool for "delete"
PRIVATE_ARG = "SECRET-ARG-9922"
PRIVATE_RESULT = "SECRET-RESULT-5833"


class SlowMain(QueueDispatcher):
    """A main thread that is busy: a post is noticed (``posted``) and pumped only when the
    test says so."""

    def __init__(self) -> None:
        super().__init__()
        self.posted = threading.Event()

    def post(self, fn: Callable[[], None]) -> None:
        super().post(fn)
        self.posted.set()


class Host:
    def __init__(
        self,
        engine: FakeEngine,
        *,
        permission: bool = False,
        dispatcher: Optional[QueueDispatcher] = None,
        handler_s: float = 0.0,
        metrics: Optional[TurnMetrics] = None,
    ) -> None:
        engine.tool_arguments = {"name": PRIVATE_ARG}
        self.args: List[str] = []
        args = self.args

        @tool(permission=permission)
        def delete_volume(name: str) -> str:
            """Delete a volume.

            Args:
                name: The volume.
            """
            args.append(name)
            time.sleep(handler_s)
            return PRIVATE_RESULT

        self.t2v = Talk2View(
            "pk_test", base_url=engine.url(), storage=MemoryStorage(), dispatcher=dispatcher
        )
        if metrics is not None:
            self.t2v.metrics = metrics
        self.t2v.tools.register_from_functions([delete_volume])
        self.t2v.auth.login("a@x.y", "pw")
        self.controller = ChatController(self.t2v)  # a real worker thread per run
        self.timings: List[TurnTiming] = []
        self.delivered = threading.Event()
        self.t2v.metrics.on_turn(self._heard)

    def _heard(self, timing: TurnTiming) -> None:
        self.timings.append(timing)
        self.delivered.set()

    def wait_timing(self, count: int = 1, timeout: float = 5.0) -> TurnTiming:
        deadline = time.monotonic() + timeout
        while len(self.timings) < count and time.monotonic() < deadline:
            time.sleep(0.005)
        assert len(self.timings) >= count, "no turn timing arrived"
        return self.timings[count - 1]

    def close(self) -> None:
        self.controller.close()
        self.t2v.close()


@pytest.fixture
def hosts() -> Any:
    made: List[Host] = []

    def make(engine: FakeEngine, **kwargs: Any) -> Host:
        host = Host(engine, **kwargs)
        made.append(host)
        return host

    yield make
    for host in made:
        host.close()


def _ordered(*marks: Optional[float]) -> None:
    present = [m for m in marks if m is not None]
    assert present == sorted(present), marks


def _assert_nothing_sensitive(timing: TurnTiming, engine: FakeEngine) -> None:
    dumped = json.dumps(timing.as_dict()) + repr(timing)
    for secret in (PRIVATE_TEXT, PRIVATE_ARG, PRIVATE_RESULT, "The model failed", engine.url()):
        assert secret not in dumped, secret
    host = engine.url().split("//", 1)[1].rstrip("/")
    assert host not in dumped
    for token in ("tok-", "ref-", "Bearer", "pk_test", "/v1/", "http"):
        assert token not in dumped, token
    with engine.lock:
        session_ids = [s["id"] for s in engine.sessions.values()]
    for session_id in session_ids:
        assert session_id not in dumped


def test_a_text_reply_is_timed_in_order(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine)
    h.controller.send("say " + PRIVATE_TEXT)
    timing = h.wait_timing()

    assert timing.outcome == "done"
    assert timing.error_type is None
    user_ids = [m["id"] for m in h.controller.snapshot()["messages"] if m["role"] == "user"]
    assert timing.turn_id == user_ids[-1]
    r = timing.request
    assert r.attempts == 1 and timing.requests == 1
    assert r.completion_id == "c"
    _ordered(0.0, r.sent_ms, r.headers_ms, r.first_event_ms, r.first_text_ms, timing.total_ms)
    assert None not in (r.sent_ms, r.headers_ms, r.first_event_ms, r.first_text_ms)
    assert timing.first_text_ms == r.first_text_ms == timing.time_to_first_text_ms
    assert timing.setup_ms == r.sent_ms
    assert timing.engine_wait_ms == pytest.approx(r.first_event_ms - r.sent_ms, abs=0.11)
    assert r.ttfb_ms == pytest.approx(r.headers_ms - r.sent_ms, abs=0.11)
    assert timing.text_events == 1 and timing.events == 2
    assert timing.tools == () and timing.ui is None
    assert abs(timing.started_at - time.time()) < 60
    _assert_nothing_sensitive(timing, engine)
    assert h.t2v.metrics.recent() == [timing]


def test_a_client_tool_shows_the_wait_for_the_main_thread(
    engine: FakeEngine, hosts: Any
) -> None:
    main = SlowMain()
    h = hosts(engine, dispatcher=main, handler_s=0.03)
    h.controller.send(PRIVATE_MESSAGE)
    assert main.posted.wait(5.0)
    time.sleep(0.06)  # the host's main thread is busy with something else
    deadline = time.monotonic() + 5.0
    while not h.timings and time.monotonic() < deadline:
        main.pump()
        time.sleep(0.002)
    timing = h.wait_timing()

    assert timing.outcome == "done"
    assert h.args == [PRIVATE_ARG]  # the handler ran, with the model's arguments
    assert len(timing.tools) == 1
    t = timing.tools[0]
    assert (t.name, t.kind, t.outcome, t.decision) == (TOOL, "client", "ok", None)
    assert t.approval_wait_ms is None
    assert t.resume is not None and timing.requests == 2
    _ordered(
        timing.request.sent_ms,
        timing.request.first_event_ms,
        t.interrupt_ms,
        t.dispatched_ms,
        t.handler_started_ms,
        t.handler_finished_ms,
        t.resume.sent_ms,
        t.resume.headers_ms,
        t.resume.first_event_ms,
        t.resume.first_text_ms,
        timing.total_ms,
    )
    assert t.main_thread_wait_ms is not None and t.main_thread_wait_ms >= 55
    assert t.run_ms is not None and t.run_ms >= 25
    parts = [t.before_ms, t.main_thread_wait_ms, t.run_ms, t.after_ms, t.resume_ms]
    assert None not in parts
    assert sum(p for p in parts if p is not None) == pytest.approx(t.total_ms, abs=0.6)
    # The first words of this turn came after the tool, on the resume.
    assert timing.request.first_text_ms is None
    assert timing.first_text_ms == t.resume.first_text_ms
    _assert_nothing_sensitive(timing, engine)


def test_an_approval_wait_is_kept_apart_and_the_turn_ends_once(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine, permission=True)
    h.controller.send(PRIVATE_MESSAGE)
    assert h.controller.wait_idle(5.0)
    assert h.controller.snapshot()["pendingApproval"] is not None
    time.sleep(0.08)  # the end-user reads the card
    assert h.timings == []  # a paused turn has not ended
    h.controller.approve(Decision("once"))
    timing = h.wait_timing()
    assert h.controller.wait_idle(5.0)

    assert timing.outcome == "done"
    (t,) = timing.tools
    assert (t.outcome, t.decision) == ("ok", "once")
    assert t.approval_wait_ms is not None and t.approval_wait_ms >= 75
    assert timing.approval_wait_ms == t.approval_wait_ms
    assert t.total_ms is not None and t.total_ms < t.approval_wait_ms
    parts = [t.before_ms, t.main_thread_wait_ms, t.run_ms, t.after_ms, t.resume_ms]
    assert sum(p for p in parts if p is not None) == pytest.approx(t.total_ms, abs=0.6)
    time.sleep(0.05)
    assert len(h.timings) == 1
    _assert_nothing_sensitive(timing, engine)


def test_a_denied_approval_is_timed_as_denied(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine, permission=True)
    h.controller.send(PRIVATE_MESSAGE)
    assert h.controller.wait_idle(5.0)
    h.controller.approve(Decision("deny", feedback=PRIVATE_MESSAGE))
    timing = h.wait_timing()
    (t,) = timing.tools
    assert (t.outcome, t.decision) == ("denied", "deny")
    assert t.handler_started_ms is None and t.resume is not None
    assert h.args == []
    _assert_nothing_sensitive(timing, engine)


def test_a_cancelled_turn(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine)
    h.controller.send("slow " + PRIVATE_TEXT)
    assert engine.slow_started.wait(5.0)
    deadline = time.monotonic() + 5.0
    while "partial" not in json.dumps(h.controller.snapshot()["messages"]):
        assert time.monotonic() < deadline, "the partial text never arrived"
        time.sleep(0.005)
    h.controller.cancel()
    timing = h.wait_timing()
    engine.release_slow.set()

    assert timing.outcome == "cancelled"
    assert timing.error_type is None
    _ordered(timing.request.sent_ms, timing.request.first_text_ms, timing.total_ms)
    assert timing.first_text_ms is not None  # the partial text arrived before Stop
    _assert_nothing_sensitive(timing, engine)


def test_an_engine_error(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine)
    h.controller.send("engine-error " + PRIVATE_TEXT)
    timing = h.wait_timing()

    assert timing.outcome == "error"
    assert timing.error_type == "model_error"
    assert timing.first_text_ms is None
    assert timing.request.first_event_ms is not None
    _assert_nothing_sensitive(timing, engine)


def test_a_failed_request_names_the_error_type_not_its_message(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine)
    engine.stop()  # every route 500s now (nothing is registered)
    try:
        h.controller.send(PRIVATE_MESSAGE)
        timing = h.wait_timing()
    finally:
        engine.start()
    assert timing.outcome == "error"
    assert timing.error_type and " " not in timing.error_type
    _assert_nothing_sensitive(timing, engine)


def test_a_turn_dropped_by_new_chat_is_abandoned(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine, permission=True)
    h.controller.send(PRIVATE_MESSAGE)
    assert h.controller.wait_idle(5.0)
    h.controller.clear()
    timing = h.wait_timing()
    assert timing.outcome == "abandoned"
    assert timing.tools[0].outcome == "pending"
    assert timing.tools[0].approval_wait_ms is not None
    assert h.controller.wait_idle(5.0)
    time.sleep(0.05)
    assert len(h.timings) == 1


def test_a_listener_that_raises_is_ignored(
    engine: FakeEngine, hosts: Any, caplog: pytest.LogCaptureFixture
) -> None:
    h = hosts(engine)

    def broken(timing: TurnTiming) -> None:
        raise RuntimeError("listener bug")

    h.t2v.metrics.on_turn(broken)
    heard: List[TurnTiming] = []
    off = h.t2v.metrics.on_turn(heard.append)
    h.controller.send("hello")
    h.wait_timing()
    assert h.controller.wait_idle(5.0)
    deadline = time.monotonic() + 5.0
    while not heard and time.monotonic() < deadline:  # the listeners run in order
        time.sleep(0.005)
    assert "a turn timing listener raised" in caplog.text
    assert len(heard) == 1
    off()
    h.controller.send("hello again")
    h.wait_timing(2)
    time.sleep(0.05)
    assert len(heard) == 1
    assert h.controller.snapshot()["error"] is None


def test_recent_keeps_the_last_turns_only() -> None:
    metrics = TurnMetrics(history=3)
    for i in range(5):
        metrics._start_turn("t{}".format(i)).finish("done")
    assert [t.turn_id for t in metrics.recent()] == ["t2", "t3", "t4"]


def test_a_turn_sent_from_the_page_waits_for_its_report(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine, metrics=TurnMetrics(page_grace_s=5.0))
    received = time.monotonic()
    with h.t2v.metrics.page_send(received=received, reports=True):
        h.controller.send("hello")
    assert h.controller.wait_idle(5.0)
    time.sleep(0.05)
    assert h.timings == []  # held for the page
    turn_id = h.controller.snapshot()["messages"][0]["id"]
    first_text_wall_ms = time.time() * 1000.0
    assert h.t2v.metrics.attach_page(
        turn_id, send_to_post_ms=4.25, first_op_to_paint_ms=16.0, first_op_at_ms=first_text_wall_ms
    )
    timing = h.wait_timing()
    assert timing.ui is not None and timing.ui.reported
    assert timing.ui.send_to_post_ms == 4.2 or timing.ui.send_to_post_ms == 4.3
    assert timing.ui.first_op_to_paint_ms == 16.0
    assert timing.ui.queue_ms is not None and timing.ui.queue_ms >= 0
    assert timing.ui.delivery_ms is not None and timing.ui.delivery_ms > 0
    # A late or unknown report is dropped.
    assert not h.t2v.metrics.attach_page(
        turn_id, send_to_post_ms=1.0, first_op_to_paint_ms=1.0, first_op_at_ms=None
    )
    assert len(h.timings) == 1


def test_a_page_that_never_reports_is_not_waited_on_for_long(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine, metrics=TurnMetrics(page_grace_s=0.1))
    with h.t2v.metrics.page_send(received=time.monotonic(), reports=True):
        h.controller.send("hello")
    timing = h.wait_timing()
    assert timing.ui is not None and not timing.ui.reported
    assert timing.ui.first_op_to_paint_ms is None


def test_a_page_send_without_reports_is_delivered_at_once(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine, metrics=TurnMetrics(page_grace_s=30.0))
    with h.t2v.metrics.page_send(received=time.monotonic(), reports=False):
        h.controller.send("hello")
    timing = h.wait_timing()
    assert timing.ui is not None and timing.ui.queue_ms is not None
    assert not timing.ui.reported


def test_close_delivers_a_turn_still_waiting_for_its_page(
    engine: FakeEngine, hosts: Any
) -> None:
    h = hosts(engine, metrics=TurnMetrics(page_grace_s=30.0))
    with h.t2v.metrics.page_send(received=time.monotonic(), reports=True):
        h.controller.send("hello")
    assert h.controller.wait_idle(5.0)
    time.sleep(0.05)
    assert h.timings == []
    h.t2v.metrics.close()
    assert len(h.timings) == 1


def test_the_timing_is_json_ready_and_plain(engine: FakeEngine, hosts: Any) -> None:
    h = hosts(engine)
    h.controller.send(PRIVATE_MESSAGE)
    timing = h.wait_timing()
    data = timing.as_dict()
    assert json.loads(json.dumps(data)) == data
    assert data["started_at"].endswith("Z")
    assert set(data) >= {
        "turn_id", "outcome", "total_ms", "time_to_first_text_ms", "engine_wait_ms", "tools",
    }
    with pytest.raises(AttributeError):
        timing.outcome = "x"
