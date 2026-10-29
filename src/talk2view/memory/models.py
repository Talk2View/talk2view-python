"""The memory store's records, topics and caps, which every client's store shares.

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
RECALLED_TEXT_MAX = 1000
RECALLED_CHARS_MAX = 4000
RESULT_CHARS_MAX = 12000
RESULT_LIMIT_MIN = 8  # the fewest results a search_memory call gets, whatever it asks
RESULT_LIMIT_DEFAULT = 12  # what it gets when it names no limit
RESULT_LIMIT_MAX = 20  # the most results a search_memory call may ask for
SUMMARY_TITLE_MAX = 120
SUMMARY_TEXT_MAX = 600
CONSOLIDATE_ENTRIES_MAX = 200  # the entries a consolidate request carries (the engine's)
CONSOLIDATE_FACTS_MAX = 400  # its facts
CONSOLIDATE_SUMMARIES_MAX = 30  # and its summaries (the engine's SUMMARIES_MAX)

# ── timing (seconds) ──
UNDO_WINDOW_S = 30
IDLE_EXTRACT_S = 600
CONSOLIDATE_EVERY_S = 7 * 86400
CONSOLIDATE_MIN_CHANGES = 10
# After a failed extraction that may be the request's own doing, the conversation's next
# automatic try waits EXTRACT_RETRY_S, four times as long after each further failure in a
# row, at most EXTRACT_RETRY_MAX_S (1, 4, 16, then 64 hours). From the EXTRACT_FAILURES_MAX-th
# failure in a row, extraction may skip the rounds (T2VMemory.run_due says when).
EXTRACT_RETRY_S = 3600
EXTRACT_RETRY_MAX_S = 64 * 3600
EXTRACT_FAILURES_MAX = 5


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
    an ``incognito`` conversation's rounds are never written.

    ``extract_failures`` counts the extractions in a row that failed in a way that may be the
    request's own doing, since extraction last moved on; ``extract_failed_at`` is when the
    last of them failed, and ``extract_failed_through`` the last round the first of them
    carried. ``extract_skipped_through`` is the last round extraction gave up on: no extract
    request carries a round up to it again, not even as context."""

    id: str
    title: Optional[str]
    started_at: str
    last_active_at: str
    summary_title: Optional[str]
    summary_text: Optional[str]
    extracted_through: int
    incognito: bool
    extract_failures: int = 0
    extract_failed_at: Optional[str] = None
    extract_failed_through: Optional[int] = None
    extract_skipped_through: int = 0


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
    (``MemoryStore.consolidation_snapshot``): the visible entries, locked ones first, then
    the most recently updated, up to a cap; the visible facts no newer one superseded,
    newest first; the summaries of the most recently active conversations, newest first;
    and the ``revision`` they were read at. ``apply_consolidation`` takes the snapshot
    itself: it checks the revision, and changes only the entries the snapshot carried."""

    revision: int
    entries: List[MemoryEntry]
    facts: List[Fact]
    summaries: List[ConversationRecord]


__all__ = [
    "CONSOLIDATE_ENTRIES_MAX",
    "CONSOLIDATE_EVERY_S",
    "CONSOLIDATE_FACTS_MAX",
    "CONSOLIDATE_MIN_CHANGES",
    "CONSOLIDATE_SUMMARIES_MAX",
    "ENTRIES_CHARS_MAX",
    "ENTRY_MAX",
    "ENTRY_TEXT_MAX",
    "EXTRACT_FAILURES_MAX",
    "EXTRACT_RETRY_MAX_S",
    "EXTRACT_RETRY_S",
    "FACT_TEXT_MAX",
    "FORGET_TEXT_MIN",
    "IDLE_EXTRACT_S",
    "RECALLED_CHARS_MAX",
    "RECALLED_MAX",
    "RECALLED_TEXT_MAX",
    "RECENT_CHARS_MAX",
    "RECENT_MAX",
    "RESULT_CHARS_MAX",
    "RESULT_LIMIT_DEFAULT",
    "RESULT_LIMIT_MAX",
    "RESULT_LIMIT_MIN",
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
