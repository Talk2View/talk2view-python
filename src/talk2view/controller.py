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

import copy
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
from .metrics import TurnMetrics, _TurnRecorder, bind_turn, current_turn
from .sessions import build_user_content
from .state import (
    VALID_HISTORY_ROLES,
    AttachmentDict,
    ChatSnapshot,
    ConversationSnapshot,
    DisplayMessage,
    HistoryEntry,
    OpBatch,
    OpRecorder,
    PendingApproval,
    SkillsState,
    counter_suffix,
    initial_snapshot,
    is_hostile_counter_digits,
    valid_attachment_dict,
    valid_tool_step_dict,
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
    "IdentityChange",
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
#: ``(run id, assistant segment id, job, turn timing)``: a turn ``_prepare_send`` started,
#: to launch.
_Prepared = Tuple[int, str, Callable[[], None], _TurnRecorder]
#: What ``_drain`` reports: the turn's outcome, or that it waits for an approval.
_PAUSED = "paused"


class ControllerBusyError(RuntimeError):
    """A run is in progress, or a tool call is waiting for the end-user's decision."""


class ControllerClosedError(RuntimeError):
    """The controller was closed."""


@dataclass(frozen=True)
class IdentityChange:
    """One change of the signed-in user id, as ``follow_identity`` reports it.

    ``epoch`` counts the changes this controller has applied (``follow_identity``'s
    baseline is the count so far). ``forgotten`` is the conversation this change dropped
    from the screen, exported in the same locked section that dropped it -- so nothing
    that reached the conversation before the drop is missing from it -- or None when the
    conversation was kept (a sign-in from signed out, a guest becoming an account)."""

    epoch: int
    user_id: Optional[str]
    anonymous: bool
    forgotten: Optional[ConversationSnapshot] = None


@dataclass(frozen=True)
class _Restore:
    """A snapshot ``_prepare_restore`` validated and copied, ready to apply under the lock."""

    messages: List[DisplayMessage]
    history: List[ChatMessage]
    marks: Dict[str, int]
    thread_id: Optional[str]
    last_user_text: Optional[str]
    last_user_attachments: Optional[List[Attachment]]


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


def _history_entry(message: ChatMessage) -> HistoryEntry:
    entry: HistoryEntry = {"role": message.role, "content": copy.deepcopy(message.content)}
    if message.tool_call_id is not None:
        entry["tool_call_id"] = message.tool_call_id
    return entry


def _chat_message(entry: HistoryEntry) -> ChatMessage:
    # A stored or hand-built entry's tool_call_id is untyped
    # JSON -- kept only when it is actually a string, the same guard every other
    # optional field on a restored entry gets.
    tool_call_id = entry.get("tool_call_id")
    return ChatMessage(
        role=entry["role"], content=copy.deepcopy(entry["content"]),
        tool_call_id=tool_call_id if isinstance(tool_call_id, str) else None,
    )


def _entry_text(content: Any) -> str:
    """The text a user display message's ``content`` would equal for this history entry:
    the string itself, or the ``text`` part of a structured (attachments) payload
    (``build_user_content``), or ``""`` when there is none -- matching what
    ``build_user_content`` omits for empty text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                return text if isinstance(text, str) else ""
    return ""


def _marks_for(messages: List[DisplayMessage], history: List[ChatMessage]) -> Dict[str, int]:
    """``_history_marks`` for a restored transcript that carries none of its own (a
    TS-written or older snapshot; see ``restore_conversation``), reproducing what a live
    turn would have marked (``_prepare_send``'s
    ``self._history_marks[user["id"]] = len(self._history)``, taken before that turn's own
    entry is appended).

    Display messages and history entries are not parallel: a rolled-back turn
    (``_rollback_orphan_turn``) stays on screen with no history entry of its own, so pairing
    the i-th user message with the i-th user entry drifts by one for every such turn before
    it, misplacing every later mark. This pairs them by content instead: for each user
    display message, the next user history entry (from ``cursor``) with the same text is
    its match -- mark = that entry's index, and the cursor moves past it.

    Content alone is not enough when a rolled-back turn's text repeats a later, successful
    one's (four "hi"s, the second failing, say): a match is only trusted -- "matched and
    consumed" -- for a user message directly followed by an assistant display message,
    which every live, paused or mid-turn user entry has (``_prepare_send`` appends the
    assistant message in the same call) and a rolled-back one never does (its would-be
    assistant reply never arrived). A user message with nothing to match, or not followed
    by an assistant message, marks the next user entry at or after ``cursor`` *without*
    consuming it -- exactly the length history had when that turn was appended, since a
    rollback pops its entry back out to that point -- or the end of history if none is
    left, so a retry or edit of it cuts nothing earlier. (One residual blind spot: a
    completed turn with no text, no steps and no plan at all keeps its history entry but
    drops its empty assistant message, so it reads as "not followed by an assistant" too --
    rare enough, and safe enough a fallback, that it is left as-is.)
    """
    marks: Dict[str, int] = {}
    cursor = 0
    for position, message in enumerate(messages):
        if message["role"] != "user":
            continue
        followed_by_assistant = (
            position + 1 < len(messages) and messages[position + 1]["role"] == "assistant"
        )
        index = None
        if followed_by_assistant:
            text = message["content"]
            index = next(
                (
                    i
                    for i in range(cursor, len(history))
                    if history[i].role == "user" and _entry_text(history[i].content) == text
                ),
                None,
            )
        if index is not None:
            marks[message["id"]] = index
            cursor = index + 1
            continue
        fallback = next((i for i in range(cursor, len(history)) if history[i].role == "user"), None)
        marks[message["id"]] = fallback if fallback is not None else len(history)
    return marks


def _valid_stored_marks(
    messages: List[DisplayMessage], history_length: int, marks: Any
) -> bool:
    """Whether a snapshot's carried ``marks`` may be trusted as-is instead of recomputing
    them with ``_marks_for``. Valid means: a dict with exactly one integer entry per user
    display message (the same set of ids, no more, no fewer -- "the right count"), each
    within ``0..history_length``, and non-decreasing in the order those messages appear on
    screen -- the invariant a live turn's own bookkeeping always produces
    (``_prepare_send``'s mark can repeat across a rolled-back turn but never goes
    backwards). TS ignores this key entirely, so a TS-written or pre-this-change snapshot
    simply has none and always falls back to ``_marks_for``."""
    if not isinstance(marks, dict):
        return False
    user_ids = [m["id"] for m in messages if m["role"] == "user"]
    if set(marks.keys()) != set(user_ids):
        return False
    values: List[int] = []
    for user_id in user_ids:
        value = marks[user_id]
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if not (0 <= value <= history_length):
            return False
        values.append(value)
    return all(values[i] <= values[i + 1] for i in range(len(values) - 1))


_VALID_DISPLAY_ROLES = ("user", "assistant")


def _valid_restore_snapshot(messages: List[DisplayMessage], history: List[HistoryEntry]) -> bool:
    """Whether ``restore_conversation`` may safely mutate on this snapshot, checked whole
    before anything is touched: every display message has a role/content the wire format
    allows, an id that is not one of the two hostile counter shapes (a Unicode digit run,
    or one too long for ``int()`` -- either marks the whole snapshot invalid rather than
    being silently dropped, a snapshot this malformed is corrupt, not partially
    trustworthy; an id that is merely not a counter id at all, such as a host-assigned
    ``"msg_welcome"``, is opaque and always accepted), only well-shaped attachments, a
    ``plan`` that is a string when present, and only well-shaped ``steps``
    (``valid_tool_step_dict`` -- the page's ``convert.ts`` reads every step's ``status``
    and ``name`` unguarded, so one bad one blanks the whole chat, not just this message,
    since the newest stored conversation is restored at every start); every history entry
    has a role the client ever replays (``VALID_HISTORY_ROLES`` -- never ``tool``, so
    stored data can never inject a tool result the engine would trust on replay) and text
    or structured content.

    TS has no equivalent case: it never parses an id or constructs an attachment object, so
    nothing in a snapshot can raise there in the first place."""
    ids: List[str] = []
    for message in messages:
        if message.get("role") not in _VALID_DISPLAY_ROLES:
            return False
        if not isinstance(message.get("content"), str):
            return False
        message_id = message.get("id")
        if not isinstance(message_id, str):
            return False
        digits = counter_suffix(message_id)
        if digits is not None and is_hostile_counter_digits(digits):
            return False
        attachments = message.get("attachments")
        if attachments is not None:
            if not isinstance(attachments, list) or not all(
                valid_attachment_dict(a) for a in attachments
            ):
                return False
        plan = message.get("plan")
        if plan is not None and not isinstance(plan, str):
            return False
        steps = message.get("steps")
        if steps is not None:
            if not isinstance(steps, list) or not all(valid_tool_step_dict(s) for s in steps):
                return False
        ids.append(message_id)
    if len(ids) != len(set(ids)):
        # Live never mints a duplicate; a message id backs both `marks` (one entry could
        # not mean two things) and edit()/retry()'s id lookup (which would find whichever
        # duplicate comes first).
        return False
    for entry in history:
        if not isinstance(entry, dict) or entry.get("role") not in VALID_HISTORY_ROLES:
            return False
        if not isinstance(entry.get("content"), (str, list)):
            return False
    return True


def _prepare_restore(snapshot: ConversationSnapshot) -> Optional[_Restore]:
    """Copy and validate ``snapshot`` for ``restore_conversation``, outside the lock; None
    when it fails ``_valid_restore_snapshot``."""
    messages = [cast(DisplayMessage, copy.deepcopy(dict(m))) for m in snapshot["messages"]]
    for message in messages:
        message.pop("isStreaming", None)
    if not _valid_restore_snapshot(messages, snapshot["history"]):
        return None
    history = [_chat_message(entry) for entry in snapshot["history"]]
    stored_marks = snapshot.get("marks")
    marks = (
        dict(stored_marks)
        if stored_marks is not None and _valid_stored_marks(messages, len(history), stored_marks)
        else _marks_for(messages, history)
    )
    last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
    return _Restore(
        messages=messages,
        history=history,
        marks=marks,
        thread_id=snapshot["threadId"],
        last_user_text=last_user["content"] if last_user is not None else None,
        last_user_attachments=(
            [Attachment(**a) for a in last_user.get("attachments", [])] or None
            if last_user is not None
            else None
        ),
    )


def _error_type(exc: BaseException) -> str:
    """An exception's type for a turn timing: a ``T2VError``'s ``type``, else its class
    name. Never its message."""
    kind = getattr(exc, "type", None)
    return kind if isinstance(kind, str) and kind else type(exc).__name__


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
    turn replays it into the account's session -- unless a conversation list said that
    conversation may not follow into an account (``exchange_conversation(carry=False)``:
    one it read back from storage may be an earlier visitor's). Nobody becoming a guest
    always keeps it: that is the person at the keyboard, often the guest start inside
    this controller's own send, which is never abandoned for it. ``follow_identity``
    reports each change, with the conversation it dropped.

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
        # message ids and event handling live here; `allows_always` guards its own
        # auto-consume of `alwaysAllowedTools` the same way `approve()` guards writing to
        # it (a tool like `save_skill` never gets a silent "always").
        self._reducer = TurnReducer(self._rec, allows_always=self._allows_always)
        self._subscribers: List[_Subscriber] = []
        self._listeners: Dict[str, List[Callable[[], None]]] = {}
        self._notices: List[str] = []
        # `follow_identity`: who hears of a change of user id; the changes recorded (under
        # the lock, where they happen) and not yet delivered (`_flush` delivers them, in
        # order, ahead of the batch that carries their ops); how many there have been; and
        # whether the conversation on screen may follow the end-user across a sign-in
        # (`exchange_conversation(carry=...)`; True unless a conversation list says not).
        self._identity_followers: List[Callable[[IdentityChange], None]] = []
        self._identity_changes: List[IdentityChange] = []
        self._identity_epoch = 0
        self._carry_on_sign_in = True

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
        # Turn timings go to `t2v.metrics` (a private hub on the test fakes). `_turn` is the
        # turn in progress, kept across an approval pause; `_dropped_turns` are turns
        # `_forget_conversation` dropped, ended (as abandoned) once the lock is released.
        metrics = getattr(t2v, "metrics", None)
        self._metrics = metrics if isinstance(metrics, TurnMetrics) else TurnMetrics()
        self._turn: Optional[_TurnRecorder] = None
        self._dropped_turns: List[_TurnRecorder] = []
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
    def metrics(self) -> TurnMetrics:
        """Where this controller's turn timings go: ``t2v.metrics`` (``talk2view.metrics``).
        ``metrics.on_turn(callback)`` hears of every turn as it ends."""
        return self._metrics

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
        """Listen for a one-shot notice that is not chat state. Events: ``demoLimitReached``;
        ``conversationReplaced`` -- a conversation was just put on screen, replacing
        whatever was there, whichever of ``restore_conversation`` or
        ``exchange_conversation`` did it (a host that only overrides
        ``restore_conversation`` misses every switch a list of conversations makes with
        ``exchange_conversation``). Not raised for an export-only ``exchange_conversation``
        (``snapshot=None``) or one refused for a stale ``epoch`` -- nothing was replaced
        either way.

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

    def follow_identity(
        self, callback: Callable[[IdentityChange], None]
    ) -> Tuple[IdentityChange, Callable[[], None]]:
        """Hear of every change of the signed-in user id this controller applies, with the
        conversation it dropped (``IdentityChange.forgotten``) when it dropped one. For a
        list of conversations kept per end-user (``talk2view.ui.ThreadsState``): the
        outgoing user's conversation arrives exactly as it stood when it left the screen,
        so it never has to be read back after the fact.

        Returns the identity now (``forgotten`` None) and the function that stops the
        callback. Every later change reaches ``callback`` once, in ``epoch`` order, on the
        emitting thread ahead of the batch carrying its ops -- so ``subscribe()``'s rules
        for a callback apply. A change at or below the returned ``epoch`` may still arrive
        (it was recorded before this call and not yet delivered); ignore it. Raises
        ``ControllerClosedError`` once closed."""
        with self._lock:
            if self._closed:
                raise ControllerClosedError("ChatController is closed")
            self._identity_followers.append(callback)
            now = IdentityChange(self._identity_epoch, self._auth_user_id, self._auth_anonymous)

        def off() -> None:
            with self._lock:
                if callback in self._identity_followers:
                    self._identity_followers.remove(callback)

        return now, off

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
            # Changes recorded and not yet delivered still reach their followers (below):
            # a list waiting on one must not wait for ever, nor lose its conversation.
            undelivered, self._identity_changes = self._identity_changes, []
            followers = tuple(self._identity_followers)
            self._identity_followers = []
            if self._turn is not None:
                self._dropped_turns.append(self._turn)
                self._turn = None
            self._idle.set()
        self._end_dropped_turns()
        if skills_off is not None:
            skills_off()
        if auth_off is not None:
            auth_off()
        if token is not None:
            token.cancel()
        if undelivered and followers:
            self._deliver_identity_changes(undelivered, followers)

    def _deliver_identity_changes(
        self,
        changes: List[IdentityChange],
        followers: Tuple[Callable[[IdentityChange], None], ...],
    ) -> None:
        """For ``close()``: in the emit window, so in order with any delivery under way
        (inside it already when ``close()`` is called from a callback)."""
        if self._emit_owner == threading.get_ident():
            self._call_followers(changes, followers)
            return
        with self._emit_lock:
            self._call_followers(changes, followers)

    @staticmethod
    def _call_followers(
        changes: List[IdentityChange],
        followers: Tuple[Callable[[IdentityChange], None], ...],
    ) -> None:
        for change in changes:
            for follower in followers:
                try:
                    follower(change)
                except Exception:
                    logger.exception("identity follower raised")

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
            # A tool that disallows "always" at all (``save_skill``) is handled
            # exactly like "once" here: the decision still approves THIS call (unchanged
            # below/in respond_to_approval), it is simply never written to memory, so the
            # next call gets a fresh card.
            if (
                decision.action == "always"
                and decision.approved
                and approval.get("kind", "client") == "client"
                and self._allows_always(approval["toolName"])
            ):
                allowed = list(self._rec.state["alwaysAllowedTools"])
                if approval["toolName"] not in allowed:
                    allowed.append(approval["toolName"])
                    self._rec.set(["alwaysAllowedTools"], allowed)
            self._set_top("pendingApproval", None)
            self._set_top("isLoading", True)
            assistant_id: str = self._last_assistant_id() or self._reducer.append_assistant()
            turn = self._turn  # the turn that paused for this approval

        def open_stream() -> Iterator[ChatEvent]:
            return self._open_approval_stream(approval, decision, token)

        def job() -> None:
            self._run(run_id, token, open_stream, assistant_id, turn)

        self._launch_turn(run_id, job, assistant_id, turn)

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
            self._carry_on_sign_in = True  # a conversation started here from now on
            # Cannot raise: the closed check is above and the run was just abandoned.
            run_id, _token = self._start_run()
            # Busy until the DELETE returns, so a quick send cannot race onto the old session.
            self._set_top("isLoading", True)
        if stale is not None:
            stale.cancel()
        self._end_dropped_turns()

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

    def export_conversation(self) -> ConversationSnapshot:
        """Everything needed to put this conversation back later (TS ``exportConversation``):
        deep copies of the messages (``isStreaming`` dropped: a stored copy must never
        show a cursor nothing writes to), the replay history, ``threadId``, and ``marks``
        (a Python extension: one entry per user message, the index into ``history`` a live
        turn marked it at -- ``restore_conversation`` prefers these over reconstructing them,
        since content alone cannot always tell two identical user messages apart; TS
        ignores the key). Take one before switching away; see ``restore_conversation``, and
        ``exchange_conversation`` for the two in one step."""
        with self._lock:
            return self._export_locked()

    def restore_conversation(self, snapshot: ConversationSnapshot) -> None:
        """Put a conversation on screen, replacing whatever is there (TS
        ``restoreConversation``). The engine is not consulted: the transcript travels with
        the next message. Everything that belonged to the conversation being replaced goes
        with it — the run in flight (abandoned), the error, a pending approval, the agent
        status, and the tools the end-user allowed for the rest of the session ("always
        allow" is a grant to a session, not a property of a conversation, so a restored one
        asks again). The engine session is detached, not deleted. Allowed while busy, as
        ``clear`` is; raises ``ControllerClosedError`` after ``close()``.

        ``snapshot`` is validated whole, before anything is touched: a bad message id, an
        attachment shaped wrong, or a role the wire never carries would otherwise raise
        from inside the locked mutation, half-applied (``ConversationStore.read`` already
        filters what it hands back, but a hand-built or future-caller snapshot may not).
        Failed validation is a no-op, logged -- the conversation on screen is unchanged, and
        nothing here ever hands stored data a role or a tool result the engine would trust
        on replay (``_valid_restore_snapshot``).

        ``snapshot``'s optional ``marks`` are used as-is when present and well-formed
        (``_valid_stored_marks``); a snapshot with none, or with marks that fail that check,
        falls back to reconstructing them (``_marks_for``) -- never a reason to refuse the
        restore itself, only to recompute one field of it."""
        prepared = _prepare_restore(snapshot)
        if prepared is None:
            logger.warning(
                "restore_conversation: the snapshot failed validation (a bad message id, "
                "attachment or role); the conversation on screen is unchanged"
            )
            return
        with self._lock:
            if self._closed:
                raise ControllerClosedError("ChatController is closed")
            stale = self._apply_restore_locked(prepared)
            self._carry_on_sign_in = True
        self._after_restore(stale)

    def exchange_conversation(
        self,
        snapshot: Optional[ConversationSnapshot],
        *,
        epoch: int,
        carry: Optional[bool] = None,
    ) -> Optional[ConversationSnapshot]:
        """``export_conversation`` and ``restore_conversation(snapshot)`` in one locked
        step, so nothing can reach the outgoing conversation between the two -- for a list
        of conversations, which must never lose the tail of the one it switches away from.
        ``snapshot`` None exports only.

        ``epoch`` is the identity ``follow_identity`` last reported to the caller: if the
        signed-in user has changed since, nothing is done and None comes back (the change,
        with the conversation it dropped, is on its way to the caller's callback). ``carry``
        says whether the conversation on screen afterwards may follow the end-user across a
        sign-in out of a guest session or out of none into an account (None leaves it as it
        is); False makes such a sign-in drop it, and report it in
        ``IdentityChange.forgotten``, as a sign-out would. Nobody becoming a guest always
        carries it: that is the person at the keyboard, and often this controller's own
        ``send`` starting the guest session, which is never abandoned for it.

        Raises ``ValueError`` when ``snapshot`` fails ``restore_conversation``'s validation,
        and ``ControllerClosedError`` for a restore once closed, both before anything
        changes. An export alone works on a closed controller: what was on screen when it
        closed."""
        prepared = None
        if snapshot is not None:
            prepared = _prepare_restore(snapshot)
            if prepared is None:
                raise ValueError("exchange_conversation: the snapshot failed validation")
        with self._lock:
            if prepared is not None and self._closed:
                raise ControllerClosedError("ChatController is closed")
            if epoch != self._identity_epoch:
                return None
            exported = self._export_locked()
            if carry is not None:
                self._carry_on_sign_in = carry
            stale = self._apply_restore_locked(prepared) if prepared is not None else None
        if prepared is not None:
            self._after_restore(stale)
        return exported

    def _export_locked(self) -> ConversationSnapshot:
        messages: List[DisplayMessage] = []
        for message in self._rec.state["messages"]:
            copied = cast(DisplayMessage, copy.deepcopy(dict(message)))
            copied.pop("isStreaming", None)
            messages.append(copied)
        history = [_history_entry(entry) for entry in self._history]
        thread_id = self._rec.state["threadId"]
        marks = dict(self._history_marks)
        return {
            "messages": messages,
            "history": history,
            "threadId": thread_id,
            "marks": marks,
        }

    def _apply_restore_locked(self, prepared: _Restore) -> Optional[CancelToken]:
        """Replace the conversation with ``prepared`` (lock held); returns the abandoned
        run's token, for ``_after_restore``."""
        stale = self._forget_conversation()
        self._set_top("alwaysAllowedTools", [])
        self._set_top("isLoading", False)
        self._rec.replace_messages(prepared.messages)
        self._history = prepared.history
        self._history_marks = prepared.marks
        self._reducer.bump_message_counter(m["id"] for m in prepared.messages)
        self._set_top("threadId", prepared.thread_id)
        self._last_user_text = prepared.last_user_text
        self._last_user_attachments = prepared.last_user_attachments
        # Queued here, under the same lock as the replacement itself, so it is delivered
        # through the flush both callers make right after (_after_restore) -- the one
        # call site both restore_conversation and exchange_conversation share for "a
        # conversation was actually put on screen".
        self._notices.append("conversationReplaced")
        return stale

    def _after_restore(self, stale: Optional[CancelToken]) -> None:
        if stale is not None:
            stale.cancel()
        self._end_dropped_turns()
        detach = getattr(self._t2v, "detach_session", None)  # absent on older test fakes
        if callable(detach):
            detach()  # field writes only: no request, no listener
        self._flush()

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
        self._end_dropped_turns()
        self._flush()

    def _follow_identity(self) -> Optional[CancelToken]:
        """Apply an identity change; returns the abandoned run's token, for the caller to
        cancel once the lock is released. The state belongs to one end-user: consent is
        reset whenever the user id changes, and the conversation is dropped too unless the
        same person is carrying on — a sign-in from signed out, or the anonymous demo
        becoming an account (convert, or the 409 -> sign-in fallback of ``signup()``) —
        and the conversation on screen may carry on with them (``_carry_on_sign_in``, which
        only a conversation list turns off). Each change of user id is recorded for
        ``follow_identity``, with the dropped conversation exported first, right here."""
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
        self._identity_epoch += 1
        # Consent is one end-user's: another identity never inherits an "always allow".
        self._set_top("alwaysAllowedTools", [])
        carrying_on = previous is None or (was_anonymous and user_id is not None and not anonymous)
        # Nobody becoming a guest is the person at the keyboard, whatever is on screen:
        # the guest id is minted by this process, most often by the anonymous start inside
        # this controller's own send, and that send must never be abandoned by it. A
        # list's `carry=False` guards a sign-in into an account, not this.
        becoming_a_guest = previous is None and user_id is not None and anonymous
        if carrying_on and (self._carry_on_sign_in or becoming_a_guest):
            # The same person carrying on. This is also the anonymous start that runs
            # inside this controller's own send: that turn continues as it is.
            self._record_identity(user_id, anonymous, None)
            return None
        # Signed out, or a different account (or a conversation its list will not let
        # follow a sign-in): forget the conversation here. No engine DELETE — the old token
        # is gone, and Talk2View has already dropped the session.
        self._record_identity(
            user_id, anonymous, self._export_locked() if self._identity_followers else None
        )
        stale = self._forget_conversation()
        self._set_top("isLoading", False)
        self._carry_on_sign_in = True
        return stale

    def _record_identity(
        self, user_id: Optional[str], anonymous: bool, forgotten: Optional[ConversationSnapshot]
    ) -> None:
        if self._identity_followers:
            self._identity_changes.append(
                IdentityChange(self._identity_epoch, user_id, anonymous, forgotten)
            )

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
        if self._turn is not None:
            self._dropped_turns.append(self._turn)
            self._turn = None
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

    def _allows_always(self, tool_name: str) -> bool:
        """Whether an "always" decision for a CLIENT tool may be remembered at all
        (``T2VTools.allows_always`` -- ``save_skill`` says no: every save is the
        end-user's own click). Defaults to True — today's behaviour, unchanged — when
        ``t2v.tools`` has no such method (an older test double, or a host's own
        stand-in); a method that raises fails closed (this gates a security
        property, so a broken check must never grant the permanent "always", only cost
        an extra approval card)."""
        probe = getattr(self._t2v.tools, "allows_always", None)
        if not callable(probe):
            return True
        try:
            return bool(probe(tool_name))
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
                        changes, self._identity_changes = self._identity_changes, []
                        followers = tuple(self._identity_followers)
                    self._call_followers(changes, followers)
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
        run_id, assistant_id, job, turn = prepared
        self._launch_turn(run_id, job, assistant_id, turn)

    def _launch_turn(
        self,
        run_id: int,
        job: Callable[[], None],
        assistant_id: str,
        turn: Optional[_TurnRecorder],
    ) -> None:
        """``_launch`` a run of a timed turn (no lock held): a launch that fails ends the
        turn's timing as an error."""
        try:
            self._launch(run_id, job, assistant_id)
        except Exception as exc:
            if turn is not None:
                turn.error(_error_type(exc))
                turn.finish("error")
            raise

    def _end_dropped_turns(self) -> None:
        """End the timings of the turns ``_forget_conversation`` dropped (no lock held)."""
        with self._lock:
            dropped, self._dropped_turns = self._dropped_turns, []
        for turn in dropped:
            turn.finish("abandoned")

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
        turn = self._turn = self._metrics._start_turn(user["id"])

        def open_stream() -> Iterator[ChatEvent]:
            return self._open_chat_stream(text, files, history, token)

        def job() -> None:
            self._run(run_id, token, open_stream, reply_id, turn)

        return run_id, reply_id, job, turn

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
        self,
        run_id: int,
        token: CancelToken,
        open_stream: StreamOpener,
        assistant_id: str,
        turn: Optional[_TurnRecorder] = None,
    ) -> None:
        outcome = "error"
        with bind_turn(turn):  # the client, the session and the tools note its timing
            try:
                outcome = self._drain(run_id, token, open_stream, assistant_id)
            except Exception as exc:  # a bug here must not leave the controller busy forever
                logger.exception("chat run failed")
                if turn is not None:
                    turn.error(_error_type(exc))
                with self._lock:
                    if self._is_current(run_id):
                        self._turn_failed = True
                        self._set_top("error", str(exc))
                        self._set_top("isLoading", False)
                        # `_drain` calls `_finalize` right before `_end_run`; reaching here
                        # means that call did not complete, so roll the orphan user turn back
                        # ourselves. A no-op if `_finalize` already appended assistant text
                        # before it broke.
                        self._rollback_orphan_turn()
            finally:
                with self._lock:
                    self._end_run(run_id)
                self._flush()
        # After the last batch went out, with no lock held: listeners never delay the UI.
        if turn is not None and outcome != _PAUSED:
            with self._lock:
                if self._turn is turn:
                    self._turn = None
            turn.finish(outcome)

    def _drain(
        self, run_id: int, token: CancelToken, open_stream: StreamOpener, assistant_id: str
    ) -> str:
        """Consume streams, auto-approving always-allowed tools, until the turn completes.
        Returns the turn's outcome for its timing (``done``, ``error``, ``cancelled``,
        ``abandoned``), or ``_PAUSED`` when it waits for an approval."""
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
                return _PAUSED
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
        outcome = "abandoned"
        with self._lock:
            if self._is_current(run_id):
                # `token.cancelled` alone would blame a cancel that only *happened* to land in
                # the same instant the stream finished on its own -- e.g. inside the natural
                # end (cancel() called from a script item right after `done`). A turn that
                # already read `done` is complete; only an EARLIER cancel (one the turn never
                # got past) should count as having interrupted it.
                cancelled = token.cancelled and not consumed.completed
                outcome = "cancelled" if cancelled else "error" if self._turn_failed else "done"
                self._finalize(current_id, cancelled=cancelled)
                self._end_run(run_id)
        self._flush()
        return outcome

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
                if event.type == "error":
                    turn = current_turn()
                    if turn is not None:
                        turn.error(event.error_type)
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
                turn = current_turn()
                if turn is not None:
                    turn.error(_error_type(exc))
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
