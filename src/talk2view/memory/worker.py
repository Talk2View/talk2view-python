"""Where ``T2VMemory``'s engine calls run (spec §7.4), so a chat never waits for one:
``Worker`` runs them one at a time on one background daemon thread, never two under the
same key, ``Ticker`` calls a function every few seconds for a host with no loop of its
own, and ``Cooldown`` holds the automatic calls back after failed ones."""

from __future__ import annotations

import collections
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Deque, Optional, Set

logger = logging.getLogger("talk2view.memory")

# After a failed engine memory call, the automatic calls hold off for COOLDOWN_S,
# twice as long after each further failure in a row, at most COOLDOWN_MAX_S.
COOLDOWN_S = 60.0
COOLDOWN_MAX_S = 3600.0


class Worker:
    """Runs jobs one at a time, in the order given, on one daemon thread, or at once in the
    caller when ``inline`` (tests and the eval). The thread starts with the first job and
    ends when none is left, so an idle worker holds no thread.

    Each job runs under a key, such as ``"extract:<conversation id>"``, and is refused while
    another under that key is queued or running; ``claim`` and ``release`` hold a key for a
    caller that runs the work itself. A job's exception is logged by its class, never
    raised; a BaseException ends the job's thread, and a new one runs the jobs left."""

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
                    self._start()
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

    def _start(self) -> None:
        """Under the lock: start the thread that runs the queued jobs."""
        threading.Thread(target=self._drain, name="talk2view-memory", daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                with self._changed:
                    if not self._jobs:
                        return
                    job = self._jobs.popleft()
                job()
        finally:
            # Also when a job let a BaseException out (keyed() catches only Exception), which
            # ends this thread: the jobs left, or queued since the check above, go to a new
            # thread, so the worker never wedges.
            with self._changed:
                if self._jobs:
                    self._start()
                else:
                    self._running = False
                    self._changed.notify_all()


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


class Cooldown:
    """How long the automatic engine calls hold off after failed ones: COOLDOWN_S
    after the first failure in a row, twice the last wait after each further one, at most
    COOLDOWN_MAX_S. A ``retry_after`` makes that failure's wait at least so long, capped the
    same, without changing how the next ones double. A success ends it.

    It keeps no clock and no lock: its owner passes the times, read from its own clock, and
    calls it under its own lock."""

    def __init__(self) -> None:
        self._wait_s = 0.0  # the last failure's wait before any retry_after; 0 after a success
        self._until: Optional[datetime] = None

    def cooling(self, at: datetime) -> bool:
        """Whether the cooldown still runs at ``at``."""
        return self._until is not None and at < self._until

    def failed(self, at: datetime, retry_after: Optional[float] = None) -> None:
        """A call failed at ``at``: the cooldown starts there, or lengthens."""
        self._wait_s = min(self._wait_s * 2, COOLDOWN_MAX_S) if self._wait_s else COOLDOWN_S
        wait = min(max(self._wait_s, retry_after or 0.0), COOLDOWN_MAX_S)
        self._until = at + timedelta(seconds=wait)

    def recovered(self) -> None:
        """A call succeeded: the cooldown ends, and the next failure starts it afresh."""
        self._wait_s, self._until = 0.0, None
