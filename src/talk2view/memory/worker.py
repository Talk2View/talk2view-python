"""Where ``T2VMemory``'s engine calls run (spec §7.4), so a chat never waits for one:
``Worker`` runs them one at a time on one background daemon thread, never two under the
same key, and ``Ticker`` calls a function every few seconds for a host with no loop of
its own."""

from __future__ import annotations

import collections
import logging
import threading
from typing import Any, Callable, Deque, Optional, Set

logger = logging.getLogger("talk2view.memory")


class Worker:
    """Runs jobs one at a time, in the order given, on one daemon thread, or at once in the
    caller when ``inline`` (tests and the eval). The thread starts with the first job and
    ends when none is left, so an idle worker holds no thread.

    Each job runs under a key, such as ``"extract:<conversation id>"``, and is refused while
    another under that key is queued or running; ``claim`` and ``release`` hold a key for a
    caller that runs the work itself. A job's exception is logged by its class, never
    raised."""

    def __init__(self, *, inline: bool = False) -> None:
        self._inline = inline
        self._changed = threading.Condition()
        self._jobs: Deque[Callable[[], None]] = collections.deque()
        self._keys: Set[str] = set()
        self._running = False
        self._stopped = False

    def run(self, key: str, job: Callable[[], Any]) -> bool:
        """Queue ``job`` under ``key``, or run it now when inline. False, doing nothing, when
        the key is held or the worker has stopped."""

        def keyed() -> None:
            try:
                job()
            except Exception as error:
                logger.warning("Memory: %s failed (%s)", key.split(":")[0], type(error).__name__)
            finally:
                self.release(key)

        with self._changed:
            if self._stopped or key in self._keys:
                return False
            self._keys.add(key)
            if not self._inline:
                self._jobs.append(keyed)
                if not self._running:
                    self._running = True
                    threading.Thread(
                        target=self._drain, name="talk2view-memory", daemon=True
                    ).start()
                return True
        keyed()
        return True

    def claim(self, key: str) -> bool:
        """Hold ``key``: True, unless it is held already or the worker has stopped."""
        with self._changed:
            if self._stopped or key in self._keys:
                return False
            self._keys.add(key)
            return True

    def release(self, key: str) -> None:
        with self._changed:
            self._keys.discard(key)

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until no job is queued or running: True once so, False when ``timeout``
        seconds pass first."""
        with self._changed:
            return self._changed.wait_for(lambda: not self._running, timeout)

    def stop(self) -> None:
        """Take no more jobs and drop the queued ones. It never waits: the job running
        finishes on its own."""
        with self._changed:
            self._stopped = True
            self._jobs.clear()

    def _drain(self) -> None:
        while True:
            with self._changed:
                if not self._jobs:
                    self._running = False
                    self._changed.notify_all()
                    return
                job = self._jobs.popleft()
            job()


class Ticker:
    """Calls ``tick`` every ``interval_s`` seconds on a daemon thread until ``stop``."""

    def __init__(self, tick: Callable[[], Any], interval_s: float) -> None:
        self._stopped = threading.Event()
        threading.Thread(
            target=self._run, args=(tick, interval_s), name="talk2view-memory-timer", daemon=True
        ).start()

    def stop(self) -> None:
        """Stop ticking. It never waits: a tick in progress finishes, and the thread ends."""
        self._stopped.set()

    def _run(self, tick: Callable[[], Any], interval_s: float) -> None:
        while not self._stopped.wait(interval_s):
            tick()
