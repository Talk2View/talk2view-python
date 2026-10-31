"""The bodies of the engine's memory calls, built from the store: POST /v1/memory/extract
for a conversation's new rounds or for an import, and POST /v1/memory/consolidate from a
consolidation snapshot. Each keeps within the engine's caps, counted in code points as the
engine counts them, so the engine never refuses one for its size. An optional field with no
value is left out, never sent as null.

The functions that read the store expect the caller to hold whatever keeps it open.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import payload
from .days import day
from .models import ConsolidationSnapshot, ConversationRecord, Fact, MemoryEntry
from .search import conversation_title
from .store import MemoryStore

# An extract request's caps (the engine's MESSAGES_MAX and MESSAGES_CHARS_MAX).
EXTRACT_MESSAGES_MAX = 200
EXTRACT_CHARS_MAX = 200_000
# The messages before the extracted point a request carries as context: all the engine reads.
CONTEXT_MESSAGES = 6
# Each message's text a request carries: a pasted document is cut, so the context and the
# first new round (eight messages) always fit within EXTRACT_CHARS_MAX.
MESSAGE_TEXT_MAX = 20_000
RELATED_FACTS_MAX = 60  # the engine's RELATED_FACTS_MAX
# The searches that find them: one per line of what the user said, at most this many.
RELATED_SEARCHES_MAX = 60
TIMEZONE_MAX = 64  # the engine refuses a longer timezone


def extract_body(
    store: MemoryStore,
    record: ConversationRecord,
    *,
    mode: str,
    today: str,
    timezone: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], int]:
    """The extract request for the conversation's rounds extraction hasn't read, and the
    index of the last round it carries. None for the request when those rounds hold nothing
    the user said: there is nothing to extract.

    ``messages`` holds up to CONTEXT_MESSAGES messages from the rounds extraction has read,
    never one it gave up on (``extract_skipped_through``), then the new rounds', a round at
    a time while they fit the caps: a user message and an assistant message per round, each
    only when it has text. ``extracted_through`` counts the context messages, so the engine
    reads every message after them as new: a count of messages, where the index returned
    counts rounds, as ``mark_extracted`` takes it. Rounds the caps leave out wait for the
    next request."""
    start = record.extracted_through
    rounds = store.rounds(
        record.id, after=max(record.extract_skipped_through, start - CONTEXT_MESSAGES)
    )
    context: List[Dict[str, Any]] = []
    for idx, user_text, reply, at in rounds:
        if idx <= start:
            context += _messages(user_text, reply, at)
    context = context[-CONTEXT_MESSAGES:]
    messages: List[Dict[str, Any]] = []
    count, size = len(context), _text_size(context)
    through = start
    for idx, user_text, reply, at in rounds:
        if idx <= start:
            continue
        more = _messages(user_text, reply, at)
        if count + len(more) > EXTRACT_MESSAGES_MAX or size + _text_size(more) > EXTRACT_CHARS_MAX:
            break
        messages += more
        count, size = count + len(more), size + _text_size(more)
        through = idx
    said = [message["text"] for message in messages if message["role"] == "user"]
    if not said:
        return None, through
    conversation: Dict[str, Any] = {
        "id": record.id,
        "started_at": record.started_at,
        "last_active_at": record.last_active_at,
        "messages": context + messages,
    }
    title = conversation_title(record.title, record.summary_title)
    if title:
        conversation["title"] = title
    body = _body(store, conversation, len(context), said, mode=mode, today=today, timezone=timezone)
    return body, through


def import_body(
    store: MemoryStore,
    conversation_id: str,
    text: str,
    *,
    now: str,
    today: str,
    timezone: Optional[str],
) -> Dict[str, Any]:
    """The extract request for an import: ``text``, the memory the user exported from
    another assistant, as the one user message of a conversation of its own (import mode
    takes it as the user's own words), with the store's "Include sensitive topics" switch."""
    conversation = {
        "id": conversation_id,
        "started_at": now,
        "last_active_at": now,
        "messages": [{"role": "user", "text": text, "at": now}],
    }
    return _body(store, conversation, 0, [text], mode="import", today=today, timezone=timezone)


def consolidate_body(
    snapshot: ConsolidationSnapshot, *, today: str, timezone: Optional[str]
) -> Dict[str, Any]:
    """The consolidate request for ``snapshot``, the store's ``consolidation_snapshot``:
    every entry it carries, with its ``locked`` flag and the day it was updated in
    ``timezone``; its facts and its conversations' summaries; and the user's ``today``."""
    body: Dict[str, Any] = {
        "entries": [_consolidated_entry(entry, timezone) for entry in snapshot.entries],
        "facts": [fact_item(fact) for fact in snapshot.facts],
        "summaries": [_summary_item(record) for record in snapshot.summaries],
        "today": today,
    }
    if _sendable_zone(timezone):
        body["timezone"] = timezone
    return body


def fact_item(fact: Fact) -> Dict[str, Any]:
    """A fact as the engine's memory calls take one (``FactIn``)."""
    item: Dict[str, Any] = {
        "id": fact.id,
        "text": fact.text,
        "topic": fact.topic,
        "recorded_at": fact.recorded_at,
    }
    if fact.occurred_at is not None:
        item["occurred_at"] = fact.occurred_at
    if fact.valid_until is not None:
        item["valid_until"] = fact.valid_until
    return item


def _body(
    store: MemoryStore,
    conversation: Dict[str, Any],
    extracted_through: int,
    said: List[str],
    *,
    mode: str,
    today: str,
    timezone: Optional[str],
) -> Dict[str, Any]:
    """An extract request around ``conversation``: with the entries a turn would send, the
    facts related to ``said`` and the user's timezone, and in import mode the switch."""
    body: Dict[str, Any] = {
        "conversation": conversation,
        "extracted_through": extracted_through,
        "entries": payload.entry_items(store.entries(), today, timezone),
        "related_facts": _related_facts(store, said, timezone),
        "mode": mode,
    }
    if _sendable_zone(timezone):
        body["timezone"] = timezone
    if mode == "import":
        body["include_sensitive"] = store.include_sensitive
    return body


def _related_facts(
    store: MemoryStore, said: Sequence[str], timezone: Optional[str]
) -> List[Dict[str, Any]]:
    """The facts extraction compares the conversation with, so it can tell a new
    fact from one it supersedes: those a search for each line of ``said`` finds, in order,
    each once, at most RELATED_FACTS_MAX. Never a superseded one."""
    lines = [line for text in said for line in text.splitlines() if line.strip()]
    found: Dict[str, None] = {}  # fact ids, in the order found
    for line in lines[:RELATED_SEARCHES_MAX]:
        for hit in store.search(line, limit=RELATED_FACTS_MAX, timezone=timezone):
            if hit.kind == "fact":
                found.setdefault(hit.ref)
        if len(found) >= RELATED_FACTS_MAX:
            break
    if not found:
        return []
    facts = {fact.id: fact for fact in store.facts()}
    return [fact_item(facts[ref]) for ref in found if ref in facts][:RELATED_FACTS_MAX]


def _messages(user_text: str, reply: str, at: str) -> List[Dict[str, Any]]:
    """A round as extract messages: what the user said, then the reply, each only when it
    has text, each cut to MESSAGE_TEXT_MAX code points."""
    return [
        {"role": role, "text": text[:MESSAGE_TEXT_MAX], "at": at}
        for role, text in (("user", user_text), ("assistant", reply))
        if text
    ]


def _text_size(messages: List[Dict[str, Any]]) -> int:
    return sum(len(message["text"]) for message in messages)


def _consolidated_entry(entry: MemoryEntry, timezone: Optional[str]) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": entry.id,
        "topic": entry.topic,
        "text": entry.text,
        "updated_at": day(entry.updated_at, timezone),
        "locked": entry.locked,
    }
    if entry.valid_until is not None:
        item["valid_until"] = entry.valid_until
    return item


def _summary_item(record: ConversationRecord) -> Dict[str, Any]:
    return {
        "id": record.id,
        "title": record.summary_title or "",
        "text": record.summary_text or "",
        "last_active_at": record.last_active_at,
    }


def _sendable_zone(timezone: Optional[str]) -> bool:
    return timezone is not None and 0 < len(timezone) <= TIMEZONE_MAX
