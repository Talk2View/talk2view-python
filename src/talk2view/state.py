"""Chat state wire types and op batches.

The dict shapes here are the wire format the embedded page consumes: key casing is
camelCase exactly as the TypeScript SDK's ``DisplayMessage`` (``src/types.ts``).
``requires-python`` is 3.10 and ``typing_extensions`` is not a dependency, so
``Required``/``NotRequired`` are unavailable: every TypedDict with optional keys is split
into a required base class plus a ``total=False`` subclass.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Literal, Optional, TypedDict, Union, cast


class AttachmentDict(TypedDict):
    id: str
    filename: str
    mime_type: str
    size_bytes: int


class _ToolStepRequired(TypedDict):
    name: str
    status: Literal["used", "denied", "running"]


class ToolStep(_ToolStepRequired, total=False):
    args: Dict[str, Any]
    result: str
    # Python extensions (absent from the TS type; the page ignores unknown keys).
    toolCallId: str
    display: Dict[str, Any]
    source: Literal["client", "platform"]


class _DisplayMessageRequired(TypedDict):
    id: str
    role: Literal["user", "assistant"]
    content: str
    timestamp: str  # ISO-8601 UTC; the page revives it to a Date before convert.ts


class DisplayMessage(_DisplayMessageRequired, total=False):
    isStreaming: bool
    attachments: List[AttachmentDict]
    plan: str
    steps: List[ToolStep]


class _PendingApprovalRequired(TypedDict):
    toolCallId: str
    toolName: str
    arguments: Dict[str, Any]
    description: str


class PendingApproval(_PendingApprovalRequired, total=False):
    # "client" | "engine" | unknown -> treated as non-client. Deliberately `str`, not a
    # Literal: an engine newer than this SDK can send a kind it has never heard of, and every
    # check that reads this key must fail closed on one rather than be cast into a guess.
    # Absent means a client tool, which is why a client approval keeps the four TS keys exactly.
    kind: str
    details: List[Dict[str, str]]


class _HistoryEntryRequired(TypedDict):
    role: str  # "user" | "assistant" | "system" | "tool"
    content: Any  # str, or a list of content parts (TS ChatMessage.content)


class HistoryEntry(_HistoryEntryRequired, total=False):
    tool_call_id: str


class _ConversationSnapshotRequired(TypedDict):
    messages: List[DisplayMessage]
    history: List[HistoryEntry]
    threadId: Optional[str]


class ConversationSnapshot(_ConversationSnapshotRequired, total=False):
    """Everything needed to put a conversation back later (TS ``ConversationSnapshot``):
    the transcript as displayed, the history the model is told, and the engine's thread
    id. ``messages`` never carry ``isStreaming``.

    ``marks``: an extension TS does not have and ignores on any snapshot it reads (extra
    keys are invisible to its ``parseConversation``, which builds a fresh object from known
    ones). One entry per user message in ``messages``, each the index into ``history`` a
    live turn would have marked it at (``ChatController``'s own ``_history_marks``) --
    without it, ``restore_conversation`` has to reconstruct the same pairing from the
    transcript's structure alone, which cannot always tell two identical user messages
    apart. Absent on an older or TS-written snapshot; ``restore_conversation`` falls back
    to reconstructing it when it is missing or fails validation."""

    marks: Dict[str, int]


#: cpython's own limit on how many digits ``int()`` will parse from a string (3.11+;
#: harmless to check on 3.10, which has no such limit). A digit run past it is rejected
#: outright rather than attempted, so a corrupt or hostile id can never reach ``int()``.
MAX_ID_DIGITS = 4300


def counter_suffix(message_id: str) -> Optional[str]:
    """The ``<n>`` segment of a ``msg_<n>_<ts>`` id (what ``next_message_id`` mints), or
    None when the id is not in that shape at all -- an opaque, host-assigned id, not
    anyone's business to read as a counter."""
    parts = message_id.split("_")
    if len(parts) >= 2 and parts[0] == "msg":
        return parts[1]
    return None


def is_safe_counter_digits(digits: str) -> bool:
    """Whether ``digits`` is safe to pass to ``int()``: ASCII digits only
    (``str.isdigit()`` is also true for non-ASCII digits, such as a superscript or a
    Devanagari numeral, that ``int()`` rejects) and no longer than cpython's own
    conversion limit. Used to decide whether to *read* a counter id -- an id that is not a
    digit run at all is simply not this function's business (see
    ``is_hostile_counter_digits`` for the id-validity check, which treats that case as
    fine rather than unsafe)."""
    return bool(digits) and digits.isascii() and digits.isdigit() and len(digits) <= MAX_ID_DIGITS


def is_hostile_counter_digits(digits: str) -> bool:
    """Whether ``digits`` (a ``msg_<digits>_...`` id's middle segment) is one of the two
    hostile shapes a corrupt or malicious store entry could carry: it satisfies
    ``str.isdigit()`` -- so it *looks* like the counter suffix ``next_message_id`` mints --
    but is not safe to hand to ``int()``, either because it is not ASCII digits (a
    superscript or a Devanagari numeral, say) or because it is longer than cpython's own
    conversion limit.

    An id whose middle segment is not a digit run at all (``"msg_welcome_1"``) is simply an
    opaque, host-assigned id -- not hostile, and not this function's business: it never
    reaches ``int()`` in the first place (``bump_message_counter`` skips it, the same as it
    skips an id with no ``msg_`` prefix), and code that only decides whether an id may be
    *accepted* (``restore_conversation``'s validator, the store's ``_parse_message``) must
    accept it too, not veto a whole snapshot or drop a message over an id nobody was ever
    going to parse."""
    return digits.isdigit() and (not digits.isascii() or len(digits) > MAX_ID_DIGITS)


def valid_attachment_dict(value: Any) -> bool:
    """Whether ``value`` is exactly the shape ``AttachmentDict``/``_attachment_dict`` write
    -- what ``Attachment(**value)`` needs to not raise."""
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and isinstance(value.get("filename"), str)
        and isinstance(value.get("mime_type"), str)
        and isinstance(value.get("size_bytes"), int)
        and not isinstance(value.get("size_bytes"), bool)
    )


