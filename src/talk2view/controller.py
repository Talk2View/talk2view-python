"""ChatController — the chat state machine, ported from the TS ``Talk2View`` class.

Python owns all chat state; a UI is a view. Every mutation goes through an ``OpRecorder`` so
the state and the op batches subscribers receive cannot diverge. What an event *means* — which
segment a delta lands in, how a step is found, how a turn is finalized — lives in
``_reducer.TurnReducer``; this module is the threaded shell around it: the public API, the
run loop, the locks and the batch delivery.

Threading: one ``RLock`` (``_lock``) guards the state, and a second lock (``_emit_lock``)
makes one thread at a time cut a batch and hand it to the subscribers, so delivery order
equals ``seq`` for every subscriber. **Lock order: ``_emit_lock`` before ``_lock``.** A
thread may take ``_lock`` while holding ``_emit_lock`` (that is what ``_flush`` does); no
thread may ever hold ``_lock`` and then wait on ``_emit_lock``, so ``_flush`` must never be
called with ``_lock`` held. ``snapshot()`` goes through the same emit window: state is only
ever copied where a batch is cut, so a snapshot is always a replay base for the batches that
follow it.

Network streaming runs on a worker (the ``executor``); tool handlers run on the host main
thread inside ``T2VTools`` (its dispatcher), and a ``deliver_on_main`` subscriber gets its
batches posted through that same dispatcher. An approval never parks a thread: the worker
ends the run in the same locked section that sets ``pendingApproval`` — so the two never
coexist, whichever thread cuts that batch — and ``approve()`` starts a new run.

Because ``_emit_lock`` is held across the subscriber and listener callbacks, a callback that
runs on the emitting thread (``deliver_on_main=False``, the default) must never block waiting
on the host's main thread: that thread may already be inside ``send()`` waiting for the same
emit lock, and neither side could then move. ``subscribe()`` states the rule for hosts. Under
an ``InlineDispatcher`` a ``deliver_on_main=True`` callback runs on the emitting thread too,
so the same rule covers it; and no ``Dispatcher.post`` may block, because it is called from
inside that window.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

from ._reducer import DEMO_LIMIT_REACHED, TurnReducer, _Consumed, _Cursor, now_iso, set_top
from .cancel import Cancelled, CancelToken
from .dispatch import Dispatcher
from .sessions import build_user_content
from .state import (
    AttachmentDict,
    ChatSnapshot,
    DisplayMessage,
    OpBatch,
    OpRecorder,
    PendingApproval,
    SkillsState,
    initial_snapshot,
)
from .types import Attachment, Capabilities, ChatEvent, ChatMessage, Decision, User

if TYPE_CHECKING:
    from . import Talk2View
    from .skills import T2VSkills

__all__ = [
    "CANCELLED_APPROVAL_FEEDBACK",
    "DEMO_LIMIT_REACHED",
    "Capabilities",
    "ChatController",
    "ControllerBusyError",
    "ControllerClosedError",
    "Decision",
]

logger = logging.getLogger("talk2view.controller")

#: Feedback sent when ``cancel()`` denies a pending approval (same string as the TS adapter).
CANCELLED_APPROVAL_FEEDBACK = "Cancelled by the user"
# DEMO_LIMIT_REACHED (the ``on()`` event name) is defined in ``_reducer`` and re-exported here.
#: How many times in a row a callback may record new ops from inside a delivery before
#: ``_flush`` stops re-flushing for it. Deep enough that no sane host reaches it; shallow
#: enough that a runaway callback cannot hold the emit lock for long.
_REFLUSH_LIMIT = 100

BatchCallback = Callable[[OpBatch], None]
StreamOpener = Callable[[], Iterator[ChatEvent]]
#: ``(run id, assistant segment id, job)``: a turn ``_prepare_send`` started, to launch.
_Prepared = Tuple[int, str, Callable[[], None]]


class ControllerBusyError(RuntimeError):
    """A run is in progress, or a tool call is waiting for the end-user's decision."""


class ControllerClosedError(RuntimeError):
    """The controller was closed."""


@dataclass(eq=False)
class _Subscriber:
    """``eq=False``: a registration is identified by object, not by value. Subscribing the
    same callback twice with the same flag makes two registrations, and ``unsubscribe`` has
    to drop the one it was handed out for — a field-wise ``__eq__`` drops the first match."""

    on_batch: BatchCallback
    deliver_on_main: bool
    active: bool = True


def _thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="t2v-chat", daemon=True).start()


def _attachment_dict(attachment: Attachment) -> AttachmentDict:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size_bytes": attachment.size_bytes,
    }


def _close_stream(stream: Optional[Iterator[ChatEvent]]) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: S110 — closing a finished generator must never raise out
            pass


