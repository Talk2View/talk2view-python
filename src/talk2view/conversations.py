"""Earlier conversations, kept on the device (TS ``chat/lib/conversation-store.ts``).

The engine keeps none: its checkpointer is in memory and it has no list or fetch API. It
does not need to: the client replays the whole transcript with every message, so a
conversation restored from here continues correctly after a deploy.

This is the storage half — no threads, no controller — so the caps and the validation are
tested on their own. ``talk2view.ui.threads.ThreadsState`` owns the list.

Shape, one ``TokenStorage`` value per partner key, one bucket per identity (the same JSON
the TypeScript SDK writes to ``localStorage``, so a device store shared between the two
SDKs holds one history)::

    t2v_chat_conversations_v1:<partnerKey>
      {"v": 1, "identities": {"u:<userId>": {"updatedAt": ms, "conversations": [...]}}}

Identity: the end-user's id and nothing else. A guest has an id of their own, so a guest
and a signed-in person land in different buckets; converting a guest into an account
keeps the id, so nothing moves. ``"none"`` is the bucket of a visitor nobody has signed
in yet.

Trust: whoever can write this device's store can already run arbitrary code as this
end-user, so a ``system``-role history entry is accepted and replayed as-is on restore --
parity with TS's own ``isChatMessage``, not a hardening gap. A ``tool``-role entry is never
accepted (``VALID_HISTORY_ROLES`` in ``state.py``): there is no path for stored data to
inject a tool result the engine would trust on replay.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from .state import (
    VALID_HISTORY_ROLES,
    ConversationSnapshot,
    DisplayMessage,
    HistoryEntry,
    counter_suffix,
    is_hostile_counter_digits,
    valid_attachment_dict,
    valid_tool_step_dict,
)
from .storage import TokenStorage

__all__ = [
    "MAX_CHARS",
    "MAX_CONVERSATIONS",
    "MAX_IDENTITIES",
    "MAX_STORED_MESSAGES",
    "MAX_STORED_TITLE",
    "MAX_TITLE",
    "UNTITLED",
    "Conversation",
    "ConversationStore",
    "identity_key",
    "new_id",
    "storage_key",
    "title_from",
]

logger = logging.getLogger("talk2view.conversations")

#: What the list shows for a conversation with no first message yet.
UNTITLED = "New chat"
#: Per identity (TS MAX_CONVERSATIONS).
MAX_CONVERSATIONS = 50
#: Identity buckets kept per partner, least recently used dropped first (TS MAX_IDENTITIES).
MAX_IDENTITIES = 8
#: Characters in the stored value, across every identity (TS MAX_CHARS).
MAX_CHARS = 512 * 1024
#: Longest title kept; longer first messages are cut at a word boundary (TS MAX_TITLE).
MAX_TITLE = 60
#: Longest STORED title kept on read, hard-cut -- not `title_from`'s nice word-boundary
#: cut, which is for a title this SDK computes from a live message. `threadRename`
#: itself never sends more than this (`ui/wire.py`'s own cap); a hand-edited or corrupt
#: file claiming more is trimmed rather than trusted.
MAX_STORED_TITLE = 200
#: Messages kept from one stored conversation on read: comfortably more than MAX_CHARS
#: could ever hold at any realistic per-message size (each needs an id, a role, a
#: timestamp and a content string at minimum), so this never bites a real conversation --
#: a backstop against a hand-edited or corrupt file claiming an unbounded list, not a cap
#: this SDK's own writes are meant to approach. The newest are kept, the oldest dropped,
#: the same direction as the write path's own per-identity LRU.
MAX_STORED_MESSAGES = 5000
_KEY_PREFIX = "t2v_chat_conversations_v1:"
_EPOCH = "1970-01-01T00:00:00.000Z"
_ROLES = ("user", "assistant")


@dataclass
class Conversation:
    """One conversation as the list holds it in memory. ``created_at``/``updated_at`` are
    epoch milliseconds; ``updated_at`` orders the list and decides what is dropped."""

    id: str
    title: Optional[str]
    created_at: int
    updated_at: int
    snapshot: ConversationSnapshot


def _dumps(payload: Any) -> str:
    """Serialise the way TS's ``JSON.stringify`` does: no whitespace, and non-ASCII kept
    raw rather than escaped. ``json.dumps`` defaults to ``ensure_ascii=True`` (every
    non-ASCII character becomes a 6-character ``\\uXXXX`` escape) and pads every separator
    with a space; either alone makes the 512 KB cap measure a different (usually much
    larger) length than TS's own cap does for the same content, binding far earlier for
    non-Latin text. ``json.loads`` reads either form back the same, so this changes nothing
    about what a stored value means -- only how many characters it costs."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def storage_key(partner_key: str) -> str:
    return _KEY_PREFIX + partner_key