#: ``ToolStep.status`` values the page's ``convert.ts`` knows how to draw (``stepPart``):
#: a spinner for ``"running"``, "Denied by the user" for ``"denied"``, the result otherwise.
#: Anything else is not a shape ``convert.ts`` was written against.
VALID_STEP_STATUSES = ("used", "denied", "running")

#: ``ToolStep.source`` values this SDK ever sets (``followup.py`` and engine tool events).
VALID_STEP_SOURCES = ("client", "platform")


def valid_tool_step_dict(value: Any) -> bool:
    """Whether ``value`` is a well-formed ``ToolStep``: what ``convert.ts``'s ``stepPart``
    reads (``step.status``, ``step.name``) without throwing, plus this SDK's own optional
    keys, all type- and shape-checked. A step this loose lets through — a ``null`` in the
    list, a numeric ``status``, a ``display`` that is not an object — used to reach the
    page verbatim from a stored conversation and blank it on every start, since the newest
    one is restored automatically."""
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("name"), str) or value.get("status") not in VALID_STEP_STATUSES:
        return False
    if "args" in value and not isinstance(value["args"], dict):
        return False
    if "result" in value and not isinstance(value["result"], str):
        return False
    if "toolCallId" in value and not isinstance(value["toolCallId"], str):
        return False
    if "display" in value and not isinstance(value["display"], dict):
        return False
    if "source" in value and value["source"] not in VALID_STEP_SOURCES:
        return False
    return True


