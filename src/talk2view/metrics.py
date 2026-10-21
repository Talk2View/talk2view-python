"""Turn timings: where the time of one chat turn went, measured on the client.

A turn starts when ``ChatController`` accepts a ``send()`` (or ``retry()``/``edit()``) and
ends when it is done, fails, is cancelled or is abandoned (``clear()``, a sign-out,
``close()``). While it runs, the SDK notes ``time.monotonic()`` marks at each step:

* the request to the engine sent, its response headers (TTFB), the first stream event
  and the first text token;
* for each tool call: the interrupt, the wait for an approval (a person's time, kept
  apart), the handler posted to the host's main thread, the handler started there (so
  the wait for that thread shows), the handler finished, and the ``/resume`` request with
  its own headers, first event and first text.

When the turn ends, a plain, frozen ``TurnTiming`` goes to every ``on_turn`` listener::

    off = t2v.metrics.on_turn(lambda timing: log.write(json.dumps(timing.as_dict())))

**Nothing the end-user or the model said is recorded**: no message or reply text, no
tool arguments or results, no tokens, no URLs. A timing holds marks, durations, tool
names, the model name, outcome and error *types*, counts, and the ids the engine put on
the stream (to find a slow turn in the engine's logs).

The engine's own account of a request, when it sends one, rides along on that request:
its ``X-Request-Id`` header (``request_id``) and its ``Server-Timing`` header, parsed into
``server_timing``, ``{metric name: duration in ms}`` (only metrics with a usable ``dur``;
None when the header is absent or has none). The names are the engine's, whatever they
are: this module assumes none.

The cost is a few ``time.monotonic()`` calls and attribute writes per event, no extra
request and no lock the stream waits on. A listener runs on the thread that ends the turn,
never under a controller lock: the chat worker, the UI server's router or a short timer
thread (for a turn held for the page's report) -- and, for a turn that ``clear()``, a
sign-out or ``close()`` dropped, whichever thread called that, which can be the host's
main thread. One that raises is logged and ignored. Keep a listener quick, and post UI
work to the main thread.

The chat UI server (``talk2view.ui``) adds the page's own part: how long the page took
from Send to posting the command, how long the command waited for the server's command
router, how long the first text took to reach the page and how long the page took to
paint it. It reports those after the turn, so a turn sent from the page is held back for
at most ``page_grace_s`` for them; one sent any other way is delivered at once.
"""

from __future__ import annotations

import contextlib
import logging
import math
import threading
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
    TypeVar,
)

from .types import ChatCompletionChunk

__all__ = [
    "EngineToolTiming",
    "RequestTiming",
    "ToolTiming",
    "TurnMetrics",
    "TurnTiming",
    "UiTiming",
    "parse_server_timing",
]

logger = logging.getLogger("talk2view.metrics")

T = TypeVar("T")
TurnListener = Callable[["TurnTiming"], None]

#: How many finished turns ``recent()`` keeps.
DEFAULT_HISTORY = 50
#: How long a turn sent from the chat page waits for the page's report before it is
#: delivered without it.
DEFAULT_PAGE_GRACE_S = 3.0
#: Per turn: tool calls and engine tool events kept (the rest are only counted).
MAX_TOOLS = 64
#: Turns in flight or waiting for their page report, at most (a leak guard).
MAX_OPEN_TURNS = 16
#: Per request: ``Server-Timing`` metrics kept (the rest are dropped), and a name's length.
MAX_SERVER_TIMINGS = 32
_MAX_METRIC_NAME = 64
_MAX_ID = 128


def _clip(value: Any, limit: int = _MAX_ID) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _ms(delta: float) -> float:
    return round(delta * 1000.0, 1)


def _between(start: Optional[float], end: Optional[float]) -> Optional[float]:
    if start is None or end is None:
        return None
    return round(end - start, 1)