def identity_key(user_id: Optional[str]) -> str:
    return "u:" + user_id if user_id else "none"


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    """Unique inside one device's store, as TS ``newId``: ``c_<ms base36>_<8 base36>``."""
    return "c_{}_{}".format(
        _base36(now_ms()),
        "".join(random.choices("0123456789abcdefghijklmnopqrstuvwxyz", k=8)),  # noqa: S311
    )


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n > 0:
        n, rem = divmod(n, 36)
        out = digits[rem] + out
    return out or "0"


def title_from(message: Optional[DisplayMessage]) -> Optional[str]:
    """A short title from the first user message (TS ``titleFrom``): one line, cut at a
    word boundary; else the first attachment's file name; else None. Total: never raises,
    even on a message a host built by hand rather than one the store validated --
    ``content`` missing or not a string, or an attachments list whose first element is not
    a dict, both read as "nothing to title from" rather than crash."""
    content = message.get("content") if message else None
    text = " ".join(content.split()) if isinstance(content, str) else ""
    if text:
        return text if len(text) <= MAX_TITLE else _cut(text)
    attachments = message.get("attachments") if message else None
    first = attachments[0] if attachments else None
    if isinstance(first, dict):
        name = str(first.get("filename", "")).strip()
        if name:
            return name if len(name) <= MAX_TITLE else _cut(name)
    return None


def _cut(text: str) -> str:
    head = text[:MAX_TITLE]
    space = head.rfind(" ")
    # Only break on a space far enough in to leave a real title; a first word longer than
    # that is cut mid-word rather than thrown away.
    return (head[:space] if space > MAX_TITLE / 2 else head).rstrip() + "…"


class ConversationStore:
    """Read and write one partner key's conversations, per identity. Never raises: a
    storage that fails reads as empty and writes as False ("this device does not keep
    history"), the same as TS treats a refused ``localStorage``.

    ``write()`` is a read-modify-write over whatever the storage holds, so two callers
    racing on it (a debounce timer and an auth-change handler, say) could otherwise lose
    one caller's update entirely: the second read lands before the first write is visible,
    and its write then overwrites the file without the first caller's identity. ``_lock``
    serialises ``write()`` against itself for that reason. It is a leaf: nothing else is
    taken while it is held, and it is taken below every lock this SDK has elsewhere
    (``ChatController._lock``/``_emit_lock``, ``T2VClient._tokens_lock``/``_refresh_lock``)
    -- nothing here ever calls back into a caller while held, only the storage's own
    ``get``/``set``, which is what makes holding it across them safe (the same pattern
    ``T2VClient._tokens_lock`` already uses around the same kind of call)."""

    def __init__(self, storage: TokenStorage, partner_key: str) -> None:
        self._storage = storage
        self._key = storage_key(partner_key)
        self._lock = threading.Lock()

    # ── Reading ──

    def read(self, identity: str) -> List[Conversation]:
        """This identity's conversations, newest first; what does not validate is dropped."""
        stored = self._read_file()
        bucket = stored["identities"].get(identity) if stored is not None else None
        if not isinstance(bucket, dict):
            return []
        found = [
            c
            for c in (_parse_conversation(v) for v in _as_list(bucket.get("conversations")))
            if c is not None
        ]
        return sorted(found, key=lambda c: c.updated_at, reverse=True)

    # ── Writing ──

    def write(
        self, identity: str, conversations: Sequence[Conversation], current: Optional[str]
    ) -> bool:
        """Replace this identity's conversations, keeping every other identity's. False
        when nothing could be stored. ``current`` is the conversation on screen: the caps
        never drop it. Serialised against a concurrent ``write()`` -- see the class
        docstring."""
        with self._lock:
            return self._write_locked(identity, conversations, current)

    def _write_locked(
        self, identity: str, conversations: Sequence[Conversation], current: Optional[str]
    ) -> bool:
        stored = self._read_file() or {"v": 1, "identities": {}}
        identities: Dict[str, Dict[str, Any]] = dict(stored["identities"])
        # Rank every OTHER identity by (updatedAt, position) descending -- TS's own rule
        # (`conversation-store.ts:162-170`): a stable sort on updatedAt, so two buckets that
        # tie keep the file's key order, which is first-insertion order in a file TS wrote
        # (re-assigning a JS object's key does not move it). Position comes from the file
        # as read, so a TS-written file evicts the same identity TS itself would; this also
        # ranks a Python-written file correctly, since Python appends a freshly-written
        # identity at the end (a later position = a later write = "newer" on a tie, the LRU
        # intent), without needing to move existing keys around.
        order = list(identities.keys())
        others = sorted(
            (key for key in identities if key != identity),
            key=lambda key: (int(identities[key].get("updatedAt") or 0), order.index(key)),
            reverse=True,
        )
        # Dropped regardless of whether `identity` itself survives below (TS's own
        # `slice(MAX_IDENTITIES - 1)` runs unconditionally): an identity that ends up with
        # nothing to keep still costs the others a slot for this write.
        for key in others[MAX_IDENTITIES - 1 :]:
            del identities[key]
        mine = sorted(conversations, key=lambda c: c.updated_at, reverse=True)[:MAX_CONVERSATIONS]
        if mine:
            identities[identity] = {
                "updatedAt": now_ms(),
                "conversations": [_serialise(c) for c in mine],
            }
        else:
            identities.pop(identity, None)
        try:
            text = _dumps({"v": 1, "identities": identities})
            while len(text) > MAX_CHARS:
                if not _drop_oldest(identities, identity, current):
                    return self._remove()
                text = _dumps({"v": 1, "identities": identities})
        except (TypeError, ValueError):
            # `export_conversation` never produces anything this raises on, but `write()`
            # takes a bare `Conversation`, not only ones this SDK built: "never raises" the
            # class promises covers a host-built one too.
            logger.warning("the conversation store could not be serialised", exc_info=True)
            return False
        return self._write(text)

    # ── The file ──

    def _read_file(self) -> Optional[Dict[str, Any]]:
        try:
            raw = self._storage.get(self._key)
        except Exception:
            logger.warning("the conversation store could not be read", exc_info=True)
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("identities"), dict):
            return None
        identities: Dict[str, Dict[str, Any]] = {}
        for key, value in parsed["identities"].items():
            if isinstance(value, dict):
                identities[str(key)] = {
                    "updatedAt": _as_int(value.get("updatedAt")) or 0,
                    "conversations": _as_list(value.get("conversations")),
                }
        return {"v": 1, "identities": identities}

    def _write(self, text: str) -> bool:
        try:
            self._storage.set(self._key, text)
            return True
        except Exception:
            logger.warning("the conversation store could not be written", exc_info=True)
            return False

    def _remove(self) -> bool:
        try:
            self._storage.delete(self._key)
        except Exception:
            logger.warning("the conversation store could not be cleared", exc_info=True)
        return False