class ChatController:
    """The headless chat state machine: one conversation, owned entirely by Python.

    One controller per ``Talk2View`` — it holds that client's chat session, its LLM history
    and the single run in flight, so a second controller on the same client would fight it
    for both. Close it explicitly (``close()``), before the ``Talk2View``: it subscribes to
    ``t2v.skills`` and ``t2v.auth``, and a controller dropped without ``close()`` stays
    subscribed — it keeps receiving skills changes and may still recycle the session. A UI
    is a view over it and never a second source of truth: read the state with
    ``snapshot()``, follow it with ``subscribe()``, and drive it with ``send()``,
    ``approve()``, ``cancel()``, ``retry()``, ``edit()`` and ``clear()``.

    The state belongs to one end-user, and the controller follows ``t2v.auth`` to keep it
    that way (``isAnonymous`` too), hearing every change before any host listener does.
    Whenever the signed-in user id changes, approval
    consent (``alwaysAllowedTools``) is reset: no identity inherits another's "always
    allow". On a sign-out — ``logout()``, or a refresh token the engine rejected — and on
    a switch to a different account (one anonymous session to another, an account to an
    anonymous session, one account to another) the conversation is dropped here as well,
    as ``clear()`` drops it but with no engine call: the run in flight is abandoned, and
    the messages, the replayed history, a pending approval and ``threadId`` go. The
    conversation is kept when someone signs in from signed out (the anonymous start a
    send makes included: that send carries on) and when the anonymous demo becomes an
    account (``signup()``'s convert, or its sign-in to an existing account), so the next
    turn replays it into the account's session.

    State reaches a view two ways, and they are two halves of one protocol: ``snapshot()``
    returns the whole state with the ``seq`` of the batch it was cut on, and each subscriber
    then receives ``{"seq", "ops"}`` batches to apply in order (``talk2view.state``'s
    ``apply_batch``). ``subscribe()`` documents the joining rules, the two things a callback
    must never do, and which thread a callback runs on.

    Every method is safe to call from any thread and none of them blocks on the run: a turn
    streams on the executor's worker, tool handlers run on the host's main thread through the
    dispatcher, and an approval parks nothing — the worker returns with ``pendingApproval``
    set and ``approve()`` starts the resume as a new run.
    """

    def __init__(
        self,
        t2v: "Talk2View",
        *,
        dispatcher: Optional[Dispatcher] = None,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        capabilities: Optional[Capabilities] = None,
        executor: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._t2v = t2v
        # Batches for `deliver_on_main` subscribers go through this dispatcher. By default it
        # is the one the tools already use, so handlers and UI callbacks share a thread.
        self._dispatcher: Dispatcher = (
            dispatcher if dispatcher is not None else t2v.tools.dispatcher
        )
        self._system_prompt = system_prompt
        self._executor = executor or _thread_executor
        # Talk2View binds model and capabilities to the session, not to each chat() call.
        if model is not None or capabilities is not None:
            t2v.set_chat_defaults(model=model, capabilities=capabilities)

        self._lock = threading.RLock()  # guards everything below
        # CPython's RLock (both the C and the pure-Python build) answers "does THIS thread
        # hold it?"; `_flush` uses it to catch a lock-order inversion. Looked up once, and
        # None on an interpreter that does not offer it -- the guard is best-effort.
        self._lock_is_owned: Optional[Callable[[], bool]] = getattr(self._lock, "_is_owned", None)
        self._emit_lock = threading.Lock()  # one deliverer at a time -> delivery order == seq
        self._emit_owner: Optional[int] = None  # thread id delivering right now
        self._reflush = False  # a subscriber mutated state from inside its callback
        # Whose conversation and consent this is: the signed-in user's id (None when signed
        # out) and whether that session is anonymous. Followed through `t2v.auth` from the
        # end of `__init__` (`_on_auth_state_change`).
        self._auth_user_id: Optional[str] = None
        self._auth_anonymous = False
        try:
            self._auth_user_id, self._auth_anonymous = self._read_identity()
        except Exception:
            logger.exception("could not read the signed-in user")
        snapshot = initial_snapshot(is_anonymous=self._auth_anonymous)
        # The skills slice is owned by `T2VSkills`: seeded here (no op); every later change
        # arrives through `set_skills_state`, subscribed at the end of `__init__`.
        skills: Optional["T2VSkills"] = getattr(t2v, "skills", None)  # absent on the test fakes
        if skills is not None:
            snapshot["skills"] = skills.state()
        # The object subscribed to below; `set_skills_state` reads its live state.
        self._skills = skills
        self._rec = OpRecorder(snapshot)
        self._reducer = TurnReducer(self._rec)  # message ids and event handling live here
        self._subscribers: List[_Subscriber] = []
        self._listeners: Dict[str, List[Callable[[], None]]] = {}
        self._notices: List[str] = []

        # LLM history: clean user/assistant alternation (display messages are segmented).
        self._history: List[ChatMessage] = []
        # user display-message id -> len(history) just before that turn was recorded.
        self._history_marks: Dict[str, int] = {}
        self._last_user_text: Optional[str] = None
        self._last_user_attachments: Optional[List[Attachment]] = None
        self._turn_failed = False

        self._run_counter = 0
        self._active_run: Optional[int] = None
        self._cancel_token: Optional[CancelToken] = None
        self._idle = threading.Event()
        self._idle.set()
        self._closed = False
        # Last, once every attribute `set_skills_state` reads exists: the skills debounce
        # timer may call it from its own thread the moment it is registered.
        self._skills_off: Optional[Callable[[], None]] = (
            skills.on_change(self.set_skills_state) if skills is not None else None
        )
        if skills is not None:
            # Resync: a change delivered between the seed above and the subscription reached
            # no listener here. Unchanged (the usual case), `_set_top` records no op. No
            # recycle decision and no flush: the next change, or `chat()`, handles a reset.
            with self._lock:
                self._set_top("skills", skills.state())
        # Early: this listener runs before every host listener, so none of them can block
        # the reset (a racing send would run B's tool on A's "always") or change the auth
        # state before it has looked (a sign-in from inside a sign-out's delivery would
        # read as the demo becoming an account). Absent on the test fakes.
        follow = getattr(t2v.auth, "_add_listener", None)
        self._auth_off: Optional[Callable[[], None]] = (
            follow(self._on_auth_state_change, early=True) if callable(follow) else None
        )
        if self._auth_off is not None:
            # The same resync for the identity. There is no conversation or consent yet for
            # a change in that window to reset, so only the identity and the slice move.
            with self._lock:
                try:
                    self._auth_user_id, self._auth_anonymous = self._read_identity()
                except Exception:
                    logger.exception("could not read the signed-in user")
                self._set_top("isAnonymous", self._auth_anonymous)

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def t2v(self) -> "Talk2View":
        """The ``Talk2View`` this controller drives. Read-only: ``talk2view.ui`` reads the
        account (auth, models, config, preferences, skills) from it."""
        return self._t2v

    def snapshot(self) -> ChatSnapshot:
        """A deep copy of the state **on a batch boundary**: ``seq`` is the batch it was cut
        with, and no op recorded after that cut is in it. That is what makes it a replay base
        — apply every batch whose ``seq`` is greater and you arrive at the live state.

        Mutations are recorded as they happen but only get their ``seq`` when a batch is cut,
        so a plain copy of the live state could carry ops the next batch will carry too, and a
        consumer following ``subscribe``'s protocol would apply them twice. This call therefore
        takes the copy inside the same locked section that cuts the pending batch, and delivers
        that batch as usual. It never waits on the run; it may wait briefly for another thread's
        delivery window to close.

        The one exception is a snapshot taken from inside a delivery callback on the emitting
        thread (``deliver_on_main=False``, or ``deliver_on_main=True`` under an inline
        dispatcher): cutting there would hand batch n+1 to the subscribers that have not seen
        batch n yet. Such a snapshot is a plain copy — fine to read, **not** a replay base.
        """
        captured: Optional[ChatSnapshot] = None
        if self._emit_owner != threading.get_ident():
            captured = self._flush(capture=True)
        if captured is not None:
            return captured
        with self._lock:
            return self._rec.snapshot()

    def subscribe(
        self, on_batch: BatchCallback, *, deliver_on_main: bool = False
    ) -> Callable[[], None]:
        """Receive every op batch, in ``seq`` order. Returns an unsubscribe function.

        Take ``snapshot()`` after subscribing and drop batches whose ``seq`` is not greater
        than the snapshot's; on a gap (``seq != last + 1``) take a fresh snapshot. Batches are
        shared between subscribers: treat them as read-only.

        With ``deliver_on_main`` the callback is posted through the dispatcher; otherwise it
        runs on the emitting thread. Note which dispatcher: the default is the one the tools
        use, and if that is an ``InlineDispatcher`` (the default for a host with no main
        thread to speak of) a post runs the callback right there on the emitting thread — so
        the ``deliver_on_main=False`` rules below apply to it as well. Whatever the
        dispatcher, its ``post`` must not block: the emitting thread calls it while holding
        the window every other emitter is waiting on.

        A callback may read ``snapshot()`` and call back into the controller (``approve()``,
        ``clear_error()``, …); the ops that produces arrive in a later batch, never out of
        order. Two things a callback must not do:

        * A ``deliver_on_main=False`` callback (the default) runs on the emitting thread,
          inside the delivery window every other emitter is waiting on. It must not block
          waiting on the host's main thread — ``run_on_main``, a blocking Qt connection, a
          queue only the UI thread drains — nor on work another thread has to route through
          this controller. That main thread may already be inside ``send()``, waiting for
          this very delivery to finish. Anything that needs the UI thread belongs in a
          ``deliver_on_main=True`` subscriber, which is posted and never waited on.
        * No callback may call ``wait_idle()``. On the emitting thread the run's last batch
          can only go idle once the callback returns, so the wait could only be ended by the
          thread doing the waiting — and it holds up every other emitter meanwhile. On the
          main thread it stalls the pump that the worker's tool handlers need.

        With ``deliver_on_main`` a batch may still be queued for the host's main thread when
        ``wait_idle()`` returns — delivery there is post-and-forget, so a host that stops
        pumping can never stall a run.
        """
        subscriber = _Subscriber(on_batch=on_batch, deliver_on_main=deliver_on_main)
        with self._lock:
            if self._closed:
                raise ControllerClosedError("ChatController is closed")
            self._subscribers.append(subscriber)

        def unsubscribe() -> None:
            subscriber.active = False
            with self._lock:
                if subscriber in self._subscribers:
                    self._subscribers.remove(subscriber)

        return unsubscribe

    def on(self, event: str, callback: Callable[[], None]) -> Callable[[], None]:
        """Listen for a one-shot notice that is not chat state. Events: ``demoLimitReached``.

        The callback always runs on the emitting thread (there is no ``deliver_on_main`` for
        notices), just before that batch reaches the subscribers, so ``subscribe()``'s rules
        for a callback apply to it too.
        """
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

        def off() -> None:
            with self._lock:
                callbacks = self._listeners.get(event, [])
                if callback in callbacks:
                    callbacks.remove(callback)

        return off

    def clear_error(self) -> None:
        with self._lock:
            self._set_top("error", None)
        self._flush()

    def wait_idle(self, timeout: Optional[float] = None) -> bool:
        """Block until no run is active. A pending approval counts as idle (no thread parks):
        idle can already be reported while the worker is still closing the paused response of
        that approval — the run itself has already ended, so this is harmless, but a `t2v-chat`
        thread may still be unwinding for a moment after this call returns."""
        return self._idle.wait(timeout)

    def close(self) -> None:
        """Abandon any run, drop subscribers and listeners, stop following ``t2v.skills``
        and ``t2v.auth``.

        Does not close the Talk2View. Close the controller before the Talk2View, so that
        what it abandons (a run, a queued session recycle) stops before the client closes.
        """
        with self._lock:
            self._closed = True
            token = self._cancel_token
            self._active_run = None
            self._cancel_token = None
            for subscriber in self._subscribers:
                subscriber.active = False
            self._subscribers = []
            self._listeners = {}
            skills_off, self._skills_off = self._skills_off, None
            auth_off, self._auth_off = self._auth_off, None
            self._idle.set()
        if skills_off is not None:
            skills_off()
        if auth_off is not None:
            auth_off()
        if token is not None:
            token.cancel()

    def set_skills_state(self, state: SkillsState) -> None:
        """Record the skills slice as ONE ``set ["skills"]`` op and deliver it.

        ``T2VSkills`` calls this through ``on_change`` after every change; a host never
        writes the slice itself, so it stays on the controller's ``seq`` line. When the
        state asks for a session reset — the engine still holds a removed or disabled skill
        it cannot drop: it is older than the ``replace`` flag, or a replace failed — and the
        controller is idle, the engine session is recycled on the executor: DELETE +
        create, on which ``Talk2View`` re-sends the tools and the enabled skills. Messages
        and history are kept: the next turn replays them. The controller is busy
        (``isLoading``) for those round trips, as it is during ``clear()``. While a run is
        active or an approval is pending nothing is recycled here; ``Talk2View.chat()``
        does it before the next turn instead. On an engine that honours ``replace`` a
        removal never asks for a reset: it is sent as the next (debounced) register call.

        ``state`` is taken as a wake-up, not as the value to record: ``T2VSkills``
        delivers outside its own locks, so two threads' deliveries can arrive here out of
        order. Recording the older one last would leave a stale slice, and deciding on it
        could start a recycle nothing needs any more or skip one that is needed. So the
        live state is read instead, under this controller's lock, and both the slice and
        the recycle decision use it — controller lock, then the skills lock, is the allowed
        order; ``T2VSkills`` never calls in here holding its own. ``state`` is used as
        given only when the ``Talk2View`` has no skills (the test fakes).
        """
        with self._lock:
            if self._closed:
                return
            current = self._skills.state() if self._skills is not None else state
            self._set_top("skills", current)
            recycle = (
                current["needsSessionReset"]
                and self._active_run is None
                and self._rec.state["pendingApproval"] is None
            )
            if recycle:
                run_id, _token = self._start_run()
                self._set_top("isLoading", True)
        if not recycle:
            self._flush()
            return

        def job() -> None:
            try:
                with self._lock:
                    abandoned = not self._is_current(run_id)
                # `clear()` (which deletes the session itself) or `close()` abandoned this
                # run before the executor got to it: a DELETE + create now is not wanted.
                if not abandoned:
                    self._t2v.recycle_session()
            except Exception:
                # Logged, not surfaced: the reset stays pending and chat() retries the
                # recycle before the next turn.
                logger.exception("recycle_session failed")
            finally:
                with self._lock:
                    if self._is_current(run_id):
                        self._set_top("isLoading", False)
                    self._end_run(run_id)
                self._flush()

        self._launch(run_id, job)

    def send(self, text: str, attachments: Optional[List[Attachment]] = None) -> None:
        """Start a turn. Non-blocking: it returns as soon as the job is on the executor.

        Raises:
            ControllerBusyError: a run is in progress, or a tool call is waiting for an
                approval. Answer or ``cancel()`` that first. A session recycle started by a
                skills change (on an engine that merges, or after a failed replace) is such a
                run too, so this can raise right after ``skills.remove(...)``, as it can
                right after ``clear()``.
            ControllerClosedError: the controller was closed.
            Exception: whatever the ``executor`` raises when it refuses the job (a pool that
                was shut down), and whatever the host's ``Dispatcher.post`` raises while the
                first batch is handed to it — re-raised unchanged, after the started run has
                been undone, so the controller is left usable rather than busy for good.
                The same goes for whatever recording the turn raises (an attachment that is
                not an ``Attachment``): the run ends with ``error`` set, then it is raised.
        """
        with self._lock:
            self._require_idle()
            prepared = self._prepare_send(text, list(attachments or []))
        self._launch_prepared(prepared)

    def cancel(self) -> None:
        """Stop generating. With an approval pending there is no stream to stop, so the call
        is denied instead (what a partner expects Stop to mean there). Safe to call from any
        thread; never blocks on the run; never raises. A denial the executor or the host's
        dispatcher refuses to launch is logged and surfaces as ``error``, as it would from
        ``approve()`` -- the controller is left idle, not busy. Only the approval pending when
        Stop was pressed is denied: one answered and replaced by another in the meantime
        leaves the new card up."""
        with self._lock:
            closed = self._closed
            approval = self._rec.state["pendingApproval"]
            # Read with the rest, under the lock, and handed to `approve()` below: it retakes
            # the lock only later, and the denial must not land on a newer card by then.
            pending_id = None if approval is None else approval["toolCallId"]
            token = self._cancel_token
        if token is not None:
            # A live token means a stream to stop. It never coexists with a pending approval:
            # `_consume` sets `pendingApproval` and ends the run (dropping the token) in ONE
            # locked section, and the read above is under that same lock -- so the two
            # branches here are exclusive, and this one is simply "stop the stream".
            token.cancel()
            return
        if pending_id is not None and not closed:
            try:
                self.approve(
                    Decision(action="deny", feedback=CANCELLED_APPROVAL_FEEDBACK),
                    tool_call_id=pending_id,
                )
            except (ControllerBusyError, ControllerClosedError):
                # ControllerBusyError/ControllerClosedError are caught defensively: close() may
                # have run since the locked read above. Another approve()/cancel() answering
                # that approval first is not a race this can lose: `approve()` is a no-op once
                # it is gone or replaced (the id above no longer matches). Either way there is
                # nothing left for Stop to do.
                pass
            except Exception:
                # The executor refused the denial's resume, or the host's `Dispatcher.post`
                # raised while its first batch was posted. `approve()` re-raises that on
                # purpose, but only after `_launch` undid the run and recorded the failure as
                # `error` -- the host already sees it there, so Stop logs it and returns.
                # This also catches a bug inside `approve()`'s own locked section. That is
                # logged the same way, but nothing has undone the run it may have started, so
                # the controller can stay busy until `clear()`.
                logger.exception("cancel() could not deny the pending approval")

    def approve(self, decision: Decision, *, tool_call_id: Optional[str] = None) -> None:
        """Answer the pending approval and resume on a new run.

        A no-op when nothing is pending. Pass ``tool_call_id`` — the ``toolCallId`` of the
        approval the card was drawn for — and the call is also a no-op when the controller is
        waiting on a *different* call: a stale or double click on a card the conversation has
        already moved past then answers nothing instead of answering the wrong thing.

        Raises:
            ControllerClosedError: the controller was closed.
            Exception: whatever the ``executor`` raises when it refuses the job (a pool that
                was shut down), and whatever the host's ``Dispatcher.post`` raises while the
                first batch is handed to it — re-raised unchanged, after the started run has
                been undone, so the controller is left usable rather than busy for good.
        """
        with self._lock:
            pending = self._rec.state["pendingApproval"]
            if pending is None:
                return
            if tool_call_id is not None and tool_call_id != pending["toolCallId"]:
                return
            approval = cast(PendingApproval, dict(pending))
            # Cannot raise ControllerBusyError: under this lock, a pending approval and an
            # active run never coexist. The worker ends its run in the locked section that
            # sets `pendingApproval`, and every run starter either clears the pending card as
            # it starts (this method, `clear()`) or refuses while one is up (`_require_idle`)
            # -- a new starter must do one or the other.
            run_id, token = self._start_run()
            # Remembered only for the literal "always" on a CLIENT tool: a
            # malformed action is a denial, and an engine-gated call — or any kind this SDK
            # does not know — must still be asked about next time. An absent `kind` is a
            # client tool (its PendingApproval carries the four TS keys and nothing else).
            if (
                decision.action == "always"
                and decision.approved
                and approval.get("kind", "client") == "client"
            ):
                allowed = list(self._rec.state["alwaysAllowedTools"])
                if approval["toolName"] not in allowed:
                    allowed.append(approval["toolName"])
                    self._rec.set(["alwaysAllowedTools"], allowed)
            self._set_top("pendingApproval", None)
            self._set_top("isLoading", True)
            assistant_id: str = self._last_assistant_id() or self._reducer.append_assistant()

        def open_stream() -> Iterator[ChatEvent]:
            return self._open_approval_stream(approval, decision, token)

        def job() -> None:
            self._run(run_id, token, open_stream, assistant_id)

        self._launch(run_id, job, assistant_id)

    def retry(self) -> None:
        """Remove the last user turn and everything after it, then send it again."""
        with self._lock:
            if self._last_user_text is None:
                return
            self._require_idle()
            text = self._last_user_text
            files = list(self._last_user_attachments or [])
            messages = list(self._rec.state["messages"])
            while messages and messages[-1]["role"] == "assistant":
                messages.pop()
            mark: Optional[int] = None
            while messages and messages[-1]["role"] == "user":
                mark = self._history_marks.get(messages[-1]["id"], mark)
                messages.pop()
            self._truncate(messages, mark)
            self._set_top("error", None)
            prepared = self._prepare_send(text, files)
        self._launch_prepared(prepared)

    def edit(self, message_id: str, text: str) -> None:
        """Rewrite a user message: drop it and everything after it, then send ``text``.

        Raises:
            ValueError: no *user* message has that id — it was never there, an assistant
                message was named, or an earlier ``edit``/``retry``/``clear`` removed it.
            ControllerBusyError: as ``send``.
            ControllerClosedError: the controller was closed.
        """
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
            self._truncate(messages[:index], self._history_marks.get(message_id))
            self._set_top("error", None)
            prepared = self._prepare_send(text, files)
        self._launch_prepared(prepared)

    def clear(self) -> None:
        """Reset the conversation and delete the engine session (on the executor).

        Allowed while busy, unlike ``retry``/``edit``: a run in flight is abandoned and a
        pending approval is dropped. Raises ``ControllerClosedError`` after ``close()``, the
        same as ``send``.
        """
        with self._lock:
            if self._closed:
                raise ControllerClosedError("ChatController is closed")
            stale = self._forget_conversation()
            self._set_top("alwaysAllowedTools", [])
            # Cannot raise: the closed check is above and the run was just abandoned.
            run_id, _token = self._start_run()
            # Busy until the DELETE returns, so a quick send cannot race onto the old session.
            self._set_top("isLoading", True)
        if stale is not None:
            stale.cancel()

        def job() -> None:
            try:
                self._t2v.clear_session()
            except Exception:
                # Logged, not surfaced: nothing the end-user did went wrong, and the
                # conversation is gone from this client either way.
                logger.exception("clear_session failed")
            finally:
                with self._lock:
                    if self._is_current(run_id):
                        self._set_top("isLoading", False)
                    self._end_run(run_id)
                self._flush()

        self._launch(run_id, job)

    # ── Identity ────────────────────────────────────────────────────────────

    def _on_auth_state_change(self, _user: Optional[User]) -> None:
        """Follow ``t2v.auth`` (subscribed at the end of ``__init__``); the rules are in the
        class docstring. ``_user`` is taken as a wake-up, not as the value to act on: auth
        listeners run on whichever thread changed the auth state — one listener may sign
        out from inside another's delivery — so two events can reach this out of order.
        The live identity is read instead, under the lock, as ``set_skills_state`` does.

        No lock of this controller is held on any path that calls this: the host's thread
        (``login()``, ``logout()``, ``signup()``), this controller's own worker (the
        anonymous start inside ``chat()``, or a rejected refresh during a turn), the
        warm-up thread, or a skills debounce timer (``T2VSkills`` holds a sign-out back
        until its post lock is released). From inside a delivery on the emitting thread,
        the flush below only asks for a re-flush, as any callback's does.
        """
        with self._lock:
            if self._closed:
                return
            stale = self._follow_identity()
        if stale is not None:
            stale.cancel()
        self._flush()

    def _follow_identity(self) -> Optional[CancelToken]:
        """Apply an identity change; returns the abandoned run's token, for the caller to
        cancel once the lock is released. The state belongs to one end-user: consent is
        reset whenever the user id changes, and the conversation is dropped too unless the
        same person is carrying on — a sign-in from signed out, or the anonymous demo
        becoming an account (convert, or the 409 -> sign-in fallback of ``signup()``)."""
        try:
            user_id, anonymous = self._read_identity()
        except Exception:
            # The token store could not be read: fail closed on consent, and leave the rest
            # for the next event that can be read.
            logger.exception("could not read the signed-in user; approval consent reset")
            self._set_top("alwaysAllowedTools", [])
            return None
        previous, was_anonymous = self._auth_user_id, self._auth_anonymous
        self._auth_user_id, self._auth_anonymous = user_id, anonymous
        self._set_top("isAnonymous", anonymous)
        if user_id == previous:
            return None  # a convert, a repeated sign-in: the same end-user
        # Consent is one end-user's: another identity never inherits an "always allow".
        self._set_top("alwaysAllowedTools", [])
        if previous is None or (was_anonymous and user_id is not None and not anonymous):
            # The same person carrying on. This is also the anonymous start that runs
            # inside this controller's own send: that turn continues as it is.
            return None
        # Signed out, or a different account: forget the conversation here. No engine
        # DELETE — the old token is gone, and Talk2View has already dropped the session.
        stale = self._forget_conversation()
        self._set_top("isLoading", False)
        return stale

    def _read_identity(self) -> Tuple[Optional[str], bool]:
        """The signed-in user's id (None when signed out) and whether that session is
        anonymous. Raises when the token store cannot be read."""
        auth = self._t2v.auth
        get_user = getattr(auth, "get_user", None)  # absent on the test fakes
        user = get_user() if callable(get_user) else None
        probe = getattr(auth, "is_anonymous", None)
        anonymous = bool(probe()) if callable(probe) else False
        return (user.id if user is not None else None), anonymous

    def _forget_conversation(self) -> Optional[CancelToken]:
        """Drop the conversation locally (lock held): abandon the run in flight — its
        token is returned, to be cancelled once the lock is released — and empty the
        messages, the replay history, the pending approval, ``error``, ``agentStatus``
        and ``threadId``. Approval consent and ``isLoading`` are the caller's."""
        stale = self._cancel_token
        self._active_run = None  # abandon: a worker still unwinding may no longer write
        self._cancel_token = None
        self._truncate([], 0)
        self._set_top("error", None)
        self._set_top("pendingApproval", None)
        self._set_top("agentStatus", None)
        self._set_top("threadId", None)
        self._last_user_text = None
        self._last_user_attachments = None
        self._turn_failed = False
        return stale

    # ── Plumbing (every helper below that touches state expects ``self._lock`` held) ──

    def _read_is_anonymous(self) -> bool:
        probe = getattr(self._t2v.auth, "is_anonymous", None)  # absent until anonymous auth ships
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _set_top(self, key: str, value: Any) -> None:
        set_top(self._rec, key, value)

    def _start_run(self) -> Tuple[int, CancelToken]:
        if self._closed:
            raise ControllerClosedError("ChatController is closed")
        if self._active_run is not None:
            # Internal-invariant guard: a caller starts a run only once it has made sure,
            # under this lock, that none is active. While every caller keeps to that, the
            # public API cannot reach this line; it stays so that a caller that ever breaks
            # the invariant gets a typed error instead of a second run.
            raise ControllerBusyError("A run is already in progress")
        self._run_counter += 1
        self._active_run = self._run_counter
        token = CancelToken()
        self._cancel_token = token
        self._idle.clear()
        return self._run_counter, token

    def _is_current(self, run_id: int) -> bool:
        return self._active_run == run_id

    def _end_run(self, run_id: int) -> None:
        if self._active_run == run_id:
            self._active_run = None
            self._cancel_token = None

    def _flush(self, *, capture: bool = False) -> Optional[ChatSnapshot]:
        """Close the current batch and deliver it. Never call with ``self._lock`` held — a
        caller that does raises ``RuntimeError`` rather than risking the deadlock.

        With ``capture`` the state is copied in the same locked section as the first cut and
        returned, which is how ``snapshot()`` lands on a batch boundary. ``None`` comes back
        only from the nested path below, which cuts nothing; ``snapshot()`` never takes it.

        The batch is cut (``seq`` assigned) and delivered while holding the emit lock, and the
        state lock is released before any subscriber runs, so delivery order equals ``seq``
        and subscribers may call ``snapshot()``. Lock order is ``_emit_lock`` then ``_lock``,
        never the reverse -- which is why the caller must not hold ``_lock`` here. A
        subscriber that calls back into the controller on the delivering thread must not
        deliver from inside the callback (later subscribers would see batch n+1 before n): its
        flush only sets ``_reflush`` and the loop below delivers the new ops next. A callback
        that records an op on *every* batch would keep that loop going for ever, so it gives
        up after ``_REFLUSH_LIMIT`` rounds and leaves the rest pending for the next flush.
        """
        if self._lock_is_owned is not None and self._lock_is_owned():
            raise RuntimeError(
                "_flush() must not be called while holding the state lock: the emit lock is "
                "taken below, and _lock before _emit_lock inverts the controller's lock order"
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
                            # Copied where the cut happened, so it carries that batch's `seq`
                            # and nothing recorded since. Only the FIRST cut: ops a callback
                            # records below belong to a later batch, which the caller of
                            # `snapshot()` receives through its subscription like anyone else.
                            captured = self._rec.snapshot()
                        subscribers = tuple(self._subscribers)
                        notices, self._notices = self._notices, []
                        callbacks = [cb for n in notices for cb in self._listeners.get(n, [])]
                    for callback in callbacks:
                        try:
                            callback()
                        except Exception:
                            logger.exception("listener raised")
                    if batch is not None:
                        for subscriber in subscribers:
                            self._deliver(subscriber, batch)
                    if not self._reflush:
                        break
                    rounds += 1
                    if rounds >= _REFLUSH_LIMIT:
                        # A callback records an op every time it is called, so there is always
                        # one more batch to cut. Give the emit lock back rather than spin on
                        # it; `_reflush` is left clear, so the ops recorded last are simply
                        # pending and the next flush from outside delivers them.
                        self._reflush = False
                        logger.error(
                            "subscriber feedback loop: a callback records an op on every "
                            "batch; stopped re-flushing after %d rounds",
                            _REFLUSH_LIMIT,
                        )
                        break
            finally:
                self._emit_owner = None
                with self._lock:
                    # Idle == no run AND every op has been handed to the subscribers. In the
                    # `finally` so that a host callback raising out of a delivery (a broken
                    # dispatcher) cannot leave the controller un-idle for good.
                    if self._active_run is None and not self._rec.pending:
                        self._idle.set()
        return captured

    def _deliver(self, subscriber: _Subscriber, batch: OpBatch) -> None:
        if subscriber.deliver_on_main:
            # Posted, never waited on (FIFO, so order survives): a host that stops pumping
            # stalls its own batches and nothing else.
            self._dispatcher.post(lambda: self._call(subscriber, batch))
        else:
            self._call(subscriber, batch)

    def _call(self, subscriber: _Subscriber, batch: OpBatch) -> None:
        if not subscriber.active:
            return
        try:
            subscriber.on_batch(batch)
        except Exception:
            logger.exception("subscriber raised; the run continues")

    def _require_idle(self) -> None:
        if self._closed:
            raise ControllerClosedError("ChatController is closed")
        if self._active_run is not None:
            raise ControllerBusyError("A run is already in progress")
        if self._rec.state["pendingApproval"] is not None:
            raise ControllerBusyError("A tool call is waiting for approval")

    def _launch(
        self, run_id: int, job: Callable[[], None], assistant_id: Optional[str] = None
    ) -> None:
        try:
            # Inside the try: the flush hands batches to the host's dispatcher, and a
            # dispatcher that raises must be recovered from exactly as a refusing executor is.
            self._flush()
            self._executor(job)
        except Exception as exc:
            # The executor refused the job (e.g. a pool that was shut down), or the host's
            # dispatcher raised while a batch was posted to it. Undo the run so the
            # controller is not left busy forever, then let the caller see the failure.
            with self._lock:
                if self._is_current(run_id):
                    self._turn_failed = True
                    self._set_top("error", str(exc))
                    if assistant_id is not None:
                        self._finalize(assistant_id, cancelled=False)
                    # For a job launched with an assistant segment `_finalize` has already
                    # cleared it; this is for a job launched without one, which set it itself.
                    self._set_top("isLoading", False)
                    self._end_run(run_id)
            self._deliver_undo()
            raise

    def _deliver_undo(self) -> None:
        """Deliver the batch that undid a run which never launched (no lock held)."""
        try:
            self._flush()
        except Exception:
            # The same broken dispatcher, most likely, failing again on the undo's batch.
            # Logged, never raised over the original: the caller is promised that one.
            logger.exception("could not deliver the batch that undid a failed launch")

    def _launch_prepared(self, prepared: Union[_Prepared, BaseException]) -> None:
        """``_launch`` what ``_prepare_send`` returned (no lock held). When that is the
        failure that ended its run instead, deliver that end and raise the failure."""
        if isinstance(prepared, BaseException):
            self._deliver_undo()
            raise prepared
        run_id, assistant_id, job = prepared
        self._launch(run_id, job, assistant_id)

    def _prepare_send(
        self, text: str, files: List[Attachment]
    ) -> Union[_Prepared, BaseException]:
        """Start a run and record the turn (lock held); ``_launch_prepared`` launches it.

        A failure once the run has started (an attachment-like object that is not an
        ``Attachment``, say) is not raised from here: that would leave the run active, and
        the controller busy, for good. It ends the run (``_end_unlaunched_run``) and is
        returned, for ``_launch_prepared`` to deliver that end and raise it once the lock
        is released."""
        run_id, token = self._start_run()
        history_length = len(self._history)
        assistant_id: Optional[str] = None
        try:
            self._set_top("isAnonymous", self._read_is_anonymous())
            user: DisplayMessage = {
                "id": self._reducer.next_message_id(),
                "role": "user",
                "content": text,
                "timestamp": now_iso(),
            }
            if files:
                user["attachments"] = [_attachment_dict(a) for a in files]
            self._history_marks[user["id"]] = len(self._history)
            self._rec.append_message(user)
            reply_id = assistant_id = self._reducer.append_assistant()
            self._last_user_text = text
            self._last_user_attachments = files or None
            self._turn_failed = False
            self._set_top("isLoading", True)
            self._set_top("error", None)
            # Prior turns only: T2VSession.send_message appends the current message itself.
            history = list(self._history)
            self._history.append(
                ChatMessage(role="user", content=build_user_content(text, files or None))
            )
        except BaseException as exc:
            self._end_unlaunched_run(run_id, assistant_id, history_length, exc)
            return exc

        def open_stream() -> Iterator[ChatEvent]:
            return self._open_chat_stream(text, files, history, token)

        def job() -> None:
            self._run(run_id, token, open_stream, reply_id)

        return run_id, reply_id, job

    def _end_unlaunched_run(
        self, run_id: int, assistant_id: Optional[str], history_length: int, exc: BaseException
    ) -> None:
        """End a run that failed before it was launched (lock held), as a failed launch
        ends one: ``error`` set, the empty reply segment settled, ``isLoading`` cleared.
        The turn never reached the engine, so the LLM history is cut back to its length
        before the turn; a rollback could take an earlier turn's entry instead."""
        del self._history[history_length:]
        self._turn_failed = True
        self._set_top("error", str(exc))
        if assistant_id is not None:
            self._reducer.finalize(assistant_id)
        self._set_top("isLoading", False)
        self._end_run(run_id)

    # The only two places that call into Talk2View (this one and _open_approval_stream).
    def _open_chat_stream(
        self, text: str, files: List[Attachment], history: List[ChatMessage], token: CancelToken
    ) -> Iterator[ChatEvent]:
        return self._t2v.chat(
            text,
            system_prompt=self._system_prompt,
            history=history,
            attachments=files or None,
            cancel=token,
        )

    def _open_approval_stream(
        self, approval: PendingApproval, decision: Decision, token: CancelToken
    ) -> Iterator[ChatEvent]:
        # respond_to_approval takes the `approval_required` ChatEvent itself.
        request = ChatEvent(
            type="approval_required",
            kind=approval.get("kind", "client"),
            tool_name=approval["toolName"],
            tool_call_id=approval["toolCallId"],
            arguments=approval["arguments"],
            description=approval["description"],
            details=approval.get("details"),
        )
        return self._t2v.respond_to_approval(request, decision, cancel=token)

    def _last_assistant_id(self) -> Optional[str]:
        """The newest assistant segment — the one a paused turn left streaming, which the
        approval's resume continues into. ``None`` only when there is none to continue."""
        for message in reversed(self._rec.state["messages"]):
            if message["role"] == "assistant":
                return message["id"]
        return None

    # ── The run (worker thread) ─────────────────────────────────────────────

    def _run(
        self, run_id: int, token: CancelToken, open_stream: StreamOpener, assistant_id: str
    ) -> None:
        try:
            self._drain(run_id, token, open_stream, assistant_id)
        except Exception as exc:  # a bug here must not leave the controller busy forever
            logger.exception("chat run failed")
            with self._lock:
                if self._is_current(run_id):
                    self._turn_failed = True
                    self._set_top("error", str(exc))
                    self._set_top("isLoading", False)
                    # `_drain` calls `_finalize` right before `_end_run`; reaching here means
                    # that call did not complete, so roll the orphan user turn back ourselves.
                    # A no-op if `_finalize` already appended assistant text before it broke.
                    self._rollback_orphan_turn()
        finally:
            with self._lock:
                self._end_run(run_id)
            self._flush()

    def _drain(
        self, run_id: int, token: CancelToken, open_stream: StreamOpener, assistant_id: str
    ) -> None:
        """Consume streams, auto-approving always-allowed tools, until the turn completes."""
        current_open = open_stream
        current_id = assistant_id
        while True:
            consumed = self._consume(run_id, token, current_open, current_id)
            current_id = consumed.assistant_id
            if not consumed.paused:
                break
            auto = consumed.auto_approval
            if auto is None:
                # Manual approval: stop WITHOUT finalizing. The message stays isStreaming so
                # the approval card renders; no thread waits; approve() starts a new run.
                # `_consume` already ended the run, in the locked section that set
                # `pendingApproval`; all that is left is to deliver that batch.
                self._flush()
                return
            with self._lock:
                if self._is_current(run_id):
                    self._set_top(
                        "agentStatus",
                        {
                            "type": "auto-approved",
                            "message": "Auto-approved {}".format(auto["toolName"]),
                        },
                    )
            self._flush()
            current_open = self._auto_approval_opener(auto, token)
        with self._lock:
            if self._is_current(run_id):
                # `token.cancelled` alone would blame a cancel that only *happened* to land in
                # the same instant the stream finished on its own -- e.g. inside the natural
                # end (cancel() called from a script item right after `done`). A turn that
                # already read `done` is complete; only an EARLIER cancel (one the turn never
                # got past) should count as having interrupted it.
                self._finalize(current_id, cancelled=token.cancelled and not consumed.completed)
                self._end_run(run_id)
        self._flush()

    def _auto_approval_opener(self, approval: PendingApproval, token: CancelToken) -> StreamOpener:
        def open_stream() -> Iterator[ChatEvent]:
            return self._open_approval_stream(approval, Decision(action="once"), token)

        return open_stream

    def _consume(
        self, run_id: int, token: CancelToken, open_stream: StreamOpener, assistant_id: str
    ) -> _Consumed:
        cursor = _Cursor(assistant_id=assistant_id)
        stream: Optional[Iterator[ChatEvent]] = None
        try:
            stream = open_stream()
            for event in stream:
                with self._lock:
                    if not self._is_current(run_id):  # clear()/close() abandoned this run
                        return _Consumed(False, None, cursor.assistant_id, cursor.completed)
                    outcome = self._reduce_locked(event, cursor)
                    if outcome is not None and outcome.paused and outcome.auto_approval is None:
                        # Paused for the host. INVARIANT: `pendingApproval` is never set on
                        # an active run -- the run ends here, in the locked section that set
                        # it, so no flusher on any thread (a `snapshot()`, a `clear_error()`)
                        # can cut a batch showing the card while `_active_run` is still
                        # this run. A host answering from that batch's callback therefore
                        # never meets `ControllerBusyError`.
                        self._set_top("isLoading", False)
                        self._end_run(run_id)
                if outcome is not None:
                    # Paused: leave these ops unflushed so `_drain` delivers them together
                    # with the pause's own (`isLoading: False`, or the auto-approved status).
                    return outcome
                self._flush()
        except Cancelled:
            pass  # cancel(): keep the partial text, surface no error
        except Exception as exc:
            if not token.cancelled:  # closing the response mid-read raises transport errors
                with self._lock:
                    if self._is_current(run_id):
                        self._turn_failed = True
                        self._set_top("error", str(exc))
                self._flush()
        finally:
            _close_stream(stream)
        return _Consumed(False, None, cursor.assistant_id, cursor.completed)

    def _reduce_locked(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        """Apply one event and fold the cursor's non-state outputs back into the shell.

        The only call site of ``TurnReducer.reduce``, because the two halves belong together:
        a handler that appends a notice and then raises, or a second ``reduce`` call site that
        forgot the absorb, would drop that notice on the floor. Hence the ``finally``. The
        caller holds ``self._lock``, as every state-touching helper here does.
        """
        try:
            return self._reducer.reduce(event, cursor)
        finally:
            self._absorb(cursor)

    def _absorb(self, cursor: _Cursor) -> None:
        """Fold the reducer's two non-state outputs into the shell, under the lock: a failed
        turn is sticky for the rest of the run, and a notice waits for the next flush."""
        if cursor.turn_failed:
            self._turn_failed = True
        if cursor.notices:
            self._notices.extend(cursor.notices)
            cursor.notices.clear()

    def _finalize(self, assistant_id: str, *, cancelled: bool) -> None:
        """Close the turn: the reducer settles the messages (``isStreaming`` off, empty
        trailing segment dropped, ``agentStatus``/``isLoading`` cleared) and hands back the
        turn's assistant text; the history half — one entry per turn, or a rollback — is the
        shell's, because ``_history`` is."""
        parts = self._reducer.finalize(assistant_id)
        if parts:
            self._history.append(ChatMessage(role="assistant", content="\n\n".join(parts)))
        elif self._turn_failed or cancelled:
            # No assistant text at all: roll the user turn back so the next send does not
            # replay [..., user, user].
            self._rollback_orphan_turn()

    def _rollback_orphan_turn(self) -> None:
        """Undo the pending user entry in ``_history``: this turn produced no assistant text,
        so leaving it in place would replay ``[..., user, user]`` on the next send. Idempotent
        -- a no-op once the trailing entry is already an assistant reply (or ``_history`` is
        empty), so every error path can call it unconditionally."""
        if self._history and self._history[-1].role == "user":
            self._history.pop()

    # ── History operations ──────────────────────────────────────────────────

    def _truncate(self, messages: List[DisplayMessage], mark: Optional[int]) -> None:
        """Shrink the display list (one whole-list set) and cut history back to ``mark``.

        Display messages and LLM history are not parallel, which is why the caller passes a
        mark rather than a message count: a turn that produced no assistant text is rolled
        out of ``_history`` but stays on screen, and a successful tools-only or plan-only turn
        leaves its user entry in ``_history`` with no assistant counterpart. A mark is the
        length ``_history`` had just before its turn was recorded, so slicing at it drops that
        turn and every later one whatever shape they took — including the "one past the end"
        mark a rolled-back turn leaves behind, where the slice is simply a no-op.
        """
        self._rec.replace_messages(messages)
        if mark is not None:
            del self._history[mark:]
        kept = {m["id"] for m in messages}
        self._history_marks = {k: v for k, v in self._history_marks.items() if k in kept}
