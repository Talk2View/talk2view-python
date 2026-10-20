"""Dispatchers — run tool handlers and permission callbacks on the host's main thread.

Desktop hosts (3D Slicer, FreeCAD, Blender) only allow scene/UI access from
their main thread, while the SDK streams on a worker thread. A ``Dispatcher``
is the bridge. The core never imports a UI toolkit: hosts hand in their timer.

A main-thread timer draining a queue is mandatory. A ``QTimer.singleShot``
started from a plain Python thread never fires, because that thread has no Qt
event loop.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional, Protocol, TypeVar

from .cancel import Cancelled, CancelToken

T = TypeVar("T")

logger = logging.getLogger("talk2view.dispatch")


class Dispatcher(Protocol):
    """Runs callables on the host's main thread, first in first out."""

    def post(self, fn: Callable[[], None]) -> None: ...

    def is_main_thread(self) -> bool: ...


class InlineDispatcher:
    """Default: no host main thread — ``post()`` runs ``fn`` at once on the caller.

    This keeps ``for event in t2v.chat(...)`` behaving exactly as it always has.
    """

    def post(self, fn: Callable[[], None]) -> None:
        fn()

    def is_main_thread(self) -> bool:
        return True


class QueueDispatcher:
    """A thread-safe FIFO that the owner (main) thread drains with ``pump()``."""

    def __init__(self, owner_thread_id: Optional[int] = None) -> None:
        self._owner_thread_id = (
            owner_thread_id if owner_thread_id is not None else threading.get_ident()
        )
        self._queue: Deque[Callable[[], None]] = deque()

    def post(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)  # deque.append is atomic

    def is_main_thread(self) -> bool:
        return threading.get_ident() == self._owner_thread_id

    def pump(self, max_items: Optional[int] = None) -> int:
        """Run queued callables on the owner thread. Returns how many ran.

        A callable that raises is logged and skipped, so one bad callback can
        neither kill the host's timer nor strand the callables queued behind it.
        """
        if not self.is_main_thread():
            raise RuntimeError("QueueDispatcher.pump() must be called on its owner thread")
        ran = 0
        while max_items is None or ran < max_items:
            try:
                fn = self._queue.popleft()  # deque.popleft is atomic
            except IndexError:
                break
            try:
                fn()
            except Exception:
                logger.exception("Callable posted to the dispatcher raised")
            ran += 1
        return ran


class TimerPumpDispatcher(QueueDispatcher):
    """A ``QueueDispatcher`` drained by a repeating timer the host provides.

    Construct it ON the host's main thread. ``start_timer(callback, interval_ms)``
    must arrange for ``callback()`` to run on that thread every ``interval_ms``
    and may return a handle; the handle is kept alive for the dispatcher's life
    (a garbage-collected ``QTimer`` stops firing).
    """

    def __init__(
        self,
        start_timer: Callable[[Callable[[], None], int], Any],
        interval_ms: int = 15,
    ) -> None:
        super().__init__()
        self._timer = start_timer(self._on_timer, interval_ms)

    def _on_timer(self) -> None:
        self.pump()


def qt_dispatcher(QTimer: Any, interval_ms: int = 15) -> TimerPumpDispatcher:
    """Dispatcher for Qt hosts. The host passes its own ``QTimer`` class
    (``qt.QTimer`` in 3D Slicer, ``PySide6.QtCore.QTimer`` in FreeCAD), so the
    core never imports Qt. Call on the main thread."""

    def start_timer(callback: Callable[[], None], ms: int) -> Any:
        timer = QTimer()
        timer.setInterval(ms)
        timer.timeout.connect(callback)
        timer.start()
        return timer

    return TimerPumpDispatcher(start_timer, interval_ms)


def bpy_dispatcher(bpy_timers: Any, interval_s: float = 0.02) -> TimerPumpDispatcher:
    """Dispatcher for Blender. Pass ``bpy.app.timers``. Call on the main thread."""

    def start_timer(callback: Callable[[], None], ms: int) -> Any:
        seconds = ms / 1000.0

        def tick() -> float:
            callback()
            return seconds  # Blender re-arms the timer with the returned delay

        bpy_timers.register(tick, first_interval=seconds, persistent=True)
        return tick

    return TimerPumpDispatcher(start_timer, max(1, int(round(interval_s * 1000))))


def run_on_main(
    d: Dispatcher,
    fn: Callable[[], T],
    cancel: Optional[CancelToken] = None,
) -> T:
    """Run ``fn`` on the dispatcher's main thread and return its result.

    Called ON the main thread it runs ``fn`` directly (no queue, no deadlock).
    Otherwise it posts ``fn`` and blocks until the main thread has run it; an
    exception raised by ``fn`` is re-raised here, on the calling thread.

    An already-cancelled token is honoured before anything runs, on the
    main-thread path too. If ``cancel`` fires while ``fn`` is still queued,
    ``Cancelled`` is raised and ``fn`` will never run. If ``fn`` has already
    started, it cannot be interrupted: this waits for it and returns its result.
    """
    if cancel is not None and cancel.cancelled:
        raise Cancelled()
    if d.is_main_thread():
        return fn()

    lock = threading.Lock()
    wake = threading.Event()  # set when fn finished OR the token was cancelled
    finished = threading.Event()
    box: Dict[str, Any] = {"started": False, "abandoned": False}

    def job() -> None:
        with lock:
            if box["abandoned"]:
                return
            box["started"] = True
        try:
            box["value"] = fn()
        except BaseException as exc:
            box["error"] = exc  # re-raised on the calling thread
            if not isinstance(exc, Exception):
                raise  # KeyboardInterrupt/SystemExit also belong to the host
        finally:
            finished.set()
            wake.set()

    unregister = cancel.on_cancel(wake.set) if cancel is not None else None
    try:
        d.post(job)
        wake.wait()
        with lock:
            if not box["started"]:
                box["abandoned"] = True
                raise Cancelled()
        finished.wait()
    finally:
        if unregister is not None:
            unregister()

    if "error" in box:
        error: BaseException = box["error"]
        raise error
    value: T = box["value"]
    return value
