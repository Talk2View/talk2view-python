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


class _LocalSkillRequired(TypedDict):
    name: str
    description: str
    enabled: bool
    origin: Literal["code", "dir"]
    label: str


class LocalSkillState(_LocalSkillRequired, total=False):
    path: str
    overridesPlatform: bool


class SkillsState(TypedDict):
    """The skills slice of the snapshot. Owned by ``skills.py``; the controller never edits it."""

    local: List[LocalSkillState]
    platform: Optional[List[Dict[str, Any]]]
    registered: List[str]
    available: bool
    needsSessionReset: bool


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
