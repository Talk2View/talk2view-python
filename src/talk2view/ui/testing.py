"""A scripted stand-in for ``ChatController``: same state shape, op format, errors and
threading contract, and no engine.

``talk2view.ui``'s own tests drive ``UIServer`` with it, and the embed page's runtime guard
runs the real page against it inside Slicer. A turn streams ``reply``
word by word, one batch per word, as the real controller does per event. A message that
starts with ``/approve`` (or every message, with ``ask_approval=True``) ends in a pending
client-tool approval; ``/fail`` ends in an error; ``/limit`` ends as a guest's run out of
allowance does, with no reply and the ``demoLimitReached`` notice (``on()``, as the real
controller's); ``/plan`` gives the reply the engine's ``- [ ]``/``- [x]`` checklist as
its ``plan`` (a ``todos`` event), and ``/plan-notes`` a plan that is free markdown. Every
public call is logged in ``calls``, and ``raise_on[name] = exc`` makes that call raise
``exc`` (for error-path tests).

Serve a built page against it, no engine needed (prints the URL, Ctrl+C stops)::

    python -m talk2view.ui.testing [--static-dir DIR] [--no-browser]

``--static-dir`` defaults to the bundled page, which a git checkout builds into
``src/talk2view/ui/static/`` (``static.BUILD_INSTRUCTIONS``); pass it
only for a bundle built somewhere else.

This is the backend for a runtime guard, not just this package's own tests, so it copies
``ChatController``'s crash- and close-safety exactly, not only its happy path: a scripted
step that raises still ends the run (``error`` set, ``isLoading`` cleared, the controller
left usable), and so does a failure while ``send``/``retry``/``edit`` record the turn,
which is then raised; ``approve()``/``cancel()`` after ``close()`` behave exactly as the
real ones do (see their docstrings), and ``close()`` deactivates every subscriber before
dropping it, so a delivery already under way on another thread is suppressed.
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union, cast

from ..controller import (
    CANCELLED_APPROVAL_FEEDBACK,
    DEMO_LIMIT_REACHED,
    ControllerBusyError,
    ControllerClosedError,
    IdentityChange,
)
from ..dispatch import Dispatcher, InlineDispatcher
from ..state import (
    ChatSnapshot,
    ConversationSnapshot,
    DisplayMessage,
    HistoryEntry,
    OpBatch,
    OpRecorder,
    initial_snapshot,
)
from ..types import Attachment, Decision

__all__ = [
    "APPROVAL_TOOL",
    "DEFAULT_REPLY",
    "DEMO_PLAN",
    "DEMO_PLAN_NOTES",
    "FakeController",
    "main",
    "words",
]

logger = logging.getLogger("talk2view.ui")

APPROVAL_TOOL = "load_sample_data"
DEFAULT_REPLY = (
    "## Segmentation plan\n\n"
    "Here is how I would approach the **MRHead** volume.\n\n"
    "1. Load the sample volume and check the *window / level* preset.\n"
    "2. Threshold the brain, then smooth with a 3 mm kernel.\n"
    "3. Measure the segment volume in millilitres.\n\n"
    "| Step | Tool |\n|---|---|\n| Load | `load_sample_data` |\n\n"
    "```python\nvolume = load_sample_data('MRHead')\n```\n\n"
    "Unicode check: café — 日本語 — ✓.\n"
)

#: ``/plan``'s plan: the checklist the engine's system prompt asks ``write_todos`` for.
DEMO_PLAN = (
    "- [x] Load the MRHead volume\n- [ ] Threshold the brain\n- [ ] Measure the segment volume\n"
)
#: ``/plan-notes``'s plan: not only a checklist, so the page shows it as markdown.
DEMO_PLAN_NOTES = "Check the **window / level** first.\n\n- [ ] Threshold the brain\n"

Executor = Callable[[Callable[[], None]], None]

#: How many times in a row a callback may record new ops from inside a delivery before
#: ``_flush`` stops re-flushing for it — the same bound and reasoning as
#: ``controller._REFLUSH_LIMIT``.
_REFLUSH_LIMIT = 100


def _thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="t2v-fake-chat", daemon=True).start()


def words(text: str) -> List[str]:
    """Split into word-sized chunks that keep their trailing whitespace."""
    out: List[str] = []
    current = ""
    for ch in text:
        current += ch
        if ch in " \n":
            out.append(current)
            current = ""
    if current:
        out.append(current)
    return out


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


class FakeController:
    #: No Talk2View behind it: the UI server's account commands answer "unavailable".
    t2v: Any = None

    def __init__(
        self,
        *,
        reply: str = DEFAULT_REPLY,
        repeat: int = 1,
        chunk_delay_s: float = 0.0,
        ask_approval: bool = False,
        tool_name: str = APPROVAL_TOOL,
        executor: Optional[Executor] = None,
        dispatcher: Optional[Dispatcher] = None,
    ) -> None:
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.raise_on: Dict[str, BaseException] = {}
        self._chunks = words(reply) * max(1, repeat)
        self._delay = chunk_delay_s
        self._ask_approval = ask_approval
        self._tool = tool_name
        self._executor: Executor = executor or _thread_executor
        self._dispatcher: Dispatcher = dispatcher or InlineDispatcher()
        self._lock = threading.RLock()
        # Looked up once, as ChatController does: best-effort lock-order guard for `_flush`,
        # None on an interpreter that offers no `_is_owned` on its RLock.
        self._lock_is_owned: Optional[Callable[[], bool]] = getattr(self._lock, "_is_owned", None)
        self._emit_lock = threading.Lock()
        self._emit_owner: Optional[int] = None
        self._reflush = False
        self._rec = OpRecorder(initial_snapshot())
        self._subscribers: List[Tuple[Callable[[OpBatch], None], bool, List[bool]]] = []
        self._listeners: Dict[str, List[Callable[[], None]]] = {}
        self._notices: List[str] = []
        self._run: Optional[int] = None
        self._runs = 0
        self._ids = 0
        self._cancel = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False

    # ── The ChatController surface ──

    def snapshot(self) -> ChatSnapshot:
        """The whole state, on a batch boundary (a valid replay base) except when called
        from inside a delivery on the emitting thread, where it is a plain copy."""
        if self._emit_owner != threading.get_ident():
            captured = self._flush(capture=True)
            if captured is not None:
                return captured
        with self._lock:
            return self._rec.snapshot()

    def subscribe(
        self, on_batch: Callable[[OpBatch], None], *, deliver_on_main: bool = False
    ) -> Callable[[], None]:
        """Receive every op batch, in ``seq`` order. Raises ``ControllerClosedError`` once
        closed. Returns an unsubscribe function."""
        entry = (on_batch, deliver_on_main, [True])
        with self._lock:
            if self._closed:
                raise ControllerClosedError("FakeController is closed")
            self._subscribers.append(entry)

        def unsubscribe() -> None:
            entry[2][0] = False
            with self._lock:
                if entry in self._subscribers:
                    self._subscribers.remove(entry)

        return unsubscribe

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        """A one-shot notice (``demoLimitReached``, ``conversationReplaced``), as
        ``ChatController.on``: the callback runs on the emitting thread, just before that
        batch reaches the subscribers. Returns the function that removes it."""
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

        def off() -> None:
            with self._lock:
                callbacks = self._listeners.get(event, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return off

    def send(self, text: str, attachments: Optional[List[Attachment]] = None) -> None:
        """Start a turn. Raises ``ControllerBusyError`` (a run or approval is already in
        progress) or ``ControllerClosedError``."""
        self._log("send", text, attachments)
        with self._lock:
            self._require_idle()
            started = self._start_turn(text, attachments)
        self._launch_turn(started, text)

    def approve(self, decision: Decision, *, tool_call_id: Optional[str] = None) -> None:
        """Answer the pending approval and resume on a new run. A no-op when nothing (or a
        stale ``tool_call_id``) is pending — even once closed: only a *matching* pending
        approval reaches ``_start_run()`` and raises ``ControllerClosedError``, exactly as
        the real controller's ``approve()`` does."""
        self._log("approve", decision, tool_call_id)
        with self._lock:
            pending = self._rec.state["pendingApproval"]
            if pending is None:
                return
            if tool_call_id is not None and tool_call_id != pending["toolCallId"]:
                return  # a stale or double click answers nothing
            run = self._start_run()  # raises ControllerClosedError once closed
            index = len(self._rec.state["messages"]) - 1
            if decision.action == "always" and decision.approved:
                allowed = list(self._rec.state["alwaysAllowedTools"])
                if pending["toolName"] not in allowed:
                    self._rec.set(["alwaysAllowedTools"], allowed + [pending["toolName"]])
            self._rec.set(["pendingApproval"], None)
            self._rec.set(["isLoading"], True)
        self._launch(run, lambda: self._resume(run, index, decision))

    def cancel(self) -> None:
        """Stop generating, or deny a pending approval when there is no stream to stop.
        Never raises — safe to call from any thread, including after ``close()``, and only
        the approval pending when it was called is denied."""
        self._log("cancel")
        with self._lock:
            closed = self._closed
            pending = self._rec.state["pendingApproval"]
            running = self._run is not None
        if running:
            self._cancel.set()
            return
        if pending is not None and not closed:
            try:
                self.approve(
                    Decision(action="deny", feedback=CANCELLED_APPROVAL_FEEDBACK),
                    tool_call_id=pending["toolCallId"],
                )
            except (ControllerBusyError, ControllerClosedError):
                pass  # answered, or closed, between the read above and this call
            except Exception:
                logger.exception("cancel() could not deny the pending approval")

    def retry(self) -> None:
        """Remove the last user turn and everything after it, then send it again with its
        original attachments."""
        self._log("retry")
        with self._lock:
            self._require_idle()
            messages = list(self._rec.state["messages"])
            users = [i for i, m in enumerate(messages) if m["role"] == "user"]
            if not users:
                return
            last = messages[users[-1]]
            text = last["content"]
            files = [Attachment(**a) for a in last.get("attachments", [])]
            self._rec.replace_messages(messages[: users[-1]])
            started = self._start_turn(text, files or None)
        self._launch_turn(started, text)

    def edit(self, message_id: str, text: str) -> None:
        """Rewrite a user message and send it again with its own attachments. Raises
        ``ValueError`` when no *user* message has that id."""
        self._log("edit", message_id, text)
        with self._lock:
            self._require_idle()
            messages = list(self._rec.state["messages"])
            index = next(
                (
                    i
                    for i, m in enumerate(messages)
                    if m["id"] == message_id and m["role"] == "user"
                ),
                None,
            )
            if index is None:
                raise ValueError("No user message with id {!r}".format(message_id))
            files = [Attachment(**a) for a in messages[index].get("attachments", [])]
            self._rec.replace_messages(messages[:index])
            started = self._start_turn(text, files or None)
        self._launch_turn(started, text)

    def clear(self) -> None:
        """Reset the conversation and, like the real ``clear_session()`` DELETE, run as a
        job on the executor: ``isLoading`` is True until that job (which does no network
        work here) ends. Allowed while busy: a run in flight is abandoned. Raises
        ``ControllerClosedError`` after ``close()``."""
        self._log("clear")
        with self._lock:
            if self._closed:
                raise ControllerClosedError("FakeController is closed")
            self._abandon()
            self._rec.replace_messages([])
            for key, value in (
                ("pendingApproval", None),
                ("error", None),
                ("agentStatus", None),
                ("threadId", None),
                ("alwaysAllowedTools", []),
            ):
                self._set(key, value)
            run = self._start_run()
            self._set("isLoading", True)
        self._launch(run, lambda: self._finish_clear(run))

    def export_conversation(self) -> ConversationSnapshot:
        """As ``ChatController.export_conversation``: copies, ``isStreaming`` dropped, and
        a history the fake builds from its messages (one entry per non-empty message).
        No ``marks``: the fake does not track them, and ``ThreadsState`` treats a snapshot
        with none as it would a TS-written one — its own ``_marks_for`` fallback applies,
        which is exactly what a fake with no history-mark bookkeeping should exercise."""
        self._log("export_conversation")
        with self._lock:
            return self._export_locked()

    def _export_locked(self) -> ConversationSnapshot:
        messages = [
            cast(DisplayMessage, copy.deepcopy(dict(m))) for m in self._rec.state["messages"]
        ]
        for message in messages:
            message.pop("isStreaming", None)
        history: List[HistoryEntry] = [
            {"role": m["role"], "content": m["content"]} for m in messages if m["content"]
        ]
        return {
            "messages": messages,
            "history": history,
            "threadId": self._rec.state["threadId"],
        }

    def restore_conversation(self, snapshot: ConversationSnapshot) -> None:
        """As ``ChatController.restore_conversation``: the run is abandoned, the transcript
        replaced, consent and the rest cleared. Raises ``ControllerClosedError`` once closed."""
        self._log("restore_conversation", snapshot)
        with self._lock:
            if self._closed:
                raise ControllerClosedError("FakeController is closed")
            self._restore_locked(snapshot)
        self._flush()

    def follow_identity(
        self, callback: Callable[[IdentityChange], None]
    ) -> Tuple[IdentityChange, Callable[[], None]]:
        """As ``ChatController.follow_identity``, for a fake nobody signs in to: epoch 0,
        nobody, and no change ever reaches ``callback``."""
        self._log("follow_identity")
        return IdentityChange(0, None, False), lambda: None

    def exchange_conversation(
        self,
        snapshot: Optional[ConversationSnapshot],
        *,
        epoch: int,
        carry: Optional[bool] = None,
    ) -> Optional[ConversationSnapshot]:
        """As ``ChatController.exchange_conversation``: export, then (with ``snapshot``)
        restore, in one locked step; None for any ``epoch`` but 0. ``carry`` is ignored:
        nobody signs in to the fake."""
        self._log("exchange_conversation", snapshot)
        with self._lock:
            if snapshot is not None and self._closed:
                raise ControllerClosedError("FakeController is closed")
            if epoch != 0:
                return None
            exported = self._export_locked()
            if snapshot is not None:
                self._restore_locked(snapshot)
        if snapshot is not None:
            self._flush()
        return exported

    def _restore_locked(self, snapshot: ConversationSnapshot) -> None:
        self._abandon()
        messages = [
            cast(DisplayMessage, copy.deepcopy(dict(m))) for m in snapshot["messages"]
        ]
        for message in messages:
            message.pop("isStreaming", None)
        self._rec.replace_messages(messages)
        for key, value in (
            ("isLoading", False),
            ("error", None),
            ("pendingApproval", None),
            ("agentStatus", None),
            ("threadId", snapshot["threadId"]),
            ("alwaysAllowedTools", []),
        ):
            self._set(key, value)
        # As the real ChatController: called by both restore_conversation and a
        # snapshot-carrying exchange_conversation, under self._lock either way.
        self._notices.append("conversationReplaced")

    def clear_error(self) -> None:
        """Clear ``error``, if any."""
        self._log("clear_error")
        with self._lock:
            self._set("error", None)
        self._flush()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until no run is active."""
        return self._idle.wait(timeout)

    def close(self) -> None:
        """Abandon any run and deactivate every subscriber — so a delivery already under
        way on another thread is suppressed — before dropping them."""
        self._log("close")
        with self._lock:
            self._closed = True
            self._abandon()
            for _on_batch, _on_main, active in self._subscribers:
                active[0] = False
            self._subscribers = []
            self._listeners = {}

    # ── Scripted runs (worker thread) ──

    def _stream_turn(self, run: int, index: int, text: str) -> None:
        if text.startswith("/limit"):
            self._demo_limit(run, index)
            return
        if text.startswith("/plan"):
            plan = DEMO_PLAN_NOTES if text.startswith("/plan-notes") else DEMO_PLAN
            with self._lock:
                if self._run != run:
                    return
                self._rec.set(["messages", index, "plan"], plan)
            self._flush()
        streamed = self._stream(run, index, self._chunks)
        if streamed and text.startswith("/fail"):
            with self._lock:
                if self._run == run:
                    self._set("error", "Scripted failure")
        elif streamed and (self._ask_approval or text.startswith("/approve")):
            with self._lock:
                if self._run != run:
                    return
                self._ids += 1
                self._rec.set(
                    ["messages", index, "steps"],
                    [{"name": self._tool, "status": "running", "args": {"name": "MRHead"}}],
                )
                self._set(
                    "pendingApproval",
                    {
                        "toolCallId": "call-{}".format(self._ids),
                        "toolName": self._tool,
                        "arguments": {"name": "MRHead"},
                        "description": "Load the MRHead sample volume into the scene?",
                    },
                )
                self._set("isLoading", False)
                self._set("agentStatus", None)
                self._end_run(run)
            self._flush()
            return
        self._finish(run, index)

    def _demo_limit(self, run: int, index: int) -> None:
        """``/limit``: as the real controller ends a guest's turn at the demo limit. No
        reply and no error: the empty reply segment goes, and a ``demoLimitReached`` notice
        is raised with the batch."""
        with self._lock:
            if self._run != run:
                return
            self._rec.replace_messages(list(self._rec.state["messages"][:index]))
            self._set("isLoading", False)
            self._set("agentStatus", None)
            self._notices.append(DEMO_LIMIT_REACHED)
            self._end_run(run)
        self._flush()

    def _resume(self, run: int, index: int, decision: Decision) -> None:
        denied = not decision.approved
        with self._lock:
            if self._run != run:
                return
            self._rec.set(
                ["messages", index, "steps"],
                [
                    {
                        "name": self._tool,
                        "status": "denied" if denied else "used",
                        "args": {"name": "MRHead"},
                        "result": (decision.feedback or "Denied by the user")
                        if denied
                        else "Loaded MRHead",
                    }
                ],
            )
        self._flush()
        tail = "\n\nUnderstood: nothing was loaded.\n" if denied else "\n\nMRHead is loaded.\n"
        self._stream(run, index, words(tail))
        self._finish(run, index)

    def _stream(self, run: int, index: int, chunks: List[str]) -> bool:
        for chunk in chunks:
            if self._cancel.is_set():
                return False
            with self._lock:
                if self._run != run:
                    return False
                self._rec.append_text(["messages", index, "content"], chunk)
            self._flush()
            if self._delay:
                time.sleep(self._delay)
        return True

    def _finish(self, run: int, index: int) -> None:
        with self._lock:
            if self._run == run:
                self._rec.set(["messages", index, "isStreaming"], False)
                self._set("isLoading", False)
                self._set("agentStatus", None)
                self._set("threadId", "fake-thread")
                self._end_run(run)
        self._flush()

    def _finish_clear(self, run: int) -> None:
        """``clear()``'s job: no network work, just ends the run it started."""
        with self._lock:
            if self._run == run:
                self._set("isLoading", False)
                self._end_run(run)
        self._flush()

    # ── Plumbing (``_lock`` held unless noted) ──

    def _log(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        exc = self.raise_on.get(name)
        if exc is not None:
            raise exc

    def _set(self, key: str, value: Any) -> None:
        if cast(Dict[str, Any], self._rec.state)[key] != value:
            self._rec.set([key], value)

    def _require_idle(self) -> None:
        if self._closed:
            raise ControllerClosedError("FakeController is closed")
        if self._run is not None:
            raise ControllerBusyError("A run is already in progress")
        if self._rec.state["pendingApproval"] is not None:
            raise ControllerBusyError("A tool call is waiting for approval")

    def _start_run(self) -> int:
        if self._closed:
            raise ControllerClosedError("FakeController is closed")
        self._runs += 1
        self._run = self._runs
        self._cancel.clear()
        self._idle.clear()
        return self._runs

    def _end_run(self, run: int) -> None:
        if self._run == run:
            self._run = None
            self._idle.set()

    def _abandon(self) -> None:
        self._run = None
        self._cancel.set()
        self._idle.set()

    def _start_turn(
        self, text: str, attachments: Optional[List[Attachment]]
    ) -> Union[Tuple[int, int], BaseException]:
        """Start a run with the user message and an empty streaming reply; returns the run
        and the reply's index, for ``_launch_turn``. As ``ChatController._prepare_send``,
        a failure once the run has started ends the run and is returned, not raised."""
        run = self._start_run()
        index: Optional[int] = None
        try:
            self._ids += 1
            user: DisplayMessage = {
                "id": "u-{}".format(self._ids),
                "role": "user",
                "content": text,
                "timestamp": _now(),
            }
            if attachments:
                user["attachments"] = [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "mime_type": a.mime_type,
                        "size_bytes": a.size_bytes,
                    }
                    for a in attachments
                ]
            self._rec.append_message(user)
            assistant: DisplayMessage = {
                "id": "a-{}".format(self._ids),
                "role": "assistant",
                "content": "",
                "timestamp": _now(),
                "isStreaming": True,
                "steps": [],
            }
            index = self._rec.append_message(assistant)
            self._set("isLoading", True)
            self._set("error", None)
            self._set("agentStatus", {"type": "thinking", "message": "Thinking"})
        except BaseException as exc:
            if index is not None:  # the empty reply goes, as the real controller drops it
                self._rec.replace_messages(list(self._rec.state["messages"][:index]))
            self._set("error", str(exc))
            self._set("isLoading", False)
            self._set("agentStatus", None)
            self._end_run(run)
            return exc
        return run, index

    def _launch_turn(self, started: Union[Tuple[int, int], BaseException], text: str) -> None:
        """Stream the turn ``_start_turn`` started (no lock held). When it returned the
        failure that ended its run instead, deliver that end and raise the failure."""
        if isinstance(started, BaseException):
            try:
                self._flush()
            except Exception:
                logger.exception("could not deliver the batch that undid a failed launch")
            raise started
        run, index = started
        self._launch(run, lambda: self._stream_turn(run, index, text))

    def _launch(self, run: int, job: Callable[[], None]) -> None:
        """Deliver the run's first batch, then run ``job`` on the executor, guarded (no
        lock held)."""
        self._flush()
        self._executor(lambda: self._guarded(run, job))

    def _guarded(self, run: int, job: Callable[[], None]) -> None:
        """A step that raises must not leave the controller busy forever — mirrors
        ``ChatController._run``'s crash safety: the run ends, ``error`` is set to the
        exception's message, and the resulting batch is delivered."""
        try:
            job()
        except Exception as exc:
            logger.exception("scripted run failed")
            with self._lock:
                if self._run == run:
                    self._set("error", str(exc))
                    self._set("isLoading", False)
        finally:
            with self._lock:
                self._end_run(run)
            self._flush()

    def _flush(self, *, capture: bool = False) -> Optional[ChatSnapshot]:
        """Cut and deliver batches in ``seq`` order (never with ``_lock`` held: doing so
        raises rather than risking the deadlock). A callback that records ops from inside a
        delivery gets them delivered next, never nested — and one that keeps doing so on
        every batch gives up its re-flushing after ``_REFLUSH_LIMIT`` rounds rather than
        holding the emit lock forever."""
        if self._lock_is_owned is not None and self._lock_is_owned():
            raise RuntimeError(
                "_flush() must not be called while holding the state lock: the emit lock "
                "is taken below, and _lock before _emit_lock inverts the lock order"
            )
        me = threading.get_ident()
        if self._emit_owner == me:
            self._reflush = True
            return None
        captured: Optional[ChatSnapshot] = None
        with self._emit_lock:
            self._emit_owner = me
            try:
                rounds = 0
                while True:
                    self._reflush = False
                    with self._lock:
                        batch = self._rec.flush()
                        if capture and captured is None:
                            captured = self._rec.snapshot()
                        subscribers = list(self._subscribers)
                        notices, self._notices = self._notices, []
                        callbacks = [cb for n in notices for cb in self._listeners.get(n, [])]
                    for callback in callbacks:
                        try:
                            callback()
                        except Exception:
                            logger.exception("listener raised")
                    if batch is not None:
                        for on_batch, on_main, active in subscribers:
                            self._deliver(on_batch, on_main, active, batch)
                    if not self._reflush:
                        break
                    rounds += 1
                    if rounds >= _REFLUSH_LIMIT:
                        self._reflush = False
                        logger.error(
                            "subscriber feedback loop: a callback records an op on every "
                            "batch; stopped re-flushing after %d rounds",
                            _REFLUSH_LIMIT,
                        )
                        break
            finally:
                self._emit_owner = None
        return captured

    def _deliver(
        self,
        on_batch: Callable[[OpBatch], None],
        on_main: bool,
        active: List[bool],
        batch: OpBatch,
    ) -> None:
        def call() -> None:
            if not active[0]:
                return
            try:
                on_batch(batch)
            except Exception:
                logger.exception("subscriber raised; the run continues")

        if on_main:
            self._dispatcher.post(call)
        else:
            call()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Serve the chat page against a ``FakeController`` until Ctrl+C (or ``host.stop()``)."""
    from .host import GenericHost
    from .server import UIServer

    parser = argparse.ArgumentParser(
        prog="python -m talk2view.ui.testing",
        description="Serve the chat page against a scripted controller (no engine).",
    )
    parser.add_argument("--static-dir", default=None, help="a built page (default: bundled)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--chunk-delay", type=float, default=0.03, help="seconds per word")
    parser.add_argument("--repeat", type=int, default=1, help="stream the reply N times")
    parser.add_argument("--ask-approval", action="store_true", help="every turn asks")
    parser.add_argument("--no-browser", action="store_true", help="only print the URL")
    args = parser.parse_args(argv)
    host = GenericHost(open_url=(lambda url: None) if args.no_browser else None)
    controller = FakeController(
        repeat=args.repeat, chunk_delay_s=args.chunk_delay, ask_approval=args.ask_approval
    )
    server = UIServer(controller, host, static_dir=args.static_dir, port=args.port)
    print(server.start(), flush=True)
    try:
        server.show()
        host.run_forever()
    finally:
        server.stop()
        controller.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
