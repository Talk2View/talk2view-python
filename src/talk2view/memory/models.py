"""The memory store's records, topics and caps (spec §7.1).

Every character cap counts Unicode code points (``code_points``), the unit the engine and
every other client's store use too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from .text import code_points

TOPICS = ("identity", "work", "people", "preferences", "ongoing", "instructions")

# ── caps (code points, or counts) ──
ENTRY_MAX = 40
ENTRY_TEXT_MAX = 300
ENTRIES_CHARS_MAX = 6000
FACT_TEXT_MAX = 400
FORGET_TEXT_MIN = 2  # a forget's phrase, once normalised: one letter is in nearly every text
RECENT_MAX = 8
RECENT_CHARS_MAX = 3000
RECALLED_MAX = 4
RECALLED_CHARS_MAX = 4000
RESULT_CHARS_MAX = 8000
SUMMARY_TITLE_MAX = 120
SUMMARY_TEXT_MAX = 600
CONSOLIDATE_FACTS_MAX = 400  # the facts a consolidate request carries
CONSOLIDATE_SUMMARIES_MAX = 30  # and its summaries (the engine's SUMMARIES_MAX)

# ── timing (seconds) ──
UNDO_WINDOW_S = 30
IDLE_EXTRACT_S = 600
CONSOLIDATE_EVERY_S = 7 * 86400
CONSOLIDATE_MIN_CHANGES = 10


@dataclass(frozen=True)
class MemoryEntry:
    """A short statement about the user, sent with every turn. Times are ISO 8601 text;
    ``valid_until`` is a ``YYYY-MM-DD`` date. ``locked`` is set once the user has edited
    the entry by hand: changes the engine proposes never touch a locked entry. ``hidden_at``
    is when the entry was removed or forgotten: it is kept, hidden, through the undo window."""

    id: str
    topic: str
    text: str
    created_at: str
    updated_at: str
    valid_until: Optional[str]
    source: str
    locked: bool
    source_conversation_id: Optional[str]
    hidden_at: Optional[str] = None


@dataclass(frozen=True)
class Fact:
    """A self-contained statement drawn from a conversation, with its dates made absolute;
    found by search. ``superseded_by`` names the newer fact that replaced it. ``hidden_at``
    is when the fact was forgotten: it is kept, hidden, through the undo window."""

    id: str
    text: str
    topic: str
    recorded_at: str
    occurred_at: Optional[str]
    valid_until: Optional[str]
    conversation_id: Optional[str]
    superseded_by: Optional[str]
    superseded_at: Optional[str]
    source: str
    hidden_at: Optional[str] = None


@dataclass(frozen=True)
class ConversationRecord:
    """The store's own record of a conversation (its rounds come from
    ``MemoryStore.rounds``). ``extracted_through`` is the last round extraction has seen;
    an ``incognito`` conversation's rounds are never written."""

    id: str
    title: Optional[str]
    started_at: str
    last_active_at: str
    summary_title: Optional[str]
    summary_text: Optional[str]
    extracted_through: int
    incognito: bool


@dataclass(frozen=True)
class SearchHit:
    """One result of a memory search: a fact, or an excerpt of a round."""

    kind: Literal["fact", "excerpt"]
    date: str
    conversation: str
    text: str
    score: float
    ref: str


@dataclass(frozen=True)
class AppliedBatch:
    """One batch of memory changes as applied: what was applied, what was dropped, and what
    was removed, kept so the batch can be undone. A forget also reaches the store's log of
    the chats: ``cleared_rounds`` counts the rounds it blanked, ``cleared_summaries`` the
    summaries it cleared and ``cleared_titles`` the chat titles it blanked."""

    id: str
    applied: List[Dict[str, Any]]
    dropped: List[str]
    removed_entries: List[MemoryEntry]
    removed_facts: List[Fact]
    cleared_rounds: int = 0
    cleared_summaries: int = 0
    cleared_titles: int = 0


@dataclass(frozen=True)
class ConsolidationSnapshot:
    """What a consolidate request carries, read from the store in one read
    (``MemoryStore.consolidation_snapshot``): the visible entries, locked ones marked; the
    visible facts no newer one superseded, newest first; the summaries of the most recently
    active conversations, newest first; and the ``revision`` they were read at, for
    ``apply_consolidation``'s ``expected_revision``."""

    revision: int
    entries: List[MemoryEntry]
    facts: List[Fact]
    summaries: List[ConversationRecord]


__all__ = [
    "CONSOLIDATE_EVERY_S",
    "CONSOLIDATE_FACTS_MAX",
    "CONSOLIDATE_MIN_CHANGES",
    "CONSOLIDATE_SUMMARIES_MAX",
    "ENTRIES_CHARS_MAX",
    "ENTRY_MAX",
    "ENTRY_TEXT_MAX",
    "FACT_TEXT_MAX",
    "FORGET_TEXT_MIN",
    "IDLE_EXTRACT_S",
    "RECALLED_CHARS_MAX",
    "RECALLED_MAX",
    "RECENT_CHARS_MAX",
    "RECENT_MAX",
    "RESULT_CHARS_MAX",
    "SUMMARY_TEXT_MAX",
    "SUMMARY_TITLE_MAX",
    "TOPICS",
    "UNDO_WINDOW_S",
    "AppliedBatch",
    "ConsolidationSnapshot",
    "ConversationRecord",
    "Fact",
    "MemoryEntry",
    "SearchHit",
    "code_points",
]
