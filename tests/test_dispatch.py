"""Tests for dispatchers and run_on_main."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from talk2view.cancel import Cancelled, CancelToken
from talk2view.dispatch import (
    InlineDispatcher,
    QueueDispatcher,
    TimerPumpDispatcher,
    bpy_dispatcher,
    qt_dispatcher,
    run_on_main,
)
from talk2view.sessions import T2VSession
from talk2view.tools import T2VTools
from talk2view.types import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    PermissionResult,
    ToolCallInterrupt,
)


def _pump_until(
    dispatcher: QueueDispatcher, worker: threading.Thread, timeout: float = 5.0
) -> None:
    """Play the host's main loop: drain the queue until the worker finishes."""
    deadline = time.monotonic() + timeout
    while worker.is_alive() and time.monotonic() < deadline:
        dispatcher.pump()
        time.sleep(0.005)
    dispatcher.pump()
    worker.join(1.0)
    assert not worker.is_alive(), "worker never finished"


class TestInlineDispatcher:
    def test_inline_runs_on_caller(self) -> None:
        dispatcher = InlineDispatcher()
        seen: List[int] = []

        dispatcher.post(lambda: seen.append(threading.get_ident()))
        result = run_on_main(dispatcher, lambda: threading.get_ident())

        assert dispatcher.is_main_thread() is True
        assert seen == [threading.get_ident()]
        assert result == threading.get_ident()


class TestQueueDispatcher:
    def test_queue_runs_only_on_pump_thread(self) -> None:
        dispatcher = QueueDispatcher()
        ran_on: List[int] = []

        worker = threading.Thread(
            target=lambda: dispatcher.post(lambda: ran_on.append(threading.get_ident()))
        )
        worker.start()
        worker.join()

        assert ran_on == []  # posted, not run
        assert dispatcher.pump() == 1
        assert ran_on == [threading.get_ident()]
        assert dispatcher.pump() == 0

    def test_pump_is_fifo_and_honours_max_items(self) -> None:
        dispatcher = QueueDispatcher()
        order: List[int] = []
        for n in range(3):
            dispatcher.post(lambda n=n: order.append(n))

        assert dispatcher.pump(max_items=2) == 2
        assert order == [0, 1]
        assert dispatcher.pump() == 1
        assert order == [0, 1, 2]

    def test_pump_off_the_owner_thread_is_refused(self) -> None:
        dispatcher = QueueDispatcher()
        errors: List[BaseException] = []

        def wrong_thread() -> None:
            try:
                dispatcher.pump()
            except RuntimeError as exc:
                errors.append(exc)

        worker = threading.Thread(target=wrong_thread)
        worker.start()
        worker.join()

        assert len(errors) == 1

    def test_explicit_owner_thread_id(self) -> None:
        dispatcher = QueueDispatcher(owner_thread_id=-1)
        assert dispatcher.is_main_thread() is False

    def test_a_raising_callable_does_not_strand_the_queue(self) -> None:
        dispatcher = QueueDispatcher()
        ran: List[str] = []

        def boom() -> None:
            raise RuntimeError("bad callback")

        dispatcher.post(boom)
        dispatcher.post(lambda: ran.append("after"))

        assert dispatcher.pump() == 2
        assert ran == ["after"]


