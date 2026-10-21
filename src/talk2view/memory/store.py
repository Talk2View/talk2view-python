"""The on-device memory store: one SQLite file per user, holding the memory entries, the facts
and the store's own log of each conversation's rounds (spec §7.1).

Three backends, chosen when the store opens (``MemoryStore.backend`` says which):

- ``sqlite3``: the standard library's, when it was built with FTS5;
- ``apsw``: when it was not and apsw is installed (``pip install 'talk2view[memory]'``,
  Python 3.11+), since apsw bundles a SQLite with FTS5;
- ``scan``: the standard library's ``sqlite3`` with the search index in plain tables, which
  search scans instead of matching. Memory works everywhere; it never crashes for want of
  FTS5.

A file can move between them: ``meta`` records the kind of index the file holds, ``fts5`` or
``scan``, and a store that opens a file indexed the other way rebuilds the index for its own
kind from the facts and rounds.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Union,
)

from ..storage import _DIR_MODE, _FILE_MODE, _POSIX_PERMISSIONS
from . import days, payload
from .models import (
    CONSOLIDATE_EVERY_S,
    CONSOLIDATE_FACTS_MAX,
    CONSOLIDATE_MIN_CHANGES,
    CONSOLIDATE_SUMMARIES_MAX,
    ENTRIES_CHARS_MAX,
    ENTRY_MAX,
    FACT_TEXT_MAX,
    RESULT_LIMIT_MAX,
    SUMMARY_TEXT_MAX,
    SUMMARY_TITLE_MAX,
    TOPICS,
    UNDO_WINDOW_S,
    AppliedBatch,
    ConsolidationSnapshot,
    ConversationRecord,
    Fact,
    MemoryEntry,
    SearchHit,
)
from .ops import apply_entry_ops, date_problem, text_problem, topic_problem
from .search import (
    best_first,
    bm25_scan,
    conversation_title,
    fact_hit,
    fts_query,
    fuse,
    query_terms,
    result_json,
    round_hit,
    round_ref,
)
from .text import code_points, index_tokens, normalise_dedupe, normalise_for_index

logger = logging.getLogger("talk2view.memory")

Backend = Literal["sqlite3", "apsw", "scan"]
_BACKENDS: Tuple[Backend, ...] = ("sqlite3", "apsw", "scan")


def utc_now() -> datetime:
    """The store's default clock."""
    return datetime.now(timezone.utc)


def _utc(moment: datetime) -> datetime:
    """``moment`` in UTC. A naive datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _timestamp(moment: datetime) -> str:
    """``moment`` as the store writes every time: ISO 8601 in UTC to the second, such as
    ``2026-10-21T10:00:00Z``. The width never varies, so text order is time order. A naive
    datetime is taken as UTC."""
    return _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── backends ──


class _Db(Protocol):
    """A connection as the store uses it. Each backend adapts its driver to these calls."""

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]: ...
    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None: ...
    def close(self) -> None: ...


class _Sqlite3Db:
    """The standard library's ``sqlite3`` in autocommit mode (``isolation_level=None``): the
    store issues ``BEGIN IMMEDIATE`` and ``COMMIT`` itself, as apsw needs, so the two
    behave alike."""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        return self._conn.execute(sql, params).fetchall()

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, rows)

    def close(self) -> None:
        self._conn.close()


class _ApswDb:
    """apsw: never begins a transaction on its own."""

    def __init__(self, apsw: ModuleType, path: str) -> None:
        self._conn = apsw.Connection(path)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        return list(self._conn.execute(sql, params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, rows)

    def close(self) -> None:
        self._conn.close()


def _fts5_available() -> bool:
    """True when the standard library's ``sqlite3`` can create an FTS5 table."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return True


def _import_apsw() -> Optional[ModuleType]:
    """The ``apsw`` module, or ``None`` when it is not installed (it needs Python 3.11+).
    Imported by name and typed as a plain module, so mypy checks this file the same
    whether or not apsw is there."""
    try:
        return importlib.import_module("apsw")
    except ImportError:
        return None


def _choose_backend() -> Backend:
    """``sqlite3`` when it has FTS5, else ``apsw`` when installed, else ``scan``."""
    if _fts5_available():
        return "sqlite3"
    if _import_apsw() is not None:
        return "apsw"
    return "scan"


# ── schema (version 1): times are ISO 8601 text, dates YYYY-MM-DD ──
# Every primary key is NOT NULL: SQLite otherwise lets a text key hold NULL.

_TABLES = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS entries (
        id TEXT PRIMARY KEY NOT NULL,
        topic TEXT NOT NULL,
        text TEXT NOT NULL,
        norm_text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        valid_until TEXT,
        source TEXT NOT NULL,
        locked INTEGER NOT NULL DEFAULT 0,
        source_conversation_id TEXT,
        hidden_at TEXT,
        hidden_batch TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS facts (
        id TEXT PRIMARY KEY NOT NULL,
        text TEXT NOT NULL,
        topic TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        occurred_at TEXT,
        valid_until TEXT,
        conversation_id TEXT,
        superseded_by TEXT,
        superseded_at TEXT,
        source TEXT NOT NULL,
        hidden_at TEXT,
        hidden_batch TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY NOT NULL,
        title TEXT,
        started_at TEXT NOT NULL,
        last_active_at TEXT NOT NULL,
        summary_title TEXT,
        summary_text TEXT,
        extracted_through INTEGER NOT NULL DEFAULT 0,
        incognito INTEGER NOT NULL DEFAULT 0,
        extracting INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS rounds (
        conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
        idx INTEGER NOT NULL,
        user_text TEXT NOT NULL,
        assistant_text TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (conversation_id, idx)
    )""",
    """CREATE TABLE IF NOT EXISTS undo_batches (
        id TEXT PRIMARY KEY NOT NULL,
        applied_at TEXT NOT NULL,
        payload TEXT NOT NULL
    )""",
)

_META_DEFAULTS = (
    ("schema_version", "1"),
    ("paused", "0"),
    ("include_sensitive", "0"),
    ("changes_since_consolidation", "0"),
    ("revision", "0"),
)

# The search index, kept by the store (no triggers): one row per fact and per round with
# text, whose ``seg_text`` is ``normalise_for_index`` of the fact's text (``_fact_text``), or
# of the round's user message and reply (``_round_text``). A round a forget blanked has no
# row. The sqlite3 and apsw backends keep it in the FTS5 tables ``facts_index`` and
# ``rounds_index``; scan keeps the same columns in the plain tables ``facts_scan`` and
# ``rounds_scan``. (The plan's data model gives scan the ``*_index`` names as well; the
# schema is internal to the SDK, and names of its own keep scan from ever naming an FTS5
# table, which a SQLite without FTS5 cannot open.) ``meta``'s ``index_kind`` records which
# kind the file holds.


@dataclass(frozen=True)
class _Index:
    """One kind of search index, as literal statements: no SQL is assembled at run time."""

    kind: str  # meta's index_kind
    create: Tuple[str, ...]
    clear: Tuple[str, ...]
    insert_fact: str
    insert_round: str
    delete_fact: str
    delete_round: str
    delete_rounds: str  # of one conversation
    optimize: Tuple[str, ...]  # run by _scrub
    drop_other: Tuple[str, ...]  # run once this kind's index is rebuilt


_FTS5_INDEX = _Index(
    kind="fts5",
    create=(
        "CREATE VIRTUAL TABLE IF NOT EXISTS facts_index USING fts5("
        "fact_id UNINDEXED, seg_text, tokenize='unicode61 remove_diacritics 2')",
        "CREATE VIRTUAL TABLE IF NOT EXISTS rounds_index USING fts5("
        "conversation_id UNINDEXED, idx UNINDEXED, seg_text, "
        "tokenize='unicode61 remove_diacritics 2')",
    ),
    clear=("DELETE FROM facts_index", "DELETE FROM rounds_index"),
    insert_fact="INSERT INTO facts_index (fact_id, seg_text) VALUES (?, ?)",
    insert_round="INSERT INTO rounds_index (conversation_id, idx, seg_text) VALUES (?, ?, ?)",
    delete_fact="DELETE FROM facts_index WHERE fact_id = ?",
    delete_round="DELETE FROM rounds_index WHERE conversation_id = ? AND idx = ?",
    delete_rounds="DELETE FROM rounds_index WHERE conversation_id = ?",
    # Merge each index into one segment: the merge leaves out the deleted rows' tokens.
    optimize=(
        "INSERT INTO facts_index (facts_index) VALUES ('optimize')",
        "INSERT INTO rounds_index (rounds_index) VALUES ('optimize')",
    ),
    drop_other=("DROP TABLE IF EXISTS facts_scan", "DROP TABLE IF EXISTS rounds_scan"),
)

_SCAN_INDEX = _Index(
    kind="scan",
    create=(
        "CREATE TABLE IF NOT EXISTS facts_scan (fact_id TEXT NOT NULL, seg_text TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS rounds_scan ("
        "conversation_id TEXT NOT NULL, idx INTEGER NOT NULL, seg_text TEXT NOT NULL)",
    ),
    clear=("DELETE FROM facts_scan", "DELETE FROM rounds_scan"),
    insert_fact="INSERT INTO facts_scan (fact_id, seg_text) VALUES (?, ?)",
    insert_round="INSERT INTO rounds_scan (conversation_id, idx, seg_text) VALUES (?, ?, ?)",
    delete_fact="DELETE FROM facts_scan WHERE fact_id = ?",
    delete_round="DELETE FROM rounds_scan WHERE conversation_id = ? AND idx = ?",
    delete_rounds="DELETE FROM rounds_scan WHERE conversation_id = ?",
    optimize=(),  # plain tables keep nothing of a deleted row
    # Scan cannot open FTS5 tables, so it leaves them. They go stale until an FTS5 store's
    # rebuild clears and refills them.
    drop_other=(),
)


def _fact_text(text: str) -> str:
    """A fact's ``seg_text``."""
    return normalise_for_index(text)


def _round_text(user_text: str, assistant_text: str) -> str:
    """A round's ``seg_text``."""
    return normalise_for_index(user_text + "\n" + assistant_text)


def _token_line(text: str) -> str:
    """``text``'s tokens (``index_tokens``) between single spaces, with a space at each end,
    as ``_phrase`` searches them. A token never holds a space, so the spaces mark exactly
    where tokens begin and end."""
    return " " + " ".join(index_tokens(text)) + " "


_NOWHERE = re.compile("(?!)")  # a pattern that matches no text


def _phrase(text: str) -> re.Pattern[str]:
    """What finds a forget's phrase in the log (R57): its tokens in order, whole, in a
    ``_token_line``, the last allowed an "s" or "es" ("cat" finds "cats", "box" "boxes"),
    so "ex" never finds "for example". A phrase with no tokens finds nothing, rather than
    nearly every text; apply_entry_ops drops one as too vague anyway."""
    tokens = index_tokens(text)
    if not tokens:
        return _NOWHERE
    return re.compile(" " + re.escape(" ".join(tokens)) + "(?:e?s)? ")


def _create_private_file(path: Path) -> None:
    """Create the store's file 0600 before SQLite opens it, so it never exists with looser
    permissions; SQLite gives its ``-wal`` and ``-shm`` files the same mode. A directory this
    call creates is 0700. An existing file is tightened to 0600; an existing directory is
    left as it was found. Modes are set on POSIX only, as ``FileStorage`` sets them."""
    directory = path.parent
    try:
        directory.mkdir(mode=_DIR_MODE, parents=True)
        created = True
    except FileExistsError:
        created = False
    if _POSIX_PERMISSIONS and created:
        os.chmod(directory, _DIR_MODE)  # umask can only strip bits from ``mode``; pin 0700
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, _FILE_MODE))
    if _POSIX_PERMISSIONS:
        os.chmod(path, _FILE_MODE)


