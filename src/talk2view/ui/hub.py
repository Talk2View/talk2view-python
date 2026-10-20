"""EventHub: one ``seq`` line for the page, fed by the chat controller and the app slices.

The page's state is flat: the chat snapshot's keys (``messages``, ``isLoading``, …) next
to the ``AppState`` slices (``auth``, ``models``, …). The hub keeps a mirror of it, numbers
every change with its own ``seq`` (+1 per batch, whichever source), keeps the last 512
batches for ``Last-Event-ID`` replay, and feeds each connected page through a queue of its
own. Snapshots for pages come from the mirror, so an HTTP thread never calls the controller.

**Queues.** What a page has not been sent yet is one list of ops on top of a known ``seq``
(``base``). A new batch is appended to it, and an ``append-text`` that follows an
``append-text`` on the same path is merged into it, so a page that falls behind a streaming
reply catches up with one event instead of hundreds. An ``ops`` event therefore carries
``base`` (the seq it applies on) and ``seq`` (the last batch folded in); a page applies it
only when ``base`` equals its own ``seq``. Past ``max_pending_ops`` ops or
``max_pending_bytes`` of JSON the queue is dropped and the page's next event is a fresh
snapshot. The replay ring is bounded the same two ways (``ring_size`` batches,
``max_ring_bytes``): a structural change sets the whole message list, so one batch can be
as large as the conversation. Every byte bound here counts UTF-8 bytes.

**Joining the controller.** A hub follows one controller. ``join()`` subscribes
(``deliver_on_main=False``: the callback encodes the batch, then applies and enqueues it
under this hub's lock and calls nothing), then takes ``controller.snapshot()`` with no hub
lock held and bases the mirror on it; batches that arrived in between are buffered (newest
kept, bounded like a page's queue) and replayed when newer. A gap, or a batch that does not
apply, means the mirror lost the chat line: the hub buffers again and calls ``on_resync``
once its lock is released. The owner must answer on ANOTHER thread with ``rebase()`` — a
snapshot taken inside a controller delivery is a plain copy, not a replay base — and the
rebase sets every chat key in one batch, so every page converges. A rebase never rolls the
mirror back: a snapshot no newer than the line the mirror is based on changes nothing. If
no rebase comes (it raised, or the job was lost), a later chat batch asks again, at most
once per ``RESYNC_RETRY_S``.

**Forgetting.** When the state stops being one end-user's (``AppState`` sees another
account on show: a sign-out, a switch), ``forget_replay()`` empties the ring and sets a
floor, so a page resuming from any seq before it gets a snapshot, never the previous
end-user's batches. Nothing is kept for replay again until a rebase read after the forget
shows the chat line has passed it (``forget_replay`` has the detail).

**Values JSON cannot carry.** The page parses with ``JSON.parse``, so NaN and the
infinities are not JSON here either. The chat line cannot refuse a value — a host's
``Path`` in a tool call's arguments would lose the line again on every rebase — so it is
scrubbed, with a warning: a non-finite float becomes ``"nan"``/``"inf"``, anything else
``"<TypeName>"``. An app slice is the caller's own, and is refused.

**Locking.** ``_lock`` is a leaf: nothing outside this module is called while it is held.
What a caller hands in (a batch, a snapshot, a slice value) is encoded before it is taken;
logging and ``on_resync`` run after it is released. Every client's condition shares it.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, FrozenSet, List, Optional, Protocol, Tuple

from ..state import ChatSnapshot, Op, OpBatch, OpError, _apply_op, initial_snapshot

__all__ = [
    "CHAT_KEYS",
    "MAX_BYTES",
    "MAX_CLIENTS",
    "MAX_PENDING_OPS",
    "RING_SIZE",
    "ChatSource",
    "EventHub",
    "TooManyClientsError",
    "merge_ops",
]

logger = logging.getLogger("talk2view.ui")

RING_SIZE = 512
MAX_PENDING_OPS = 1024
#: The default byte bound (UTF-8 bytes of JSON) of the replay ring, of each page's queue and
#: of the chat batches buffered while the hub waits for a (re)base.
MAX_BYTES = 8 * 2**20
MAX_CLIENTS = 16
#: Batches held while the hub waits for a (re)base (and at most ``max_pending_bytes`` of
#: them); past either bound it drops the oldest and keeps the newest waiting for the
#: snapshot, and a gap after it simply asks for another rebase.
MAX_CHAT_BUFFER = 4096
#: While the line stays unbased after asking for a rebase, a chat batch asks again at most
#: this often: a rebase that raised is retried, and one that keeps failing never loops hot.
RESYNC_RETRY_S = 1.0
#: How deep a scrubbed value is copied; anything deeper becomes a placeholder.
_SAFE_DEPTH = 64
#: The keys the chat controller owns in the page's state (``seq`` is the hub's).
CHAT_KEYS: FrozenSet[str] = frozenset(initial_snapshot()) - {"seq"}

#: ``(kind, seq, data)``: kind is ``"snapshot"``, ``"ops"`` or ``"hb"`` (heartbeat).
Event = Tuple[str, int, str]
#: Why the mirror lost the chat line ``(message, error)``: logged once the lock is released.
_Loss = Tuple[str, Optional[BaseException]]
#: A controller batch ready for the lock: ``(seq, ops as JSON, UTF-8 bytes)``.
_Encoded = Tuple[int, str, int]


class TooManyClientsError(RuntimeError):
    """Every stream slot is taken (``max_clients``)."""


class ChatSource(Protocol):
    def subscribe(
        self, on_batch: Callable[[OpBatch], None], *, deliver_on_main: bool = False
    ) -> Callable[[], None]: ...

    def snapshot(self) -> ChatSnapshot: ...


def _dumps(value: Any) -> str:
    """Compact JSON. NaN and the infinities raise ``ValueError``: ``JSON.parse`` rejects them."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _utf8_len(text: str) -> int:
    """The UTF-8 size of ``text``. A lone surrogate counts its 3 bytes instead of raising."""
    return len(text.encode("utf-8", "surrogatepass"))


