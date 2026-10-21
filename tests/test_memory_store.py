"""MemoryStore core: the file's permissions, the backend choice (FTS5 detection and its
fallbacks), the schema and its index kinds, entries and facts, the conversation log, what
deleting leaves in the file, the index text and threads.

Every test that takes ``backend`` runs on each real backend: stdlib ``sqlite3`` (FTS5),
``scan`` (plain index tables) and ``apsw`` when it is installed (Python 3.11+)."""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
import stat
import sys
import threading
import unicodedata
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from talk2view.memory import (
    STOP_WORDS,
    ConversationRecord,
    Fact,
    MemoryEntry,
    MemoryStore,
    code_points,
    index_tokens,
    normalise_dedupe,
    normalise_for_index,
    segment_cjk,
)
from talk2view.memory import store as store_module
from talk2view.storage import _POSIX_PERMISSIONS

posix_only = pytest.mark.skipif(not _POSIX_PERMISSIONS, reason="chmod is skipped on Windows")


def _compiled_with_fts5() -> bool:
    """Read from the build options, independently of the store's own probe."""
    with closing(sqlite3.connect(":memory:")) as conn:
        return ("ENABLE_FTS5",) in conn.execute("PRAGMA compile_options").fetchall()


SQLITE3_HAS_FTS5 = _compiled_with_fts5()
APSW_IMPORTABLE = importlib.util.find_spec("apsw") is not None

SQLITE3 = pytest.param(
    "sqlite3", marks=pytest.mark.skipif(not SQLITE3_HAS_FTS5, reason="sqlite3 lacks FTS5")
)
APSW = pytest.param(
    "apsw", marks=pytest.mark.skipif(not APSW_IMPORTABLE, reason="apsw is not installed")
)
BACKENDS = [SQLITE3, "scan", APSW]
FTS5_BACKENDS = [SQLITE3, APSW]