class TestRunOnMain:
    def test_run_on_main_returns_value_from_the_main_thread(self) -> None:
        dispatcher = QueueDispatcher()
        out: Dict[str, Any] = {}

        def work() -> None:
            out["value"] = run_on_main(dispatcher, lambda: ("ran", threading.get_ident()))

        worker = threading.Thread(target=work)
        worker.start()
        _pump_until(dispatcher, worker)

        assert out["value"] == ("ran", threading.get_ident())

    def test_run_on_main_propagates_exception(self) -> None:
        dispatcher = QueueDispatcher()
        out: Dict[str, Any] = {}

        def fail() -> str:
            raise ValueError("scene is locked")

        def work() -> None:
            try:
                run_on_main(dispatcher, fail)
            except ValueError as exc:
                out["error"] = str(exc)
                out["thread"] = threading.get_ident()

        worker = threading.Thread(target=work)
        worker.start()
        _pump_until(dispatcher, worker)

        assert out["error"] == "scene is locked"
        assert out["thread"] != threading.get_ident()  # re-raised on the worker

    def test_run_on_main_from_main_thread_no_deadlock(self) -> None:
        dispatcher = QueueDispatcher()  # owner = this thread, and nobody is pumping

        assert run_on_main(dispatcher, lambda: 42) == 42
        assert dispatcher.pump() == 0  # it ran directly, nothing was queued

    def test_run_on_main_cancelled_while_waiting(self) -> None:
        dispatcher = QueueDispatcher()
        token = CancelToken()
        ran: List[str] = []
        out: Dict[str, Any] = {}

        def work() -> None:
            try:
                run_on_main(dispatcher, lambda: ran.append("handler"), token)
            except Cancelled:
                out["cancelled"] = True

        worker = threading.Thread(target=work)
        worker.start()
        time.sleep(0.05)  # let it post and block; the main thread is NOT pumping
        token.cancel()
        worker.join(1.0)

        assert not worker.is_alive()
        assert out == {"cancelled": True}
        assert dispatcher.pump() == 1  # the abandoned job is drained...
        assert ran == []  # ...but the handler never runs

    def test_run_on_main_already_cancelled_never_posts(self) -> None:
        dispatcher = QueueDispatcher(owner_thread_id=-1)
        token = CancelToken()
        token.cancel()

        with pytest.raises(Cancelled):
            run_on_main(dispatcher, lambda: 1, token)

        assert len(dispatcher._queue) == 0

    def test_run_on_main_already_cancelled_does_not_run_inline_either(self) -> None:
        """A cancelled token wins over the main-thread shortcut too: the
        default InlineDispatcher must not run the handler after a stop()."""
        ran: List[str] = []
        token = CancelToken()
        token.cancel()

        with pytest.raises(Cancelled):
            run_on_main(InlineDispatcher(), lambda: ran.append("handler"), token)

        assert ran == []

    def test_run_on_main_cancelled_after_the_handler_started_returns_its_value(self) -> None:
        """A handler that has begun cannot be interrupted: wait for it and
        return its value rather than raising Cancelled and losing the result.

        The handler cancels the token itself, which is exactly a stop() landing
        mid-handler — and needs no cross-thread choreography to place.
        """
        dispatcher = QueueDispatcher()
        token = CancelToken()
        out: Dict[str, Any] = {}

        def handler() -> str:
            token.cancel()
            return "finished anyway"

        def work() -> None:
            try:
                out["value"] = run_on_main(dispatcher, handler, token)
            except BaseException as exc:  # pragma: no cover — reported below
                out["error"] = repr(exc)

        worker = threading.Thread(target=work)
        worker.start()
        _pump_until(dispatcher, worker)

        assert out == {"value": "finished anyway"}
        assert token.cancelled is True


class TestTimerPumpDispatcher:
    def test_timer_pump_uses_injected_timer(self) -> None:
        started: Dict[str, Any] = {}

        def fake_start_timer(callback: Callable[[], None], interval_ms: int) -> str:
            started["callback"] = callback
            started["interval_ms"] = interval_ms
            return "timer-handle"

        dispatcher = TimerPumpDispatcher(fake_start_timer, interval_ms=20)
        ran: List[str] = []
        dispatcher.post(lambda: ran.append("a"))

        assert started["interval_ms"] == 20
        assert dispatcher._timer == "timer-handle"  # kept alive
        assert ran == []

        started["callback"]()  # the host's timer fires on the main thread

        assert ran == ["a"]

    def test_qt_dispatcher_wires_a_repeating_qtimer(self) -> None:
        class FakeSignal:
            def __init__(self) -> None:
                self.slot: Optional[Callable[[], None]] = None

            def connect(self, slot: Callable[[], None]) -> None:
                self.slot = slot

        class FakeQTimer:
            instances: List["FakeQTimer"] = []

            def __init__(self) -> None:
                self.timeout = FakeSignal()
                self.interval: Optional[int] = None
                self.started = False
                FakeQTimer.instances.append(self)

            def setInterval(self, ms: int) -> None:
                self.interval = ms

            def start(self) -> None:
                self.started = True

        dispatcher = qt_dispatcher(FakeQTimer, interval_ms=25)
        timer = FakeQTimer.instances[0]
        ran: List[str] = []
        dispatcher.post(lambda: ran.append("qt"))

        assert (timer.interval, timer.started) == (25, True)
        assert timer.timeout.slot is not None
        timer.timeout.slot()
        assert ran == ["qt"]

    def test_bpy_dispatcher_registers_a_persistent_timer(self) -> None:
        bpy_timers = MagicMock()

        dispatcher = bpy_dispatcher(bpy_timers, interval_s=0.02)

        tick = bpy_timers.register.call_args[0][0]
        assert bpy_timers.register.call_args[1] == {"first_interval": 0.02, "persistent": True}
        ran: List[str] = []
        dispatcher.post(lambda: ran.append("bpy"))
        assert tick() == 0.02  # Blender re-arms with the returned delay
        assert ran == ["bpy"]