def _split_outside_quotes(text: str, separator: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    quoted = escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return parts


def parse_server_timing(value: Optional[str]) -> Optional[Dict[str, float]]:
    """A ``Server-Timing`` header as ``{metric name: dur in ms}``, in the header's order.

    Each metric is ``name;dur=12.5;desc="..."``. A metric with no ``dur``, or one that is
    not a finite, non-negative number, is left out; so is a name seen before (the first
    wins, names compared once clipped to 64 characters) and anything past
    ``MAX_SERVER_TIMINGS``. ``desc`` and other parameters are dropped: only names and
    numbers are kept. None when ``value`` is None or nothing in it is usable. Never
    raises."""
    if not isinstance(value, str) or not value.strip():
        return None
    timings: Dict[str, float] = {}
    for metric in _split_outside_quotes(value, ","):
        name, *params = _split_outside_quotes(metric, ";")
        name = name.strip()
        if not name or '"' in name or "=" in name:
            continue
        name = name[:_MAX_METRIC_NAME]  # clipped first: "first wins" among clipped names
        if name in timings:
            continue
        for param in params:
            key, sep, raw = param.partition("=")
            if not sep or key.strip().lower() != "dur":
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] == '"':
                raw = raw[1:-1]
            try:
                dur = float(raw)
            except ValueError:
                break
            if math.isfinite(dur) and dur >= 0.0:
                timings[name] = round(dur, 1)
            break
        if len(timings) >= MAX_SERVER_TIMINGS:
            break
    return timings or None


# ── The finished, plain-data timing ──────────────────────────────────────────


@dataclass(frozen=True)
class RequestTiming:
    """One streamed POST: the turn's message, or a ``/resume`` after a tool call.

    Marks are milliseconds since the turn started; ``None`` when that step never happened
    (a stream that failed before any event, a paused turn with no text)."""

    sent_ms: Optional[float]
    headers_ms: Optional[float]
    first_event_ms: Optional[float]
    first_text_ms: Optional[float]
    #: How many times it was sent: more than one after a token refresh or a lost session.
    attempts: int
    #: The engine's ``X-Request-Id`` response header, when it sends one.
    request_id: Optional[str]
    #: The ``id`` of the stream's first chunk (``chatcmpl-...``).
    completion_id: Optional[str]
    #: The engine's ``Server-Timing`` response header, ``{metric name: dur ms}``
    #: (``parse_server_timing``); None when it sent none. The names are the engine's.
    server_timing: Optional[Dict[str, float]] = None

    @property
    def ttfb_ms(self) -> Optional[float]:
        """Sent to response headers."""
        return _between(self.sent_ms, self.headers_ms)

    @property
    def wait_ms(self) -> Optional[float]:
        """Sent to the first stream event: the engine and the model."""
        return _between(self.sent_ms, self.first_event_ms)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sent_ms": self.sent_ms,
            "headers_ms": self.headers_ms,
            "first_event_ms": self.first_event_ms,
            "first_text_ms": self.first_text_ms,
            "ttfb_ms": self.ttfb_ms,
            "wait_ms": self.wait_ms,
            "attempts": self.attempts,
            "request_id": self.request_id,
            "completion_id": self.completion_id,
            "server_timing": dict(self.server_timing) if self.server_timing else None,
        }


