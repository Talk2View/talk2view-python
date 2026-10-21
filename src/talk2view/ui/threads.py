"""ThreadsState: the chat's list of conversations (TS ``chat/lib/use-conversations.ts``).

It owns the list, writes the ``threads`` slice and answers ``threadNew``, ``threadSwitch``,
``threadRename`` and ``threadDelete``. ``talk2view.conversations`` is the storage half.

What happens when:

- **New chat** keeps the conversation on screen in the list and starts an empty one. New
  again on an empty chat does nothing. The engine session is detached, never deleted
  (``newSession``'s DELETE on the wire is a different command).
- **Switching** hands the controller the stored transcript; the next message opens a new
  engine session and replays it.
- **Signing in or out**: the list follows the end-user. Whoever is there now sees their own
  conversations and an empty chat. The one exception is a visitor who started typing
  before anything authenticated -- a guest, or nobody -- whose conversation moves across
  with them into the account they sign in to, provided it was started in this process's
  life (``_fresh``): one read back from storage may be an earlier visitor's. The
  controller makes that call at the moment of the change, from what this list told it
  when it put the conversation on screen (``exchange_conversation(carry=...)``), and this
  list follows it. A guest who signs out leaves their conversation in the guest's bucket
  (the controller drops it from the screen; TS carries it into the signed-out bucket).
- **A guest becoming an account** keeps its user id, so nothing moves.
- Empty conversations other than the current one are dropped, so New / switch / New never
  fills the list, or its 50-per-identity cap, with blanks.

Threads. One worker thread, ``t2v-ui-threads``, owns every field of this object from
``_hub`` down: the list, the current id, the identity, the debounce. Nothing else reads or
writes them. Everything that reaches this object from outside posts an event to the
mailbox and returns:

- a controller batch (``_on_batch``: the controller's emitting thread, inside its delivery
  window) posts ``_BATCH``;
- a change of end-user (``_on_identity``: the same thread, since
  ``ChatController.follow_identity`` delivers a change ahead of the batch carrying its
  ops) posts ``_IDENTITY``;
- the four commands (the router's worker thread), ``attach()`` and ``close()`` post theirs
  and then wait for the result, up to ``COMMAND_TIMEOUT_S``: the only waits on the worker,
  and never made from the worker itself.

There is no timer thread. The worker waits on the mailbox with a timeout that ends at the
debounce deadline (``WRITE_DELAY_S`` after the last change, ``WRITE_MAX_DELAY_S`` after the
first one it holds) and syncs and writes once that passes.

The worker alone calls out, one call at a time, in event order, holding no lock:
``exchange_conversation`` on the controller, ``read``/``write`` on the store (whose own
write lock is a leaf), ``set_slice`` on the hub. The only lock this class has is the
mailbox's condition, held for a deque append or pop and never across a call. The lock
order is therefore "whatever the posting thread holds (the controller's emit window, say)
-> the mailbox lock", and nothing waits the other way round.

Nothing is read back after the fact. Every change to what is on screen is one
``exchange_conversation``, which exports the outgoing conversation and restores the
incoming one in one locked section of the controller, so no message can land between the
two. An identity change arrives with the conversation it took off the screen
(``IdentityChange.forgotten``), exported in the locked section that took it. And every
exchange names the identity epoch the worker has caught up with: one that races an
identity change is refused whole, nothing changed -- that change's event, carrying the
conversation, is on its way -- and a command refused that way runs again once the worker
has taken the event.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from ..controller import IdentityChange
from ..conversations import (
    Conversation,
    ConversationStore,
    identity_key,
    new_id,
    now_ms,
    title_from,
)
from ..state import ConversationSnapshot, DisplayMessage
from ..storage import MemoryStorage, TokenStorage
from .hub import EventHub

if TYPE_CHECKING:
    from .. import Talk2View

__all__ = ["COMMAND_TIMEOUT_S", "WRITE_DELAY_S", "WRITE_MAX_DELAY_S", "ThreadsState"]

logger = logging.getLogger("talk2view.ui")

#: Written this long after the last change (TS WRITE_DELAY_MS).
WRITE_DELAY_S = 0.5
#: ... but never later than this after the first pending one (TS WRITE_MAX_DELAY_MS).
WRITE_MAX_DELAY_S = 5.0
#: How long a command, ``attach()`` or ``close()`` waits for the worker. A command's own
#: work is one controller exchange (a deep copy, milliseconds) and one store write (at
#: most 512 KB, with a ``FileStorage`` fsync tens of milliseconds, a slow host store a few
#: hundred), possibly queued behind one debounce write: well under a second. The router
#: runs one command at a time, so this is also how long a stuck list can hold up the
#: page's ``send`` and ``cancel`` behind it. Five seconds is over ten times the slowest
#: honest case and short enough that a hang surfaces as an error the page can show.
#: A command that runs out of it raises ``TimeoutError`` (the router's
#: ``internal_error``); ``attach()`` too, which fails ``UIServer.start()``.
COMMAND_TIMEOUT_S = 5.0

_BATCH = "batch"
_IDENTITY = "identity"
_COMMAND = "command"
_ATTACH = "attach"
_CLOSE = "close"
_SETTLE = "settle"
_TICK = "tick"


def _empty() -> ConversationSnapshot:
    return {"messages": [], "history": [], "threadId": None}


def _first_user(messages: List[DisplayMessage]) -> Optional[DisplayMessage]:
    return next((m for m in messages if m["role"] == "user"), None)


def _changes_the_list(batch: Any) -> bool:
    """Whether a controller batch adds, drops or replaces a message (``set`` on
    ``messages`` or on one message) or ends a turn (``isLoading`` set False). A streamed
    token is an ``append-text`` and does not."""
    for op in batch.get("ops", ()) if isinstance(batch, dict) else ():
        path = op.get("path") or []
        if op.get("type") != "set" or not path:
            continue
        if path[0] == "messages" and len(path) <= 2:
            return True
        if path == ["isLoading"] and op.get("value") is False:
            return True
    return False


class _Refused(Exception):
    """An exchange named an identity epoch the controller has moved past."""


class _Reply:
    """One caller waiting on the worker for one event's result."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._result: Any = None
        self._error: Optional[BaseException] = None

    def resolve(self, result: Any = None, error: Optional[BaseException] = None) -> None:
        self._result, self._error = result, error
        self._done.set()

    def wait(self, timeout: float, what: str) -> Any:
        """The result, or the event's exception. ``TimeoutError`` when the worker did not
        get to it in time (it still runs, in its turn)."""
        if not self._done.wait(timeout):
            raise TimeoutError(
                "thread list: {} did not finish within {:.1f} s".format(what, timeout)
            )
        if self._error is not None:
            raise self._error
        return self._result


