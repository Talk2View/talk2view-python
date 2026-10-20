"""Helpers for the talk2view.ui tests: a model of the page, and SSE readers.

``PageModel`` does with events exactly what the embed page must: a snapshot
replaces the state; an ``ops`` event applies only when its ``base`` is the page's ``seq``
(anything else is a gap) and then moves the page to the event's ``seq``. An ``ops`` event
whose ops do not apply means the page's state went wrong: the page drops it and reconnects
WITHOUT ``Last-Event-ID``, so that its first event is a snapshot (with it, the ring would
replay the very op that failed, forever).
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from talk2view.state import OpError, _apply_op
from talk2view.ui.hub import EventHub


class Gap(AssertionError):
    pass


class Resync(Exception):
    """An ``ops`` event did not apply: reconnect without ``Last-Event-ID``."""


class PageModel:
    def __init__(self) -> None:
        self.state: Optional[Dict[str, Any]] = None
        self.seq: Optional[int] = None
        self.kinds: List[str] = []
        self.resyncs = 0
        self.client: Any = None  # the stream ``pump`` reads (a replacement after a resync)
        self._given: Any = None  # the stream the caller last passed to ``pump``

    def feed(self, kind: str, event_id: int, data: str) -> None:
        payload = json.loads(data)
        self.kinds.append(kind)
        if kind == "snapshot":
            assert payload["seq"] == event_id
            self.state = payload["state"]
            self.seq = payload["seq"]
        elif kind == "ops":
            assert payload["seq"] == event_id
            if self.state is None or payload["base"] != self.seq:
                raise Gap("ops base {} on page seq {}".format(payload["base"], self.seq))
            try:
                for op in payload["ops"]:
                    _apply_op(self.state, op)
            except OpError as exc:
                self.state = None  # half applied: no longer the state at any seq
                self.seq = None
                self.resyncs += 1
                raise Resync(str(exc)) from exc
            self.seq = payload["seq"]
        else:
            raise AssertionError("unknown event " + kind)

    def pump(self, hub: EventHub, client: Any, timeout: float = 0.01) -> int:
        """Apply every event already waiting; returns how many applied. On a ``Resync`` it
        reconnects as the page must (no ``Last-Event-ID``: a snapshot comes first) and reads
        the replacement, ``self.client``, from then on — also when the caller passes the
        same ``client`` again. A stream it has not been passed before is adopted, and the
        one it was reading detached: a page has one stream."""
        if client is not self.client and client is not self._given:
            if self.client is not None:
                hub.detach(self.client)
            self.client = client
        self._given = client
        count = 0
        while True:
            event = hub.next_event(self.client, timeout)
            if event is None or event[0] == "hb":
                return count
            try:
                self.feed(*event)
            except Resync:
                hub.detach(self.client)
                self.client = hub.attach()  # no Last-Event-ID
                continue
            count += 1


def read_event(resp: http.client.HTTPResponse) -> Optional[Dict[str, str]]:
    """One SSE event from a streaming response; comment lines (``: hb``) are collected
    under ``"comments"``, so a heartbeat alone is an event with no ``data``."""
    event: Dict[str, str] = {}
    while True:
        line = resp.readline().decode("utf-8")
        if line == "":
            return None
        line = line.rstrip("\n")
        if line == "":
            if event:
                return event
            continue
        if line.startswith(":"):
            event.setdefault("comments", "")
            event["comments"] += line + "\n"
            continue
        key, _, value = line.partition(":")
        event[key] = value[1:] if value.startswith(" ") else value


def read_data_event(resp: http.client.HTTPResponse) -> Optional[Dict[str, str]]:
    """The next SSE event that has data (heartbeat-only comments are skipped)."""
    while True:
        event = read_event(resp)
        if event is None or "data" in event:
            return event


class EventStream:
    """One ``/events`` stream read on a thread into a ``PageModel``, as the page does."""

    def __init__(self, port: int, token: str) -> None:
        self.page = PageModel()
        self.error: Optional[BaseException] = None
        self._cond = threading.Condition()
        self._conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        self._conn.request("GET", "/events", headers={"Authorization": "Bearer " + token})
        self._resp = self._conn.getresponse()
        assert self._resp.status == 200
        self._thread = threading.Thread(target=self._read, name="test-page", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            while True:
                event = read_data_event(self._resp)
                if event is None:
                    return
                with self._cond:
                    self.page.feed(event["event"], int(event["id"]), event["data"])
                    self._cond.notify_all()
        except BaseException as exc:  # a Gap, or the socket closing under us
            with self._cond:
                self.error = exc
                self._cond.notify_all()

    def wait_until(
        self, predicate: Callable[[Dict[str, Any]], bool], timeout: float = 10.0
    ) -> bool:
        with self._cond:
            return self._cond.wait_for(
                lambda: self.error is not None
                or (self.page.state is not None and predicate(self.page.state)),
                timeout,
            ) and self.error is None

    def state(self) -> Dict[str, Any]:
        with self._cond:
            assert self.page.state is not None
            copied: Dict[str, Any] = json.loads(json.dumps(self.page.state))
            return copied


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True