@dataclass(frozen=True)
class ToolTiming:
    """One tool call in a turn. Marks are milliseconds since the turn started.

    ``outcome``: ``"ok"`` or ``"error"`` (the handler ran and returned or raised),
    ``"invalid"`` (its arguments failed the schema; nothing ran), ``"denied"`` (a
    permission callback or the end-user said no), ``"no_handler"``, ``"cancelled"``,
    ``"approved"`` (an engine tool the end-user allowed: it ran on the engine),
    ``"pending"`` (the turn ended while it waited for an approval)."""

    name: str
    #: ``"client"`` (a handler here) or ``"engine"`` (a platform tool behind an approval).
    kind: str
    outcome: str
    #: The approval's answer (``"once"``, ``"always"``, ``"deny"``); None when none was asked.
    decision: Optional[str]
    interrupt_ms: float
    #: Time the approval card waited for the end-user; excluded from ``total_ms``.
    approval_wait_ms: Optional[float]
    dispatched_ms: Optional[float]
    handler_started_ms: Optional[float]
    handler_finished_ms: Optional[float]
    resume: Optional[RequestTiming]

    @property
    def before_ms(self) -> Optional[float]:
        """Interrupt to the handler posted to the main thread, minus any approval wait
        (permission checks, argument validation)."""
        span = _between(self.interrupt_ms, self.dispatched_ms)
        if span is None:
            return None
        return round(span - (self.approval_wait_ms or 0.0), 1)

    @property
    def main_thread_wait_ms(self) -> Optional[float]:
        """Posted to the host's main thread until the handler started there."""
        return _between(self.dispatched_ms, self.handler_started_ms)

    @property
    def run_ms(self) -> Optional[float]:
        """The handler itself."""
        return _between(self.handler_started_ms, self.handler_finished_ms)

    @property
    def after_ms(self) -> Optional[float]:
        """Handler finished to the ``/resume`` request sent."""
        return _between(
            self.handler_finished_ms, self.resume.sent_ms if self.resume else None
        )

    @property
    def resume_ms(self) -> Optional[float]:
        """``/resume`` sent to the continuation's first event (the engine and the model)."""
        return self.resume.wait_ms if self.resume else None

    @property
    def total_ms(self) -> Optional[float]:
        """Interrupt to the continuation's first event, minus the approval wait. When every
        part is there it equals ``before + main_thread_wait + run + after + resume``."""
        end = self.resume.first_event_ms if self.resume else None
        span = _between(self.interrupt_ms, end)
        if span is None:
            return None
        return round(span - (self.approval_wait_ms or 0.0), 1)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "outcome": self.outcome,
            "decision": self.decision,
            "interrupt_ms": self.interrupt_ms,
            "approval_wait_ms": self.approval_wait_ms,
            "dispatched_ms": self.dispatched_ms,
            "handler_started_ms": self.handler_started_ms,
            "handler_finished_ms": self.handler_finished_ms,
            "before_ms": self.before_ms,
            "main_thread_wait_ms": self.main_thread_wait_ms,
            "run_ms": self.run_ms,
            "after_ms": self.after_ms,
            "resume_ms": self.resume_ms,
            "total_ms": self.total_ms,
            "resume": self.resume.as_dict() if self.resume else None,
        }


@dataclass(frozen=True)
class EngineToolTiming:
    """A platform tool the engine ran and reported (``tool_event``, with the
    ``tool_events`` capability on). Marks are milliseconds since the turn started."""

    name: str
    start_ms: float
    end_ms: Optional[float]
    status: Optional[str]

    @property
    def duration_ms(self) -> Optional[float]:
        return _between(self.start_ms, self.end_ms)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "duration_ms": self.duration_ms,
            "status": self.status,
        }


@dataclass(frozen=True)
class UiTiming:
    """The chat page's part of a turn sent from it (``talk2view.ui``)."""

    #: The page: Send pressed to the command's POST.
    send_to_post_ms: Optional[float] = None
    #: The server: the command received to the controller accepting the send (the
    #: command router's queue, and anything ahead of it).
    queue_ms: Optional[float] = None
    #: The SDK's first text token to the page receiving that op (wall clocks of one machine).
    delivery_ms: Optional[float] = None
    #: The page: the first text op received to it painted (within one frame).
    first_op_to_paint_ms: Optional[float] = None
    #: Whether the page's report arrived (False: the turn went out without it).
    reported: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "send_to_post_ms": self.send_to_post_ms,
            "queue_ms": self.queue_ms,
            "delivery_ms": self.delivery_ms,
            "first_op_to_paint_ms": self.first_op_to_paint_ms,
            "reported": self.reported,
        }