def _entry_from_row(row: Sequence[Any]) -> MemoryEntry:
    return MemoryEntry(
        id=row[0],
        topic=row[1],
        text=row[2],
        created_at=row[3],
        updated_at=row[4],
        valid_until=row[5],
        source=row[6],
        locked=bool(row[7]),
        source_conversation_id=row[8],
        hidden_at=row[9],
    )


def _fact_from_row(row: Sequence[Any]) -> Fact:
    return Fact(
        id=row[0],
        text=row[1],
        topic=row[2],
        recorded_at=row[3],
        occurred_at=row[4],
        valid_until=row[5],
        conversation_id=row[6],
        superseded_by=row[7],
        superseded_at=row[8],
        source=row[9],
        hidden_at=row[10],
    )


def _conversation_from_row(row: Sequence[Any]) -> ConversationRecord:
    return ConversationRecord(
        id=row[0],
        title=row[1],
        started_at=row[2],
        last_active_at=row[3],
        summary_title=row[4],
        summary_text=row[5],
        extracted_through=row[6],
        incognito=bool(row[7]),
    )


# An entry's whole row, as undo keeps it (its "image"): the ten columns _entry_from_row
# reads, then norm_text and hidden_batch.
_SELECT_ENTRY_IMAGE = (
    "SELECT id, topic, text, created_at, updated_at, valid_until, source, locked,"
    " source_conversation_id, hidden_at, norm_text, hidden_batch FROM entries WHERE id = ?"
)
_SELECT_VISIBLE_ENTRY_IMAGES = (
    "SELECT id, topic, text, created_at, updated_at, valid_until, source, locked,"
    " source_conversation_id, hidden_at, norm_text, hidden_batch FROM entries"
    " WHERE hidden_at IS NULL ORDER BY rowid"
)
# Takes an image without its id, then the id.
_UPDATE_ENTRY_IMAGE = (
    "UPDATE entries SET topic = ?, text = ?, created_at = ?, updated_at = ?, valid_until = ?,"
    " source = ?, locked = ?, source_conversation_id = ?, hidden_at = ?, norm_text = ?,"
    " hidden_batch = ? WHERE id = ?"
)
_SELECT_VISIBLE_FACTS = (
    "SELECT id, text, topic, recorded_at, occurred_at, valid_until, conversation_id,"
    " superseded_by, superseded_at, source, hidden_at FROM facts"
    " WHERE hidden_at IS NULL ORDER BY rowid"
)
# What a consolidate request carries (consolidation_snapshot): the newest visible facts no
# other superseded, and the newest summaries, never an incognito conversation's. Each takes
# a LIMIT.
_SELECT_NEWEST_FACTS = (
    "SELECT id, text, topic, recorded_at, occurred_at, valid_until, conversation_id,"
    " superseded_by, superseded_at, source, hidden_at FROM facts"
    " WHERE hidden_at IS NULL AND superseded_by IS NULL"
    " ORDER BY recorded_at DESC, rowid DESC LIMIT ?"
)
_SELECT_NEWEST_SUMMARIES = (
    "SELECT id, title, started_at, last_active_at, summary_title, summary_text,"
    " extracted_through, incognito FROM conversations"
    " WHERE incognito = 0 AND (summary_title IS NOT NULL OR summary_text IS NOT NULL)"
    " ORDER BY last_active_at DESC, rowid DESC LIMIT ?"
)
# The rounds, summaries and chat titles a forget can clear, as undo keeps them: a round as
# [conversation_id, idx, user_text, assistant_text], a summary as [id, title, text], a
# title as [id, title].
_SELECT_ROUNDS_WITH_TEXT = (
    "SELECT conversation_id, idx, user_text, assistant_text FROM rounds"
    " WHERE user_text != '' OR assistant_text != '' ORDER BY rowid"
)
_SELECT_SUMMARIES = (
    "SELECT id, summary_title, summary_text FROM conversations"
    " WHERE summary_title IS NOT NULL OR summary_text IS NOT NULL ORDER BY rowid"
)
_SELECT_TITLES = "SELECT id, title FROM conversations WHERE title != '' ORDER BY rowid"

# Search's reads (search.py ranks and fuses). A search lists the facts and the rounds that
# match, each list best first: bm25() ascending (FTS5's sign: lower is better), then the
# newer, then the later row. It lists only visible facts, superseded ones only when asked,
# and only rounds with text from conversations that aren't incognito. A fact's valid_until
# never leaves it out (R65): facts stay true as history. A date filter keeps what was
# recorded or said at or after one time and before another (days.bounds: the starts of the
# user's first day and of the day after the last), and the conversation to leave out, when
# there is one, takes its rounds and the facts drawn from it along. The FTS5 lists take the
# query, (for facts, whether superseded ones count,) the two times, the conversation and a
# LIMIT.
_FTS_FACTS = (
    "SELECT f.id, f.recorded_at FROM facts_index JOIN facts AS f ON f.id = facts_index.fact_id"
    " WHERE facts_index MATCH ?1 AND f.hidden_at IS NULL AND (?2 OR f.superseded_by IS NULL)"
    " AND (?3 IS NULL OR f.recorded_at >= ?3) AND (?4 IS NULL OR f.recorded_at < ?4)"
    " AND (?5 IS NULL OR f.conversation_id IS NOT ?5)"
    " ORDER BY bm25(facts_index), f.recorded_at DESC, f.rowid DESC LIMIT ?6"
)
_FTS_ROUNDS = (
    "SELECT r.conversation_id, r.idx, r.at FROM rounds_index"
    " JOIN rounds AS r ON r.conversation_id = rounds_index.conversation_id"
    " AND r.idx = rounds_index.idx"
    " JOIN conversations AS c ON c.id = r.conversation_id"
    " WHERE rounds_index MATCH ?1 AND c.incognito = 0"
    " AND (r.user_text != '' OR r.assistant_text != '')"
    " AND (?2 IS NULL OR r.at >= ?2) AND (?3 IS NULL OR r.at < ?3)"
    " AND r.conversation_id IS NOT ?4"
    " ORDER BY bm25(rounds_index), r.at DESC, r.rowid DESC LIMIT ?5"
)
# The scan backend reads every index row, since bm25_scan measures the whole table as FTS5
# does, each with its time, its row and whether the same filters list it (and, for a fact,
# whether it is superseded). They take the two times and the conversation to leave out.
_SCAN_FACTS = (
    "SELECT fs.fact_id, fs.seg_text, f.recorded_at, f.rowid,"
    " f.id IS NOT NULL AND f.hidden_at IS NULL"
    " AND (?1 IS NULL OR f.recorded_at >= ?1) AND (?2 IS NULL OR f.recorded_at < ?2)"
    " AND (?3 IS NULL OR f.conversation_id IS NOT ?3),"
    " f.superseded_by IS NOT NULL"
    " FROM facts_scan AS fs LEFT JOIN facts AS f ON f.id = fs.fact_id"
)
_SCAN_ROUNDS = (
    "SELECT rs.conversation_id, rs.idx, rs.seg_text, r.at, r.rowid,"
    " r.rowid IS NOT NULL AND c.incognito = 0"
    " AND (r.user_text != '' OR r.assistant_text != '')"
    " AND (?1 IS NULL OR r.at >= ?1) AND (?2 IS NULL OR r.at < ?2)"
    " AND r.conversation_id IS NOT ?3"
    " FROM rounds_scan AS rs"
    " LEFT JOIN rounds AS r ON r.conversation_id = rs.conversation_id AND r.idx = rs.idx"
    " LEFT JOIN conversations AS c ON c.id = r.conversation_id"
)
# A hit's text, time and conversation names (and a fact's valid_until). A fact whose
# conversation has since turned incognito takes no name from it.
_FACT_HIT = (
    "SELECT f.text, f.recorded_at, f.valid_until, c.title, c.summary_title, c.incognito"
    " FROM facts AS f LEFT JOIN conversations AS c ON c.id = f.conversation_id WHERE f.id = ?"
)
_ROUND_HIT = (
    "SELECT r.user_text, r.assistant_text, r.at, c.title, c.summary_title FROM rounds AS r"
    " JOIN conversations AS c ON c.id = r.conversation_id"
    " WHERE r.conversation_id = ? AND r.idx = ?"
)

# Where a change the engine proposed came from. An entry's source can also be "edited",
# which only the user's own edit (edit_entry) sets, as it locks the entry.
_SOURCES = ("said", "inferred", "imported")

# Each kind of id the store hands out: its prefix, the meta key that keeps the last number
# issued, and the query that finds an id in use.
_ID_KINDS: Dict[str, Tuple[str, str, str]] = {
    "entry": ("e", "entry_seq", "SELECT 1 FROM entries WHERE id = ?"),
    "fact": ("f", "fact_seq", "SELECT 1 FROM facts WHERE id = ?"),
    "batch": ("b", "batch_seq", "SELECT 1 FROM undo_batches WHERE id = ?"),
}


