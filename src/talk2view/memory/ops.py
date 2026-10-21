"""The rules for applying memory entry ops (spec §7.3).

``apply_entry_ops`` is pure: it reads no clock, store or other state, so the same entries,
ops, ``now`` and ids always give the same result. It is the reference every client's store
mirrors, and the contract fixtures pin it.

The engine checks an op before it reaches the device (``validate_ops`` for the
``update_memory`` tool, and the extraction call's own check), so these rules accept every
op it passes. They drop the rest, and the ops the device's own state rules out (a missing
or locked entry, a duplicate), each with a short rule name. A rule name never quotes the
op's text, so it can be logged.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .models import ENTRY_TEXT_MAX, FORGET_TEXT_MIN, TOPICS, MemoryEntry
from .text import normalise_dedupe

# The engine's patterns, applied with fullmatch. re.ASCII keeps \d to 0-9.
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)

_OPS = ("add", "update", "remove", "forget")


def id_problem(value: Any) -> Optional[str]:
    """Why ``value`` can't be an entry id, or None when it can."""
    if isinstance(value, str) and ID_RE.fullmatch(value):
        return None
    return "bad id"


def topic_problem(value: Any) -> Optional[str]:
    """Why ``value`` can't be a topic, or None when it is one of TOPICS or absent."""
    return None if value is None or value in TOPICS else "unknown topic"


def date_problem(value: Any) -> Optional[str]:
    """Why ``value`` can't be a date, or None when it is YYYY-MM-DD or absent."""
    if value is None or (isinstance(value, str) and DATE_RE.fullmatch(value)):
        return None
    return "bad date"