T0 = datetime(2026, 10, 21, 10, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path, backend: str, clock: FakeClock) -> Iterator[MemoryStore]:
    memory = MemoryStore(tmp_path / "u1.sqlite3", backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        yield memory
    finally:
        memory.close()


def _entry(entry_id: str, text: str, **fields: Any) -> MemoryEntry:
    values: Dict[str, Any] = {
        "id": entry_id,
        "topic": "identity",
        "text": text,
        "created_at": "2026-10-20T09:00:00Z",
        "updated_at": "2026-10-20T09:00:00Z",
        "valid_until": None,
        "source": "said",
        "locked": False,
        "source_conversation_id": None,
    }
    values.update(fields)
    return MemoryEntry(**values)


def _fact(fact_id: str, text: str, **fields: Any) -> Fact:
    values: Dict[str, Any] = {
        "id": fact_id,
        "text": text,
        "topic": "ongoing",
        "recorded_at": "2026-10-20T09:00:00Z",
        "occurred_at": None,
        "valid_until": None,
        "conversation_id": None,
        "superseded_by": None,
        "superseded_at": None,
        "source": "said",
    }
    values.update(fields)
    return Fact(**values)


def _meta(path: Path) -> Dict[str, str]:
    with closing(sqlite3.connect(str(path))) as conn:
        return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def _conversation(store: MemoryStore, conversation_id: str) -> ConversationRecord:
    record = store.conversation(conversation_id)
    assert record is not None
    return record


def _ids(records: List[ConversationRecord]) -> List[str]:
    return [record.id for record in records]


def _indexed_facts(store: MemoryStore) -> List[Tuple[Any, ...]]:
    """The fact index's rows, ``(fact_id, seg_text)``, from the tables of the store's kind."""
    if store.backend == "scan":
        return store._query("SELECT fact_id, seg_text FROM facts_scan ORDER BY fact_id")
    return store._query("SELECT fact_id, seg_text FROM facts_index ORDER BY fact_id")


def _indexed_rounds(store: MemoryStore) -> List[Tuple[Any, ...]]:
    """The round index's rows, ``(conversation_id, idx, seg_text)``."""
    if store.backend == "scan":
        return store._query(
            "SELECT conversation_id, idx, seg_text FROM rounds_scan ORDER BY conversation_id, idx"
        )
    return store._query(
        "SELECT conversation_id, idx, seg_text FROM rounds_index ORDER BY conversation_id, idx"
    )


def _found(store: MemoryStore, word: str) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
    """The facts and the rounds the store's index finds by ``word``: through FTS5's own
    index, or, for scan, by a substring test on the plain tables."""
    if store.backend == "scan":
        pattern = "%" + word + "%"
        return (
            store._query("SELECT fact_id FROM facts_scan WHERE seg_text LIKE ?", (pattern,)),
            store._query(
                "SELECT conversation_id, idx FROM rounds_scan WHERE seg_text LIKE ?", (pattern,)
            ),
        )
    return (
        store._query("SELECT fact_id FROM facts_index WHERE facts_index MATCH ?", (word,)),
        store._query(
            "SELECT conversation_id, idx FROM rounds_index WHERE rounds_index MATCH ?", (word,)
        ),
    )


# ── the file ──


@posix_only
def test_open_creates_file_0600_and_dir_0700(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory" / "u1.sqlite3"
    modes_at_connect: List[int] = []
    real_connect = sqlite3.connect

    def connect_spy(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if database != ":memory:":  # the FTS5 probe
            modes_at_connect.append(stat.S_IMODE(os.stat(database).st_mode))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_spy)
    memory = MemoryStore(path, backend="scan")  # stdlib sqlite3 opens the file
    try:
        memory.record_round("c1", "hello", "hi")  # a write, so the WAL files exist
        # The file existed, already 0600, before SQLite first opened it.
        assert modes_at_connect == [0o600]
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for name in (path.name, path.name + "-wal", path.name + "-shm"):
            assert stat.S_IMODE((path.parent / name).stat().st_mode) == 0o600, name
    finally:
        memory.close()


@posix_only
def test_open_tightens_an_existing_file_and_leaves_an_existing_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "shared"
    directory.mkdir()
    os.chmod(directory, 0o755)  # noqa: S103 — a directory the host app already had
    path = directory / "u1.sqlite3"
    path.touch()
    os.chmod(path, 0o644)
    MemoryStore(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_two_users_two_files(tmp_path: Path) -> None:
    first = MemoryStore(tmp_path / "user-1.sqlite3")
    second = MemoryStore(tmp_path / "user-2.sqlite3")
    try:
        first.record_round("c1", "I am user one", "Hello, one.", at=T0)
        first._insert_entry(_entry("e1", "Is user one."))
        second.record_round("c1", "I am user two", "Hello, two.", at=T0)
        assert first.rounds("c1") == [(1, "I am user one", "Hello, one.", "2026-10-21T10:00:00Z")]
        assert second.rounds("c1") == [(1, "I am user two", "Hello, two.", "2026-10-21T10:00:00Z")]
        assert [entry.id for entry in first.entries()] == ["e1"]
        assert second.entries() == []
    finally:
        first.close()
        second.close()


# ── backends ──


@pytest.mark.skipif(not SQLITE3_HAS_FTS5, reason="this Python's sqlite3 lacks FTS5")
def test_fts5_detection_picks_sqlite3_when_available(tmp_path: Path) -> None:
    assert store_module._fts5_available() is True
    memory = MemoryStore(tmp_path / "u1.sqlite3")
    try:
        assert memory.backend == "sqlite3"
    finally:
        memory.close()


def test_missing_fts5_falls_back_to_apsw_then_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_module, "_fts5_available", lambda: False)
    if APSW_IMPORTABLE:  # apsw needs Python 3.11+; on 3.10 only the scan step runs
        with_apsw = MemoryStore(tmp_path / "apsw.sqlite3")
        try:
            assert with_apsw.backend == "apsw"
            assert with_apsw.record_round("c1", "hello", "hi") == 1
        finally:
            with_apsw.close()
    monkeypatch.setitem(sys.modules, "apsw", None)  # `import apsw` now raises ImportError
    scan = MemoryStore(tmp_path / "scan.sqlite3")
    try:
        assert scan.backend == "scan"
        assert scan.record_round("c1", "hello", "hi") == 1
    finally:
        scan.close()


def test_backend_argument_forces_one_or_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forced = MemoryStore(tmp_path / "scan.sqlite3", backend="scan")
    try:
        assert forced.backend == "scan"
    finally:
        forced.close()
    with pytest.raises(ValueError, match="backend"):
        MemoryStore(tmp_path / "bad.sqlite3", backend="nope")  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "apsw", None)
    with pytest.raises(ImportError, match="apsw"):
        MemoryStore(tmp_path / "apsw.sqlite3", backend="apsw")
    # Both fail before the file is created.
    assert not (tmp_path / "bad.sqlite3").exists()
    assert not (tmp_path / "apsw.sqlite3").exists()


# ── schema ──


def test_schema_version_and_meta_defaults(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "u1.sqlite3"
    MemoryStore(path, backend=backend, clock=FakeClock()).close()  # type: ignore[arg-type]
    assert _meta(path) == {
        "schema_version": "1",
        "paused": "0",
        "include_sensitive": "0",
        "changes_since_consolidation": "0",
        "revision": "0",
        "index_kind": "scan" if backend == "scan" else "fts5",
        # A new store counts the days to its first consolidation from when it was created.
        "last_consolidated_at": "2026-10-21T10:00:00Z",
    }
    # Opening again never resets what is stored.
    with closing(sqlite3.connect(str(path))) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'paused'")
        conn.commit()
    later = FakeClock(T0 + timedelta(days=1))
    MemoryStore(path, backend=backend, clock=later).close()  # type: ignore[arg-type]
    assert _meta(path)["paused"] == "1"
    assert _meta(path)["last_consolidated_at"] == "2026-10-21T10:00:00Z"


def test_index_tables_are_fts5_or_plain_by_backend(store: MemoryStore, backend: str) -> None:
    sql = dict(
        store._query(
            "SELECT name, sql FROM sqlite_master WHERE name IN"
            " ('facts_index', 'rounds_index', 'facts_scan', 'rounds_scan')"
        )
    )
    if backend == "scan":
        assert set(sql) == {"facts_scan", "rounds_scan"}
        for statement in sql.values():
            assert statement.startswith("CREATE TABLE")
    else:
        assert set(sql) == {"facts_index", "rounds_index"}
        for statement in sql.values():
            assert statement.startswith("CREATE VIRTUAL TABLE")
            assert "fts5" in statement
            assert "unicode61 remove_diacritics 2" in statement


@pytest.mark.parametrize("fts5_backend", FTS5_BACKENDS)
def test_switching_index_kind_rebuilds_the_index(tmp_path: Path, fts5_backend: str) -> None:
    """One file, opened in turn with scan and with an FTS5 backend: scan, sqlite3, scan,
    sqlite3 (and the same with apsw). Each open of the other kind rebuilds the index from the
    facts and rounds, so what every earlier store added is found, once, through the current
    store's index."""
    path = tmp_path / "u1.sqlite3"
    words = ["alpha", "bravo", "charlie", "delta"]
    for step, backend in enumerate(["scan", fts5_backend, "scan", fts5_backend]):
        memory = MemoryStore(path, backend=backend)  # type: ignore[arg-type]
        try:
            kind = "scan" if backend == "scan" else "fts5"
            # Read through the store: closing a connection of a second SQLite library (stdlib
            # beside apsw) on an open file deletes the WAL from under the first.
            assert memory._query("SELECT value FROM meta WHERE key = 'index_kind'") == [(kind,)]
            memory._insert_fact(_fact("f{}".format(step), "Said " + words[step] + "."))
            memory.record_round("c1", "Say " + words[step], "OK.", at=T0)
            for earlier, word in enumerate(words[: step + 1]):
                assert _found(memory, word) == ([("f{}".format(earlier),)], [("c1", earlier + 1)])
            # The rebuilt rows are the ones indexing each row as it was written gives.
            assert _indexed_facts(memory) == [
                ("f{}".format(n), normalise_for_index("Said " + word + "."))
                for n, word in enumerate(words[: step + 1])
            ]
            assert _indexed_rounds(memory) == [
                ("c1", n + 1, normalise_for_index("Say " + word + "\nOK."))
                for n, word in enumerate(words[: step + 1])
            ]
            tables = {name for (name,) in memory._query("SELECT name FROM sqlite_master")}
            # FTS5 drops the scan tables. Scan cannot open FTS5's, so it leaves them, stale.
            assert ({"facts_scan", "rounds_scan"} <= tables) == (kind == "scan")
            assert ({"facts_index", "rounds_index"} <= tables) == (step > 0)
        finally:
            memory.close()


@pytest.mark.skipif(not SQLITE3_HAS_FTS5, reason="needs sqlite3 to write the FTS5 tables")
def test_scan_leaves_the_fts5_tables_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A SQLite without FTS5 fails on any statement that names an FTS5 table ("no such
    module: fts5"). So scan, opening a file an FTS5 store wrote, must never name one. An
    authorizer that refuses them stands in for that SQLite."""
    path = tmp_path / "u1.sqlite3"
    word = "quokkazanzibar"  # only chat c1 holds it
    with_fts5 = MemoryStore(path, backend="sqlite3")
    try:
        with_fts5._insert_fact(_fact("f1", "Has a dog called Rex."))
        with_fts5.record_round("c1", "My locker code is " + word, "Noted.", at=T0)
    finally:
        with_fts5.close()

    refused: List[str] = []
    real_connect = sqlite3.connect

    def refuse_fts5_tables(action: int, arg1: Optional[str], arg2: Optional[str], *_: Any) -> int:
        for name in (arg1, arg2):
            if name is not None and name.startswith(("facts_index", "rounds_index")):
                refused.append(name)  # the FTS5 tables and their shadow tables
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = real_connect(database, *args, **kwargs)
        conn.set_authorizer(refuse_fts5_tables)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    scan = MemoryStore(path, backend="scan")
    try:
        scan._insert_fact(_fact("f2", "Has a cat called Tom."))
        scan.record_round("c1", "Bye", "Goodbye.", at=T0)
        scan.delete_conversation("c1")
        assert [fact_id for fact_id, _ in _indexed_facts(scan)] == ["f1", "f2"]
        assert _indexed_rounds(scan) == []
    finally:
        scan.close()
    assert refused == []
    # Scan cannot open the FTS5 tables, so their stale copy still holds the deleted chat...
    assert word.encode() in path.read_bytes()

    # ...until FTS5 takes the file back: it refills its tables and scrubs the file.
    monkeypatch.undo()
    back = MemoryStore(path, backend="sqlite3")
    try:
        assert [fact_id for fact_id, _ in _indexed_facts(back)] == ["f1", "f2"]
        assert _found(back, "tom") == ([("f2",)], [])
        assert _found(back, word) == ([], [])
        assert _indexed_rounds(back) == []
        for name in (path.name, path.name + "-wal"):
            assert word.encode() not in (path.parent / name).read_bytes(), name
    finally:
        back.close()


def test_reopening_with_the_same_index_kind_keeps_the_index(tmp_path: Path, backend: str) -> None:
    """Only a change of kind rebuilds the index; any other open leaves it as it is."""
    path = tmp_path / "u1.sqlite3"
    memory = MemoryStore(path, backend=backend)  # type: ignore[arg-type]
    try:
        memory._insert_fact(_fact("f1", "Has a dog."))
        with memory._write():  # the fact goes and its index row stays; a rebuild would drop it
            memory._db.execute("DELETE FROM facts WHERE id = 'f1'")
    finally:
        memory.close()
    memory = MemoryStore(path, backend=backend)  # type: ignore[arg-type]
    try:
        assert _indexed_facts(memory) == [("f1", normalise_for_index("Has a dog."))]
    finally:
        memory.close()


# ── entries and facts ──


def test_entry_and_fact_round_trip(store: MemoryStore) -> None:
    locked = _entry(
        "e1",
        "Works as a radiologist at St Vincent's.",
        topic="work",
        created_at="2026-10-19T08:30:00Z",
        updated_at="2026-10-21T09:15:00Z",
        valid_until="2027-06-30",
        source="edited",
        locked=True,
        source_conversation_id="c7",
    )
    plain = _entry("e2", "Prefers metric units.", topic="preferences", source="inferred")
    old = _fact(
        "f1",
        "The user planned to fly to Tokyo in December 2026.",
        recorded_at="2026-10-01T12:00:00Z",
        occurred_at="2026-12-01",
        conversation_id="c3",
        superseded_by="f2",
        superseded_at="2026-10-14T08:00:00Z",
        source="inferred",
    )
    new = _fact(
        "f2",
        "On 2026-10-14 the user booked flights to Tokyo for 3-10 Nov 2026.",
        topic="ongoing",
        recorded_at="2026-10-14T08:00:00Z",
        occurred_at="2026-11-03",
        valid_until="2026-11-10",
        conversation_id="c7",
    )
    store._insert_entry(locked)
    store._insert_entry(plain)
    store._insert_fact(old)
    store._insert_fact(new)

    assert store.entries() == [locked, plain]
    assert store.entries()[0].locked is True  # a bool, not SQLite's 1
    assert store.facts() == [new]
    assert store.facts(include_superseded=True) == [old, new]
    # Entries keep their dedupe key; every fact is indexed by its normalised text.
    assert store._query("SELECT id, norm_text FROM entries ORDER BY id") == [
        ("e1", normalise_dedupe(locked.text)),
        ("e2", normalise_dedupe(plain.text)),
    ]
    assert _indexed_facts(store) == [
        ("f1", normalise_for_index(old.text)),
        ("f2", normalise_for_index(new.text)),
    ]


def test_hidden_entries_and_facts_are_left_out(store: MemoryStore) -> None:
    kept, hidden = _entry("e1", "Has a dog."), _entry("e2", "Has a cat.")
    fact, hidden_fact = _fact("f1", "The dog is called Rex."), _fact("f2", "The cat is Tom.")
    for entry in (kept, hidden):
        store._insert_entry(entry)
    for one in (fact, hidden_fact):
        store._insert_fact(one)
    # What removing or forgetting does: hide the row, keeping it for undo.
    with store._write():
        store._db.execute(
            "UPDATE entries SET hidden_at = ?, hidden_batch = ? WHERE id = ?",
            ("2026-10-21T10:00:00Z", "b1", "e2"),
        )
        store._db.execute(
            "UPDATE facts SET hidden_at = ?, hidden_batch = ? WHERE id = ?",
            ("2026-10-21T10:00:00Z", "b1", "f2"),
        )
    assert store.entries() == [kept]
    assert store.entries(include_hidden=True) == [
        kept,
        replace(hidden, hidden_at="2026-10-21T10:00:00Z"),
    ]
    assert store.facts() == [fact]
    assert store.facts(include_superseded=True) == [fact]

    # An entry or fact inserted already hidden stays hidden.
    born_hidden = _entry("e3", "Had a hamster.", hidden_at="2026-10-21T09:00:00Z")
    store._insert_entry(born_hidden)
    store._insert_fact(_fact("f3", "The hamster was Bob.", hidden_at="2026-10-21T09:00:00Z"))
    assert store.entries() == [kept]
    assert store.entries(include_hidden=True)[2] == born_hidden
    assert store.facts() == [fact]


def test_a_failed_write_rolls_back_the_whole_transaction(store: MemoryStore) -> None:
    """The insert helpers join an enclosing ``_write``, so a batch of them is atomic."""
    fact = _fact("f1", "The user has a dog called Rex.")
    # Twice: the second batch must roll back as wholly as the first, so the first failure
    # left no transaction state behind.
    for _ in range(2):
        with pytest.raises(Exception, match="UNIQUE constraint failed"):  # each driver's type
            with store._write():
                store._insert_fact(fact)
                store._insert_entry(_entry("e1", "Has a dog."))
                store._insert_entry(_entry("e1", "Has a dog."))  # the same id again
        assert store.facts() == []
        assert store.entries() == []
        assert _indexed_facts(store) == []
    store._insert_fact(fact)
    assert store.facts() == [fact]


# ── conversations ──


def test_record_round_appends_and_updates_last_active(store: MemoryStore, clock: FakeClock) -> None:
    later = T0 + timedelta(minutes=5)
    assert store.record_round("c1", "Book me a flight", "Booked.", at=T0, title="Trip") == 1
    assert store.record_round("c1", "And a hotel", "Done.", at=later, title="Not this") == 2
    assert store.conversation("c1") == ConversationRecord(
        id="c1",
        title="Trip",
        started_at="2026-10-21T10:00:00Z",
        last_active_at="2026-10-21T10:05:00Z",
        summary_title=None,
        summary_text=None,
        extracted_through=0,
        incognito=False,
    )
    assert store.rounds("c1") == [
        (1, "Book me a flight", "Booked.", "2026-10-21T10:00:00Z"),
        (2, "And a hotel", "Done.", "2026-10-21T10:05:00Z"),
    ]
    assert store.rounds("c1", after=1) == [(2, "And a hotel", "Done.", "2026-10-21T10:05:00Z")]

    # `at` defaults to the clock; a round recorded late never moves last_active_at back.
    clock.now = T0 + timedelta(minutes=1)
    assert store.record_round("c1", "Late", "OK.") == 3
    assert store.rounds("c1", after=2) == [(3, "Late", "OK.", "2026-10-21T10:01:00Z")]
    assert _conversation(store, "c1").last_active_at == "2026-10-21T10:05:00Z"

    # Each round is indexed as the user's message and the reply, normalised.
    assert _indexed_rounds(store) == [
        ("c1", 1, normalise_for_index("Book me a flight\nBooked.")),
        ("c1", 2, normalise_for_index("And a hotel\nDone.")),
        ("c1", 3, normalise_for_index("Late\nOK.")),
    ]

    # A conversation recorded without a title takes the first one given later.
    store.record_round("c2", "Hi", "Hello.", at=T0)
    assert _conversation(store, "c2").title is None
    store.record_round("c2", "Hi again", "Hello.", at=T0, title="Greetings")
    assert _conversation(store, "c2").title == "Greetings"


def test_timestamps_are_utc_iso_to_the_second(store: MemoryStore) -> None:
    eleven_hours_ahead = timezone(timedelta(hours=11))
    store.record_round(
        "c1", "a", "b", at=datetime(2026, 10, 21, 21, 0, 0, tzinfo=eleven_hours_ahead)
    )
    store.record_round("c1", "c", "d", at=T0.replace(second=59, microsecond=999999))
    store.record_round("c1", "e", "f", at=datetime(2026, 10, 21, 12, 0, 0))  # naive: taken as UTC
    assert [at for _, _, _, at in store.rounds("c1")] == [
        "2026-10-21T10:00:00Z",
        "2026-10-21T10:00:59Z",
        "2026-10-21T12:00:00Z",
    ]


def test_delete_conversation_removes_rounds_and_summary_keeps_facts(store: MemoryStore) -> None:
    store.record_round("c1", "I moved to Lyon", "Noted.", at=T0)
    store.record_round("c1", "Find me a dentist", "Here are three.", at=T0)
    store.set_summary("c1", "Moving to Lyon", "The user moved to Lyon.")
    store.record_round("c2", "Hello", "Hi.", at=T0)
    fact = _fact("f1", "The user moved to Lyon in October 2026.", conversation_id="c1")
    entry = _entry("e1", "Lives in Lyon.", source_conversation_id="c1")
    store._insert_fact(fact)
    store._insert_entry(entry)

    store.delete_conversation("c1")

    assert store.conversation("c1") is None
    assert store.rounds("c1") == []
    assert _indexed_rounds(store) == [("c2", 1, normalise_for_index("Hello\nHi."))]
    assert store.rounds("c2") == [(1, "Hello", "Hi.", "2026-10-21T10:00:00Z")]
    # Facts and entries drawn from the chat stay until the user deletes them (spec §7.1).
    assert store.facts() == [fact]
    assert store.entries() == [entry]
    store.delete_conversation("c1")  # deleting again is a no-op


def test_deleting_a_conversation_leaves_none_of_its_text_in_the_files(store: MemoryStore) -> None:
    """Spec §7.1: deleting a chat deletes its text from the file, not just from view.
    secure_delete zeros what SQLite frees, and ``_scrub`` merges FTS5's segments and
    truncates the WAL, so a word only the deleted chat held is in neither file afterwards."""
    word = "quokkazanzibar"  # no other text holds it, and no other term starts with its q
    store.record_round("c1", "My locker code is " + word, "Noted.", at=T0, title="Locker " + word)
    store.set_summary("c1", "Locker " + word, "The user's locker code is " + word + ".")
    store.record_round("c2", "Hello", "Hi.", at=T0)
    files = [store.path, store.path.with_name(store.path.name + "-wal")]
    # Set on every backend, whatever the SQLite build's default (3.12's stdlib has it on).
    assert store._query("PRAGMA secure_delete") == [(1,)]
    assert any(word.encode() in path.read_bytes() for path in files)
    if store.backend != "scan":
        # FTS5 stores the term whole, so the byte scan also covers the index's segments.
        blocks = store._query("SELECT block FROM rounds_index_data")
        assert any(word.encode() in bytes(block) for (block,) in blocks)

    store.delete_conversation("c1")

    for path in files:
        assert not path.exists() or word.encode() not in path.read_bytes(), path.name
    assert _found(store, word) == ([], [])
    assert store.rounds("c2") == [(1, "Hello", "Hi.", "2026-10-21T10:00:00Z")]


def test_scrub_clears_a_deleted_fact_from_the_files(store: MemoryStore) -> None:
    """The facts index is scrubbed too, as purge and forget will need."""
    word = "quokkazanzibar"
    store._insert_fact(_fact("f1", "The locker code is " + word + "."))
    store._insert_fact(_fact("f2", "Has a dog."))
    files = [store.path, store.path.with_name(store.path.name + "-wal")]
    assert any(word.encode() in path.read_bytes() for path in files)
    with store._write():  # deleted as purging a fact will delete it
        store._db.execute("DELETE FROM facts WHERE id = 'f1'")
        if store.backend == "scan":
            store._db.execute("DELETE FROM facts_scan WHERE fact_id = 'f1'")
        else:
            store._db.execute("DELETE FROM facts_index WHERE fact_id = 'f1'")
    store._scrub()
    for path in files:
        assert not path.exists() or word.encode() not in path.read_bytes(), path.name
    assert [fact_id for fact_id, _ in _indexed_facts(store)] == ["f2"]


def test_scrub_warns_when_another_connection_keeps_the_wal(
    store: MemoryStore, backend: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A checkpoint cannot truncate the WAL while another connection reads from it. The
    scrub says so rather than failing silently; the text goes at a later checkpoint."""
    store.record_round("c1", "Hello", "Hi.", at=T0)
    store._db.execute("PRAGMA busy_timeout = 0")  # give up at once, not after 5 s
    reader = MemoryStore(store.path, backend=backend)  # type: ignore[arg-type]
    try:
        reader._db.execute("BEGIN")
        reader._db.execute("SELECT count(*) FROM rounds")  # its read transaction stays open
        with caplog.at_level(logging.WARNING, logger="talk2view.memory"):
            store.delete_conversation("c1")
        reader._db.execute("COMMIT")
    finally:
        reader.close()
    assert "deleted text" in caplog.text
    assert store.rounds("c1") == []  # the delete itself went through


def test_incognito_conversation_records_nothing(store: MemoryStore) -> None:
    store.set_incognito("c1", True)
    assert store.record_round("c1", "Something private", "OK.", at=T0) == 0
    assert store.rounds("c1") == []
    assert _indexed_rounds(store) == []
    assert _conversation(store, "c1").incognito is True
    assert store.unextracted() == []
    assert store.recent_conversations() == []

    # Unmarking an unknown conversation creates nothing; unmarking c1 records again.
    store.set_incognito("c9", False)
    assert store.conversation("c9") is None
    store.set_incognito("c1", False)
    assert store.record_round("c1", "Public now", "OK.", at=T0) == 1


def test_unextracted_lists_conversations_with_new_rounds(
    store: MemoryStore, clock: FakeClock
) -> None:
    for n in range(3):
        store.record_round("partly", "q{}".format(n), "a", at=T0 - timedelta(minutes=30))
    store.mark_extracted("partly", 1)
    for n in range(2):
        store.record_round("done", "q{}".format(n), "a", at=T0 - timedelta(minutes=40))
    store.mark_extracted("done", 2)
    store.record_round("idle", "q", "a", at=T0 - timedelta(minutes=10))  # idle exactly 600 s
    store.record_round("fresh", "q", "a", at=T0 - timedelta(minutes=5))

    # Oldest activity first; with idle_for_s, only those idle that long by the clock.
    assert _ids(store.unextracted()) == ["partly", "idle", "fresh"]
    assert _ids(store.unextracted(idle_for_s=600)) == ["partly", "idle"]
    clock.now = T0 + timedelta(minutes=5)
    assert _ids(store.unextracted(idle_for_s=600)) == ["partly", "idle", "fresh"]

    # extracted_through never moves back; marking every round extracted drops it.
    store.mark_extracted("partly", 0)
    assert _conversation(store, "partly").extracted_through == 1
    store.mark_extracted("partly", 3)
    assert _ids(store.unextracted()) == ["idle", "fresh"]


def test_recent_conversations_newest_first_without_incognito_or_excluded(
    store: MemoryStore,
) -> None:
    for minute, conversation_id in enumerate(["a", "b", "c", "d"]):
        store.record_round(conversation_id, "q", "a", at=T0 + timedelta(minutes=minute))
    store.record_round("e", "q", "a", at=T0 + timedelta(minutes=3))  # ties with d: newer first
    store.set_incognito("secret", True)

    assert _ids(store.recent_conversations()) == ["e", "d", "c", "b", "a"]
    assert _ids(store.recent_conversations(2)) == ["e", "d"]
    assert _ids(store.recent_conversations(exclude="e")) == ["d", "c", "b", "a"]


def test_set_summary_caps_title_120_and_text_600(store: MemoryStore) -> None:
    store.record_round("c1", "q", "a", at=T0)
    accented = "e" + chr(0x301)  # one grapheme, two code points
    store.set_summary("c1", accented * 70, "x" * 601)
    record = _conversation(store, "c1")
    assert record.summary_title == accented * 60  # 120 code points, only 60 graphemes
    assert record.summary_text == "x" * 600

    store.set_summary("c1", "t" * 120, "y" * 600)  # at the caps: kept whole
    record = _conversation(store, "c1")
    assert (record.summary_title, record.summary_text) == ("t" * 120, "y" * 600)

    store.set_summary("unknown", "Title", "Text")  # no record: nothing is created
    assert store.conversation("unknown") is None


def test_concurrent_writes_are_serialised(store: MemoryStore) -> None:
    errors: List[BaseException] = []
    start = threading.Barrier(8)

    def writer(n: int) -> None:
        try:
            start.wait()
            for i in range(50):
                store.record_round("shared", "{}-{}".format(n, i), "reply", at=T0)
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rounds = store.rounds("shared")
    assert [idx for idx, _, _, _ in rounds] == list(range(1, 401))
    assert len({user_text for _, user_text, _, _ in rounds}) == 400
    assert len(_indexed_rounds(store)) == 400


def test_two_stores_on_one_file_wait_for_each_other(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "u1.sqlite3"
    stores = [MemoryStore(path, backend=backend) for _ in range(2)]  # type: ignore[arg-type]
    errors: List[BaseException] = []
    start = threading.Barrier(4)

    def writer(memory: MemoryStore, n: int) -> None:
        try:
            start.wait()
            for i in range(25):
                memory.record_round("shared", "{}-{}".format(n, i), "reply", at=T0)
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(stores[n % 2], n)) for n in range(4)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        assert [idx for idx, _, _, _ in stores[0].rounds("shared")] == list(range(1, 101))
    finally:
        for memory in stores:
            memory.close()


# ── text ──


def test_normalise_for_index() -> None:
    # NFKC folds compatibility forms (fullwidth letters, the fi ligature); casefold lowers.
    fullwidth_t = chr(0xFF34)
    assert normalise_for_index(fullwidth_t + "OKYO " + chr(0xFB01) + "le") == "tokyo file"
    assert normalise_for_index("STRA" + chr(0xDF) + "E") == "strasse"
    # Combining marks (category Mn) go: decomposed and precomposed Vietnamese index alike,
    # Arabic with harakat indexes as the bare letters, a stray mark leaves its base.
    precomposed = "Vi" + chr(0x1EC7) + "t"
    decomposed = "Vie" + chr(0x323) + chr(0x302) + "t"
    assert normalise_for_index(decomposed) == normalise_for_index(precomposed)
    bare = chr(0x643) + chr(0x62A) + chr(0x628)
    with_harakat = chr(0x643) + chr(0x64E) + chr(0x62A) + chr(0x64E) + chr(0x628) + chr(0x64E)
    assert normalise_for_index(with_harakat) == normalise_for_index(bare) == bare
    assert normalise_for_index("x" + chr(0x301)) == "x"
    assert not any(
        unicodedata.category(ch) == "Mn" for ch in normalise_for_index(decomposed + with_harakat)
    )
    # Han characters are spaced apart, so each one is a token.
    assert normalise_for_index(chr(0x4E1C) + chr(0x4EAC)) == chr(0x4E1C) + " " + chr(0x4EAC)
    # Thai too, once its vowel marks (Mn) are gone: sawatdi -> s w s d.
    sawatdi = "".join(chr(cp) for cp in (0xE2A, 0xE27, 0xE31, 0xE2A, 0xE14, 0xE35))
    assert normalise_for_index(sawatdi) == " ".join(chr(cp) for cp in (0xE2A, 0xE27, 0xE2A, 0xE14))


def test_segment_cjk_leaves_latin_alone() -> None:
    assert segment_cjk("hello world") == "hello world"
    wo, qu = chr(0x6211), chr(0x53BB)  # "I go"
    assert segment_cjk(wo + qu + " tokyo") == wo + " " + qu + " tokyo"
    # With no spaces, a CJK code point is set apart from the Latin text on either side, so
    # the Latin word stays one token: "I use Python to write" indexes as I, use, python, write.
    yong, xie = chr(0x7528), chr(0x5199)
    assert segment_cjk(wo + yong + "Python" + xie) == wo + " " + yong + " Python " + xie
    assert normalise_for_index(wo + yong + "Python" + xie) == wo + " " + yong + " python " + xie
    assert segment_cjk("2026" + chr(0x5E74)) == "2026 " + chr(0x5E74)  # digits too: "2026 year"
    # A space or line break already there is left as the only separator.
    assert segment_cjk("Python " + wo + "\n" + yong) == "Python " + wo + "\n" + yong
    hiragana = chr(0x3068) + chr(0x3046) + chr(0x304D)
    assert segment_cjk(hiragana) == " ".join(hiragana)
    tokyo_hangul = chr(0xB3C4) + chr(0xCFC4)  # Korean puts spaces between words already
    assert segment_cjk(tokyo_hangul) == tokyo_hangul


def test_normalise_dedupe_folds_case_space_and_trailing_punctuation() -> None:
    assert normalise_dedupe("  Likes   green TEA. ") == "likes green tea"
    assert normalise_dedupe("Likes green tea!?") == "likes green tea"
    assert normalise_dedupe(chr(0xFB01) + "ne, thanks ;:") == "fine, thanks"
    assert normalise_dedupe("Dr. Lee, the dentist") == "dr. lee, the dentist"


@pytest.mark.skipif(not SQLITE3_HAS_FTS5, reason="sqlite3 lacks FTS5")
def test_index_tokens_are_the_tokens_fts5_makes() -> None:
    """index_tokens gives what the index's tokenizer, unicode61 with remove_diacritics 2,
    makes of normalise_for_index's text, read back through fts5vocab, so the scan backend
    and the forget rules split text as FTS5 does."""
    ca_phe = "c" + chr(0xE0) + " ph" + chr(0xEA)  # cà phê
    sua_da = "s" + chr(0x1EEF) + "a " + chr(0x111) + chr(0xE1)  # sữa đá: đ has no decomposition
    texts = [
        "Uong " + ca_phe + " " + sua_da,
        "O" + chr(0xF9) + " est le caf" + chr(0xE9) + "? " + chr(0xC9) + "T" + chr(0xC9) + "!",
        "Vie" + chr(0x323) + chr(0x302) + "t Nam",  # decomposed marks
        "Deep-dish pizza; don't, C# and C++, e_mail a.b@c.d 2026-10-21 3.5",
        chr(0x6211) + chr(0x53BB) + chr(0x4E1C) + chr(0x4EAC) + "Python" + chr(0x65C5),
        chr(0xFF34) + "OKYO " + chr(0xFB01) + "le STRA" + chr(0xDF) + "E",  # NFKC and casefold
        chr(0x643) + chr(0x64E) + chr(0x62A) + chr(0x64E) + chr(0x628) + chr(0x64E) + " book",
        chr(0xC11C) + chr(0xC6B8) + chr(0xC5D0) + " " + chr(0xAC14) + chr(0xC5B4),  # Korean
        "I " + chr(0x2764) + " tea " + chr(0x1F375) + " and " + chr(0xE001) + "x",
        "",
    ]
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='unicode61 remove_diacritics 2')"
        )
        conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(t, 'instance')")
        conn.executemany(
            "INSERT INTO t (rowid, x) VALUES (?, ?)",
            [(number, normalise_for_index(text)) for number, text in enumerate(texts)],
        )
        for number, text in enumerate(texts):
            made = conn.execute(
                "SELECT term FROM v WHERE doc = ? ORDER BY offset", (number,)
            ).fetchall()
            assert index_tokens(text) == [term for (term,) in made], text
    assert index_tokens("Deep-dish pizza.") == ["deep", "dish", "pizza"]


def test_stop_words_are_48_normalised_function_words() -> None:
    assert len(STOP_WORDS) == 48
    assert {"the", "and", "of"} <= STOP_WORDS
    for word in STOP_WORDS:
        assert normalise_for_index(word) == word
        assert len(word) > 1  # one-letter tokens are dropped by length already


def test_code_points_counts_code_points() -> None:
    assert code_points("e" + chr(0x301)) == 2  # one grapheme
    assert code_points(chr(0x1F600)) == 1  # two UTF-16 units