def _json_safe(value: Any, depth: int = 0) -> Any:
    """A copy of ``value`` that JSON can carry: a non-finite float becomes ``str(x)``
    (``"nan"``, ``"inf"``); anything else JSON cannot carry, or nested deeper than
    ``_SAFE_DEPTH``, becomes ``"<TypeName>"``."""
    placeholder = "<{}>".format(type(value).__name__)
    try:
        if value is None or isinstance(value, (bool, str)):
            return value
        if isinstance(value, int):
            int.__repr__(value)  # past Python's int-to-str digit limit this raises
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if depth < _SAFE_DEPTH:
            if isinstance(value, dict):
                return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [_json_safe(v, depth + 1) for v in value]
    except Exception:  # a container or key of the caller's that raises
        return placeholder
    return placeholder  # not JSON, or nested too deep


def _encode(value: Any) -> Tuple[str, bool]:
    """``(json, scrubbed)``: ``value`` as JSON or, when JSON cannot carry it, as
    ``_json_safe`` copies it. Call it with no hub lock held: both may run the caller's code."""
    try:
        return _dumps(value), False
    except Exception:  # TypeError, ValueError (NaN), RecursionError, a container's own error
        return _dumps(_json_safe(value)), True


def merge_ops(pending: List[Op], ops: List[Op]) -> None:
    """Append ``ops`` to ``pending``, folding an ``append-text`` into an ``append-text`` on
    the same path right before it. Ops are shared (ring, other clients): a merge builds a
    new op and never mutates one it was given."""
    for op in ops:
        last = pending[-1] if pending else None
        if (
            op["type"] == "append-text"
            and last is not None
            and last["type"] == "append-text"
            and last["path"] == op["path"]
        ):
            pending[-1] = {
                "type": "append-text",
                "path": last["path"],
                "value": last["value"] + op["value"],
            }
        else:
            pending.append(op)


