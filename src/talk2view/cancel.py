"""Cooperative cancellation shared by SSE streaming and main-thread dispatch."""

from __future__ import annotations

import contextlib
import threading
from typing import Callable, List


class Cancelled(Exception):
    """Raised when an operation was cancelled through a :class:`CancelToken`."""


class CancelToken:
    """A one-shot, thread-safe cancellation flag with close callbacks.

    ``cancel()`` may be called from any thread. Closers registered through
    ``on_cancel()`` run exactly once, on the thread that calls ``cancel()``.

    ``unregister()`` is final: when it returns, the closer it removed is
    neither running nor able to run later. Callers rely on that — a stream
    reader unregisters its abort closer and then hands its socket back to the
    connection pool, and a closer landing after that would shut down a socket
    the next request already owns.

    Two locks, always taken in this order (``_run_lock`` outermost):

    - ``_run_lock`` (re-entrant) is held for a whole closer run and is also
      taken by ``unregister()``, which is what makes unregistering final.
      Re-entrant because a closer runs host code that may call back into
      ``cancel()``/``unregister()`` on the same thread.
    - ``_lock`` guards the flag and the closer list. It is never held while
      host code runs, so no closer can deadlock on it and nothing ever needs
      ``_run_lock`` while holding it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_lock = threading.RLock()
        self._event = threading.Event()
        self._closers: List[Callable[[], None]] = []

    def cancel(self) -> None:
        """Mark the token cancelled and run every registered closer once.

        A closer may wait. ``T2VAuth.sign_in_with_provider`` registers one that waits for
        a session store already under way (and its ``on_auth_state_change`` listeners), so
        never call ``cancel()`` on its token while holding a lock one of those listeners
        takes."""
        with self._run_lock:
            with self._lock:
                if self._event.is_set():
                    return
                self._event.set()
                closers = list(self._closers)
                self._closers.clear()
            for closer in closers:
                with contextlib.suppress(Exception):
                    closer()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def on_cancel(self, closer: Callable[[], None]) -> Callable[[], None]:
        """Register ``closer`` to run on cancel. Returns an unregister function.

        The unregister function blocks while a closer run is in flight, so once
        it returns ``closer`` has either never started or already finished.

        If the token is already cancelled the closer runs immediately, on the
        calling thread, and the returned function is a no-op.
        """
        with self._lock:
            if not self._event.is_set():
                self._closers.append(closer)

                def unregister() -> None:
                    with self._run_lock:
                        with self._lock:
                            with contextlib.suppress(ValueError):
                                self._closers.remove(closer)

                return unregister
        with contextlib.suppress(Exception):
            closer()
        return lambda: None