#: History roles the client ever replays back to the engine (TS ``isChatMessage``). Never
#: ``tool``: the client resends a tool's own text as part of the assistant's turn, not as a
#: separate ``tool``-role entry, so stored data can never inject one the engine would trust
#: as a tool result on replay.
VALID_HISTORY_ROLES = ("user", "assistant", "system")


def valid_history_entry(value: Any) -> bool:
    """Whether ``value`` is a well-formed ``HistoryEntry``: one of ``VALID_HISTORY_ROLES``
    and text or structured content."""
    return (
        isinstance(value, dict)
        and value.get("role") in VALID_HISTORY_ROLES
        and isinstance(value.get("content"), (str, list))
    )


class _LocalSkillRequired(TypedDict):
    name: str
    description: str
    enabled: bool
    origin: Literal["code", "dir", "bundled", "user"]
    label: str
    editable: bool
    hasExtraFiles: bool
    problems: List[Dict[str, str]]


class LocalSkillState(_LocalSkillRequired, total=False):
    path: str
    overridesPlatform: bool
    #: The on-disk entry (folder name, or a flat file's stem) of a ``"user"``/``"bundled"``
    #: skill: what the page sends to edit, delete, restore, customize or read it. Usually
    #: equal to ``name``; differs for a hand-made skill whose frontmatter name is not its
    #: folder's.
    key: str
    #: A bundled skill the end-user's own skill of the same name replaces: listed only,
    #: never registered and never toggled ("Replaced by yours").
    overridden: bool


class SkillsState(TypedDict):
    """The skills slice of the snapshot. Owned by ``skills.py``; the controller never edits it."""

    local: List[LocalSkillState]
    platform: Optional[List[Dict[str, Any]]]
    registered: List[str]
    available: bool
    needsSessionReset: bool
    store: Dict[str, bool]


class ChatSnapshot(TypedDict):
    seq: int
    messages: List[DisplayMessage]
    isLoading: bool
    error: Optional[str]
    pendingApproval: Optional[PendingApproval]
    agentStatus: Optional[Dict[str, str]]
    threadId: Optional[str]
    alwaysAllowedTools: List[str]
    skills: SkillsState
    isAnonymous: bool


Path = List[Union[str, int]]


class Op(TypedDict):
    type: Literal["set", "append-text"]
    path: Path
    value: Any


class OpBatch(TypedDict):
    seq: int  # monotonic per controller, +1 per batch
    ops: List[Op]


def empty_skills_state() -> SkillsState:
    return {
        "local": [],
        "platform": None,
        "registered": [],
        "available": False,
        "needsSessionReset": False,
        "store": {"writable": False, "canOpenFolder": False},
    }


def initial_snapshot(*, is_anonymous: bool = False) -> ChatSnapshot:
    return {
        "seq": 0,
        "messages": [],
        "isLoading": False,
        "error": None,
        "pendingApproval": None,
        "agentStatus": None,
        "threadId": None,
        "alwaysAllowedTools": [],
        "skills": empty_skills_state(),
        "isAnonymous": is_anonymous,
    }


class OpError(ValueError):
    """An op cannot be applied: bad path, wrong value type, or unknown op type."""


class SeqGapError(OpError):
    """``batch.seq`` is not ``snapshot.seq + 1``; the consumer must fetch a fresh snapshot."""


def _child(node: Any, key: Union[str, int], path: Path) -> Any:
    if isinstance(node, dict) and isinstance(key, str):
        if key not in node:
            raise OpError("path {!r}: key {!r} does not exist".format(path, key))
        return node[key]
    if isinstance(node, list) and isinstance(key, int) and not isinstance(key, bool):
        if not 0 <= key < len(node):
            raise OpError("path {!r}: index {} is out of range".format(path, key))
        return node[key]
    raise OpError("path {!r}: cannot step into {} with {!r}".format(path, type(node).__name__, key))