def _entry_image(entry: MemoryEntry, hidden_batch: Optional[str]) -> List[Any]:
    """The row ``entry`` is stored as, in _SELECT_ENTRY_IMAGE's order."""
    return [
        entry.id,
        entry.topic,
        entry.text,
        entry.created_at,
        entry.updated_at,
        entry.valid_until,
        entry.source,
        int(entry.locked),
        entry.source_conversation_id,
        entry.hidden_at,
        normalise_dedupe(entry.text),
        hidden_batch,
    ]


def _changed_memory(record: Dict[str, Any]) -> bool:
    """Whether an applied op changed anything: every one does but a forget that matched
    nothing."""
    if record["op"] != "forget":
        return True
    return bool(
        record["entry_ids"]
        or record["fact_ids"]
        or record["cleared_rounds"]
        or record["cleared_summaries"]
        or record["cleared_titles"]
    )


def _exported(item: Union[MemoryEntry, Fact]) -> Dict[str, Any]:
    """An entry or fact for export_json: its fields but ``hidden_at``, which an exported
    one never has."""
    fields = asdict(item)
    del fields["hidden_at"]
    return fields


def _one_line(text: str) -> str:
    """``text`` on one line, for a Markdown list item."""
    return " ".join(text.split())


def _entry_line(entry: MemoryEntry) -> str:
    notes = ["until " + entry.valid_until] if entry.valid_until else []
    if entry.locked:
        notes.append("edited by you")
    return "- " + _one_line(entry.text) + (" (" + "; ".join(notes) + ")" if notes else "")


def _fact_line(fact: Fact, timezone: Optional[str]) -> str:
    notes = ["until " + fact.valid_until] if fact.valid_until else []
    if fact.superseded_by:
        notes.append("superseded")
    line = "- " + days.day(fact.recorded_at, timezone) + ": " + _one_line(fact.text)
    return line + (" (" + "; ".join(notes) + ")" if notes else "")


def _date_or_none(value: Any) -> Optional[str]:
    """``value`` when it is a YYYY-MM-DD date, else None."""
    return value if isinstance(value, str) and date_problem(value) is None else None