@dataclass(frozen=True)
class TurnTiming:
    """Where one turn's time went. Marks are milliseconds since ``started_at``.

    ``outcome`` is ``"done"``, ``"error"`` (``error_type`` says which: the engine's error
    type, or the exception's class name), ``"cancelled"`` or ``"abandoned"`` (``clear()``,
    a sign-out or ``close()`` dropped it)."""

    #: The id of the turn's user message in the controller state (``messages[].id``).
    turn_id: str
    #: Wall clock at the start, seconds since the epoch.
    started_at: float
    #: ``time.monotonic()`` at the start, to line up the host's own measures.
    started_monotonic: float
    outcome: str
    error_type: Optional[str]
    model: Optional[str]
    total_ms: float
    #: The turn's own request (the message); a tool's ``/resume`` is on that tool.
    request: RequestTiming
    #: The first text token of the turn, wherever it came (after a tool, too).
    first_text_ms: Optional[float]
    tools: Tuple[ToolTiming, ...]
    engine_tools: Tuple[EngineToolTiming, ...]
    #: Tool calls and engine tool events beyond the ``MAX_TOOLS`` kept.
    dropped_tools: int
    #: Streamed POSTs in the turn (the message, and every resume).
    requests: int
    #: Stream chunks in the turn, and how many carried text or reasoning.
    events: int
    text_events: int
    reasoning_events: int
    #: The page's part; None for a turn not sent from the chat page.
    ui: Optional[UiTiming] = None

    @property
    def setup_ms(self) -> Optional[float]:
        """Turn start to the request sent: session create, sign-in, skills, tool schemas."""
        return self.request.sent_ms

    @property
    def engine_wait_ms(self) -> Optional[float]:
        """Request sent to the first stream event."""
        return self.request.wait_ms

    @property
    def time_to_first_text_ms(self) -> Optional[float]:
        return self.first_text_ms

    @property
    def approval_wait_ms(self) -> float:
        return round(sum(t.approval_wait_ms or 0.0 for t in self.tools), 1)

    def as_dict(self) -> Dict[str, Any]:
        """JSON-ready: one plain dict (what the 3D Slicer extension writes as a JSONL line)."""
        started = datetime.fromtimestamp(self.started_at, timezone.utc)
        return {
            "turn_id": self.turn_id,
            "started_at": started.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "outcome": self.outcome,
            "error_type": self.error_type,
            "model": self.model,
            "total_ms": self.total_ms,
            "setup_ms": self.setup_ms,
            "engine_wait_ms": self.engine_wait_ms,
            "time_to_first_text_ms": self.time_to_first_text_ms,
            "approval_wait_ms": self.approval_wait_ms,
            "request": self.request.as_dict(),
            "tools": [t.as_dict() for t in self.tools],
            "engine_tools": [t.as_dict() for t in self.engine_tools],
            "dropped_tools": self.dropped_tools,
            "requests": self.requests,
            "events": self.events,
            "text_events": self.text_events,
            "reasoning_events": self.reasoning_events,
            "ui": self.ui.as_dict() if self.ui else None,
        }


# ── Recording one turn (internal) ────────────────────────────────────────────


class _Leg:
    __slots__ = (
        "sent", "headers", "first_event", "first_text", "attempts", "request_id",
        "completion_id", "server_timing",
    )

    def __init__(self) -> None:
        self.sent: Optional[float] = None
        self.headers: Optional[float] = None
        self.first_event: Optional[float] = None
        self.first_text: Optional[float] = None
        self.attempts = 0
        self.request_id: Optional[str] = None
        self.completion_id: Optional[str] = None
        self.server_timing: Optional[Dict[str, float]] = None


class _Tool:
    __slots__ = (
        "name", "kind", "outcome", "decision", "interrupt", "asked", "approval_wait",
        "dispatched", "started", "finished", "resume",
    )

    def __init__(self, name: str, kind: str, at: float) -> None:
        self.name = name
        self.kind = kind
        self.outcome = "pending" if kind == "engine" else "no_handler"
        self.decision: Optional[str] = None
        self.interrupt = at
        self.asked: Optional[float] = None
        self.approval_wait = 0.0
        self.dispatched: Optional[float] = None
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.resume: Optional[_Leg] = None


_current: ContextVar[Optional["_TurnRecorder"]] = ContextVar("t2v_turn", default=None)


def current_turn() -> Optional["_TurnRecorder"]:
    """The turn the calling thread is streaming for, or None (no controller turn)."""
    return _current.get()


@contextlib.contextmanager
def bind_turn(recorder: Optional["_TurnRecorder"]) -> Iterator[None]:
    """Make ``recorder`` the current turn on this thread for the ``with`` block."""
    token = _current.set(recorder)
    try:
        yield
    finally:
        _current.reset(token)