def text_problem(value: Any, cap: int = ENTRY_TEXT_MAX) -> Optional[str]:
    """Why ``value`` can't be the text, or None when it is 1 to ``cap`` code points or
    absent."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        return "no text"
    if len(value) > cap:
        return "text over {} code points".format(cap)
    return None


def apply_entry_ops(
    entries: Sequence[MemoryEntry],
    ops: Sequence[Dict[str, Any]],
    *,
    now: str,
    next_id: Callable[[], str],
    source: str = "said",
) -> Tuple[List[MemoryEntry], List[Dict[str, Any]], List[str]]:
    """Apply ``ops`` to ``entries`` in order. Returns the entries afterwards, the ops as
    applied, and a rule name for what was dropped: one for each op dropped, and "locked" for
    each locked entry a forget passed by.

    - ``add`` needs a topic and text, and may set ``valid_until``. It is dropped as a
      "duplicate" when a visible entry has the same ``normalise_dedupe`` text, an entry
      added earlier in the batch included. Otherwise it becomes a new entry, last in the
      list, with ``next_id()``, ``created_at`` and ``updated_at`` set to ``now``, and
      ``source``. Its record carries the id it was given.
    - ``update`` sets the topic, text or ``valid_until`` it gives, and ``updated_at``. It
      "changes nothing" when it gives none of them, or only the values the entry has.
    - ``remove`` hides the entry: it stays in the list with ``hidden_at`` set to ``now``.
    - ``forget`` hides every unlocked visible entry whose ``normalise_dedupe`` text holds
      its own. That must be at least FORGET_TEXT_MIN (2) code points ("forget text too
      short"): one letter is in nearly every text. It is applied even when nothing
      matches; its record lists the ids it hid (``entry_ids``).

    ``update`` and ``remove`` need the id of a visible entry ("missing" otherwise) that is
    not locked: changes the engine proposes never touch a locked entry. Text is at most
    ENTRY_TEXT_MAX (300) code points, a topic one of TOPICS, a date YYYY-MM-DD. An entry
    passed in already hidden is carried through as it is, and no op can see it. Fields an
    op doesn't use are ignored. ``next_id`` is called once for each add applied.

    The arguments are not changed. ``now`` is the timestamp the store writes, and the
    store hands out ids that are not in use.
    """
    state = list(entries)
    applied: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in _OPS:
            dropped.append("not an op")
        elif op["op"] == "add":
            dropped += _add(state, op, applied, now=now, next_id=next_id, source=source)
        elif op["op"] == "update":
            dropped += _update(state, op, applied, now=now)
        elif op["op"] == "remove":
            dropped += _remove(state, op, applied, now=now)
        else:
            dropped += _forget(state, op, applied, now=now)
    return state, applied, dropped


# Each op's rules run in the order the engine checks them, then the device's own. Each
# returns the rule names of what it dropped: the op's own when it drops the op; otherwise
# it changes `state`, records the op in `applied`, and returns none, or for a forget one
# "locked" for each locked entry it passed by. An op's fields are untyped JSON (Any) until
# its rules have checked them.


def _add(
    state: List[MemoryEntry],
    op: Dict[str, Any],
    applied: List[Dict[str, Any]],
    *,
    now: str,
    next_id: Callable[[], str],
    source: str,
) -> List[str]:
    topic: Any = op.get("topic")
    text: Any = op.get("text")
    valid_until: Any = op.get("valid_until")
    reason = (
        ("no topic" if topic is None else None)
        or ("no text" if text is None else None)
        or topic_problem(topic)
        or date_problem(valid_until)
        or text_problem(text)
    )
    if reason is not None:
        return [reason]
    key = normalise_dedupe(text)
    if any(entry.hidden_at is None and normalise_dedupe(entry.text) == key for entry in state):
        return ["duplicate"]
    entry = MemoryEntry(
        id=next_id(),
        topic=topic,
        text=text,
        created_at=now,
        updated_at=now,
        valid_until=valid_until,
        source=source,
        locked=False,
        source_conversation_id=None,
    )
    state.append(entry)
    applied.append(_record("add", id=entry.id, topic=topic, text=text, valid_until=valid_until))
    return []


def _update(
    state: List[MemoryEntry], op: Dict[str, Any], applied: List[Dict[str, Any]], *, now: str
) -> List[str]:
    entry_id: Any = op.get("id")
    topic: Any = op.get("topic")
    text: Any = op.get("text")
    valid_until: Any = op.get("valid_until")
    reason = id_problem(entry_id)
    if reason is not None:
        return [reason]
    position = _visible(state, entry_id)
    if position is None:
        return ["missing"]
    reason = (
        ("changes nothing" if topic is None and text is None and valid_until is None else None)
        or topic_problem(topic)
        or date_problem(valid_until)
        or text_problem(text)
    )
    if reason is not None:
        return [reason]
    entry = state[position]
    if entry.locked:
        return ["locked"]
    changed = replace(
        entry,
        topic=entry.topic if topic is None else topic,
        text=entry.text if text is None else text,
        valid_until=entry.valid_until if valid_until is None else valid_until,
    )
    if changed == entry:
        return ["changes nothing"]
    state[position] = replace(changed, updated_at=now)
    applied.append(_record("update", id=entry_id, topic=topic, text=text, valid_until=valid_until))
    return []


def _remove(
    state: List[MemoryEntry], op: Dict[str, Any], applied: List[Dict[str, Any]], *, now: str
) -> List[str]:
    entry_id: Any = op.get("id")
    reason = id_problem(entry_id)
    if reason is not None:
        return [reason]
    position = _visible(state, entry_id)
    if position is None:
        return ["missing"]
    if state[position].locked:
        return ["locked"]
    state[position] = replace(state[position], hidden_at=now)
    applied.append(_record("remove", id=entry_id))
    return []


def _forget(
    state: List[MemoryEntry], op: Dict[str, Any], applied: List[Dict[str, Any]], *, now: str
) -> List[str]:
    text: Any = op.get("text")
    reason = ("no text" if text is None else None) or text_problem(text)
    if reason is not None:
        return [reason]
    key = normalise_dedupe(text)
    if len(key) < FORGET_TEXT_MIN:
        return ["forget text too short"]
    hidden: List[str] = []
    passed: List[str] = []
    for position, entry in enumerate(state):
        if entry.hidden_at is not None or key not in normalise_dedupe(entry.text):
            continue
        if entry.locked:
            passed.append("locked")
        else:
            state[position] = replace(entry, hidden_at=now)
            hidden.append(entry.id)
    applied.append({"op": "forget", "text": text, "entry_ids": hidden})
    return passed


def _visible(state: List[MemoryEntry], entry_id: str) -> Optional[int]:
    """The position of the visible (not hidden) entry ``entry_id``, or None."""
    for position, entry in enumerate(state):
        if entry.id == entry_id and entry.hidden_at is None:
            return position
    return None


def _record(op: str, **fields: Optional[str]) -> Dict[str, Any]:
    """An applied op as the wire writes ops: the fields that are set, in order."""
    record: Dict[str, Any] = {"op": op}
    record.update((name, value) for name, value in fields.items() if value is not None)
    return record


__all__ = [
    "DATE_RE",
    "ID_RE",
    "apply_entry_ops",
    "date_problem",
    "id_problem",
    "text_problem",
    "topic_problem",
]
