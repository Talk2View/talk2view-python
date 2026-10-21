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
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
    List,
    Literal,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from ..storage import _DIR_MODE, _FILE_MODE, _POSIX_PERMISSIONS
from .models import SUMMARY_TEXT_MAX, SUMMARY_TITLE_MAX, ConversationRecord, Fact, MemoryEntry
from .text import normalise_dedupe, normalise_for_index

logger = logging.getLogger("talk2view.memory")

Backend = Literal["sqlite3", "apsw", "scan"]
_BACKENDS: Tuple[Backend, ...] = ("sqlite3", "apsw", "scan")


def utc_now() -> datetime:
    """The store's default clock."""
    return datetime.now(timezone.utc)


def _timestamp(moment: datetime) -> str:
    """``moment`` as the store writes every time: ISO 8601 in UTC to the second, such as
    ``2026-10-21T10:00:00Z``. The width never varies, so text order is time order. A naive
    datetime is taken as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
)

# The search index, kept by the store (no triggers): one row per fact and per round, whose
# ``seg_text`` is ``normalise_for_index`` of the fact's text, or of the round's user message
# and reply. The sqlite3 and apsw backends keep it in the FTS5 tables ``facts_index`` and
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
    delete_rounds="DELETE FROM rounds_scan WHERE conversation_id = ?",
    optimize=(),  # plain tables keep nothing of a deleted row
    # Scan cannot open FTS5 tables, so it leaves them. They go stale until an FTS5 store's
    # rebuild clears and refills them.
    drop_other=(),
)


def _round_text(user_text: str, assistant_text: str) -> str:
    """A round's ``seg_text``."""
    return normalise_for_index(user_text + "\n" + assistant_text)


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

    def _insert_entry(self, entry: MemoryEntry) -> None:
        """Add one entry, with its dedupe key (``norm_text``)."""
        with self._write():
            self._db.execute(
                "INSERT INTO entries (id, topic, text, norm_text, created_at, updated_at,"
                " valid_until, source, locked, source_conversation_id, hidden_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            self._db.execute(self._index.insert_fact, (fact.id, normalise_for_index(fact.text)))

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
            self._db.execute(
                self._index.insert_round,
                (conversation_id, idx, _round_text(user_text, assistant_text)),
            )
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
        without one."""
        with self._write():
            self._db.execute(
                "UPDATE conversations SET summary_title = ?, summary_text = ? WHERE id = ?",
                (title[:SUMMARY_TITLE_MAX], text[:SUMMARY_TEXT_MAX], conversation_id),
            )

    def mark_extracted(self, conversation_id: str, through: int) -> None:
        """Record that extraction has seen the conversation's rounds up to index
        ``through``. It never moves back."""
        with self._write():
            self._db.execute(
                "UPDATE conversations SET extracted_through = max(extracted_through, ?)"
                " WHERE id = ?",
                (through, conversation_id),
            )

    def set_incognito(self, conversation_id: str, incognito: bool) -> None:
        """Mark a conversation incognito, so its rounds are never written, or not. Marking
        an unknown conversation creates its record; unmarking one does nothing."""
        now = _timestamp(self._clock())
        with self._write():
            if incognito:
                self._db.execute(
                    "INSERT OR IGNORE INTO conversations (id, started_at, last_active_at)"
                    " VALUES (?, ?, ?)",
                    (conversation_id, now, now),
                )
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
        With ``idle_for_s``, only those idle at least that long by the clock."""
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
            " WHERE r.conversation_id = c.id AND r.idx > c.extracted_through)"
            " ORDER BY c.last_active_at, c.rowid",
            (cutoff, cutoff),
        )
        return [_conversation_from_row(row) for row in rows]

    def rounds(self, conversation_id: str, *, after: int = 0) -> List[Tuple[int, str, str, str]]:
        """The conversation's rounds with index above ``after``, in order, as
        ``(idx, user_text, assistant_text, at)``."""
        rows = self._query(
            "SELECT idx, user_text, assistant_text, at FROM rounds"
            " WHERE conversation_id = ? AND idx > ? ORDER BY idx",
            (conversation_id, after),
        )
        return [(row[0], row[1], row[2], row[3]) for row in rows]

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
                _META_DEFAULTS + (("index_kind", self._index.kind),),
            )
            recorded = self._db.execute("SELECT value FROM meta WHERE key = 'index_kind'")
            rebuilt = recorded != [(self._index.kind,)]
            if rebuilt:
                self._rebuild_index()
        if rebuilt:
            # The cleared index's segments still hold its old text, deleted rows' included.
            self._scrub()

    def _rebuild_index(self) -> None:
        """Fill this store's kind of index afresh from ``facts`` and ``rounds``, in the
        caller's transaction. The file was indexed the other way, so the other kind's writes
        are missing from these tables, and whatever they hold is stale."""
        for statement in self._index.clear:
            self._db.execute(statement)
        facts = self._db.execute("SELECT id, text FROM facts ORDER BY rowid")
        self._db.executemany(
            self._index.insert_fact,
            [(fact_id, normalise_for_index(text)) for fact_id, text in facts],
        )
        rounds = self._db.execute(
            "SELECT conversation_id, idx, user_text, assistant_text FROM rounds ORDER BY rowid"
        )
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
        segment, then checkpoint the WAL into the file and truncate it. A checkpoint fails
        inside a transaction, so this never runs inside ``_write``."""
        with self._lock:
            with self._write():
                for statement in self._index.optimize:
                    self._db.execute(statement)
            busy = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")[0][0]
        if busy:
            logger.warning(
                "Memory: another connection is reading the store, so deleted text stays in"
                " its WAL file until a later checkpoint"
            )

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        with self._lock:
            return self._db.execute(sql, params)

    @contextlib.contextmanager
    def _write(self) -> Iterator[None]:
        """Hold the lock and one write transaction for the block. A ``_write`` inside
        another joins it, so the private helpers compose into one transaction."""
        with self._lock:
            if self._in_write:
                yield
                return
            self._db.execute("BEGIN IMMEDIATE")
            self._in_write = True
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(Exception):  # SQLite may have rolled back already
                    self._db.execute("ROLLBACK")
                raise
            finally:
                self._in_write = False