def _drop_oldest(
    identities: Dict[str, Dict[str, Any]], identity: str, current: Optional[str]
) -> bool:
    """Drop the single oldest conversation allowed: another identity's first, then this
    one's, never ``current``. False when there is nothing left to give up."""
    candidates: List[Any] = []  # (mine, updatedAt, key, index)
    for key, bucket in identities.items():
        for index, conversation in enumerate(bucket["conversations"]):
            if key == identity and conversation.get("id") == current:
                continue
            rank = _as_int(conversation.get("updatedAt")) or 0
            candidates.append((key == identity, rank, key, index))
    if not candidates:
        return False
    candidates.sort(key=lambda c: (c[0], c[1]))
    _mine, _updated, key, index = candidates[0]
    bucket = identities[key]
    bucket["conversations"] = [c for i, c in enumerate(bucket["conversations"]) if i != index]
    if not bucket["conversations"]:
        del identities[key]
    return True


# ── Serialising (the TS StoredConversation shape) ──


def _serialise(conversation: Conversation) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": conversation.id,
        "title": conversation.title,
        "createdAt": conversation.created_at,
        "updatedAt": conversation.updated_at,
        "threadId": conversation.snapshot["threadId"],
        "messages": [_serialise_message(m) for m in conversation.snapshot["messages"]],
        "history": list(conversation.snapshot["history"]),
    }
    marks = conversation.snapshot.get("marks")
    if marks is not None:
        out["marks"] = dict(marks)
    return out


def _serialise_message(message: DisplayMessage) -> Dict[str, Any]:
    out = dict(message)
    out.pop("isStreaming", None)  # a stored copy never shows a cursor nothing writes to
    return out