class TestToolsUseTheDispatcher:
    def test_tool_handler_runs_on_pump_thread_while_stream_is_consumed_elsewhere(self) -> None:
        main_thread = threading.get_ident()
        dispatcher = QueueDispatcher()
        seen: Dict[str, int] = {}

        def create_shape(shape: str) -> str:
            seen["handler"] = threading.get_ident()
            return "created " + shape

        client = MagicMock()
        client.stream_request.side_effect = [
            iter(
                [
                    ChatCompletionChunk(
                        choices=[ChatCompletionChunkChoice(finish_reason="tool_calls")],
                        interrupt=ToolCallInterrupt(
                            type="tool_call",
                            tool_name="create_shape",
                            tool_call_id="call_1",
                            arguments={"shape": "cube"},
                        ),
                    )
                ]
            ),
            iter(
                [
                    ChatCompletionChunk(
                        choices=[ChatCompletionChunkChoice(finish_reason="stop")],
                        thread_id="t1",
                    )
                ]
            ),
        ]
        tools = T2VTools(client, dispatcher=dispatcher)
        tools.handle("create_shape", create_shape)
        session = T2VSession("ses_1", "thr_1", "m", client, tools)
        events: List[str] = []

        def consume() -> None:
            seen["stream"] = threading.get_ident()
            events.extend(e.type for e in session.send_message("make a cube"))

        worker = threading.Thread(target=consume)
        worker.start()
        _pump_until(dispatcher, worker)

        assert events == ["tool_call", "done"]
        assert seen["handler"] == main_thread
        assert seen["stream"] != main_thread
        resume_body = client.stream_request.call_args_list[1][0][1]
        assert resume_body["result"] == "created cube"

    def test_permission_callback_runs_on_pump_thread(self) -> None:
        main_thread = threading.get_ident()
        dispatcher = QueueDispatcher()
        seen: Dict[str, int] = {}
        out: Dict[str, Any] = {}

        def policy(tool_name: str, args: Dict[str, Any]) -> PermissionResult:
            seen["permission"] = threading.get_ident()
            return PermissionResult.deny("read-only")

        tools = T2VTools(MagicMock(), dispatcher=dispatcher)
        tools.handle("save", lambda: "ok", permission=policy)

        def check() -> None:
            out["check"] = tools.check_permission("save", {})

        worker = threading.Thread(target=check)
        worker.start()
        _pump_until(dispatcher, worker)

        assert seen["permission"] == main_thread
        assert out["check"].action == "deny"
        assert out["check"].message == "read-only"

    def test_cancel_while_handler_is_queued_is_not_reported_as_a_tool_error(self) -> None:
        dispatcher = QueueDispatcher(owner_thread_id=-1)  # nobody will ever pump
        tools = T2VTools(MagicMock(), dispatcher=dispatcher)
        tools.handle("slow", lambda: "never")
        token = CancelToken()
        token.cancel()

        with pytest.raises(Cancelled):
            tools.execute_tool_call("slow", {}, cancel=token)

    def test_a_cancelled_token_stops_the_inline_handler_too(self) -> None:
        """The default (inline) dispatcher took the main-thread shortcut before
        looking at the token, so a stop() did not stop the next handler."""
        ran: List[str] = []
        tools = T2VTools(MagicMock())  # InlineDispatcher by default
        tools.handle("slow", lambda: ran.append("handler") or "done")
        token = CancelToken()
        token.cancel()

        with pytest.raises(Cancelled):
            tools.execute_tool_call("slow", {}, cancel=token)

        assert ran == []

    def test_default_dispatcher_is_inline(self) -> None:
        tools = T2VTools(MagicMock())
        assert isinstance(tools.dispatcher, InlineDispatcher)