class _Client:
    """One connected page. Guarded by the hub's lock (``cond`` shares it)."""

    def __init__(self, lock: threading.Lock) -> None:
        self.cond = threading.Condition(lock)
        self.pending: List[Op] = []
        self.pending_bytes = 0  # UTF-8 bytes, an upper bound: merges only shrink it
        self.base = 0  # the seq `pending` applies on (= the last seq this page was sent)
        self.seq = 0  # the seq this page reaches once `pending` is sent
        self.snapshot = False  # the next event is a fresh snapshot
        self.announce = False  # send an ops event even with nothing pending (a resume)
        self.closed = False


class EventHub:
    def __init__(
        self,
        *,
        ring_size: int = RING_SIZE,
        max_ring_bytes: int = MAX_BYTES,
        max_pending_ops: int = MAX_PENDING_OPS,
        max_pending_bytes: int = MAX_BYTES,
        max_clients: int = MAX_CLIENTS,
        on_resync: Optional[Callable[[], None]] = None,
    ) -> None:
        bounds = {
            "ring_size": ring_size,
            "max_ring_bytes": max_ring_bytes,
            "max_pending_ops": max_pending_ops,
            "max_pending_bytes": max_pending_bytes,
            "max_clients": max_clients,
        }
        for name, bound in bounds.items():
            if bound < 1:
                raise ValueError("{} must be at least 1, not {!r}".format(name, bound))
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {}
        self._seq = 0
        self._slices: Dict[str, str] = {}  # each app slice's JSON, as last emitted
        self._ring: Deque[Tuple[int, List[Op], int]] = deque()  # (seq, ops, UTF-8 bytes)
        self._ring_size = ring_size
        self._ring_bytes = 0
        self._max_ring_bytes = max_ring_bytes
        self._max_pending_ops = max_pending_ops
        self._max_pending_bytes = max_pending_bytes
        self._max_clients = max_clients
        self._clients: List[_Client] = []
        self._joined = False
        self._chat_keys: FrozenSet[str] = CHAT_KEYS
        self._chat_seq: Optional[int] = None  # None: not based yet, batches are buffered
        self._chat_buffer: Deque[_Encoded] = deque()
        self._chat_buffer_bytes = 0
        self._resync_asked_at: Optional[float] = None  # the last on_resync, until a base
        self._on_resync = on_resync
        self._replay_floor = 0  # no page resumes from a seq below this: it gets a snapshot
        self._forget_epoch = 0  # +1 per forget_replay(): a rebase read before one is dropped
        self._forgetting = False  # nothing is kept for replay until a rebase passes a forget
        self._closed = False

    # ── Reading ──

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def snapshot(self) -> Dict[str, Any]:
        """``{"seq", "state"}``: a detached copy of the mirror."""
        with self._lock:
            copied: Dict[str, Any] = json.loads(_dumps({"seq": self._seq, "state": self._state}))
            return copied

    def set_resync_handler(self, callback: Optional[Callable[[], None]]) -> None:
        with self._lock:
            self._on_resync = callback

    # ── Replay ──

    def forget_replay(self) -> None:
        """Forget the replay history: the ring is emptied and a floor set at the current
        seq, so a page resuming from any seq before now gets a snapshot, never ops. Call it
        when the page's state stops being one end-user's (a sign-out, a switch), once the
        chat line has dropped their conversation, as ``AppState`` does.

        A chat batch cut before the controller dropped it can still reach the hub after
        this call: one delivered from inside a delivery window, or a rebase's snapshot read
        just before. So nothing is kept for replay (every resume gets a snapshot) until the
        chat line is known to be past the change: until a rebase whose snapshot was read
        after this call, which this call asks for through ``on_resync``. A rebase whose
        snapshot was read before it is dropped, not applied. With no rebase (no handler,
        or a router that stopped) every resume keeps getting a snapshot."""
        with self._lock:
            if self._closed:
                return
            self._ring.clear()
            self._ring_bytes = 0
            self._replay_floor = self._seq
            self._forget_epoch += 1
            self._forgetting = True
            callback = self._on_resync
        self._report(None, callback)

    # ── App slices ──

    def set_slice(self, key: str, value: Any) -> None:
        """Record one top-level app slice: one ``set [key]`` batch, or none when its JSON is
        unchanged (``True`` and ``1`` differ on the page, so they differ here). A value JSON
        cannot carry is refused — ``TypeError``, or ``ValueError`` for NaN and the
        infinities — and it is encoded before the lock is taken, so none of the caller's
        code runs under it."""
        key = str.__str__(key)  # an exact str (TypeError for a non-str): no caller's hash or eq
        text = _dumps(value)
        encoded = _dumps([{"type": "set", "path": [key], "value": json.loads(text)}])
        size = _utf8_len(encoded)
        with self._lock:
            if key in self._chat_keys or key == "seq":
                raise ValueError("{!r} belongs to the chat state".format(key))
            if self._closed or self._slices.get(key) == text:
                return
            self._emit_locked(encoded, size)
            self._slices[key] = text

    # ── The chat line ──

    def join(self, controller: ChatSource) -> Callable[[], None]:
        """Subscribe to ``controller`` and base the mirror on its snapshot. Returns the
        unsubscribe. A hub follows one controller: a second ``join()`` raises
        ``RuntimeError`` (one that raised leaves the hub free to join). Never call it from
        inside one of the controller's deliveries."""
        with self._lock:
            if self._joined:
                raise RuntimeError("an EventHub follows one chat controller")
            self._joined = True
        try:
            unsubscribe = controller.subscribe(self.on_chat_batch)
        except BaseException:
            self._unjoin()
            raise
        try:
            self.rebase(controller)
        except BaseException:
            unsubscribe()
            self._unjoin()
            raise
        return unsubscribe

    def _unjoin(self) -> None:
        with self._lock:
            self._joined = False
            self._chat_buffer.clear()
            self._chat_buffer_bytes = 0

    def rebase(self, controller: ChatSource) -> None:
        """Re-join the chat line after ``on_resync`` (same rule as ``join``). A snapshot
        read before a ``forget_replay()`` that lands meanwhile is dropped; one read after
        the latest ends its forgetting."""
        with self._lock:
            epoch = self._forget_epoch
        self._base_chat(controller.snapshot(), epoch)

    def on_chat_batch(self, batch: OpBatch) -> None:
        """The controller subscription: runs on its emitting thread, only enqueues."""
        seq = int(batch["seq"])
        encoded, scrubbed = _encode(batch["ops"])
        size = _utf8_len(encoded)
        with self._lock:
            loss = self._chat_batch_locked((seq, encoded, size))
            callback = self._resync_callback_locked(loss)
        if scrubbed:
            logger.warning(
                "chat batch %d held a value JSON cannot carry: the page gets a placeholder", seq
            )
        self._report(loss, callback)

    def base_chat(self, snapshot: ChatSnapshot) -> None:
        """Set every chat key from ``snapshot`` (one batch), then replay what was buffered.
        Nothing happens when the mirror is already based at or past ``snapshot["seq"]``: a
        rebase whose snapshot a batch overtook must not roll the mirror back. The snapshot is
        read before the lock is taken, its keys as exact ``str`` (``TypeError`` otherwise).
        Its age is unknown here, so it never ends a ``forget_replay()``: ``rebase`` does."""
        self._base_chat(snapshot, None)

    def _base_chat(self, snapshot: ChatSnapshot, epoch: Optional[int]) -> None:
        """``base_chat``; ``epoch`` is ``_forget_epoch`` as it was before ``snapshot`` was
        read (None: unknown)."""
        chat: Dict[str, Any] = {str.__str__(key): value for key, value in snapshot.items()}
        seq = int(chat.pop("seq"))
        keys = CHAT_KEYS | frozenset(chat)
        encoded, scrubbed = _encode(
            [{"type": "set", "path": [key], "value": value} for key, value in chat.items()]
        )
        size = _utf8_len(encoded)
        loss: Optional[_Loss] = None
        with self._lock:
            if self._closed:
                return
            if epoch is not None and epoch != self._forget_epoch:
                # Read before a forget_replay(): it may hold what was just forgotten. That
                # forget asked for a rebase of its own, which reads a snapshot after it.
                return
            if epoch is not None:
                # Read after the latest forget: every batch cut before it has reached this
                # hub (the snapshot waited out the controller's delivery window).
                self._forgetting = False
            if self._chat_seq is not None and seq <= self._chat_seq:
                return
            self._chat_keys = keys
            self._chat_seq = seq
            buffered, self._chat_buffer = self._chat_buffer, deque()
            self._chat_buffer_bytes = 0
            try:
                self._emit_locked(encoded, size)
            except Exception as exc:
                loss = self._lose_chat_locked("could not apply a chat snapshot", exc)
            else:
                self._resync_asked_at = None
            for batch in buffered:
                loss = self._chat_batch_locked(batch) or loss
            callback = self._resync_callback_locked(loss)
        if scrubbed:
            logger.warning(
                "the chat snapshot held a value JSON cannot carry: the page gets a placeholder"
            )
        self._report(loss, callback)

    def _chat_batch_locked(self, batch: _Encoded) -> Optional[_Loss]:
        """Apply one encoded controller batch. Not None when the chat line was just lost."""
        if self._closed:
            return None
        if self._chat_seq is None:
            self._buffer_locked(batch)
            return None
        seq, encoded, size = batch
        if seq <= self._chat_seq:
            return None  # already in the snapshot this hub was based on
        if seq != self._chat_seq + 1:
            return self._lose_chat_locked(
                "chat batch {} after {}: rebasing".format(seq, self._chat_seq)
            )
        self._chat_seq = seq
        try:
            self._emit_locked(encoded, size)
        except Exception as exc:  # whatever it was, the mirror did not follow this batch
            return self._lose_chat_locked("a chat batch did not apply to the mirror: rebasing", exc)
        return None

    def _buffer_locked(self, batch: _Encoded) -> None:
        self._chat_buffer.append(batch)
        self._chat_buffer_bytes += batch[2]
        while len(self._chat_buffer) > MAX_CHAT_BUFFER or (
            self._chat_buffer_bytes > self._max_pending_bytes and len(self._chat_buffer) > 1
        ):
            self._chat_buffer_bytes -= self._chat_buffer.popleft()[2]

    def _lose_chat_locked(self, message: str, error: Optional[BaseException] = None) -> _Loss:
        self._chat_seq = None
        self._chat_buffer.clear()
        self._chat_buffer_bytes = 0
        return (message, error)

    def _resync_callback_locked(self, loss: Optional[_Loss]) -> Optional[Callable[[], None]]:
        """The ``on_resync`` to call once the lock is released, or None. The hub asks when the
        line was just lost, and again on a chat batch while it stays unbased after asking (the
        rebase raised, or never came): at once after a base, then at most once per
        ``RESYNC_RETRY_S``."""
        if self._closed or self._chat_seq is not None:
            return None
        asked = self._resync_asked_at
        if loss is None and asked is None:
            return None  # not asked yet: joining, the first snapshot is on its way
        now = time.monotonic()
        if asked is not None and now - asked < RESYNC_RETRY_S:
            return None
        self._resync_asked_at = now
        return self._on_resync

    def _report(self, loss: Optional[_Loss], callback: Optional[Callable[[], None]]) -> None:
        """Log a lost chat line, then ask the owner for a rebase: never under the lock."""
        if loss is not None:
            message, error = loss
            if error is None:
                logger.warning("%s", message)
            else:
                logger.error("%s", message, exc_info=error)
        if callback is None:
            return
        try:
            callback()
        except Exception:
            logger.exception("on_resync raised")

    # ── Emitting ──

    def _emit_locked(self, encoded: str, size: int) -> None:
        """Apply one batch (``encoded``: a JSON list of ops; ``size``: its UTF-8 bytes) to
        the mirror and queue it for every page. When an op fails, the ops before it are in
        the mirror already: they still go out, as this batch, so a page equals the mirror at
        every seq. Then the error propagates."""
        ops: List[Op] = json.loads(encoded)
        if not isinstance(ops, list):
            raise OpError("a batch is a list of ops")
        applied = 0
        try:
            for op in ops:
                _apply_op(self._state, op)
                applied += 1
        finally:
            if applied < len(ops):
                ops = ops[:applied]
                size = _utf8_len(_dumps(ops))
            if ops:
                self._commit_locked(ops, size)

    def _commit_locked(self, ops: List[Op], size: int) -> None:
        """Number an applied batch, keep it for replay (unless a ``forget_replay()`` is not
        yet passed: then the floor moves past it) and queue it for every page."""
        self._seq += 1
        if self._forgetting:
            self._replay_floor = self._seq
        else:
            self._ring.append((self._seq, ops, size))
            self._ring_bytes += size
            while len(self._ring) > self._ring_size or (
                self._ring_bytes > self._max_ring_bytes and len(self._ring) > 1
            ):
                self._ring_bytes -= self._ring.popleft()[2]
        for client in self._clients:
            self._offer_locked(client, self._seq, ops, size)

    def _offer_locked(self, client: _Client, seq: int, ops: List[Op], size: int) -> None:
        if client.closed or client.snapshot:
            return
        merge_ops(client.pending, ops)
        client.pending_bytes += size
        client.seq = seq
        if (
            len(client.pending) > self._max_pending_ops
            or client.pending_bytes > self._max_pending_bytes
        ):
            client.pending = []
            client.pending_bytes = 0
            client.announce = False
            client.snapshot = True
        client.cond.notify()

    # ── Pages ──

    def attach(self, last_event_id: Optional[int] = None) -> _Client:
        """Register a page. Its first event is a snapshot, or, when ``last_event_id`` is a
        seq still in the ring and not below the floor of a ``forget_replay()``, an ``ops``
        event replaying everything after it (possibly none: the page is told it is
        current)."""
        with self._lock:
            if self._closed:
                raise TooManyClientsError("the UI server is stopping")
            if len(self._clients) >= self._max_clients:
                raise TooManyClientsError("too many open event streams")
            client = _Client(self._lock)
            if last_event_id is None or not self._replay_locked(client, last_event_id):
                client.pending = []
                client.pending_bytes = 0
                client.snapshot = True
            self._clients.append(client)
            return client

    def _replay_locked(self, client: _Client, since: int) -> bool:
        if since < self._replay_floor or since < 0 or since > self._seq:
            return False
        if since < self._seq and (not self._ring or self._ring[0][0] > since + 1):
            return False  # older than the ring
        client.base = client.seq = since
        for seq, ops, size in self._ring:
            if seq > since:
                merge_ops(client.pending, ops)
                client.pending_bytes += size
                client.seq = seq
        if (
            len(client.pending) > self._max_pending_ops
            or client.pending_bytes > self._max_pending_bytes
        ):
            return False
        client.announce = True
        return True

    def detach(self, client: _Client) -> None:
        with self._lock:
            client.closed = True
            if client in self._clients:
                self._clients.remove(client)
            client.cond.notify()

    def next_event(self, client: _Client, timeout: float) -> Optional[Event]:
        """The page's next event, waiting up to ``timeout`` s (then ``"hb"``). ``None`` once
        the page is detached or the hub is closed."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while not (client.closed or client.snapshot or client.pending or client.announce):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ("hb", client.seq, "")
                client.cond.wait(remaining)
            if client.closed:
                return None
            if client.snapshot:
                client.snapshot = client.announce = False
                client.pending = []
                client.pending_bytes = 0
                client.base = client.seq = self._seq
                return ("snapshot", self._seq, _dumps({"seq": self._seq, "state": self._state}))
            ops, client.pending = client.pending, []
            client.pending_bytes = 0
            base, seq = client.base, client.seq
            client.base = seq
            client.announce = False
        return ("ops", seq, _dumps({"seq": seq, "base": base, "ops": ops}))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients, self._clients = self._clients, []
            for client in clients:
                client.closed = True
                client.cond.notify()