@dataclass
class _Event:
    kind: str
    args: Tuple[Any, ...] = ()
    reply: Optional[_Reply] = None
    name: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


class _Mailbox:
    """The worker's queue. Its condition is the only lock ``ThreadsState`` has."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._events: Deque[_Event] = deque()
        self._closed = False

    def put(self, event: _Event, *, front: bool = False, force: bool = False) -> bool:
        """Queue ``event``; False once closed, unless ``force`` (an identity change, whose
        conversation ``close`` still has to take)."""
        with self._cond:
            if self._closed and not force:
                return False
            if front:
                self._events.appendleft(event)
            else:
                self._events.append(event)
            self._cond.notify()
            return True

    def get(self, timeout: Optional[float]) -> Optional[_Event]:
        with self._cond:
            if not self._events:
                self._cond.wait(timeout)
            return self._events.popleft() if self._events else None

    def close(self) -> None:
        """Refuse every later ``put`` but a forced one; what is queued stays."""
        with self._cond:
            self._closed = True

    def drain(self) -> List[_Event]:
        with self._cond:
            events = list(self._events)
            self._events.clear()
            return events


class ThreadsState:
    """One ``UIServer``'s list. Build it over the controller and the ``Talk2View`` (None
    for a controller without one: the list then lives in ``storage`` under ``partner_key``,
    defaulting to memory and ``"unknown"``). The controller must offer
    ``exchange_conversation`` and ``follow_identity``, as ``ChatController`` and
    ``talk2view.ui.testing.FakeController`` do. ``clock`` (monotonic seconds) is for tests;
    ``timeout`` defaults to ``COMMAND_TIMEOUT_S``.
    """

    def __init__(
        self,
        controller: Any,
        t2v: Optional["Talk2View"],
        *,
        storage: Optional[TokenStorage] = None,
        partner_key: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: Optional[float] = None,
    ) -> None:
        if storage is None:
            storage = t2v.data_storage if t2v is not None else MemoryStorage()
        if partner_key is None:
            partner_key = t2v.partner_key if t2v is not None else "unknown"
        self._controller = controller
        self._store = ConversationStore(storage, partner_key)
        self._clock = clock
        self._timeout = COMMAND_TIMEOUT_S if timeout is None else timeout
        self._mailbox = _Mailbox()
        self._worker: Optional[threading.Thread] = None
        self._at_exit: Optional[Callable[[], None]] = None
        # ── The worker's alone from here down ──
        self._hub: Optional[EventHub] = None
        self._conversations: List[Conversation] = []  # newest first
        self._current = ""
        self._seen = 0  # messages the current one had when its title and date were last set
        self._fresh: Set[str] = set()  # ids started in this process's life
        self._identity = "none"
        self._epoch = 0  # the last identity change taken (follow_identity's count)
        self._dirty = False  # a batch arrived since the last sync
        self._oldest: Optional[float] = None  # when the first change the debounce holds came
        self._deadline: Optional[float] = None
        self._parked: List[_Event] = []  # commands refused by an identity change
        self._offs: List[Callable[[], None]] = []
        self._closed = False

    # ── Lifecycle (the host's thread) ──

    def attach(self, hub: EventHub) -> None:
        """Read the identity's list, adopt a transcript the controller already holds (else
        restore the newest stored one, else start blank), seed the slice, and follow the
        controller. Returns once the slice is seeded. Call once. Raises ``TimeoutError`` when
        the worker could not seed it in time; the list then stops as soon as the worker is
        free, writing nothing, and needs no ``close()``."""
        if self._worker is not None:
            raise RuntimeError("ThreadsState.attach() runs once")
        started = _Reply()
        start = _Event(_ATTACH, (hub,), started)
        self._mailbox.put(start)  # first: every event the subscriptions post queues behind it
        try:
            baseline, off = self._controller.follow_identity(self._on_identity)
            self._offs.append(off)
            self._offs.append(self._controller.subscribe(self._on_batch))
        except BaseException:
            for off in self._offs:
                off()
            self._offs = []
            raise
        start.extra["baseline"] = baseline
        self._worker = threading.Thread(target=self._run, name="t2v-ui-threads", daemon=True)
        self._worker.start()
        ref = weakref.ref(self)

        def close_at_exit() -> None:
            threads = ref()
            if threads is not None:
                threads.close()

        # A host that quits without UIServer.stop() still gets the last change written.
        self._at_exit = close_at_exit
        atexit.register(close_at_exit)
        try:
            started.wait(self._timeout, "attach")
        except TimeoutError:
            # Nothing was seeded, so nothing may be written: stop once the worker is free.
            atexit.unregister(close_at_exit)
            self._at_exit = None
            self._mailbox.put(_Event(_CLOSE, extra={"write": False}), force=True)
            self._mailbox.close()
            raise

    def close(self) -> None:
        """Stop following; then, after everything already queued, sync the conversation on
        screen, write the list and stop the worker. Idempotent; a no-op before
        ``attach()``. Waits up to the timeout for that, and never raises for it. Called
        from inside the controller's delivery window (``UIServer.stop()`` from a
        subscriber, which the server already warns against) it can wait the whole timeout:
        a command queued ahead of it needs that window to restore a conversation."""
        if self._at_exit is not None:
            atexit.unregister(self._at_exit)
            self._at_exit = None
        worker = self._worker
        if worker is None:
            return
        done = _Reply()
        queued = self._mailbox.put(_Event(_CLOSE, reply=done))
        self._mailbox.close()
        if queued and threading.current_thread() is not worker:
            try:
                done.wait(self._timeout, "close")
            except TimeoutError:
                # Stopping must not fail: the worker writes when it gets there.
                logger.warning("thread list: close() did not finish in time", exc_info=True)

    # ── Commands (the router's worker thread) ──

    def new_thread(self) -> None:
        """``threadNew``: keep the current conversation in the list, detached, and start a
        blank one. A no-op while the chat on screen is empty."""
        self._command("new_thread")

    def switch(self, thread_id: str) -> None:
        """``threadSwitch``: put ``thread_id``'s transcript on screen. Raises ``KeyError``
        for an id this list does not have."""
        self._command("switch", thread_id)

    def rename(self, thread_id: str, title: str) -> None:
        """``threadRename``. Renaming does not move a conversation's place in the list.
        Raises ``KeyError`` for an unknown id."""
        self._command("rename", thread_id, title)

    def delete(self, thread_id: str) -> None:
        """``threadDelete``. Deleting the current conversation blanks it. Raises
        ``KeyError`` for an unknown id."""
        self._command("delete", thread_id)

    def _command(self, name: str, *args: Any) -> None:
        if self._worker is None:
            return  # not attached: there is no list to act on
        reply = _Reply()
        event = _Event(_COMMAND, args, reply, name=name)
        if threading.current_thread() is self._worker:
            self._handle(event)  # a call-out re-entering: never wait on ourselves
            reply.wait(0.0, name)
            return
        if self._mailbox.put(event):
            reply.wait(self._timeout, name)

    # ── Callbacks (other threads: post and return) ──

    def _on_batch(self, batch: Any) -> None:
        # The debounce runs from now. A batch that adds or replaces a message, or ends a
        # turn, also brings the list's title and count up to date at once (TS syncs on
        # `messagesChange` and on `loadingChange(false)`); a streamed token does not.
        self._mailbox.put(_Event(_BATCH, (self._clock(), _changes_the_list(batch))))

    def _on_identity(self, change: IdentityChange) -> None:
        self._mailbox.put(_Event(_IDENTITY, (change,)), force=True)

    # ── Test hooks ──

    def _settle(self, timeout: float = 5.0, *, flush: bool = False) -> bool:
        """Wait until everything posted so far has run, a write that is due included --
        with ``flush``, a pending one too, so nothing is left to publish. False once
        closed."""
        reply = _Reply()
        if not self._mailbox.put(_Event(_SETTLE, reply=reply, extra={"flush": flush})):
            return False
        reply.wait(timeout, "settle")
        return True

    def _poke(self) -> None:
        """Wake the worker to look at the clock again, without waiting for it."""
        self._mailbox.put(_Event(_TICK))

    # ── The worker ──

    def _run(self) -> None:
        while True:
            wait = None if self._deadline is None else max(0.0, self._deadline - self._clock())
            event = self._mailbox.get(wait)
            try:
                if event is not None and self._handle(event):
                    return
                self._write_if_due()
            except Exception:
                logger.exception("thread list: an event failed")

    def _handle(self, event: _Event) -> bool:
        """Run one event; True for ``close``, after which the worker stops."""
        if event.kind == _BATCH:
            self._changed(event.args[0])
            if event.args[1]:
                self._refresh()
        elif event.kind == _IDENTITY:
            self._follow(event.args[0])
        elif event.kind == _COMMAND:
            self._run_command(event)
        elif event.kind == _ATTACH:
            self._reply(event, lambda: self._start(event.args[0], event.extra["baseline"]))
        elif event.kind == _SETTLE:
            self._reply(event, lambda: self._write_if_due(force=event.extra["flush"]))
        elif event.kind == _CLOSE:
            self._reply(event, lambda: self._finish(write=event.extra.get("write", True)))
            return True
        return False

    @staticmethod
    def _reply(event: _Event, job: Callable[[], Any]) -> None:
        reply = event.reply or _Reply()
        try:
            result = job()
        except Exception as exc:
            reply.resolve(error=exc)
        else:
            reply.resolve(result)

    def _run_command(self, event: _Event) -> None:
        reply = event.reply or _Reply()
        if self._closed:
            reply.resolve()
            return
        jobs: Dict[str, Callable[..., None]] = {
            "new_thread": self._new_thread,
            "switch": self._switch,
            "rename": self._rename,
            "delete": self._delete,
        }
        job = jobs[event.name]
        try:
            job(*event.args)
        except _Refused:
            self._parked.append(event)  # the end-user changed under it: again after that
        except Exception as exc:
            reply.resolve(error=exc)
        else:
            reply.resolve()

    def _changed(self, now: float) -> None:
        self._dirty = True
        if self._oldest is None:
            self._oldest = now
        self._deadline = min(now + WRITE_DELAY_S, self._oldest + WRITE_MAX_DELAY_S)

    def _refresh(self) -> None:
        """Title, date and count from the screen now; the write waits for the debounce."""
        try:
            self._sync()
        except _Refused:
            return  # the identity change's event brings the conversation
        self._dirty = True  # synced, not written: the deadline still stands
        self._publish()

    def _write_if_due(self, force: bool = False) -> None:
        if self._deadline is None or (not force and self._clock() < self._deadline):
            return
        self._deadline = self._oldest = None
        try:
            self._sync()
        except _Refused:
            return  # the identity change's event carries the conversation, and re-arms
        self._publish()
        self._persist()

    # ── The controller (worker only) ──

    def _exchange(
        self, snapshot: Optional[ConversationSnapshot], carry: Optional[bool] = None
    ) -> ConversationSnapshot:
        exported: Optional[ConversationSnapshot] = self._controller.exchange_conversation(
            snapshot, epoch=self._epoch, carry=carry
        )
        if exported is None:
            raise _Refused()
        return exported

    def _sync(self) -> None:
        """Take the controller's transcript into the current conversation."""
        exported = self._exchange(None)
        self._dirty = False
        self._fold(exported)

    def _fold(self, snapshot: ConversationSnapshot) -> None:
        """``snapshot`` is the current conversation's latest state."""
        current = self._find(self._current)
        if current is None:
            return
        current.snapshot = snapshot
        # Title and date move when the transcript gains a message, not per token.
        if len(snapshot["messages"]) != self._seen:
            self._seen = len(snapshot["messages"])
            current.updated_at = now_ms()
            if current.title is None:
                current.title = title_from(_first_user(snapshot["messages"]))
            self._conversations.sort(key=lambda c: c.updated_at, reverse=True)

    # ── Lifecycle events (worker only) ──

    def _start(self, hub: EventHub, baseline: IdentityChange) -> None:
        self._hub = hub
        self._epoch = baseline.epoch
        self._identity = identity_key(baseline.user_id)
        self._conversations = self._stored(self._identity)
        try:
            # A transcript already on screen is one somebody is in the middle of: it carries.
            on_screen: Optional[ConversationSnapshot] = self._exchange(None, carry=True)
        except _Refused:
            on_screen = None  # the change's own event follows, with the conversation
        if on_screen is not None and on_screen["messages"]:
            self._adopt(on_screen)
        elif self._conversations:
            self._resume(self._conversations[0])
        else:
            self._blank()
        self._prune()
        self._publish()

    def _adopt(self, snapshot: ConversationSnapshot) -> None:
        """Into the stored conversation it came from, where the last message identifies
        one, else into a new one; started here either way."""
        last_id = snapshot["messages"][-1]["id"]
        known = next(
            (
                c
                for c in self._conversations
                if c.snapshot["messages"] and c.snapshot["messages"][-1]["id"] == last_id
            ),
            None,
        )
        if known is None:
            known = self._new_conversation(snapshot)
            self._conversations.insert(0, known)
        known.snapshot = snapshot
        self._current = known.id
        self._seen = len(snapshot["messages"])
        self._fresh.add(known.id)

    def _resume(self, newest: Conversation) -> None:
        """Pick up where this device left off. Not fresh: it may be an earlier visitor's."""
        try:
            outgoing = self._exchange(newest.snapshot, carry=False)
        except (_Refused, ValueError):
            logger.warning("thread list: the newest stored conversation could not be restored")
            self._blank()
            return
        self._current = newest.id
        self._seen = len(newest.snapshot["messages"])
        if outgoing["messages"]:
            # Typed in the moment between looking at the screen and replacing it: kept.
            self._conversations.insert(1, self._new_conversation(outgoing))

    def _follow(self, change: IdentityChange) -> None:
        """A change of end-user, taken once each, in epoch order."""
        if change.epoch <= self._epoch:
            return  # recorded before attach() read the identity
        self._epoch = change.epoch
        carried: Optional[Conversation] = None
        if change.forgotten is not None:
            # Off the screen: the outgoing user's conversation as it stood at that moment.
            self._fold(change.forgotten)
            self._dirty = False
        else:
            # Kept on screen, which the controller only does for one this list let follow.
            carried = self._find(self._current)
            try:
                self._sync()  # what it holds now, before it is written anywhere
            except _Refused:
                pass  # a later change is queued: its event brings the conversation
        left = [
            c
            for c in self._conversations
            if (carried is None or c.id != carried.id) and c.snapshot["messages"]
        ]
        self._store.write(self._identity, left, None)
        self._identity = identity_key(change.user_id)
        self._conversations = self._stored(self._identity)
        if carried is not None:
            self._conversations.insert(0, carried)
            self._conversations.sort(key=lambda c: c.updated_at, reverse=True)
        else:
            self._blank()  # the controller has emptied the screen already
        self._prune()
        self._publish()
        self._persist()
        if self._dirty and self._deadline is None:
            self._changed(self._clock())
        parked, self._parked = self._parked, []
        for event in reversed(parked):
            self._mailbox.put(event, front=True, force=True)

    def _finish(self, write: bool = True) -> None:
        """``close``: sync once more, stop following, write, and let the worker go.

        The last sync runs while this list still follows identity changes, so one that
        beats it is certain to arrive here: the sync is refused, the change's event is
        taken (and with it the outgoing conversation), and the sync runs again. Only once
        a sync has gone through does the list stop following; what reaches the screen
        after that is after close."""
        self._closed = True
        offs, self._offs = self._offs, []
        if not write:  # attach() gave up: nothing was seeded, so nothing is written
            for off in reversed(offs):
                off()
            for event in self._parked + self._mailbox.drain():
                if event.reply is not None:
                    event.reply.resolve()
            self._parked = []
            return
        for _ in range(8):
            try:
                self._sync()
                break
            except _Refused:
                if not self._take_identity_change():
                    logger.warning("thread list: an identity change never arrived at close")
                    break
        for off in reversed(offs):  # the batch subscription first, the identity one last
            off()
        for event in self._mailbox.drain():
            if event.kind == _IDENTITY:
                self._follow(event.args[0])  # a change after the last sync: written too
            elif event.reply is not None:
                event.reply.resolve()  # a command behind close: nothing left to act on
        self._publish()
        self._persist()
        for event in self._parked:
            if event.reply is not None:
                event.reply.resolve()
        self._parked = []

    def _take_identity_change(self) -> bool:
        """Wait (a moment, at most) for the identity change a refused exchange announced,
        and take it; events queued ahead of it run as usual."""
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            event = self._mailbox.get(max(0.0, deadline - time.monotonic()))
            if event is None:
                continue
            if event.kind == _IDENTITY:
                self._follow(event.args[0])
                return True
            if event.kind == _BATCH:
                self._changed(event.args[0])
            elif event.reply is not None:
                event.reply.resolve()  # a command behind close: nothing left to act on
        return False

    # ── Commands (worker only) ──

    def _new_thread(self) -> None:
        on_screen = self._exchange(None)
        self._dirty = False
        self._fold(on_screen)
        if not on_screen["messages"]:
            self._publish()
            return  # already on an empty chat: nothing stacks up
        self._fold(self._exchange(_empty(), carry=True))
        self._blank()
        self._prune()
        self._publish()
        self._persist()

    def _switch(self, thread_id: str) -> None:
        target = self._find(thread_id)
        if target is None:
            raise KeyError(thread_id)
        if thread_id == self._current:
            return
        self._fold(self._exchange(target.snapshot, carry=thread_id in self._fresh))
        self._dirty = False
        self._current = thread_id
        self._seen = len(target.snapshot["messages"])
        self._prune()
        self._publish()
        self._persist()

    def _rename(self, thread_id: str, title: str) -> None:
        target = self._find(thread_id)
        if target is None:
            raise KeyError(thread_id)
        target.title = title.strip() or None  # keeps its place: renaming is not activity
        self._publish()
        self._persist()

    def _delete(self, thread_id: str) -> None:
        if self._find(thread_id) is None:
            raise KeyError(thread_id)
        if thread_id == self._current:
            self._exchange(_empty(), carry=True)  # what was on screen goes with it
            self._dirty = False
            self._conversations = [c for c in self._conversations if c.id != thread_id]
            self._blank()
        else:
            self._conversations = [c for c in self._conversations if c.id != thread_id]
        self._publish()
        self._persist()

    # ── Helpers (worker only) ──

    def _find(self, thread_id: str) -> Optional[Conversation]:
        return next((c for c in self._conversations if c.id == thread_id), None)

    @staticmethod
    def _new_conversation(snapshot: ConversationSnapshot) -> Conversation:
        return Conversation(
            id=new_id(),
            title=title_from(_first_user(snapshot["messages"])),
            created_at=now_ms(),
            updated_at=now_ms(),
            snapshot=snapshot,
        )

    def _blank(self) -> None:
        conversation = self._new_conversation(_empty())
        self._conversations.insert(0, conversation)
        self._current = conversation.id
        self._seen = 0
        self._fresh.add(conversation.id)

    def _prune(self) -> None:
        self._conversations = [
            c for c in self._conversations if c.snapshot["messages"] or c.id == self._current
        ]

    def _publish(self) -> None:
        if self._hub is None:
            return
        self._hub.set_slice(
            "threads",
            {
                "currentId": self._current or None,
                "items": [
                    {
                        "id": c.id,
                        "title": c.title,
                        "updatedAt": c.updated_at,
                        "messageCount": len(c.snapshot["messages"]),
                    }
                    for c in self._conversations
                ],
            },
        )

    def _persist(self) -> None:
        """Write the identity's conversations. Never an empty one: a blank is not a
        conversation yet, and one stored would come back at the next start as somebody's
        (``_resume`` treats what it reads as possibly an earlier visitor's). An identity
        with nothing but a blank has its bucket removed."""
        if not self._dirty:
            self._deadline = self._oldest = None  # nothing left for the debounce to write
        kept = [c for c in self._conversations if c.snapshot["messages"]]
        self._store.write(self._identity, kept, self._current)

    def _stored(self, identity: str) -> List[Conversation]:
        """The identity's stored conversations, less any empty one (an older write, or a
        TS page's, may hold blanks): a blank read back is nobody's, so never resumed."""
        return [c for c in self._store.read(identity) if c.snapshot["messages"]]