class MemoryStore:
    """One user's memory, in a SQLite file the user alone can read (0600).

    Thread-safe: one lock serialises every call on the store's connection. Each write runs
    in a ``BEGIN IMMEDIATE`` transaction, which takes SQLite's write lock up front, so a
    second store open on the same file waits for it (5 s busy timeout) instead of failing
    midway. ``backend=`` forces a backend, raising if it cannot run here; left out, the
    store picks one and never raises for want of FTS5 (see the module docstring).
    """

    def __init__(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        clock: Callable[[], datetime] = utc_now,
        backend: Optional[Backend] = None,
    ) -> None:
        if backend is not None and backend not in _BACKENDS:
            raise ValueError(
                "backend must be one of {}, not {!r}".format(", ".join(_BACKENDS), backend)
            )
        self.path = Path(path)
        self.backend: Backend = backend if backend is not None else _choose_backend()
        self._index = _SCAN_INDEX if self.backend == "scan" else _FTS5_INDEX
        self._clock = clock
        self._lock = threading.RLock()
        self._in_write = False
        self._in_read = False
        self._scrub_due = False  # a _scrub asked for inside the current _write
        self._revision_raised = False  # the current _write has raised the revision
        # The scan backend's token counts and folded text of the index rows, by their text,
        # from one search to the next (see bm25_scan); emptied whenever text is deleted.
        self._scan_cache: Dict[str, Dict[str, Tuple[int, str]]] = {"facts": {}, "rounds": {}}
        apsw = _import_apsw() if self.backend == "apsw" else None
        if self.backend == "apsw" and apsw is None:
            raise ImportError(
                "the apsw backend needs apsw (Python 3.11+): pip install 'talk2view[memory]'"
            )
        _create_private_file(self.path)
        self._db: _Db = (
            _ApswDb(apsw, str(self.path)) if apsw is not None else _Sqlite3Db(str(self.path))
        )
        try:
            self._create_schema()
            # What was removed or forgotten before the file was last closed may be past its
            # undo window by now, with no store open since to purge it.
            self.purge()
        except BaseException:
            self._db.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ── entries and facts ──

    def entries(self, *, include_hidden: bool = False) -> List[MemoryEntry]:
        """The memory entries in the order they were added. Hidden entries (removed, but
        still in their undo window) only with ``include_hidden``."""
        rows = self._query(
            "SELECT id, topic, text, created_at, updated_at, valid_until, source, locked,"
            " source_conversation_id, hidden_at FROM entries"
            " WHERE hidden_at IS NULL OR ? ORDER BY rowid",
            (int(include_hidden),),
        )
        return [_entry_from_row(row) for row in rows]

    def facts(self, *, include_superseded: bool = False) -> List[Fact]:
        """The facts in the order they were recorded, never hidden ones. Facts a newer one
        superseded only with ``include_superseded``."""
        rows = self._query(
            "SELECT id, text, topic, recorded_at, occurred_at, valid_until, conversation_id,"
            " superseded_by, superseded_at, source, hidden_at FROM facts"
            " WHERE hidden_at IS NULL AND (superseded_by IS NULL OR ?) ORDER BY rowid",
            (int(include_superseded),),
        )
        return [_fact_from_row(row) for row in rows]

    def _insert_entry(self, entry: MemoryEntry, hidden_batch: Optional[str] = None) -> None:
        """Add one entry, with its dedupe key (``norm_text``) and, when a batch hid it, the
        batch's id."""
        with self._write():
            self._db.execute(
                "INSERT INTO entries (id, topic, text, norm_text, created_at, updated_at,"
                " valid_until, source, locked, source_conversation_id, hidden_at, hidden_batch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.id,
                    entry.topic,
                    entry.text,
                    normalise_dedupe(entry.text),
                    entry.created_at,
                    entry.updated_at,
                    entry.valid_until,
                    entry.source,
                    int(entry.locked),
                    entry.source_conversation_id,
                    entry.hidden_at,
                    hidden_batch,
                ),
            )

    def _insert_fact(self, fact: Fact) -> None:
        """Add one fact, and index its text."""
        with self._write():
            self._db.execute(
                "INSERT INTO facts (id, text, topic, recorded_at, occurred_at, valid_until,"
                " conversation_id, superseded_by, superseded_at, source, hidden_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fact.id,
                    fact.text,
                    fact.topic,
                    fact.recorded_at,
                    fact.occurred_at,
                    fact.valid_until,
                    fact.conversation_id,
                    fact.superseded_by,
                    fact.superseded_at,
                    fact.source,
                    fact.hidden_at,
                ),
            )
            self._db.execute(self._index.insert_fact, (fact.id, _fact_text(fact.text)))

    # ── changes to memory ──

    def apply_ops(self, ops: Sequence[Dict[str, Any]], *, source: str = "said") -> AppliedBatch:
        """Apply a batch of entry ops (a ``memory`` event's, or extraction's entry_ops) by
        ``apply_entry_ops``'s rules, in one transaction, as one batch ``undo`` can reverse.
        ``source`` is that of the entries it adds: "said", "inferred" or "imported".

        A removed entry is hidden, not deleted: ``undo`` can bring it back for
        UNDO_WINDOW_S (30) seconds, and ``purge`` deletes it after that. A ``forget`` also
        hides every fact, superseded ones included, whose ``normalise_dedupe`` text holds its
        own, and clears its phrase from the log of the chats, where it matches as whole
        tokens (see ``_phrase``): both texts of each round whose user text and reply hold it
        are blanked, and the round's index row deleted, each summary whose title or text
        holds it is cleared, and each chat title that holds it is blanked. Its record gains
        the facts' ids (``fact_ids``) and the counts (``cleared_rounds``,
        ``cleared_summaries``, ``cleared_titles``). The batch keeps each entry row it changed
        as it was before and after, the facts it hid, and the rounds, summaries and titles
        as they were, for ``undo``. A batch that changed entries or facts, or applied a
        forget, raises the ``revision``."""
        if source not in _SOURCES:
            raise ValueError("source must be one of " + ", ".join(_SOURCES))
        now = _timestamp(self._clock())
        with self._write():
            rows = self._db.execute(_SELECT_VISIBLE_ENTRY_IMAGES)
            before = {row[0]: (_entry_from_row(row), list(row)) for row in rows}
            entries, applied, dropped = apply_entry_ops(
                [entry for entry, _ in before.values()],
                ops,
                now=now,
                next_id=lambda: self._new_id("entry"),
                source=source,
            )
            batch_id = self._new_id("batch")
            images: List[List[Any]] = []  # [id, row before (None when added), row after]
            removed_entries: List[MemoryEntry] = []
            for entry in entries:
                old_entry, old_row = before.get(entry.id, (None, None))
                if entry == old_entry:
                    continue
                # Every entry came in visible, so a hidden one was hidden by this batch.
                hidden_batch = batch_id if entry.hidden_at is not None else None
                image = _entry_image(entry, hidden_batch)
                if old_row is None:
                    self._insert_entry(entry, hidden_batch)
                else:
                    self._db.execute(_UPDATE_ENTRY_IMAGE, image[1:] + [entry.id])
                images.append([entry.id, old_row, image])
                if hidden_batch is not None:
                    removed_entries.append(entry)
            forgets = [record for record in applied if record["op"] == "forget"]
            removed_facts = self._hide_forgotten_facts(forgets, batch_id, now)
            rounds, summaries, titles = self._clear_forgotten_log(forgets)
            changes = sum(1 for record in applied if _changed_memory(record))
            if images or removed_facts or rounds or summaries or titles:
                self._save_batch(
                    batch_id,
                    now,
                    entries=images,
                    facts=[fact.id for fact in removed_facts],
                    rounds=rounds,
                    summaries=summaries,
                    titles=titles,
                    changes=changes,
                )
            # Every forget (R55a): a consolidation built before it may hold the phrase in a
            # summary or a title, even when it matched nothing here, and would write it back.
            if images or removed_facts or forgets:
                self._raise_revision()
            self._add_changes(changes)
        return AppliedBatch(
            id=batch_id,
            applied=applied,
            dropped=dropped,
            removed_entries=removed_entries,
            removed_facts=removed_facts,
            cleared_rounds=len(rounds),
            cleared_summaries=len(summaries),
            cleared_titles=len(titles),
        )

    def forget(self, text: str) -> AppliedBatch:
        """Hide every unlocked entry and every fact whose ``normalise_dedupe`` text holds
        ``text``'s, and clear it, as whole tokens, from the log of the chats, as one batch
        ``undo`` can reverse: ``apply_ops`` with one ``forget``. The batch's
        ``removed_entries`` and ``removed_facts`` are exactly what went;
        ``cleared_rounds``, ``cleared_summaries`` and ``cleared_titles`` count what was
        cleared. A locked entry it holds is passed by, with "locked" in ``dropped``. A
        phrase under FORGET_TEXT_MIN (2) code points once normalised is dropped whole, as
        "forget text too short", and one that names nothing ("it", "that one") as "forget
        text too vague"."""
        return self.apply_ops([{"op": "forget", "text": text}])

    def delete_entry(self, entry_id: str) -> Optional[str]:
        """The user's own delete of an entry (spec §10.1), locked or not: it is hidden as a
        ``remove`` hides one, in a batch ``undo`` can reverse for UNDO_WINDOW_S (30)
        seconds, and ``purge`` deletes it after that. Returns the batch's id, for ``undo``;
        None, changing nothing, for an id that is not a visible entry: unknown, or deleted
        already."""
        now = _timestamp(self._clock())
        with self._write():
            rows = self._db.execute(_SELECT_ENTRY_IMAGE, (entry_id,))
            entry = _entry_from_row(rows[0]) if rows else None
            if entry is None or entry.hidden_at is not None:
                return None
            batch_id = self._new_id("batch")
            image = _entry_image(replace(entry, hidden_at=now), batch_id)
            self._db.execute(_UPDATE_ENTRY_IMAGE, image[1:] + [entry_id])
            self._save_batch(batch_id, now, entries=[[entry_id, list(rows[0]), image]], changes=1)
            self._raise_revision()
            self._add_changes(1)
        return batch_id

    def undo(self, batch_id: str) -> bool:
        """Reverse the batch ``batch_id`` exactly, in one transaction, raising the
        ``revision``: the entries it added are deleted, the entries it changed or hid get
        back the rows they had, the facts it hid are shown again, and the rounds, summaries
        and chat titles a forget cleared get their text back, with the rounds' index rows.
        Then the file is scrubbed of the text that went.

        True when undone. False for a batch applied over UNDO_WINDOW_S (30) seconds ago,
        one undone already, one that changed nothing, or an unknown id; and False, changing
        nothing, when a row the batch wrote has changed since (a later batch, an edit, a
        new summary or a deleted chat), so an undo never overwrites a newer change."""
        window_start = self._window_start()
        with self._write():
            found = self._db.execute(
                "SELECT applied_at, payload FROM undo_batches WHERE id = ?", (batch_id,)
            )
            if not found or found[0][0] < window_start:
                return False
            payload = json.loads(found[0][1])
            if not self._unchanged_since(batch_id, payload):
                return False
            for entry_id, before, _ in payload["entries"]:
                if before is None:
                    self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
                else:
                    self._db.execute(_UPDATE_ENTRY_IMAGE, before[1:] + [entry_id])
            for fact_id in payload["facts"]:
                self._db.execute(
                    "UPDATE facts SET hidden_at = NULL, hidden_batch = NULL WHERE id = ?",
                    (fact_id,),
                )
            for conversation_id, idx, user_text, assistant_text in payload["rounds"]:
                self._db.execute(
                    "UPDATE rounds SET user_text = ?, assistant_text = ?"
                    " WHERE conversation_id = ? AND idx = ?",
                    (user_text, assistant_text, conversation_id, idx),
                )
                self._index_round(conversation_id, idx, user_text, assistant_text)
            for conversation_id, title, text in payload["summaries"]:
                self._db.execute(
                    "UPDATE conversations SET summary_title = ?, summary_text = ? WHERE id = ?",
                    (title, text, conversation_id),
                )
            for conversation_id, title in payload["titles"]:
                self._db.execute(
                    "UPDATE conversations SET title = ? WHERE id = ?", (title, conversation_id)
                )
            self._db.execute("DELETE FROM undo_batches WHERE id = ?", (batch_id,))
            self._add_changes(-payload["changes"])
            self._raise_revision()
            self._scrub()
        return True

    def purge(self) -> int:
        """Delete the entries and facts removed or forgotten over UNDO_WINDOW_S (30) seconds
        ago, with their index rows, and the batches no longer undoable, which keep the text
        a forget cleared from the log, in one transaction; then scrub the file of that text.
        Returns how many entries and facts went."""
        window_start = self._window_start()
        with self._write():
            entries = self._db.execute(
                "SELECT id FROM entries WHERE hidden_at < ?", (window_start,)
            )
            facts = self._db.execute("SELECT id FROM facts WHERE hidden_at < ?", (window_start,))
            batches = self._db.execute(
                "SELECT id FROM undo_batches WHERE applied_at < ?", (window_start,)
            )
            self._db.execute("DELETE FROM entries WHERE hidden_at < ?", (window_start,))
            for (fact_id,) in facts:
                self._db.execute(self._index.delete_fact, (fact_id,))
            self._db.execute("DELETE FROM facts WHERE hidden_at < ?", (window_start,))
            self._db.execute("DELETE FROM undo_batches WHERE applied_at < ?", (window_start,))
            if entries or facts or batches:
                self._scrub()
        return len(entries) + len(facts)

    def edit_entry(
        self,
        entry_id: str,
        *,
        text: Optional[str] = None,
        topic: Optional[str] = None,
        valid_until: Optional[str] = None,
        clear_valid_until: bool = False,
    ) -> MemoryEntry:
        """The user's own edit of an entry: set the fields given, ``updated_at``, source
        "edited" and ``locked``, so changes the engine proposes never touch it again. A field
        left None stays as it is; ``clear_valid_until`` takes the date off. Raises KeyError
        for an entry that doesn't exist or is hidden, and ValueError, naming the rule, for
        text that isn't 1 to ENTRY_TEXT_MAX (300) code points, a topic not in TOPICS, a date
        that isn't YYYY-MM-DD, or a ``valid_until`` with ``clear_valid_until``."""
        problem = (
            text_problem(text)
            or topic_problem(topic)
            or date_problem(valid_until)
            or (
                "both valid_until and clear_valid_until"
                if clear_valid_until and valid_until is not None
                else None
            )
        )
        if problem is not None:
            raise ValueError(problem)
        now = _timestamp(self._clock())
        with self._write():
            rows = self._db.execute(_SELECT_ENTRY_IMAGE, (entry_id,))
            entry = _entry_from_row(rows[0]) if rows else None
            if entry is None or entry.hidden_at is not None:
                raise KeyError(entry_id)
            until = entry.valid_until if valid_until is None else valid_until
            edited = replace(
                entry,
                topic=entry.topic if topic is None else topic,
                text=entry.text if text is None else text,
                valid_until=None if clear_valid_until else until,
                updated_at=now,
                source="edited",
                locked=True,
            )
            self._db.execute(_UPDATE_ENTRY_IMAGE, _entry_image(edited, None)[1:] + [entry_id])
            self._raise_revision()
            self._add_changes(1)
        return edited

    # ── facts ──

    def add_facts(
        self,
        facts: Sequence[Dict[str, Any]],
        *,
        conversation_id: Optional[str],
        source: str,
    ) -> List[Fact]:
        """Record extraction's facts in one transaction, each with a new id and
        ``recorded_at`` from the clock, and apply their ``supersedes``. Returns the facts
        recorded.

        A fact keeps its own ``source`` ("said", "inferred" or "imported": the engine sets
        it); ``source`` stands in when it has none. Each id in its ``supersedes`` that names
        a visible fact not yet superseded gets ``superseded_by`` and ``superseded_at``;
        other ids are ignored. As the engine does, a fact is dropped without a topic in
        TOPICS or text of 1 to FACT_TEXT_MAX (400) code points, and a date that isn't
        YYYY-MM-DD is left out.

        Nothing is recorded for a conversation marked incognito: an extraction already on
        its way when the chat turned incognito writes nothing back (review Minor 8)."""
        if source not in _SOURCES:
            raise ValueError("source must be one of " + ", ".join(_SOURCES))
        now = _timestamp(self._clock())
        recorded: List[Fact] = []
        with self._write():
            if conversation_id is not None and self._db.execute(
                "SELECT 1 FROM conversations WHERE id = ? AND incognito != 0", (conversation_id,)
            ):
                return recorded
            for item in facts:
                if not isinstance(item, dict):
                    continue
                text, topic = item.get("text"), item.get("topic")
                if text is None or topic is None or text_problem(text, FACT_TEXT_MAX):
                    continue
                if topic_problem(topic):
                    continue
                own_source = item.get("source")
                fact = Fact(
                    id=self._new_id("fact"),
                    text=text,
                    topic=topic,
                    recorded_at=now,
                    occurred_at=_date_or_none(item.get("occurred_at")),
                    valid_until=_date_or_none(item.get("valid_until")),
                    conversation_id=conversation_id,
                    superseded_by=None,
                    superseded_at=None,
                    source=own_source if own_source in _SOURCES else source,
                )
                self._insert_fact(fact)
                supersedes = item.get("supersedes")
                for old_id in supersedes if isinstance(supersedes, list) else []:
                    if isinstance(old_id, str) and old_id != fact.id:
                        self._supersede(old_id, fact.id, now)
                recorded.append(fact)
            if recorded:
                self._raise_revision()
            self._add_changes(len(recorded))
        return recorded

    def merge_facts(self, keep: str, drop: Sequence[str]) -> None:
        """Consolidation's merge of facts that say the same thing: each fact in ``drop`` is
        superseded by ``keep``, so it stays as history, as a superseded fact does. Nothing
        happens when ``keep`` is not a visible fact or is superseded itself, which would
        take the merged facts out of sight with it; a fact in ``drop`` that is hidden or
        superseded already is left as it is."""
        now = _timestamp(self._clock())
        with self._write():
            if not self._db.execute(
                "SELECT 1 FROM facts WHERE id = ? AND hidden_at IS NULL AND superseded_by IS NULL",
                (keep,),
            ):
                return
            merged = False
            for fact_id in drop:
                if isinstance(fact_id, str) and fact_id != keep:
                    merged = self._supersede(fact_id, keep, now) or merged
            if merged:
                self._raise_revision()

    def expire_facts(self, ids: Sequence[str], *, timezone: Optional[str] = None) -> None:
        """Consolidation's facts about something current or upcoming that is over: each
        visible one gets ``valid_until`` the day before the clock's date in ``timezone``,
        the user's (see ``days``: None means the system's local zone), unless it ends
        earlier already. Facts stay true as history, so none is deleted."""
        yesterday = (days.today(_utc(self._clock()), timezone) - timedelta(days=1)).isoformat()
        with self._write():
            expired = False
            for fact_id in ids:
                if isinstance(fact_id, str):
                    self._db.execute(
                        "UPDATE facts SET valid_until = ? WHERE id = ? AND hidden_at IS NULL"
                        " AND (valid_until IS NULL OR valid_until > ?)",
                        (yesterday, fact_id, yesterday),
                    )
                    expired = self._changed_rows() > 0 or expired
            if expired:
                self._raise_revision()

    # ── consolidation ──

    def apply_consolidation(
        self,
        entries: Sequence[Dict[str, Any]],
        merge_facts: Sequence[Dict[str, Any]],
        expire_facts: Sequence[str],
        *,
        expected_revision: int,
    ) -> bool:
        """Apply a consolidation's result, all or nothing, in one transaction: consolidation
        applies its result only through this. ``expected_revision`` is the ``revision`` of
        the ``consolidation_snapshot`` its request was built from. If memory has changed
        since, the result would undo that change, so nothing is written and it returns
        False. Otherwise it replaces the entries (``replace_entries``), merges each
        ``{"keep": id, "drop": [ids]}`` group of facts (``merge_facts``; a malformed group is
        skipped), expires the facts named (``expire_facts``), records the consolidation
        (``mark_consolidated``) and returns True."""
        with self._write():
            if self.revision() != expected_revision:
                return False
            self.replace_entries(entries)
            for group in merge_facts:
                keep: Any = group.get("keep") if isinstance(group, dict) else None
                drop: Any = group.get("drop") if isinstance(group, dict) else None
                if isinstance(keep, str) and isinstance(drop, list):
                    self.merge_facts(keep, drop)
            self.expire_facts(expire_facts)
            self.mark_consolidated()
        return True

    def consolidation_snapshot(
        self,
        *,
        facts_max: int = CONSOLIDATE_FACTS_MAX,
        summaries_max: int = CONSOLIDATE_SUMMARIES_MAX,
    ) -> ConsolidationSnapshot:
        """What a consolidate request carries, with the ``revision`` it was read at, in one
        read: under the lock and in one read transaction, so no write, from this store or
        another connection to the file, lands between its reads. Build the request only
        from this, and give its ``revision`` to ``apply_consolidation`` as
        ``expected_revision``.

        It holds the visible entries, locked ones marked; the visible facts no newer one
        superseded, the ``facts_max`` (400) recorded last, newest first; and the summaries
        of the ``summaries_max`` (30) most recently active conversations that have one,
        newest first, never an incognito one's."""
        with self._read():
            revision = self.revision()
            entries = self.entries()
            facts = self._query(_SELECT_NEWEST_FACTS, (facts_max,))
            summaries = self._query(_SELECT_NEWEST_SUMMARIES, (summaries_max,))
        return ConsolidationSnapshot(
            revision=revision,
            entries=entries,
            facts=[_fact_from_row(row) for row in facts],
            summaries=[_conversation_from_row(row) for row in summaries],
        )

    def revision(self) -> int:
        """A count, kept in the file, that each committed write changing the visible entries
        or facts raises by one, however many changes it holds, and nothing lowers. Every
        forget raises it too, one that cleared only the log or matched nothing included, as
        do an undo and a reset. The log's other writes (rounds, summaries, titles and
        extraction's marks), purge, incognito and the switches leave it.

        ``apply_consolidation`` checks it. Build a consolidate request only from
        ``consolidation_snapshot``, which reads it with the request's entries, facts and
        summaries in one read. Read on its own after the entries, it would let an entry
        added in between pass the check, and the result, which never saw that entry, would
        delete it."""
        return int(self._meta_value("revision") or 0)

    def replace_entries(self, entries: Sequence[Dict[str, Any]]) -> None:
        """Make consolidation's result the set of visible entries, in one transaction.

        An item is the entry's whole state. One with the id of a visible entry rewrites its
        topic, text and ``valid_until`` (``updated_at`` too when they change), keeping its
        id and ``created_at``; a ``valid_until`` it leaves out, or null, means no date. An
        item with id None, or none, becomes a new entry, source "inferred", unless an entry
        the call keeps, or a new one added before it, has the same ``normalise_dedupe``
        text, as apply_entry_ops' add rule has it. A visible entry no item names is deleted,
        and the file scrubbed of it. Locked entries come through verbatim whatever the items
        say, and hidden ones (in their undo window) are left alone. As the engine does, an
        existing entry whose item breaks a rule (a topic in TOPICS, text of 1 to
        ENTRY_TEXT_MAX code points, a YYYY-MM-DD ``valid_until``) stays as it was, and a new
        one is dropped. Only the first item with an id counts, and an id that is no longer
        a visible entry is skipped rather than brought back."""
        now = _timestamp(self._clock())
        with self._write():
            current = {
                row[0]: _entry_from_row(row)
                for row in self._db.execute(_SELECT_VISIBLE_ENTRY_IMAGES)
            }
            kept: Dict[str, MemoryEntry] = {}  # each entry an item names, as the call leaves it
            new: List[Tuple[Any, Any, Any]] = []  # each new item's topic, text and valid_until
            for item in entries:
                if not isinstance(item, dict):
                    continue
                entry_id: Any = item.get("id")
                topic: Any = item.get("topic")
                text: Any = item.get("text")
                valid_until: Any = item.get("valid_until")
                broken = (
                    topic is None
                    or text is None
                    or bool(topic_problem(topic) or text_problem(text) or date_problem(valid_until))
                )
                if entry_id is None:
                    if not broken:
                        new.append((topic, text, valid_until))
                    continue
                entry = current.get(entry_id) if isinstance(entry_id, str) else None
                if entry is None or entry.id in kept:
                    continue
                kept[entry.id] = (
                    entry
                    if entry.locked or broken
                    else replace(entry, topic=topic, text=text, valid_until=valid_until)
                )
            changed = False
            for entry in kept.values():
                if entry != current[entry.id]:
                    image = _entry_image(replace(entry, updated_at=now), None)
                    self._db.execute(_UPDATE_ENTRY_IMAGE, image[1:] + [entry.id])
                    changed = True
            # A locked entry stays whether an item names it or not.
            texts = {
                normalise_dedupe(kept.get(entry.id, entry).text)
                for entry in current.values()
                if entry.id in kept or entry.locked
            }
            for topic, text, valid_until in new:
                if normalise_dedupe(text) in texts:
                    continue
                texts.add(normalise_dedupe(text))
                self._insert_entry(
                    MemoryEntry(
                        id=self._new_id("entry"),
                        topic=topic,
                        text=text,
                        created_at=now,
                        updated_at=now,
                        valid_until=valid_until,
                        source="inferred",
                        locked=False,
                        source_conversation_id=None,
                    )
                )
                changed = True
            gone = [
                entry.id for entry in current.values() if entry.id not in kept and not entry.locked
            ]
            for entry_id in gone:
                self._db.execute("DELETE FROM entries WHERE id = ?", (entry_id,))
            if gone:
                self._scrub()
            if changed or gone:
                self._raise_revision()

    def needs_consolidation(self) -> bool:
        """Whether a consolidation is due: the visible entries are over the caps (more than
        ENTRY_MAX (40) of them, or more than ENTRIES_CHARS_MAX (6,000) code points of text),
        or CONSOLIDATE_MIN_CHANGES (10) changes have built up and CONSOLIDATE_EVERY_S (7
        days) have passed since the last one, or since the store was created."""
        texts = self._query("SELECT text FROM entries WHERE hidden_at IS NULL")
        if len(texts) > ENTRY_MAX:
            return True
        if sum(code_points(text) for (text,) in texts) > ENTRIES_CHARS_MAX:
            return True
        due_since = _timestamp(self._clock() - timedelta(seconds=CONSOLIDATE_EVERY_S))
        last = self._meta_value("last_consolidated_at")
        return self.changes_since_consolidation() >= CONSOLIDATE_MIN_CHANGES and (
            last is None or last <= due_since
        )

    def changes_since_consolidation(self) -> int:
        """Changes to memory since the last consolidation: each op applied that changed
        something, each fact added and each edit. Undoing a batch takes its changes back;
        consolidation's own writes don't count."""
        return int(self._meta_value("changes_since_consolidation") or 0)

    def mark_consolidated(self) -> None:
        """Record that a consolidation ran now: no changes since."""
        now = _timestamp(self._clock())
        with self._write():
            self._set_meta("last_consolidated_at", now)
            self._set_meta("changes_since_consolidation", "0")

    # ── conversations ──

    def record_round(
        self,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        *,
        at: Optional[datetime] = None,
        title: Optional[str] = None,
    ) -> int:
        """Append a round (a user message and the reply that followed) to the conversation's
        log, creating its record with the first round, and index it. Returns the round's
        index (1, 2, ...), or 0 for an incognito conversation, whose rounds are never
        written. ``at`` defaults to the clock; ``last_active_at`` becomes the latest ``at``
        recorded. The first title given is kept."""
        stamp = _timestamp(at if at is not None else self._clock())
        with self._write():
            found = self._db.execute(
                "SELECT incognito FROM conversations WHERE id = ?", (conversation_id,)
            )
            if not found:
                self._db.execute(
                    "INSERT INTO conversations (id, title, started_at, last_active_at)"
                    " VALUES (?, ?, ?, ?)",
                    (conversation_id, title, stamp, stamp),
                )
            elif found[0][0]:
                return 0
            else:
                self._db.execute(
                    "UPDATE conversations SET title = COALESCE(title, ?),"
                    " last_active_at = max(last_active_at, ?) WHERE id = ?",
                    (title, stamp, conversation_id),
                )
            idx: int = self._db.execute(
                "SELECT COALESCE(MAX(idx), 0) + 1 FROM rounds WHERE conversation_id = ?",
                (conversation_id,),
            )[0][0]
            self._db.execute(
                "INSERT INTO rounds (conversation_id, idx, user_text, assistant_text, at)"
                " VALUES (?, ?, ?, ?, ?)",
                (conversation_id, idx, user_text, assistant_text, stamp),
            )
            self._index_round(conversation_id, idx, user_text, assistant_text)
        return idx

    def conversation(self, conversation_id: str) -> Optional[ConversationRecord]:
        rows = self._query(
            "SELECT id, title, started_at, last_active_at, summary_title, summary_text,"
            " extracted_through, incognito FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        return _conversation_from_row(rows[0]) if rows else None

    def recent_conversations(
        self, limit: int = 8, *, exclude: Optional[str] = None
    ) -> List[ConversationRecord]:
        """The most recently active conversations, newest first (the newer record first on
        a tie), leaving out incognito ones and ``exclude``, the current conversation."""
        rows = self._query(
            "SELECT id, title, started_at, last_active_at, summary_title, summary_text,"
            " extracted_through, incognito FROM conversations"
            " WHERE incognito = 0 AND id IS NOT ?"
            " ORDER BY last_active_at DESC, rowid DESC LIMIT ?",
            (exclude, limit),
        )
        return [_conversation_from_row(row) for row in rows]

    def set_summary(self, conversation_id: str, title: str, text: str) -> None:
        """Store extraction's summary of a conversation, cut to ``SUMMARY_TITLE_MAX`` (120)
        and ``SUMMARY_TEXT_MAX`` (600) code points. A conversation with no record is left
        without one, and one marked incognito keeps none: an extraction already on its way
        when the chat turned incognito writes nothing back (review Minor 8)."""
        with self._write():
            self._db.execute(
                "UPDATE conversations SET summary_title = ?, summary_text = ?"
                " WHERE id = ? AND incognito = 0",
                (title[:SUMMARY_TITLE_MAX], text[:SUMMARY_TEXT_MAX], conversation_id),
            )

    def mark_extracted(self, conversation_id: str, through: int) -> None:
        """Record that extraction has seen the conversation's rounds up to index
        ``through``. It never moves back, and stays put for a conversation marked
        incognito (review Minor 8)."""
        with self._write():
            self._db.execute(
                "UPDATE conversations SET extracted_through = max(extracted_through, ?)"
                " WHERE id = ? AND incognito = 0",
                (through, conversation_id),
            )

    def set_incognito(self, conversation_id: str, incognito: bool) -> None:
        """Mark a conversation incognito, so its rounds are never written, or not. Marking
        an unknown conversation creates its record; unmarking one does nothing.

        Marking one that has rounds already deletes them, with their index rows, and its
        summary, and starts its log over (``extracted_through`` 0), then scrubs the file of
        their text: an incognito chat's rounds are never kept (spec §7.1). Its title stays;
        facts and entries drawn from it stay, as they do when the chat is deleted."""
        now = _timestamp(self._clock())
        with self._write():
            if incognito:
                self._db.execute(
                    "INSERT OR IGNORE INTO conversations (id, started_at, last_active_at)"
                    " VALUES (?, ?, ?)",
                    (conversation_id, now, now),
                )
                kept = self._db.execute(
                    "SELECT EXISTS (SELECT 1 FROM rounds WHERE conversation_id = ?1)"
                    " OR summary_title IS NOT NULL OR summary_text IS NOT NULL"
                    " FROM conversations WHERE id = ?1",
                    (conversation_id,),
                )
                if kept[0][0]:
                    self._db.execute(self._index.delete_rounds, (conversation_id,))
                    self._db.execute(
                        "DELETE FROM rounds WHERE conversation_id = ?", (conversation_id,)
                    )
                    self._db.execute(
                        "UPDATE conversations SET summary_title = NULL, summary_text = NULL,"
                        " extracted_through = 0 WHERE id = ?",
                        (conversation_id,),
                    )
                    self._scrub()  # once this write has committed
            self._db.execute(
                "UPDATE conversations SET incognito = ? WHERE id = ?",
                (int(incognito), conversation_id),
            )

    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation's record, rounds and summary, as when the user deletes the
        chat, and scrub the file of their text. Facts and entries drawn from it stay until the
        user deletes them."""
        with self._write():
            self._db.execute(self._index.delete_rounds, (conversation_id,))
            # Its rounds go with it: ON DELETE CASCADE.
            self._db.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
            self._scrub()

    def unextracted(self, *, idle_for_s: Optional[float] = None) -> List[ConversationRecord]:
        """Conversations with rounds extraction has not seen, least recently active first.
        Rounds with neither text, as a forget leaves them, don't count. With
        ``idle_for_s``, only those idle at least that long by the clock."""
        cutoff = (
            _timestamp(self._clock() - timedelta(seconds=idle_for_s))
            if idle_for_s is not None
            else None
        )
        rows = self._query(
            "SELECT id, title, started_at, last_active_at, summary_title, summary_text,"
            " extracted_through, incognito FROM conversations AS c"
            " WHERE incognito = 0 AND (? IS NULL OR last_active_at <= ?)"
            " AND EXISTS (SELECT 1 FROM rounds AS r"
            " WHERE r.conversation_id = c.id AND r.idx > c.extracted_through"
            " AND (r.user_text != '' OR r.assistant_text != ''))"
            " ORDER BY c.last_active_at, c.rowid",
            (cutoff, cutoff),
        )
        return [_conversation_from_row(row) for row in rows]

    def rounds(self, conversation_id: str, *, after: int = 0) -> List[Tuple[int, str, str, str]]:
        """The conversation's rounds with index above ``after``, in order, as
        ``(idx, user_text, assistant_text, at)``. A round with neither text, as a forget
        leaves one, is left out."""
        rows = self._query(
            "SELECT idx, user_text, assistant_text, at FROM rounds"
            " WHERE conversation_id = ? AND idx > ?"
            " AND (user_text != '' OR assistant_text != '') ORDER BY idx",
            (conversation_id, after),
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    # ── search and the turn payload (the rules are search.py's and payload.py's) ──

    def search(
        self,
        query: str,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 8,
        timezone: Optional[str] = None,
    ) -> List[SearchHit]:
        """Search the facts and the log of the chats for ``query`` (spec §7.2), in one read:
        at most ``limit`` hits, best first.

        The query's terms (``search.query_terms``: at most 12 words, stop words and single
        letters left out, and 24 bigrams of CJK or Thai text) are looked up in the facts
        and, separately, in the rounds, each list ranked by BM25. Superseded facts are left
        out unless nothing else matches; hidden facts, rounds a forget blanked and incognito
        conversations' rounds never show. With ``from_date`` or ``to_date`` (YYYY-MM-DD,
        both included; ValueError otherwise), only what was recorded or said on those days
        in ``timezone`` (``days.bounds``: a day the calendar lacks, such as 2026-04-31,
        reads as the nearest real one). The two lists are fused by reciprocal rank fusion
        (k = 60), a tie going to the newer. A fact comes whole; a round as an excerpt of
        about 600 code points around its first match. Each hit has its date, the day in
        ``timezone``, and its conversation's name ("" when it has none). A query with no
        terms finds nothing.

        ``timezone`` is the user's, an IANA name such as "Australia/Perth". None, or a name
        this platform doesn't know, means the system's local zone (``days``)."""
        found = self._found(
            query_terms(query),
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            timezone=timezone,
        )
        return [hit for hit, _ in found]

    def search_result_json(
        self,
        query: str,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 8,
        timezone: Optional[str] = None,
    ) -> str:
        """The ``search_memory`` tool's result for these arguments (spec §8.4): ``search``,
        with ``limit`` kept within 1 to RESULT_LIMIT_MAX (20), as ``{"results": [{"date",
        "conversation", "kind", "text"}]}`` JSON of at most RESULT_CHARS_MAX (8,000) code
        points, with ``"note": "truncated"`` when results were dropped to fit."""
        hits = self.search(
            query,
            from_date=from_date,
            to_date=to_date,
            limit=max(1, min(limit, RESULT_LIMIT_MAX)),
            timezone=timezone,
        )
        return result_json(hits)

    def turn_payload(
        self,
        latest_user_message: Optional[str],
        *,
        exclude_conversation: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The ``memory`` field for a turn (spec §8.1), read in one read: ``{"status",
        "entries", "recent", "recalled"}``, every list within the engine's caps (see
        payload.py). ``exclude_conversation`` is the current conversation. ``timezone`` is
        the user's, as ``search`` takes it: every day in the field is a day there, "today"
        for expiry among them.

        Status "paused" while memory is paused, or "incognito" when the current conversation
        is, both with empty lists. Otherwise "on": the visible entries not yet expired; the
        recent conversations but the current one and incognito ones; and the hits of a search
        for ``latest_user_message`` in older conversations (the current one's rounds and the
        facts drawn from it left out) that clear the pre-selection floor."""
        today = days.today(_utc(self._clock()), timezone).isoformat()
        with self._read():
            if self.paused:
                return payload.empty("paused")
            current = (
                self.conversation(exclude_conversation)
                if exclude_conversation is not None
                else None
            )
            if current is not None and current.incognito:
                return payload.empty("incognito")
            entries = self.entries()
            recent = self.recent_conversations(payload.RECENT_WINDOW, exclude=exclude_conversation)
            terms = query_terms(latest_user_message or "")
            found = self._found(
                terms,
                from_date=None,
                to_date=None,
                limit=payload.PRESELECT_SEARCH_LIMIT,
                exclude_conversation=exclude_conversation,
                timezone=timezone,
            )
        return {
            "status": "on",
            "entries": payload.entry_items(entries, today, timezone),
            "recent": payload.recent_items(recent, timezone),
            "recalled": payload.recalled_items(found, terms),
        }

    def _found(
        self,
        terms: List[str],
        *,
        from_date: Optional[str],
        to_date: Optional[str],
        limit: int,
        exclude_conversation: Optional[str] = None,
        timezone: Optional[str] = None,
    ) -> List[Tuple[SearchHit, str]]:
        """``search``'s hits for ``terms``, each with the whole text it was indexed by
        (pre-selection's floor counts the terms in it), leaving out the rounds of
        ``exclude_conversation`` and the facts drawn from it (review Minor 3)."""
        for name, value in (("from_date", from_date), ("to_date", to_date)):
            if date_problem(value):
                raise ValueError(name + " must be a YYYY-MM-DD date")
        span = days.bounds(from_date, to_date, timezone)
        if not terms or limit < 1 or span is None:
            return []
        start, end = span
        with self._read():
            rounds = self._ranked_rounds(terms, start, end, exclude_conversation, limit)
            facts = self._ranked_facts(terms, start, end, exclude_conversation, False, limit)
            if not facts and not rounds:  # superseded facts only when nothing else matches
                facts = self._ranked_facts(terms, start, end, exclude_conversation, True, limit)
            lists = [[ref for ref, _ in facts], [ref for ref, _ in rounds]]
            fused = fuse(lists, dict(facts + rounds))[:limit]
            return [self._hit(ref, score, terms, timezone) for ref, score in fused]

    def _ranked_facts(
        self,
        terms: List[str],
        start: Optional[str],
        end: Optional[str],
        exclude_conversation: Optional[str],
        superseded: bool,
        limit: int,
    ) -> List[Tuple[str, str]]:
        """The facts that match, best first, at most ``limit``, as ``(id, recorded_at)``:
        recorded from ``start`` to before ``end``, not drawn from ``exclude_conversation``."""
        if self.backend != "scan":
            return [
                (fact_id, recorded_at)
                for fact_id, recorded_at in self._query(
                    _FTS_FACTS,
                    (fts_query(terms), int(superseded), start, end, exclude_conversation, limit),
                )
            ]
        rows = self._query(_SCAN_FACTS, (start, end, exclude_conversation))
        scores = dict(
            bm25_scan([(row[0], row[1]) for row in rows], terms, cache=self._scan_cache["facts"])
        )
        return best_first(
            [
                (scores[fact_id], recorded_at, rowid, fact_id)
                for fact_id, _, recorded_at, rowid, listed, replaced in rows
                if fact_id in scores and listed and (superseded or not replaced)
            ],
            limit,
        )

    def _ranked_rounds(
        self,
        terms: List[str],
        start: Optional[str],
        end: Optional[str],
        exclude_conversation: Optional[str],
        limit: int,
    ) -> List[Tuple[str, str]]:
        """The rounds that match, best first, at most ``limit``, as ``(round_ref, at)``:
        said from ``start`` to before ``end``."""
        if self.backend != "scan":
            return [
                (round_ref(conversation_id, idx), at)
                for conversation_id, idx, at in self._query(
                    _FTS_ROUNDS,
                    (fts_query(terms), start, end, exclude_conversation, limit),
                )
            ]
        rows = self._query(_SCAN_ROUNDS, (start, end, exclude_conversation))
        refs = [round_ref(row[0], row[1]) for row in rows]
        scores = dict(
            bm25_scan(
                [(ref, row[2]) for ref, row in zip(refs, rows)],
                terms,
                cache=self._scan_cache["rounds"],
            )
        )
        return best_first(
            [
                (scores[ref], row[3], row[4], ref)
                for ref, row in zip(refs, rows)
                if ref in scores and row[5]
            ],
            limit,
        )

    def _hit(
        self, ref: str, score: float, terms: List[str], timezone: Optional[str]
    ) -> Tuple[SearchHit, str]:
        """The hit ``ref`` (a fact's id or a ``round_ref``) with its fused ``score``, dated
        the day in ``timezone``, and the whole text it was indexed by."""
        if ref.startswith("r:"):
            conversation_id, idx = ref[2:].rsplit(":", 1)
            user_text, assistant_text, at, title, summary_title = self._query(
                _ROUND_HIT, (conversation_id, int(idx))
            )[0]
            name = conversation_title(title, summary_title)
            date = days.day(at, timezone)
            hit = round_hit(ref, score, user_text, assistant_text, date, name, terms)
            return hit, user_text + "\n" + assistant_text
        text, recorded_at, valid_until, title, summary_title, incognito = self._query(
            _FACT_HIT, (ref,)
        )[0]
        name = "" if incognito else conversation_title(title, summary_title)
        date = days.day(recorded_at, timezone)
        return fact_hit(ref, score, text, date, name, valid_until), text

    # ── settings and lifecycle ──

    @property
    def paused(self) -> bool:
        """The user's "pause memory" switch (spec §10.1), kept in the file."""
        return self._meta_value("paused") == "1"

    @paused.setter
    def paused(self, value: bool) -> None:
        self._set_meta("paused", "1" if value else "0")

    @property
    def include_sensitive(self) -> bool:
        """The user's "Include sensitive topics" switch for imports (spec §10.1), off by
        default, kept in the file."""
        return self._meta_value("include_sensitive") == "1"

    @include_sensitive.setter
    def include_sensitive(self, value: bool) -> None:
        self._set_meta("include_sensitive", "1" if value else "0")

    def export_json(self) -> str:
        """Every entry and fact, as JSON for the user to keep: ``{"exported_at", "entries",
        "facts"}``, each item with its fields. Superseded facts are in it; removed and
        forgotten entries and facts are not."""
        with self._lock:
            entries, facts = self.entries(), self.facts(include_superseded=True)
        document = {
            "exported_at": _timestamp(self._clock()),
            "entries": [_exported(entry) for entry in entries],
            "facts": [_exported(fact) for fact in facts],
        }
        return json.dumps(document, ensure_ascii=False, indent=2)

    def export_markdown(self, *, timezone: Optional[str] = None) -> str:
        """Every entry and fact, as Markdown for the user to read: the entries under their
        topics, then the facts with the date each was recorded, the day in ``timezone``,
        the user's (see ``days``: None means the system's local zone). Superseded facts are
        in it, marked; removed and forgotten entries and facts are not."""
        with self._lock:
            entries, facts = self.entries(), self.facts(include_superseded=True)
        lines = ["# Memory", "", "Exported " + _timestamp(self._clock()) + ".", "", "## Entries"]
        topics = list(TOPICS) + [
            topic
            for topic in dict.fromkeys(entry.topic for entry in entries)
            if topic not in TOPICS
        ]
        for topic in topics:
            under = [entry for entry in entries if entry.topic == topic]
            if under:
                lines += ["", "### " + topic.capitalize(), ""]
                lines += [_entry_line(entry) for entry in under]
        if not entries:
            lines += ["", "None."]
        lines += ["", "## Facts", ""]
        lines += [_fact_line(fact, timezone) for fact in facts] or ["None."]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        """Forget everything (spec §10.1): delete every entry, fact, conversation record,
        round and batch, and the search index, in one transaction, then scrub the file. The
        user's switches stay, and so do the id counters, so no id is ever used twice; the
        ``revision`` rises."""
        now = _timestamp(self._clock())
        with self._write():
            for statement in self._index.clear + (
                "DELETE FROM rounds",
                "DELETE FROM conversations",
                "DELETE FROM entries",
                "DELETE FROM facts",
                "DELETE FROM undo_batches",
            ):
                self._db.execute(statement)
            self._set_meta("changes_since_consolidation", "0")
            self._set_meta("last_consolidated_at", now)
            self._raise_revision()
            self._scrub()

    # ── internals ──

    def _create_schema(self) -> None:
        self._db.execute("PRAGMA busy_timeout = 5000")  # sqlite3's default; apsw's is 0
        # Zero what SQLite frees, so deleted text is not left in the file (see _scrub). It is
        # off unless SQLite was built with it on.
        self._db.execute("PRAGMA secure_delete = ON")
        # These two only take effect outside a transaction.
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA foreign_keys = ON")
        with self._write():
            for statement in _TABLES + self._index.create:
                self._db.execute(statement)
            self._db.executemany(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                _META_DEFAULTS
                + (
                    ("index_kind", self._index.kind),
                    # A new store counts its first consolidation's 7 days from now.
                    ("last_consolidated_at", _timestamp(self._clock())),
                ),
            )
            recorded = self._db.execute("SELECT value FROM meta WHERE key = 'index_kind'")
            if recorded != [(self._index.kind,)]:
                self._rebuild_index()
                # The cleared index's segments still hold its old text, deleted rows' included.
                self._scrub()

    def _rebuild_index(self) -> None:
        """Fill this store's kind of index afresh from ``facts`` and ``rounds``, in the
        caller's transaction, as the writes fill it: every fact, hidden and superseded ones
        included, and every round with text. The file was indexed the other way, so the other
        kind's writes are missing from these tables, and whatever they hold is stale."""
        for statement in self._index.clear:
            self._db.execute(statement)
        facts = self._db.execute("SELECT id, text FROM facts ORDER BY rowid")
        self._db.executemany(
            self._index.insert_fact,
            [(fact_id, _fact_text(text)) for fact_id, text in facts],
        )
        rounds = self._db.execute(_SELECT_ROUNDS_WITH_TEXT)
        self._db.executemany(
            self._index.insert_round,
            [
                (conversation, idx, _round_text(user, reply))
                for conversation, idx, user, reply in rounds
            ],
        )
        for statement in self._index.drop_other:
            self._db.execute(statement)
        self._db.execute("UPDATE meta SET value = ? WHERE key = 'index_kind'", (self._index.kind,))

    def _scrub(self) -> None:
        """Clear deleted text out of the file, after the write that deleted it has committed:
        every method that deletes the user's text calls this. ``secure_delete`` zeros what
        SQLite frees, but FTS5 keeps a deleted row's tokens until its segments merge, and the
        WAL keeps old page images until it is truncated. So merge each FTS5 index into one
        segment, then checkpoint the WAL into the file and truncate it.

        A checkpoint fails inside a transaction, so inside a ``_write`` this only marks the
        scrub due: the outermost ``_write`` runs it once it has committed, and never after a
        rollback, which deleted nothing. Every method that deletes text asks for it that
        way, so a scrub that fails never fails the method (see ``_write``). It empties the
        scan backend's search cache too, so no deleted text stays in it."""
        with self._lock:
            for cache in self._scan_cache.values():
                cache.clear()
            if self._in_write:
                self._scrub_due = True
                return
            with self._write():
                for statement in self._index.optimize:
                    self._db.execute(statement)
            busy = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")[0][0]
        if busy:
            logger.warning(
                "Memory: another connection is reading the store, so deleted text stays in"
                " its WAL file until a later checkpoint"
            )

    def _hide_forgotten_facts(
        self, forgets: List[Dict[str, Any]], batch_id: str, now: str
    ) -> List[Fact]:
        """In the caller's write: for each forget record in ``forgets``, hide every visible
        fact, superseded ones included, whose ``normalise_dedupe`` text holds the forget's,
        and record their ids in its ``fact_ids``. Returns the facts hidden."""
        if not forgets:
            return []
        facts = [
            (_fact_from_row(row), normalise_dedupe(row[1]))
            for row in self._db.execute(_SELECT_VISIBLE_FACTS)
        ]
        hidden: List[Fact] = []
        taken: Set[str] = set()
        for record in forgets:
            key = normalise_dedupe(record["text"])
            matched = [fact for fact, text in facts if fact.id not in taken and key in text]
            for fact in matched:
                self._db.execute(
                    "UPDATE facts SET hidden_at = ?, hidden_batch = ? WHERE id = ?",
                    (now, batch_id, fact.id),
                )
                taken.add(fact.id)
                hidden.append(replace(fact, hidden_at=now))
            record["fact_ids"] = [fact.id for fact in matched]
        return hidden

    def _clear_forgotten_log(
        self, forgets: List[Dict[str, Any]]
    ) -> Tuple[List[List[Any]], List[List[Any]], List[List[Any]]]:
        """In the caller's write: for each forget record in ``forgets``, blank both texts of
        every round whose user text and reply hold its phrase, as whole tokens (``_phrase``)
        of the two together, as the index holds them, so a phrase split between them is
        found too, and delete the round's index row; clear every summary whose title or
        text holds it; and blank every chat title that holds it. Record the counts in its
        ``cleared_rounds``, ``cleared_summaries`` and ``cleared_titles``. Returns the
        rounds, summaries and titles as they were, for undo, as _SELECT_ROUNDS_WITH_TEXT,
        _SELECT_SUMMARIES and _SELECT_TITLES give them."""
        if not forgets:
            return [], [], []
        rounds = [
            (list(row), _token_line(row[2] + "\n" + row[3]))
            for row in self._db.execute(_SELECT_ROUNDS_WITH_TEXT)
        ]
        summaries = [
            (list(row), _token_line(row[1] or ""), _token_line(row[2] or ""))
            for row in self._db.execute(_SELECT_SUMMARIES)
        ]
        titles = [(list(row), _token_line(row[1])) for row in self._db.execute(_SELECT_TITLES)]
        cleared_rounds: List[List[Any]] = []
        cleared_summaries: List[List[Any]] = []
        cleared_titles: List[List[Any]] = []
        taken_rounds: Set[Tuple[str, int]] = set()
        taken_summaries: Set[str] = set()
        taken_titles: Set[str] = set()
        for record in forgets:
            phrase = _phrase(record["text"])
            blanked = [
                row
                for row, line in rounds
                if (row[0], row[1]) not in taken_rounds and phrase.search(line)
            ]
            for conversation_id, idx, _, _ in blanked:
                self._db.execute(
                    "UPDATE rounds SET user_text = '', assistant_text = ''"
                    " WHERE conversation_id = ? AND idx = ?",
                    (conversation_id, idx),
                )
                self._db.execute(self._index.delete_round, (conversation_id, idx))
                taken_rounds.add((conversation_id, idx))
            cleared = [
                row
                for row, title, text in summaries
                if row[0] not in taken_summaries and (phrase.search(title) or phrase.search(text))
            ]
            for conversation_id, _, _ in cleared:
                self._db.execute(
                    "UPDATE conversations SET summary_title = NULL, summary_text = NULL"
                    " WHERE id = ?",
                    (conversation_id,),
                )
                taken_summaries.add(conversation_id)
            untitled = [
                row for row, line in titles if row[0] not in taken_titles and phrase.search(line)
            ]
            for conversation_id, _ in untitled:
                self._db.execute(
                    "UPDATE conversations SET title = '' WHERE id = ?", (conversation_id,)
                )
                taken_titles.add(conversation_id)
            cleared_rounds += blanked
            cleared_summaries += cleared
            cleared_titles += untitled
            record["cleared_rounds"] = len(blanked)
            record["cleared_summaries"] = len(cleared)
            record["cleared_titles"] = len(untitled)
        return cleared_rounds, cleared_summaries, cleared_titles

    def _index_round(
        self, conversation_id: str, idx: int, user_text: str, assistant_text: str
    ) -> None:
        """In the caller's write: index a round. A round without text, as a forget leaves
        one, gets no index row, as _rebuild_index gives it none."""
        if user_text or assistant_text:
            self._db.execute(
                self._index.insert_round,
                (conversation_id, idx, _round_text(user_text, assistant_text)),
            )

    def _save_batch(
        self,
        batch_id: str,
        applied_at: str,
        *,
        entries: List[List[Any]],
        facts: Sequence[str] = (),
        rounds: Sequence[List[Any]] = (),
        summaries: Sequence[List[Any]] = (),
        titles: Sequence[List[Any]] = (),
        changes: int,
    ) -> None:
        """In the caller's write: keep what a batch did, for ``undo``: each entry row it
        wrote as ``[id, row before (None when added), row after]``, the ids of the facts it
        hid, the rounds, summaries and chat titles it cleared as they were, and its count of
        changes."""
        payload = {
            "entries": entries,
            "facts": list(facts),
            "rounds": list(rounds),
            "summaries": list(summaries),
            "titles": list(titles),
            "changes": changes,
        }
        self._db.execute(
            "INSERT INTO undo_batches (id, applied_at, payload) VALUES (?, ?, ?)",
            (batch_id, applied_at, json.dumps(payload, ensure_ascii=False)),
        )

    def _unchanged_since(self, batch_id: str, payload: Dict[str, Any]) -> bool:
        """In the caller's write: whether every row the batch ``batch_id`` wrote is still as
        it left it, so undoing it overwrites nothing newer."""
        for entry_id, _, after in payload["entries"]:
            rows = self._db.execute(_SELECT_ENTRY_IMAGE, (entry_id,))
            if (list(rows[0]) if rows else None) != after:
                return False
        for fact_id in payload["facts"]:
            hidden_by = self._db.execute("SELECT hidden_batch FROM facts WHERE id = ?", (fact_id,))
            if hidden_by != [(batch_id,)]:
                return False
        for conversation_id, idx, _, _ in payload["rounds"]:
            texts = self._db.execute(
                "SELECT user_text, assistant_text FROM rounds"
                " WHERE conversation_id = ? AND idx = ?",
                (conversation_id, idx),
            )
            if texts != [("", "")]:
                return False
        for conversation_id, _, _ in payload["summaries"]:
            summary = self._db.execute(
                "SELECT summary_title, summary_text FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            if summary != [(None, None)]:
                return False
        for conversation_id, _ in payload["titles"]:
            title = self._db.execute(
                "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
            )
            if title != [("",)]:
                return False
        return True

    def _supersede(self, fact_id: str, by: str, now: str) -> bool:
        """In the caller's write: mark the visible fact ``fact_id`` superseded by ``by``,
        unless a fact has superseded it already. Returns whether it did."""
        self._db.execute(
            "UPDATE facts SET superseded_by = ?, superseded_at = ? WHERE id = ?"
            " AND superseded_by IS NULL AND hidden_at IS NULL",
            (by, now, fact_id),
        )
        return self._changed_rows() > 0

    def _changed_rows(self) -> int:
        """In the caller's write: how many rows the last INSERT, UPDATE or DELETE changed."""
        return int(self._db.execute("SELECT changes()")[0][0])

    def _raise_revision(self) -> None:
        """In the caller's write: raise the ``revision`` by one, once per transaction."""
        if self._revision_raised:
            return
        self._db.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'"
        )
        self._revision_raised = True

    def _new_id(self, kind: str) -> str:
        """In the caller's write: a new id of ``kind`` (see _ID_KINDS), its prefix and a
        number. Numbers only go up (meta keeps the last one issued), so an id is never used
        twice, not even once its row is purged, and one already in use is skipped."""
        prefix, key, in_use = _ID_KINDS[kind]
        self._db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, '0')", (key,))
        while True:
            self._db.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = ?", (key,)
            )
            number = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,))[0][0]
            candidate = prefix + str(number)
            if not self._db.execute(in_use, (candidate,)):
                return candidate

    def _window_start(self) -> str:
        """The time UNDO_WINDOW_S (30) seconds ago: a batch applied before it can no longer
        be undone, and what it hid is purged."""
        return _timestamp(self._clock() - timedelta(seconds=UNDO_WINDOW_S))

    def _add_changes(self, count: int) -> None:
        """In the caller's write: add ``count`` to the changes since the last consolidation,
        or take it away (never below 0)."""
        self._db.execute(
            "UPDATE meta SET value = max(0, CAST(value AS INTEGER) + ?)"
            " WHERE key = 'changes_since_consolidation'",
            (count,),
        )

    def _meta_value(self, key: str) -> Optional[str]:
        rows = self._query("SELECT value FROM meta WHERE key = ?", (key,))
        return str(rows[0][0]) if rows else None

    def _set_meta(self, key: str, value: str) -> None:
        with self._write():
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, params)

    @contextlib.contextmanager
    def _write(self) -> Iterator[None]:
        """Hold the lock and one write transaction for the block. A ``_write`` inside
        another joins it, so the private helpers compose into one transaction. A scrub asked
        for inside runs once the outermost ``_write`` has committed (see ``_scrub``).

        The write has committed by then, so a scrub that fails must not fail the method
        that wrote: it logs a warning, and the scrub stays due for the next write to
        retry.

        Inside a ``_read`` it raises RuntimeError before writing anything. A read can't
        become a write: under WAL, a read transaction whose snapshot is stale gets
        SQLITE_BUSY when it tries to write."""
        with self._lock:
            if self._in_write:
                yield
                return
            if self._in_read:
                raise RuntimeError("a write can't start inside a read: finish the read first")
            self._db.execute("BEGIN IMMEDIATE")
            self._in_write = True
            retry = self._scrub_due  # one that failed after an earlier write
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._scrub_due = retry  # what this write asked for, it never deleted
                with contextlib.suppress(Exception):  # SQLite may have rolled back already
                    self._db.execute("ROLLBACK")
                raise
            finally:
                self._in_write = False
                self._revision_raised = False
            if self._scrub_due:
                self._scrub_due = False
                try:
                    self._scrub()
                except Exception as error:
                    self._scrub_due = True
                    logger.warning(
                        "Memory: scrubbing the store failed (%s); the next write retries it",
                        type(error).__name__,
                    )

    @contextlib.contextmanager
    def _read(self) -> Iterator[None]:
        """Hold the lock and one read transaction for the block, so all its reads see the
        file as it was at the first, whatever another connection commits meanwhile. A
        ``_read`` inside another ``_read`` or a ``_write`` joins its transaction, so readers
        compose; a ``_write`` inside one raises (see ``_write``)."""
        with self._lock:
            if self._in_write or self._in_read:
                yield
                return
            self._db.execute("BEGIN")
            self._in_read = True
            try:
                yield
            except BaseException:
                with contextlib.suppress(Exception):  # SQLite may have ended it already
                    self._db.execute("ROLLBACK")
                raise
            finally:
                self._in_read = False
            self._db.execute("COMMIT")