class _TurnRecorder:
    """The marks of one turn. The stream's worker writes most of them; the handler marks
    come from the host's main thread. Every method is cheap and never raises."""

    def __init__(
        self, hub: "TurnMetrics", turn_id: str, *, page: bool, received: Optional[float]
    ) -> None:
        self._hub = hub
        self._lock = threading.Lock()
        self.turn_id = turn_id
        self.start = time.monotonic()
        self.start_wall = time.time()
        self.page = page
        self.queue = None if received is None else max(0.0, self.start - received)
        self.page_report: Optional[Dict[str, Optional[float]]] = None
        self._main = _Leg()
        self._leg = self._main
        self._tools: List[_Tool] = []
        self._tool: Optional[_Tool] = None  # the latest call (kept or past MAX_TOOLS)
        self._engine_tools: List[Tuple[str, str, float, Optional[float], Optional[str]]] = []
        self._dropped = 0
        self._first_text: Optional[float] = None
        self._events = 0
        self._text = 0
        self._reasoning = 0
        self._requests = 0
        self._model: Optional[str] = None
        self._error_type: Optional[str] = None
        self._finished = False

    # The stream (the chat worker thread).

    def request_sent(self) -> None:
        now = time.monotonic()
        with self._lock:
            leg = self._leg
            if leg.sent is None:
                leg.sent = now
                self._requests += 1
            leg.attempts += 1

    def response_headers(self, headers: Mapping[str, str]) -> None:
        now = time.monotonic()
        try:
            request_id = _clip(headers.get("x-request-id"))
        except Exception:
            request_id = None
        try:
            server_timing = parse_server_timing(headers.get("server-timing"))
        except Exception:
            server_timing = None
        with self._lock:
            self._leg.headers = now
            if request_id is not None:
                self._leg.request_id = request_id
            # The latest response's (a retry after a token refresh answers for the request).
            self._leg.server_timing = server_timing

    def chunk(self, chunk: ChatCompletionChunk) -> None:
        now = time.monotonic()
        with self._lock:
            leg = self._leg
            self._events += 1
            if leg.first_event is None:
                leg.first_event = now
                leg.completion_id = _clip(chunk.id)
            if self._model is None:
                self._model = _clip(chunk.model)
            if chunk.error:
                self._error_type = _clip(chunk.error.type, 64)
            elif chunk.choices:
                delta = chunk.choices[0].delta
                if delta.content:
                    self._text += 1
                    if leg.first_text is None:
                        leg.first_text = now
                    if self._first_text is None:
                        self._first_text = now
                if delta.reasoning_content:
                    self._reasoning += 1
            event = chunk.tool_event
            if event is not None:
                self._engine_tool(
                    event.type, event.tool_name, event.tool_call_id, event.status, now
                )

    def _engine_tool(
        self, kind: str, name: str, call_id: str, status: Optional[str], now: float
    ) -> None:
        if kind == "tool_end":
            for i in range(len(self._engine_tools) - 1, -1, -1):
                entry = self._engine_tools[i]
                if entry[1] == call_id and entry[3] is None:
                    self._engine_tools[i] = (entry[0], entry[1], entry[2], now, _clip(status, 32))
                    return
        if kind != "tool_start":
            return
        if len(self._engine_tools) >= MAX_TOOLS:
            self._dropped += 1
            return
        self._engine_tools.append((_clip(name) or "?", call_id, now, None, None))

    def error(self, error_type: Optional[str]) -> None:
        with self._lock:
            self._error_type = _clip(error_type, 64) or "error"

    # Tool calls.

    def tool_interrupt(self, name: str, kind: str = "client") -> None:
        now = time.monotonic()
        with self._lock:
            self._tool = _Tool(_clip(name) or "?", kind, now)
            if len(self._tools) < MAX_TOOLS:
                self._tools.append(self._tool)
            else:
                self._dropped += 1

    def tool_outcome(self, outcome: str) -> None:
        with self._lock:
            if self._tool is not None:
                self._tool.outcome = outcome

    def approval_asked(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._tool is not None:
                self._tool.asked = now

    def approval_answered(self, decision: str) -> None:
        now = time.monotonic()
        with self._lock:
            tool = self._tool
            if tool is None:
                return
            if tool.asked is not None:
                tool.approval_wait += now - tool.asked
                tool.asked = None
            tool.decision = decision
            if decision == "deny":
                tool.outcome = "denied"
            elif tool.kind == "engine":
                tool.outcome = "approved"

    def tool_dispatched(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._tool is not None:
                self._tool.dispatched = now

    def wrap_handler(self, fn: Callable[[], T]) -> Callable[[], T]:
        """``fn`` with the handler's start and end marked, wherever it runs (the host's
        main thread): the tool it belongs to is fixed now, on the stream's thread."""
        with self._lock:
            tool = self._tool
        if tool is None:
            return fn

        def run() -> T:
            tool.started = time.monotonic()
            ok = False
            try:
                value = fn()
                ok = True
                return value
            finally:
                tool.finished = time.monotonic()
                tool.outcome = "ok" if ok else "error"

        return run

    def begin_resume(self) -> None:
        """The next request is a ``/resume``: its marks go on the latest tool call."""
        with self._lock:
            tool = self._tool
            if tool is not None and tool.resume is None:
                tool.resume = _Leg()
                self._leg = tool.resume
            else:
                self._leg = _Leg()  # a resume with no tool call to go on: marks go nowhere

    # The end.

    def finish(self, outcome: str) -> None:
        """End the turn (once; later calls are ignored) and hand it to the hub."""
        now = time.monotonic()
        with self._lock:
            if self._finished:
                return
            self._finished = True
            timing = self._build(outcome, now)
        self._hub._finished(self, timing)

    @property
    def finished(self) -> bool:
        return self._finished

    def _rel(self, mark: Optional[float]) -> Optional[float]:
        return None if mark is None else _ms(mark - self.start)

    def _leg_timing(self, leg: _Leg) -> RequestTiming:
        return RequestTiming(
            sent_ms=self._rel(leg.sent),
            headers_ms=self._rel(leg.headers),
            first_event_ms=self._rel(leg.first_event),
            first_text_ms=self._rel(leg.first_text),
            attempts=leg.attempts,
            request_id=leg.request_id,
            completion_id=leg.completion_id,
            server_timing=dict(leg.server_timing) if leg.server_timing else None,
        )

    def _build(self, outcome: str, end: float) -> TurnTiming:
        tools = []
        for tool in self._tools:
            waited = tool.approval_wait
            if tool.asked is not None:  # still waiting when the turn ended
                waited += end - tool.asked
            tools.append(
                ToolTiming(
                    name=tool.name,
                    kind=tool.kind,
                    outcome="pending" if tool.asked is not None else tool.outcome,
                    decision=tool.decision,
                    interrupt_ms=_ms(tool.interrupt - self.start),
                    approval_wait_ms=_ms(waited) if (waited or tool.decision) else None,
                    dispatched_ms=self._rel(tool.dispatched),
                    handler_started_ms=self._rel(tool.started),
                    handler_finished_ms=self._rel(tool.finished),
                    resume=self._leg_timing(tool.resume) if tool.resume else None,
                )
            )
        engine_tools = tuple(
            EngineToolTiming(
                name=name, start_ms=_ms(start - self.start), end_ms=self._rel(stop), status=status
            )
            for name, _call, start, stop, status in self._engine_tools
        )
        return TurnTiming(
            turn_id=self.turn_id,
            started_at=self.start_wall,
            started_monotonic=self.start,
            outcome=outcome,
            error_type=self._error_type if outcome == "error" else None,
            model=self._model,
            total_ms=_ms(end - self.start),
            request=self._leg_timing(self._main),
            first_text_ms=self._rel(self._first_text),
            tools=tuple(tools),
            engine_tools=engine_tools,
            dropped_tools=self._dropped,
            requests=self._requests,
            events=self._events,
            text_events=self._text,
            reasoning_events=self._reasoning,
            ui=(
                UiTiming(queue_ms=None if self.queue is None else _ms(self.queue))
                if self.page or self.queue is not None
                else None
            ),
        )


# ── The hub ──────────────────────────────────────────────────────────────────


class TurnMetrics:
    """Turn timings for one ``Talk2View`` (``t2v.metrics``): listeners, and the last
    ``history`` turns. Only a ``ChatController``'s turns are timed; a bare ``t2v.chat()``
    loop is not. Thread-safe."""

    def __init__(
        self, *, history: int = DEFAULT_HISTORY, page_grace_s: float = DEFAULT_PAGE_GRACE_S
    ) -> None:
        self._lock = threading.Lock()
        self._listeners: List[TurnListener] = []
        self._recent: Deque[TurnTiming] = deque(maxlen=max(1, history))
        self._page_grace_s = page_grace_s
        self._local = threading.local()
        #: Turns in flight, by turn id.
        self._active: "OrderedDict[str, _TurnRecorder]" = OrderedDict()
        #: Finished turns sent from the page, waiting for its report.
        self._parked: "OrderedDict[str, Tuple[TurnTiming, _TurnRecorder, threading.Timer]]" = (
            OrderedDict()
        )
        self._closed = False

    def on_turn(self, callback: TurnListener) -> Callable[[], None]:
        """Call ``callback(timing)`` once for every turn that ends from now on. Returns the
        function that stops it. See the module docstring for the thread it runs on."""
        with self._lock:
            self._listeners.append(callback)

        def off() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return off

    def recent(self) -> List[TurnTiming]:
        """The last turns delivered, oldest first."""
        with self._lock:
            return list(self._recent)

    def close(self) -> None:
        """Deliver every turn still waiting for a page report, without it."""
        with self._lock:
            self._closed = True
            parked = list(self._parked.values())
            self._parked.clear()
        for timing, _rec, timer in parked:
            timer.cancel()
            self._emit(timing)

    # The chat UI server's part.

    @contextlib.contextmanager
    def page_send(self, *, received: Optional[float], reports: bool) -> Iterator[None]:
        """Around the UI server's ``controller.send()``: the turn it starts was received as
        a command at ``received`` (``time.monotonic()``), and ``reports`` says the page will
        send its own timings afterwards (``attach_page``)."""
        self._local.pending = (received, reports)
        try:
            yield
        finally:
            self._local.pending = None

    def attach_page(
        self,
        turn_id: str,
        *,
        send_to_post_ms: Optional[float],
        first_op_to_paint_ms: Optional[float],
        first_op_at_ms: Optional[float],
    ) -> bool:
        """The page's report for ``turn_id``. False when that turn is unknown or already
        delivered (the report is dropped)."""
        report = {
            "send_to_post_ms": send_to_post_ms,
            "first_op_to_paint_ms": first_op_to_paint_ms,
            "first_op_at_ms": first_op_at_ms,
        }
        with self._lock:
            active = self._active.get(turn_id)
            if active is not None:
                active.page_report = report
                return True
            parked = self._parked.pop(turn_id, None)
        if parked is None:
            return False
        timing, rec, timer = parked
        timer.cancel()
        self._emit(_with_page(timing, rec, report))
        return True

    # The controller's part.

    def _start_turn(self, turn_id: str) -> _TurnRecorder:
        pending = getattr(self._local, "pending", None)
        self._local.pending = None
        received, reports = pending if pending is not None else (None, False)
        rec = _TurnRecorder(self, turn_id, page=reports, received=received)
        with self._lock:
            self._active[turn_id] = rec
            while len(self._active) > MAX_OPEN_TURNS:
                self._active.popitem(last=False)
        return rec

    def _finished(self, rec: _TurnRecorder, timing: TurnTiming) -> None:
        with self._lock:
            if self._active.get(rec.turn_id) is rec:
                del self._active[rec.turn_id]
            report = rec.page_report
            wait = rec.page and report is None and not self._closed and self._page_grace_s > 0
            if wait:
                timer = threading.Timer(self._page_grace_s, self._release, (rec.turn_id,))
                timer.daemon = True
                self._parked[rec.turn_id] = (timing, rec, timer)
                overflow = []
                while len(self._parked) > MAX_OPEN_TURNS:
                    overflow.append(self._parked.popitem(last=False)[1])
        if wait:
            timer.start()
            for old_timing, _old_rec, old_timer in overflow:
                old_timer.cancel()
                self._emit(old_timing)
            return
        self._emit(_with_page(timing, rec, report) if report is not None else timing)

    def _release(self, turn_id: str) -> None:
        with self._lock:
            parked = self._parked.pop(turn_id, None)
        if parked is not None:
            self._emit(parked[0])

    def _emit(self, timing: TurnTiming) -> None:
        with self._lock:
            self._recent.append(timing)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(timing)
            except Exception:
                logger.exception("a turn timing listener raised")


def _with_page(
    timing: TurnTiming, rec: _TurnRecorder, report: Mapping[str, Optional[float]]
) -> TurnTiming:
    first_op_at = report.get("first_op_at_ms")
    delivery: Optional[float] = None
    if first_op_at is not None and timing.first_text_ms is not None:
        delivery = round(first_op_at - (rec.start_wall * 1000.0 + timing.first_text_ms), 1)
    ui = UiTiming(
        send_to_post_ms=_round(report.get("send_to_post_ms")),
        queue_ms=timing.ui.queue_ms if timing.ui else None,
        delivery_ms=delivery,
        first_op_to_paint_ms=_round(report.get("first_op_to_paint_ms")),
        reported=True,
    )
    return replace(timing, ui=ui)


def _round(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(float(value), 1)