def _apply_op(root: Dict[str, Any], op: Op) -> None:
    """Apply one op to ``root`` in place. The single implementation both sides share."""
    path = op["path"]
    if not path:
        raise OpError("empty path")
    if path == ["seq"]:
        raise OpError("seq is carried by the batch, never by an op")
    parent: Any = root
    for key in path[:-1]:
        parent = _child(parent, key, path)
    last = path[-1]

    if op["type"] == "set":
        value = copy.deepcopy(op["value"])
        if isinstance(parent, list):
            if not isinstance(last, int) or isinstance(last, bool):
                raise OpError("path {!r}: a list needs an integer index".format(path))
            if last == len(parent):
                parent.append(value)  # append == set at index len
            elif 0 <= last < len(parent):
                parent[last] = value
            else:
                raise OpError("path {!r}: index {} is out of range".format(path, last))
        elif isinstance(parent, dict):
            if not isinstance(last, str):
                raise OpError("path {!r}: a dict needs a string key".format(path))
            parent[last] = value
        else:
            raise OpError("path {!r}: cannot set inside {}".format(path, type(parent).__name__))
        return

    if op["type"] == "append-text":
        current = _child(parent, last, path)
        if not isinstance(current, str) or not isinstance(op["value"], str):
            raise OpError("path {!r}: append-text needs a string on both sides".format(path))
        parent[last] = current + op["value"]
        return

    raise OpError("unknown op type {!r}".format(op["type"]))


def apply_batch(snapshot: ChatSnapshot, batch: OpBatch) -> ChatSnapshot:
    """Return a new snapshot with ``batch`` applied. Never mutates its arguments."""
    if batch["seq"] != snapshot["seq"] + 1:
        raise SeqGapError(
            "batch seq {} does not follow snapshot seq {}".format(batch["seq"], snapshot["seq"])
        )
    result = copy.deepcopy(snapshot)
    root = cast(Dict[str, Any], result)
    for op in batch["ops"]:
        _apply_op(root, op)
    result["seq"] = batch["seq"]
    return result


class OpRecorder:
    """Owns the live chat state and records an op for every mutation.

    Each mutator builds the op first and then applies that very op to the live state with
    ``_apply_op`` — the same function ``apply_batch`` uses — so the state and the op stream
    cannot diverge. Not thread-safe: the controller calls it under its state lock.
    """

    def __init__(self, snapshot: Optional[ChatSnapshot] = None) -> None:
        self._state: ChatSnapshot = (
            copy.deepcopy(snapshot) if snapshot is not None else initial_snapshot()
        )
        self._ops: List[Op] = []

    @property
    def state(self) -> ChatSnapshot:
        """The live state. Read it; never mutate it except through this recorder."""
        return self._state

    @property
    def pending(self) -> bool:
        return bool(self._ops)

    def set(self, path: Path, value: Any) -> None:
        self._record({"type": "set", "path": list(path), "value": copy.deepcopy(value)})

    def append_text(self, path: Path, text: str) -> None:
        if text == "":
            return
        self._record({"type": "append-text", "path": list(path), "value": text})

    def append_message(self, message: DisplayMessage) -> int:
        index = len(self._state["messages"])
        self.set(["messages", index], message)
        return index

    def replace_messages(self, messages: List[DisplayMessage]) -> None:
        """Any structural shrink (finalize pop, retry, edit, clear) is one whole-list set."""
        self.set(["messages"], messages)

    def flush(self) -> Optional[OpBatch]:
        """Close the current batch: bump ``seq`` by one and hand the ops over."""
        if not self._ops:
            return None
        seq = self._state["seq"] + 1
        self._state["seq"] = seq
        batch: OpBatch = {"seq": seq, "ops": self._ops}
        self._ops = []
        return batch

    def snapshot(self) -> ChatSnapshot:
        return copy.deepcopy(self._state)

    def _record(self, op: Op) -> None:
        _apply_op(cast(Dict[str, Any], self._state), op)
        self._ops.append(op)
