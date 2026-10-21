"""The ``memory`` field a turn sends (spec §8.1): the entries, recent conversations and
pre-selected hits, chosen and capped here so the engine never has to trim one.

The engine (``normalise_memory_field``) drops an item that breaks a per-item rule and then
trims each list to its count and its total of code points: entries by ``text``, recent
conversations by ``title`` + ``summary``, recalled hits by ``conversation_title`` +
``text``. Everything built here keeps to the same rules, measured the same way, so a payload
from this SDK comes back whole. Only the wire's field names are sent, dates as
``YYYY-MM-DD``, and an optional field that has no value is left out, never sent as null.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import (
    ENTRIES_CHARS_MAX,
    ENTRY_MAX,
    RECALLED_CHARS_MAX,
    RECALLED_MAX,
    RECALLED_TEXT_MAX,
    RECENT_CHARS_MAX,
    RECENT_MAX,
    SUMMARY_TEXT_MAX,
    ConversationRecord,
    MemoryEntry,
    SearchHit,
)
from .ops import date_problem, id_problem, text_problem, topic_problem
from .search import conversation_title, preselected
from .text import index_tokens

#: How many hits pre-selection searches for. A hit over PRESELECT_FLOOR is rank 4 or better
#: in its list, so the fused top 8 (4 facts and 4 rounds at most) holds every one. An
#: ablation that lowers the floor raises this with it.
PRESELECT_SEARCH_LIMIT = 2 * RECALLED_MAX
#: How many of the most recently active conversations ``recent`` picks from, as some may
#: have no title the engine would keep.
RECENT_WINDOW = 8 * RECENT_MAX


def empty(status: str) -> Dict[str, Any]:
    """The field of a turn that sends no memory: memory paused, or an incognito chat."""
    return {"status": status, "entries": [], "recent": [], "recalled": []}


def keep_within(
    sizes: Sequence[int], ranked: Iterable[int], max_items: int, max_chars: int
) -> List[int]:
    """The positions to keep, in order: the longest run of ``ranked`` (positions, the most
    wanted first) whose count is at most ``max_items`` and whose ``sizes`` add up to at most
    ``max_chars``. The engine trims a list the same way, so a list kept to this is never
    trimmed."""
    kept: List[int] = []
    chars = 0
    for position in ranked:
        chars += sizes[position]
        if len(kept) == max_items or chars > max_chars:
            break
        kept.append(position)
    return sorted(kept)


def entry_items(entries: Sequence[MemoryEntry], today: str) -> List[Dict[str, Any]]:
    """The entries a turn sends, in the store's order: every visible one still valid on
    ``today`` (a ``valid_until`` before it has expired) that the engine would accept, within
    ENTRY_MAX (40) entries and ENTRIES_CHARS_MAX (6,000) code points of text. Over a cap,
    unlocked entries are left out before locked ones, the least recently updated first and,
    on a tie, the later in the store's order, as the engine trims."""
    usable = [
        entry
        for entry in entries
        if not (entry.valid_until is not None and entry.valid_until < today) and _valid_entry(entry)
    ]
    ranked = list(range(len(usable)))  # stable sorts: on a tie, the earlier stays ahead
    ranked.sort(key=lambda position: usable[position].updated_at, reverse=True)
    ranked.sort(key=lambda position: usable[position].locked, reverse=True)
    sizes = [len(entry.text) for entry in usable]
    return [
        _entry_item(usable[position])
        for position in keep_within(sizes, ranked, ENTRY_MAX, ENTRIES_CHARS_MAX)
    ]


def _valid_entry(entry: MemoryEntry) -> bool:
    """Whether the engine accepts the entry as the store holds it."""
    return not (
        id_problem(entry.id)
        or topic_problem(entry.topic)
        or text_problem(entry.text)
        or date_problem(entry.updated_at[:10])
        or date_problem(entry.valid_until)
    )


def _entry_item(entry: MemoryEntry) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": entry.id,
        "topic": entry.topic,
        "text": entry.text,
        "updated_at": entry.updated_at[:10],
    }
    if entry.valid_until is not None:
        item["valid_until"] = entry.valid_until
    return item


def recent_items(conversations: Sequence[ConversationRecord]) -> List[Dict[str, Any]]:
    """The recent conversations a turn sends, newest first, from ``conversations`` (most
    recently active first, incognito ones and the current one left out already), within
    RECENT_MAX (8) and RECENT_CHARS_MAX (3,000) code points of title and summary, the
    least recently active going first. A conversation is named by ``conversation_title``,
    with its summary's text when it has one; one with no name left, as when a forget has
    cleared it, or with an id the engine would refuse, is passed over."""
    items: List[Dict[str, Any]] = []
    for record in conversations:
        item = _recent_item(record)
        if item is not None:
            items.append(item)
    sizes = [len(item["title"]) + len(item["summary"]) for item in items]
    kept = keep_within(sizes, range(len(items)), RECENT_MAX, RECENT_CHARS_MAX)
    return [items[position] for position in kept]


def _recent_item(record: ConversationRecord) -> Optional[Dict[str, Any]]:
    title = conversation_title(record.title, record.summary_title)
    last_active = record.last_active_at[:10]
    if not title or id_problem(record.id) or date_problem(last_active):
        return None
    return {
        "id": record.id,
        "title": title,
        "summary": (record.summary_text or "")[:SUMMARY_TEXT_MAX],
        "last_active_at": last_active,
    }


def recalled_items(
    found: Sequence[Tuple[SearchHit, str]], terms: Sequence[str]
) -> List[Dict[str, Any]]:
    """The pre-selected hits a turn sends, from ``found``: the hits of a search for the
    user's latest message, best first, each with the whole text it was indexed by. Those
    that clear the floor (``search.preselected``), at most RECALLED_MAX (4), within
    RECALLED_CHARS_MAX (4,000) code points of conversation title and text."""
    items = [
        {
            "kind": hit.kind,
            "date": hit.date,
            "conversation_title": hit.conversation,
            "text": hit.text,
        }
        for hit, indexed in found
        if 0 < len(hit.text) <= RECALLED_TEXT_MAX
        and not date_problem(hit.date)
        and preselected(hit.score, index_tokens(indexed), terms)
    ]
    sizes = [len(item["conversation_title"]) + len(item["text"]) for item in items]
    kept = keep_within(sizes, range(len(items)), RECALLED_MAX, RECALLED_CHARS_MAX)
    return [items[position] for position in kept]


__all__ = [
    "PRESELECT_SEARCH_LIMIT",
    "RECENT_WINDOW",
    "empty",
    "entry_items",
    "keep_within",
    "recalled_items",
    "recent_items",
]