def _parse_conversation(value: Any) -> Optional[Conversation]:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
        return None
    # The newest MAX_STORED_MESSAGES raw entries only -- sliced before _parse_message
    # ever runs on them, so a claimed-unbounded list costs no more parsing than the cap
    # itself, not whatever length the file claims.
    raw_messages = _as_list(value.get("messages"))[-MAX_STORED_MESSAGES:]
    messages = [m for m in (_parse_message(v) for v in raw_messages) if m is not None]
    history: List[HistoryEntry] = [
        h for h in _as_list(value.get("history")) if _is_history_entry(h)
    ]
    parsed_created = _as_int(value.get("createdAt"))
    created: int = parsed_created if parsed_created is not None else now_ms()
    parsed_updated = _as_int(value.get("updatedAt"))
    updated: int = parsed_updated if parsed_updated is not None else created
    raw_title = value.get("title")
    title = raw_title[:MAX_STORED_TITLE] if isinstance(raw_title, str) and raw_title else None
    thread_id = value.get("threadId") if isinstance(value.get("threadId"), str) else None
    snapshot: ConversationSnapshot = {
        "messages": messages,
        "history": history,
        "threadId": thread_id,
    }
    marks = _parse_marks(value.get("marks"))
    if marks is not None:
        snapshot["marks"] = marks
    return Conversation(
        id=value["id"], title=title, created_at=created, updated_at=updated, snapshot=snapshot
    )


def _parse_marks(value: Any) -> Optional[Dict[str, int]]:
    """The ``marks`` a stored conversation carries (round-tripped from
    ``ChatController.export_conversation``'s optional field), or None when absent or not
    the right shape: a dict of message id to a non-boolean int. This is a shape check only
    -- semantic validity (the right ids for the messages that survived parsing, in bounds,
    non-decreasing) is ``restore_conversation``'s job, done again there; keeping the store's
    own check loose here means a mismatch from messages this function already dropped does
    not need to be anticipated twice."""
    if not isinstance(value, dict):
        return None
    out: Dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, int) or isinstance(item, bool):
            return None
        out[key] = item
    return out


def _parse_message(value: Any) -> Optional[DisplayMessage]:
    """Drops what does not validate, the same promise ``read`` makes as a whole: a message
    whose id's ``msg_<n>_...`` counter suffix is one of the two hostile shapes (a Unicode
    digit run, or one too long for ``int()``) -- a store this corrupt is not partially
    trusted, so the whole message goes, not just the mutation it would otherwise crash. An
    id that is merely not a counter id at all (``"msg_welcome"``) is opaque, not hostile,
    and is kept as-is: it never reaches ``int()`` either way. Any attachment element that
    is not exactly the shape ``Attachment(**a)`` needs is dropped on its own (the message
    itself is kept; a message's text is still worth restoring without a file that no
    longer validates), and the same for any ``steps`` element that is not exactly a
    ``ToolStep`` (``valid_tool_step_dict``) -- the page's renderer reads every stored
    step unguarded, so one bad one used to blank the whole chat, not just this message."""
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("id"), str) or not isinstance(value.get("content"), str):
        return None
    if value.get("role") not in _ROLES:
        return None
    message_id = value["id"]
    digits = counter_suffix(message_id)
    if digits is not None and is_hostile_counter_digits(digits):
        return None
    message: Dict[str, Any] = {
        "id": message_id,
        "role": value["role"],
        "content": value["content"],
        "timestamp": value["timestamp"] if _is_iso(value.get("timestamp")) else _EPOCH,
    }
    if isinstance(value.get("attachments"), list):
        valid = [a for a in value["attachments"] if valid_attachment_dict(a)]
        if valid:
            message["attachments"] = valid
    if isinstance(value.get("plan"), str):
        message["plan"] = value["plan"]
    if isinstance(value.get("steps"), list):
        # Same rule as attachments: a step that is not exactly the shape convert.ts's
        # stepPart needs is dropped on its own, not the whole message -- and, because the
        # newest stored conversation is restored on every start, not the whole chat.
        valid_steps = [s for s in value["steps"] if valid_tool_step_dict(s)]
        if valid_steps:
            message["steps"] = valid_steps
    return message  # type: ignore[return-value]


def _is_iso(value: Any) -> bool:
    """An ISO-8601 UTC timestamp as ``state.now_iso`` writes them; anything else reads as
    the epoch (TS ``asDate`` falls back to ``new Date(0)``)."""
    if not isinstance(value, str) or len(value) < 20 or not value.endswith("Z"):
        return False
    try:
        time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return False
    return True


def _is_history_entry(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("role") not in VALID_HISTORY_ROLES:
        return False
    return isinstance(value.get("content"), (str, list))


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _as_int(value: Any) -> Optional[int]:
    """``value`` as an ``int``, or None: ``isinstance(True, int)`` is true in Python, so a
    JSON boolean would otherwise read as 1/0 where TS's ``typeof x === 'number'`` rejects
    it."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
