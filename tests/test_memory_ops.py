"""Memory op rules (spec §7.3): ``apply_entry_ops``, the pure function every client's
store mirrors and the contract fixtures pin, and the store methods built on it.

Every test that takes ``store`` runs on each real backend, as test_memory_store's do."""

from __future__ import annotations

import itertools
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import pytest

from talk2view.memory import Fact, MemoryEntry, MemoryStore, normalise_for_index
from talk2view.memory.ops import apply_entry_ops

from .test_memory_store import (
    BACKENDS,
    T0,
    FakeClock,
    _entry,
    _found,
    _indexed_facts,
    _indexed_rounds,
)

NOW = "2026-10-21T10:00:00Z"  # T0 as the store writes it
WORD = "quokkazanzibar"  # no other text holds it, and no other term starts with its q

# Every row of each table undo restores, every column and the rowid: what an exact undo must
# give back. Literal statements, so no SQL is assembled from names.
_ALL_ROWS = {
    "entries": "SELECT rowid, * FROM entries ORDER BY rowid",
    "facts": "SELECT rowid, * FROM facts ORDER BY rowid",
    "rounds": "SELECT rowid, * FROM rounds ORDER BY rowid",
    "conversations": "SELECT rowid, * FROM conversations ORDER BY rowid",
}


def ids(prefix: str = "e") -> Callable[[], str]:
    """``next_id``: e1, e2, ..."""
    numbers = itertools.count(1)
    return lambda: prefix + str(next(numbers))


def add(text: str, topic: str = "preferences", **fields: Any) -> Dict[str, Any]:
    return {"op": "add", "topic": topic, "text": text, **fields}


def at(seconds: float) -> str:
    """The timestamp ``seconds`` after T0, as the store writes it."""
    return (T0 + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _rows(store: MemoryStore, table: str) -> List[Tuple[Any, ...]]:
    """Every row of ``table`` (see _ALL_ROWS)."""
    return store._query(_ALL_ROWS[table])


def _fail_last_write(store: MemoryStore) -> None:
    """Make every count of changes fail, as a full disk would: a batch, a delete and an undo
    each end with one. A temporary trigger on the store's own connection, kept out of the
    file, so real SQLite rolls the transaction back. ``_stop_failing`` drops it."""
    store._db.execute(
        "CREATE TEMP TRIGGER fail BEFORE UPDATE ON meta"
        " WHEN NEW.key = 'changes_since_consolidation'"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )


def _stop_failing(store: MemoryStore) -> None:
    store._db.execute("DROP TRIGGER fail")


def _in_files(store: MemoryStore, word: str) -> bool:
    """Whether the store's file or its WAL holds ``word``, as raw bytes."""
    files = [store.path, store.path.with_name(store.path.name + "-wal")]
    return any(path.exists() and word.encode() in path.read_bytes() for path in files)


# ── apply_entry_ops ──


def test_add_assigns_id_dates_and_source() -> None:
    entries, applied, dropped = apply_entry_ops(
        [],
        [
            add("Prefers metric units."),
            # An add never carries an id: the device assigns them.
            add("Flying to Tokyo.", topic="ongoing", valid_until="2026-11-10", id="x9"),
        ],
        now=NOW,
        next_id=ids(),
    )
    assert entries == [
        MemoryEntry(
            id="e1",
            topic="preferences",
            text="Prefers metric units.",
            created_at=NOW,
            updated_at=NOW,
            valid_until=None,
            source="said",
            locked=False,
            source_conversation_id=None,
        ),
        MemoryEntry(
            id="e2",
            topic="ongoing",
            text="Flying to Tokyo.",
            created_at=NOW,
            updated_at=NOW,
            valid_until="2026-11-10",
            source="said",
            locked=False,
            source_conversation_id=None,
        ),
    ]
    assert applied == [
        {"op": "add", "id": "e1", "topic": "preferences", "text": "Prefers metric units."},
        {
            "op": "add",
            "id": "e2",
            "topic": "ongoing",
            "text": "Flying to Tokyo.",
            "valid_until": "2026-11-10",
        },
    ]
    assert dropped == []
    # The caller says where the ops came from: extraction's are "inferred".
    inferred, _, _ = apply_entry_ops(
        [], [add("Likes tea.")], now=NOW, next_id=ids(), source="inferred"
    )
    assert [entry.source for entry in inferred] == ["inferred"]


def test_add_skips_duplicate_normalised_text(store: MemoryStore) -> None:
    # The store dedupes across batches.
    store.apply_ops([add("Prefers metric units.")])
    batch = store.apply_ops([add("prefers  METRIC units")])
    assert [entry.text for entry in store.entries()] == ["Prefers metric units."]
    assert (batch.applied, batch.dropped) == ([], ["duplicate"])

    existing = _entry("e1", "Prefers metric units.")
    entries, applied, dropped = apply_entry_ops(
        [existing],
        [add("prefers  METRIC units"), add("Likes tea"), add("likes tea!")],
        now=NOW,
        next_id=ids("n"),
    )
    # The first is the same as e1 once normalised; the third the same as the second.
    assert [entry.text for entry in entries] == ["Prefers metric units.", "Likes tea"]
    assert [record["id"] for record in applied] == ["n1"]
    assert dropped == ["duplicate", "duplicate"]
    # A removed (hidden) entry is no longer there to be duplicated.
    hidden = replace(existing, hidden_at="2026-10-21T09:59:00Z")
    entries, _, dropped = apply_entry_ops(
        [hidden], [add("Prefers metric units.")], now=NOW, next_id=ids("n")
    )
    assert [(entry.id, entry.hidden_at) for entry in entries] == [
        ("e1", "2026-10-21T09:59:00Z"),
        ("n1", None),
    ]
    assert dropped == []


def test_update_replaces_fields_and_sets_updated_at() -> None:
    old = _entry("e1", "Lives in Melbourne.", topic="identity", valid_until="2026-12-31")
    other = _entry("e2", "Likes tea.")
    entries, applied, dropped = apply_entry_ops(
        [old, other],
        [
            {"op": "update", "id": "e1", "text": "Lives in Sydney."},
            {"op": "update", "id": "e2", "topic": "preferences", "valid_until": "2027-01-01"},
        ],
        now=NOW,
        next_id=ids(),
    )
    # Only the given fields change; created_at, source and the rest stay.
    assert entries == [
        replace(old, text="Lives in Sydney.", updated_at=NOW),
        replace(other, topic="preferences", valid_until="2027-01-01", updated_at=NOW),
    ]
    assert applied == [
        {"op": "update", "id": "e1", "text": "Lives in Sydney."},
        {"op": "update", "id": "e2", "topic": "preferences", "valid_until": "2027-01-01"},
    ]
    assert dropped == []

    # An update that gives no field, or only the values the entry has, changes nothing,
    # so applying an update twice leaves updated_at where the first left it.
    entries, applied, dropped = apply_entry_ops(
        entries,
        [
            {"op": "update", "id": "e1"},
            {"op": "update", "id": "e1", "text": "Lives in Sydney.", "topic": "identity"},
        ],
        now="2026-10-21T10:05:00Z",
        next_id=ids(),
    )
    assert entries[0] == replace(old, text="Lives in Sydney.", updated_at=NOW)
    assert applied == []
    assert dropped == ["changes nothing", "changes nothing"]


def test_update_locked_is_dropped() -> None:
    locked = _entry("e1", "Allergic to peanuts.", locked=True, source="edited")
    entries, applied, dropped = apply_entry_ops(
        [locked],
        [
            {"op": "update", "id": "e1", "text": "Not allergic to anything."},
            {"op": "remove", "id": "e1"},
            {"op": "forget", "text": "peanuts"},
        ],
        now=NOW,
        next_id=ids(),
    )
    # Changes the engine proposes never touch a locked entry. A forget passes it by, and
    # says so: one "locked" for each locked entry it would have hidden.
    assert entries == [locked]
    assert applied == [{"op": "forget", "text": "peanuts", "entry_ids": []}]
    assert dropped == ["locked", "locked", "locked"]


def test_update_missing_is_dropped() -> None:
    hidden = _entry("e1", "Has a dog.", hidden_at="2026-10-21T09:59:50Z")
    entries, applied, dropped = apply_entry_ops(
        [hidden],
        [
            {"op": "update", "id": "e7", "text": "Has a cat."},  # never there
            {"op": "update", "id": "e1", "text": "Has a cat."},  # removed, in its undo window
            {"op": "remove", "id": "e1"},
            {"op": "update", "id": "e 1", "text": "Has a cat."},  # not an id at all
            {"op": "remove", "id": "e" * 33},
            {"op": "remove"},
        ],
        now=NOW,
        next_id=ids(),
    )
    assert entries == [hidden]
    assert applied == []
    assert dropped == ["missing", "missing", "missing", "bad id", "bad id", "bad id"]


def test_caps_measured_in_code_points(store: MemoryStore) -> None:
    accented = "e" + chr(0x301)  # one grapheme, two code points
    at_cap = accented * 150  # 300 code points, 150 graphemes
    over_cap = at_cap + "x"
    batch = store.apply_ops([add(at_cap), add(over_cap)])
    assert [entry.text for entry in store.entries()] == [at_cap]
    assert batch.dropped == ["text over 300 code points"]

    target = _entry("e1", "Short.")
    entries, applied, dropped = apply_entry_ops(
        [target],
        [
            add(at_cap),
            add(over_cap),
            {"op": "update", "id": "e1", "text": over_cap},
            {"op": "forget", "text": over_cap},
            # A character outside the BMP is one code point (two UTF-16 units).
            add(chr(0x1F600) * 300),
        ],
        now=NOW,
        next_id=ids("n"),
    )
    assert [entry.text for entry in entries] == ["Short.", at_cap, chr(0x1F600) * 300]
    assert [record["id"] for record in applied] == ["n1", "n2"]
    assert dropped == ["text over 300 code points"] * 3


def test_ops_are_checked_as_the_engine_checks_them() -> None:
    """The device accepts every op the engine's validate_ops passes and drops the rest with
    a short rule name, never the op's text. Fields an op doesn't use are ignored unchecked,
    as the engine strips them."""
    target = _entry("e1", "Has a dog.")
    entries, applied, dropped = apply_entry_ops(
        [target],
        [
            "add",  # not an object
            {"op": "rename", "id": "e1"},
            {"text": "Has a cat."},
            {"op": "add", "text": "Has a cat."},
            {"op": "add", "topic": "pets", "text": "Has a cat."},
            {"op": "add", "topic": "people"},
            {"op": "add", "topic": "people", "text": ""},
            {"op": "add", "topic": "people", "text": 7},
            {"op": "add", "topic": "people", "text": "Has a cat.", "valid_until": "next week"},
            {"op": "add", "topic": "people", "text": "Has a cat.", "valid_until": "2026-1-1"},
            {"op": "update", "id": "e1", "topic": "pets"},
            {"op": "update", "id": "e1", "valid_until": "2026-11-31x"},
            {"op": "forget"},
            # A forget's phrase is measured once normalised, and needs 2 code points: one
            # letter is in nearly every text. Nothing is left of the first; "e" and a
            # combining acute are one code point after NFKC.
            {"op": "forget", "text": " ...! "},
            {"op": "forget", "text": " A. "},
            {"op": "forget", "text": "e" + chr(0x301)},
            # Accepted: the fields each op uses are well formed; the rest are ignored.
            {"op": "remove", "id": "e1", "topic": "pets", "text": 7},
            {"op": "add", "topic": "people", "text": "Has a cat.", "id": None, "extra": 1},
        ],
        now=NOW,
        next_id=ids("n"),
    )
    assert dropped == [
        "not an op",
        "not an op",
        "not an op",
        "no topic",
        "unknown topic",
        "no text",
        "no text",
        "no text",
        "bad date",
        "bad date",
        "unknown topic",
        "bad date",
        "no text",
        "forget text too short",
        "forget text too short",
        "forget text too short",
    ]
    assert applied == [
        {"op": "remove", "id": "e1"},
        {"op": "add", "id": "n1", "topic": "people", "text": "Has a cat."},
    ]
    assert [(entry.id, entry.hidden_at) for entry in entries] == [("e1", NOW), ("n1", None)]
    # A date is checked for its shape, as the engine checks it: YYYY-MM-DD in ASCII digits.
    _, _, dropped = apply_entry_ops(
        [],
        [add("Trip.", valid_until=chr(0x0662) + "026-11-10")],  # an Arabic-Indic two
        now=NOW,
        next_id=ids(),
    )
    assert dropped == ["bad date"]


def test_forget_hides_entries_whose_normalised_text_holds_it() -> None:
    peanuts = _entry("e1", "Allergic to PEANUTS.")
    butter = _entry("e2", "Loves peanut  butter on toast.")
    tea = _entry("e3", "Likes tea.")
    sauce = _entry("e4", "Peanut sauce is fine.", locked=True, source="edited")
    entries, applied, dropped = apply_entry_ops(
        [peanuts, butter, tea, sauce],
        [
            {"op": "forget", "text": "Peanut."},
            {"op": "forget", "text": "coffee"},
            {"op": "forget", "text": "TE"},  # two code points: the least a phrase can be
        ],
        now=NOW,
        next_id=ids(),
    )
    # Matched on the normalised text: case, spacing and trailing punctuation fold away. The
    # user's own (locked) entry is passed by, and reported.
    assert entries == [
        replace(peanuts, hidden_at=NOW),
        replace(butter, hidden_at=NOW),
        replace(tea, hidden_at=NOW),
        sauce,
    ]
    # A forget that matches nothing is applied, and says so.
    assert applied == [
        {"op": "forget", "text": "Peanut.", "entry_ids": ["e1", "e2"]},
        {"op": "forget", "text": "coffee", "entry_ids": []},
        {"op": "forget", "text": "TE", "entry_ids": ["e3"]},
    ]
    assert dropped == ["locked"]


def test_apply_entry_ops_is_pure_and_deterministic() -> None:
    entries = [_entry("e1", "Has a dog."), _entry("e2", "Likes tea.")]
    ops: List[Dict[str, Any]] = [
        add("Has a dog."),  # a duplicate: no id is taken for it
        add("Has a cat."),
        {"op": "update", "id": "e2", "text": "Likes green tea."},
        {"op": "remove", "id": "e1"},
    ]
    snapshot = (list(entries), [dict(op) for op in ops])
    calls: List[str] = []

    def next_id() -> str:
        calls.append("n{}".format(len(calls) + 1))
        return calls[-1]

    first = apply_entry_ops(entries, ops, now=NOW, next_id=next_id)
    assert calls == ["n1"]  # one id per add applied, none for the dropped one
    assert (entries, ops) == snapshot  # the arguments are left as they were
    assert apply_entry_ops(entries, ops, now=NOW, next_id=ids("n")) == first
    assert first == (
        [
            replace(entries[0], hidden_at=NOW),
            replace(entries[1], text="Likes green tea.", updated_at=NOW),
            MemoryEntry(
                id="n1",
                topic="preferences",
                text="Has a cat.",
                created_at=NOW,
                updated_at=NOW,
                valid_until=None,
                source="said",
                locked=False,
                source_conversation_id=None,
            ),
        ],
        [
            {"op": "add", "id": "n1", "topic": "preferences", "text": "Has a cat."},
            {"op": "update", "id": "e2", "text": "Likes green tea."},
            {"op": "remove", "id": "e1"},
        ],
        ["duplicate"],
    )


# ── the store: batches, undo and purge ──


def test_apply_ops_stores_what_the_rules_return(store: MemoryStore, clock: FakeClock) -> None:
    first = store.apply_ops(
        [add("Has a dog.", topic="people"), add("Likes tea.")], source="inferred"
    )
    assert first.applied == [
        {"op": "add", "id": "e1", "topic": "people", "text": "Has a dog."},
        {"op": "add", "id": "e2", "topic": "preferences", "text": "Likes tea."},
    ]
    assert (first.dropped, first.removed_entries, first.removed_facts) == ([], [], [])
    assert [(e.id, e.source, e.created_at, e.updated_at) for e in store.entries()] == [
        ("e1", "inferred", NOW, NOW),
        ("e2", "inferred", NOW, NOW),
    ]

    clock.now = T0 + timedelta(minutes=1)
    ops = [
        {"op": "update", "id": "e1", "text": "Has two dogs."},
        {"op": "remove", "id": "e2"},
        add("has two dogs"),  # the same as e1's new text
        add("Likes tea."),  # e2 is hidden now, so this is new
    ]
    expected = apply_entry_ops(store.entries(), ops, now=at(60), next_id=iter(["e3"]).__next__)
    batch = store.apply_ops(ops)
    assert store.entries(include_hidden=True) == expected[0]
    assert (batch.applied, batch.dropped) == (expected[1], expected[2])
    assert batch.removed_entries == [store.entries(include_hidden=True)[1]]
    # The dedupe key follows each entry's text.
    assert store._query("SELECT id, norm_text FROM entries ORDER BY rowid") == [
        ("e1", "has two dogs"),
        ("e2", "likes tea"),
        ("e3", "likes tea"),
    ]
    # "edited" belongs to the user's own edit alone, which locks the entry.
    for source in ("guessed", "edited"):
        with pytest.raises(ValueError, match="source"):
            store.apply_ops([add("Has a cat.")], source=source)


def test_remove_hides_then_purge_after_window(store: MemoryStore, clock: FakeClock) -> None:
    store.apply_ops([add("My locker code is " + WORD + ".", topic="ongoing"), add("Likes tea.")])
    locker = store.entries()[0]
    batch = store.apply_ops([{"op": "remove", "id": "e1"}])
    assert batch.removed_entries == [replace(locker, hidden_at=NOW)]
    assert [entry.id for entry in store.entries()] == ["e2"]
    assert [(e.id, e.hidden_at) for e in store.entries(include_hidden=True)] == [
        ("e1", NOW),
        ("e2", None),
    ]
    assert _in_files(store, WORD)

    for seconds in (29, 30):  # 30 s is the window's last second
        clock.now = T0 + timedelta(seconds=seconds)
        assert store.purge() == 0
        assert [entry.id for entry in store.entries(include_hidden=True)] == ["e1", "e2"]
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 1
    assert [entry.id for entry in store.entries(include_hidden=True)] == ["e2"]
    assert store.undo(batch.id) is False
    # Deleted from the files, not just from view: the row and the batches' copies of it.
    assert store._query("SELECT count(*) FROM undo_batches") == [(0,)]
    assert not _in_files(store, WORD)
    assert store.purge() == 0


def test_undo_restores_a_batch_within_window(store: MemoryStore, clock: FakeClock) -> None:
    store.apply_ops([add("Has a dog.", topic="people"), add("Likes tea.")])
    entries_before = _rows(store, "entries")
    changes_before = store.changes_since_consolidation()
    clock.now = T0 + timedelta(seconds=5)
    batch = store.apply_ops(
        [
            add("My locker code is " + WORD + "."),
            {"op": "remove", "id": "e2"},
            {"op": "update", "id": "e1", "text": "Has two dogs."},
        ]
    )
    assert [entry.text for entry in store.entries()] == [
        "Has two dogs.",
        "My locker code is " + WORD + ".",
    ]

    clock.now = T0 + timedelta(seconds=35)  # 30 s after the batch: still undoable
    assert store.undo(batch.id) is True
    # Exactly as before, every column and rowid; the added entry is deleted, not hidden.
    assert _rows(store, "entries") == entries_before
    assert store.changes_since_consolidation() == changes_before
    assert not _in_files(store, WORD)
    assert store.undo(batch.id) is False  # once only

    late = store.apply_ops([add("Has a cat.")])
    clock.now = T0 + timedelta(seconds=66)  # 31 s after it
    assert store.undo(late.id) is False
    assert [entry.text for entry in store.entries()] == ["Has a dog.", "Likes tea.", "Has a cat."]
    assert store.undo("b404") is False
    # A batch that changed nothing has nothing to undo.
    assert store.undo(store.apply_ops([add("Has a cat.")]).id) is False


def test_undo_never_overwrites_a_later_change(store: MemoryStore) -> None:
    first = store.apply_ops([add("Has a dog.")])
    second = store.apply_ops([{"op": "update", "id": "e1", "text": "Has two dogs."}])
    assert store.undo(first.id) is False  # e1 has changed since
    assert [entry.text for entry in store.entries()] == ["Has two dogs."]
    assert store.undo(second.id) is True
    assert store.undo(first.id) is True  # undone in order, both go
    assert store.entries(include_hidden=True) == []
    # The user's own edit is a later change too.
    third = store.apply_ops([add("Likes tea.")])
    store.edit_entry("e2", text="Likes green tea.")
    assert store.undo(third.id) is False
    assert [entry.text for entry in store.entries()] == ["Likes green tea."]
    # Whatever changed a row the batch wrote, the undo refuses: here a fact it hid, shown
    # again by a writer outside the store's methods.
    store.add_facts(
        [{"text": "Has a cat.", "topic": "people"}], conversation_id=None, source="said"
    )
    forgot = store.forget("cat")
    store._db.execute("UPDATE facts SET hidden_at = NULL, hidden_batch = NULL")
    assert store.undo(forgot.id) is False
    assert store._query("SELECT id, hidden_at FROM facts") == [("f1", None)]


def test_a_failed_batch_or_undo_leaves_nothing_behind(store: MemoryStore) -> None:
    """apply_ops, delete_entry and undo each run in one transaction: a failure at their last
    write rolls back every write before it, ids taken included."""
    tables = ("entries", "facts", "rounds", "conversations")
    store.apply_ops([add("Has a dog.")])
    store.add_facts(
        [{"text": "The dog is called Rex.", "topic": "people"}], conversation_id=None, source="said"
    )
    store.record_round("c1", "My dog is called Rex.", "Noted.", at=T0)
    store.set_summary("c1", "Dog", "The user's dog is called Rex.")
    before = [_rows(store, table) for table in tables]
    rounds_index = _indexed_rounds(store)

    _fail_last_write(store)
    with pytest.raises(Exception, match="disk full"):  # each driver's type
        store.apply_ops([add("Likes tea."), {"op": "forget", "text": "dog"}])
    with pytest.raises(Exception, match="disk full"):
        store.delete_entry("e1")
    _stop_failing(store)
    assert [_rows(store, table) for table in tables] == before
    assert _indexed_rounds(store) == rounds_index
    assert store._query("SELECT id FROM undo_batches") == [("b1",)]

    batch = store.apply_ops([add("Likes tea."), {"op": "forget", "text": "dog"}])
    assert batch.id == "b2"  # the failed batches' ids were not used up
    assert batch.applied[0]["id"] == "e2"
    applied = [_rows(store, table) for table in tables]
    _fail_last_write(store)
    with pytest.raises(Exception, match="disk full"):
        store.undo(batch.id)
    _stop_failing(store)
    assert [_rows(store, table) for table in tables] == applied
    assert _indexed_rounds(store) == []
    assert store.undo(batch.id) is True
    assert [_rows(store, table) for table in tables] == before
    assert _indexed_rounds(store) == rounds_index


def test_forget_matches_entries_and_facts_and_reports_them(
    store: MemoryStore, clock: FakeClock
) -> None:
    store.apply_ops([add("Allergic to peanuts.", topic="identity"), add("Likes tea.")])
    store.add_facts(
        [{"text": "The user is allergic to peanuts and " + WORD + ".", "topic": "identity"}],
        conversation_id="c1",
        source="said",
    )
    tea = store.add_facts(
        [
            {
                "text": "The user is allergic to peanuts only.",
                "topic": "identity",
                "supersedes": ["f1"],
            },
            {"text": "The user drinks green tea.", "topic": "preferences"},
        ],
        conversation_id="c1",
        source="said",
    )[1]
    entries_before, facts_before = store.entries(), store.facts(include_superseded=True)
    rows_before = (_rows(store, "entries"), _rows(store, "facts"))

    batch = store.forget("Peanut")
    assert batch.removed_entries == [replace(entries_before[0], hidden_at=NOW)]
    # The superseded fact goes too: it says the same.
    assert batch.removed_facts == [replace(fact, hidden_at=NOW) for fact in facts_before[:2]]
    assert batch.applied == [
        {
            "op": "forget",
            "text": "Peanut",
            "entry_ids": ["e1"],
            "fact_ids": ["f1", "f2"],
            "cleared_rounds": 0,
            "cleared_summaries": 0,
        }
    ]
    assert (batch.dropped, batch.cleared_rounds, batch.cleared_summaries) == ([], 0, 0)
    assert store.entries() == entries_before[1:]
    assert store.facts(include_superseded=True) == [tea]

    assert store.undo(batch.id) is True
    assert (_rows(store, "entries"), _rows(store, "facts")) == rows_before

    # Forgotten again and past the window, the fact is deleted from the files: its row and
    # its index row.
    store.forget("peanut")
    assert _in_files(store, WORD)
    if store.backend != "scan":  # FTS5 keeps the term whole, so the byte scan covers it
        blocks = store._query("SELECT block FROM facts_index_data")
        assert any(WORD.encode() in bytes(block) for (block,) in blocks)
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 3
    assert not _in_files(store, WORD)
    assert [fact_id for fact_id, _ in _indexed_facts(store)] == ["f3"]
    # The index holds what rebuilding it from the facts gives.
    indexed = _indexed_facts(store)
    with store._write():
        store._rebuild_index()
    assert _indexed_facts(store) == indexed


def test_reapplying_a_batch_is_idempotent(store: MemoryStore, clock: FakeClock) -> None:
    """Spec §9 rule 8: extraction may repeat what update_memory did."""
    store.apply_ops([add("Has a dog."), add("Likes tea.")])
    ops = [add("Prefers metric units."), {"op": "remove", "id": "e2"}]
    first = store.apply_ops(ops)
    clock.now = T0 + timedelta(seconds=5)
    second = store.apply_ops(ops)
    assert (second.applied, second.dropped) == ([], ["duplicate", "missing"])
    assert [entry.text for entry in store.entries()] == ["Has a dog.", "Prefers metric units."]
    # e2 is hidden once, by the first batch, which alone can bring it back.
    assert store._query("SELECT hidden_at, hidden_batch FROM entries WHERE id = 'e2'") == [
        (NOW, first.id)
    ]
    assert store.undo(second.id) is False
    assert store.undo(first.id) is True
    assert [entry.text for entry in store.entries()] == ["Has a dog.", "Likes tea."]


def test_an_entry_added_and_forgotten_in_one_batch(store: MemoryStore) -> None:
    batch = store.apply_ops([add("Likes peanuts."), {"op": "forget", "text": "peanut"}])
    assert batch.applied == [
        {"op": "add", "id": "e1", "topic": "preferences", "text": "Likes peanuts."},
        {
            "op": "forget",
            "text": "peanut",
            "entry_ids": ["e1"],
            "fact_ids": [],
            "cleared_rounds": 0,
            "cleared_summaries": 0,
        },
    ]
    assert batch.removed_entries == store.entries(include_hidden=True)
    assert store._query("SELECT id, hidden_at, hidden_batch FROM entries") == [
        ("e1", NOW, batch.id)
    ]
    assert store.undo(batch.id) is True
    assert store.entries(include_hidden=True) == []


def test_forget_clears_the_memory_log(store: MemoryStore, clock: FakeClock) -> None:
    """A forget reaches the store's own log of the chats too, which search reads: both texts
    of each round where either holds the phrase are blanked and the round's index row
    deleted, and each summary whose title or text holds it is cleared. The batch keeps what
    was there, for undo, through the window; then purge deletes it from the files."""
    store.record_round("c1", "My locker code is " + WORD + ".", "Noted.", at=T0, title="Locker")
    store.record_round("c1", "Find me a dentist.", "Here are three.", at=T0)
    store.record_round("c2", "Hello", "Hi. Your " + WORD + " code is safe.", at=T0)
    store.record_round("c3", "", "", at=T0)  # a round without text has no index row
    store.set_summary("c1", "Locker", "The user's locker code is " + WORD + ".")
    store.set_summary("c2", WORD + " chat", "The user said hello.")
    store.set_summary("c3", "Dentist", "The user asked for a dentist.")
    tables = ("rounds", "conversations")
    before = [_rows(store, table) for table in tables]
    index_before = _indexed_rounds(store)
    assert [(c, i) for c, i, _ in index_before] == [("c1", 1), ("c1", 2), ("c2", 1)]

    def summary(conversation_id: str) -> Tuple[Any, Any]:
        record = store.conversation(conversation_id)
        assert record is not None
        return record.summary_title, record.summary_text

    batch = store.forget(WORD.upper() + ".")  # matched once normalised, as entries are
    assert (batch.cleared_rounds, batch.cleared_summaries) == (2, 2)
    assert batch.applied == [
        {
            "op": "forget",
            "text": WORD.upper() + ".",
            "entry_ids": [],
            "fact_ids": [],
            "cleared_rounds": 2,
            "cleared_summaries": 2,
        }
    ]
    assert store.rounds("c1") == [
        (1, "", "", NOW),
        (2, "Find me a dentist.", "Here are three.", NOW),
    ]
    assert store.rounds("c2") == [(1, "", "", NOW)]
    assert _indexed_rounds(store) == [
        ("c1", 2, normalise_for_index("Find me a dentist.\nHere are three."))
    ]
    assert _found(store, WORD) == ([], [])
    assert [summary(c) for c in ("c1", "c2", "c3")] == [
        (None, None),
        (None, None),
        ("Dentist", "The user asked for a dentist."),
    ]
    chat = store.conversation("c1")
    assert chat is not None and chat.title == "Locker"  # the app's name for the chat stays
    # The index holds what rebuilding it from the rounds gives.
    indexed = _indexed_rounds(store)
    with store._write():
        store._rebuild_index()
    assert _indexed_rounds(store) == indexed

    # Undo brings every text back exactly, and the index rows with them.
    assert store.undo(batch.id) is True
    assert [_rows(store, table) for table in tables] == before
    assert _indexed_rounds(store) == index_before

    # A round or a summary changed since is a later change: the undo refuses, whole.
    again = store.forget(WORD)
    store.delete_conversation("c2")  # a round and a summary the batch cleared are gone
    assert store.undo(again.id) is False
    assert store.rounds("c1")[0] == (1, "", "", NOW)  # nothing came back
    assert summary("c1") == (None, None)
    store.set_summary("c3", "Dentist", "Booked with Dr Ngata.")
    third = store.forget("ngata")
    assert (third.cleared_rounds, third.cleared_summaries) == (0, 1)
    store.set_summary("c3", "Dentist", "Booked.")  # a new summary since
    assert store.undo(third.id) is False
    assert summary("c3") == ("Dentist", "Booked.")
    store.record_round("c3", "Ask about wombats.", "Noted.", at=T0)
    fourth = store.forget("wombats")
    assert (fourth.cleared_rounds, fourth.cleared_summaries) == (1, 0)
    # The chat's rounds deleted while the chat stays, as marking it incognito will: the
    # undo refuses rather than index rounds that are gone.
    store._db.execute("DELETE FROM rounds WHERE conversation_id = 'c3'")
    assert store.undo(fourth.id) is False
    assert [(c, i) for c, i, _ in _indexed_rounds(store)] == [("c1", 2)]
    assert _in_files(store, WORD)  # the batch keeps the texts through the window
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 0  # no entry or fact went, only the batch
    assert store._query("SELECT count(*) FROM undo_batches") == [(0,)]
    assert not _in_files(store, WORD)


def test_forget_matches_a_round_on_its_two_texts_joined(store: MemoryStore) -> None:
    """A round is matched on its user text and its reply joined with a space, normalised:
    a phrase the user started and the reply finished is found, and the same words apart
    are not."""
    store.record_round("c1", "I could eat deep", "Dish pizza, then?", at=T0)
    store.record_round("c1", "Deep breaths.", "Dish soap is under the sink.", at=T0)
    store.record_round("c1", "Deep-dish pizza.", "Noted.", at=T0)

    batch = store.forget("Deep dish")
    assert batch.cleared_rounds == 1
    assert store.rounds("c1") == [
        (1, "", "", NOW),
        (2, "Deep breaths.", "Dish soap is under the sink.", NOW),
        (3, "Deep-dish pizza.", "Noted.", NOW),
    ]
    assert [(c, i) for c, i, _ in _indexed_rounds(store)] == [("c1", 2), ("c1", 3)]


def test_delete_entry_is_the_users_own_delete(store: MemoryStore, clock: FakeClock) -> None:
    """Spec §10.1: the user can delete any entry, their own locked ones included. It is
    hidden, with the same undo window as a removal, and purged after it."""
    store.apply_ops([add("My locker code is " + WORD + "."), add("Likes tea.")])
    locked = store.edit_entry("e1", text="My locker code is " + WORD + ", for now.")
    tea = store.entries()[1]
    rows_before = _rows(store, "entries")
    changes = store.changes_since_consolidation()

    batch_id = store.delete_entry("e1")
    assert batch_id == "b2"  # the batch to undo
    assert store.entries() == [tea]
    assert store.entries(include_hidden=True) == [replace(locked, hidden_at=NOW), tea]
    assert store.changes_since_consolidation() == changes + 1
    assert store.undo(batch_id) is True
    assert _rows(store, "entries") == rows_before
    assert store.changes_since_consolidation() == changes

    assert store.delete_entry("e9") is None
    assert store.delete_entry("e1") == "b3"
    rows_deleted = _rows(store, "entries")
    assert store.delete_entry("e1") is None  # deleted already, in its undo window
    assert _rows(store, "entries") == rows_deleted
    assert store.changes_since_consolidation() == changes + 1
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 1
    assert store.delete_entry("e1") is None
    assert store.entries(include_hidden=True) == [tea]
    assert not _in_files(store, WORD)


def test_a_scrub_waits_for_the_outermost_write_to_commit(
    store: MemoryStore, clock: FakeClock
) -> None:
    """A scrub checkpoints the WAL, which fails inside a transaction. Asked for inside a
    write (a method that deletes text, run in a caller's transaction), it waits until the
    outermost write commits; after a rollback, which deleted nothing, it never runs."""
    wal = store.path.with_name(store.path.name + "-wal")
    chat_word = "numbatvortex"  # as WORD: no other text holds it
    store.apply_ops([add("My locker code is " + WORD + "."), add("Likes tea.")])
    store.apply_ops([{"op": "remove", "id": "e1"}])
    store.record_round("c1", "My gate code is " + chat_word + ".", "Noted.", at=T0)
    store.set_summary("c1", "Gate", "The user's gate code is " + chat_word + ".")
    clock.now = T0 + timedelta(seconds=31)
    with store._write():
        assert store.purge() == 1
        assert wal.stat().st_size > 0  # not scrubbed yet
    assert wal.stat().st_size == 0  # the checkpoint truncated it
    assert not _in_files(store, WORD)
    assert _in_files(store, chat_word)

    with store._write():
        store.delete_conversation("c1")
        assert _in_files(store, chat_word)  # not scrubbed yet
    assert store.conversation("c1") is None
    assert not _in_files(store, chat_word)

    store.record_round("c1", "Hello", "Hi.", at=T0)
    with pytest.raises(RuntimeError):
        with store._write():
            store.reset()
            raise RuntimeError("disk full")
    assert store.rounds("c1") == [(1, "Hello", "Hi.", NOW)]
    assert wal.stat().st_size > 0  # no scrub ran: one would have truncated the WAL
    store.paused = True  # nor does the next write run it
    assert wal.stat().st_size > 0


def test_expired_entries_stay_in_entries_and_are_never_purged(
    store: MemoryStore, clock: FakeClock
) -> None:
    """Expiry is the payload's to apply (B3): the store keeps an expired entry until the
    user or a consolidation removes it."""
    store.apply_ops([add("In Tokyo this week.", topic="ongoing", valid_until="2026-10-20")])
    clock.now = T0 + timedelta(days=30)
    assert store.purge() == 0
    assert [(entry.id, entry.valid_until) for entry in store.entries()] == [("e1", "2026-10-20")]


def test_open_purges_what_expired_while_closed(tmp_path: Path, backend: str) -> None:
    clock = FakeClock()
    path = tmp_path / "u1.sqlite3"
    memory = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        memory.apply_ops([add("My locker code is " + WORD + ".")])
        memory.forget(WORD)
    finally:
        memory.close()
    clock.now = T0 + timedelta(seconds=31)
    memory = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        assert memory.entries(include_hidden=True) == []
        assert not _in_files(memory, WORD)
    finally:
        memory.close()


# ── the store: edits, facts and consolidation ──


def test_edit_entry_locks_it(store: MemoryStore, clock: FakeClock) -> None:
    store.apply_ops([add("Likes tea."), add("Has a dog.")])
    original, dog = store.entries()
    clock.now = T0 + timedelta(minutes=1)
    edited = store.edit_entry("e1", text="Likes green tea.", valid_until="2027-01-01")
    assert edited == replace(
        original,
        text="Likes green tea.",
        valid_until="2027-01-01",
        updated_at=at(60),
        source="edited",
        locked=True,
    )
    assert store.entries()[0] == edited
    # The engine can no longer change it, and its new text is what dedupes.
    batch = store.apply_ops(
        [
            {"op": "update", "id": "e1", "text": "Hates tea."},
            {"op": "remove", "id": "e1"},
            add("likes green tea"),
        ]
    )
    assert (batch.applied, batch.dropped) == ([], ["locked", "locked", "duplicate"])
    # With no field given, an edit only makes the entry the user's own.
    assert store.edit_entry("e2") == replace(dog, source="edited", locked=True, updated_at=at(60))

    for fields, rule in (
        ({"text": "x" * 301}, "text over 300 code points"),
        ({"text": ""}, "no text"),
        ({"topic": "pets"}, "unknown topic"),
        ({"valid_until": "soon"}, "bad date"),
    ):
        with pytest.raises(ValueError, match="^" + rule + "$"):
            store.edit_entry("e1", **fields)
    assert store.entries()[0] == edited
    with pytest.raises(KeyError):
        store.edit_entry("e9", text="Has a cat.")
    store.apply_ops([add("Has a cat.")])
    store.apply_ops([{"op": "remove", "id": "e3"}])
    with pytest.raises(KeyError):  # hidden, in its undo window
        store.edit_entry("e3", text="Has two cats.")


def test_add_facts_applies_supersedes(store: MemoryStore, clock: FakeClock) -> None:
    (first,) = store.add_facts(
        [{"text": "The user lives in Melbourne.", "topic": "identity"}],
        conversation_id="c1",
        source="said",
    )
    assert first == Fact(
        id="f1",
        text="The user lives in Melbourne.",
        topic="identity",
        recorded_at=NOW,
        occurred_at=None,
        valid_until=None,
        conversation_id="c1",
        superseded_by=None,
        superseded_at=None,
        source="said",
    )
    clock.now = T0 + timedelta(hours=1)
    (moved,) = store.add_facts(
        [
            {
                "text": "On 2026-10-21 the user moved to Sydney.",
                "topic": "identity",
                "occurred_at": "2026-10-21",
                "supersedes": ["f1", "f404"],
                "source": "inferred",
            }
        ],
        conversation_id="c2",
        source="said",
    )
    assert (moved.id, moved.source, moved.occurred_at) == ("f2", "inferred", "2026-10-21")
    assert store.facts() == [moved]
    superseded = replace(first, superseded_by="f2", superseded_at=at(3600))
    assert store.facts(include_superseded=True) == [superseded, moved]

    # A fact superseded already keeps its first successor. As the engine does, a fact
    # without a topic or with text out of bounds is dropped, and a bad date left out.
    added = store.add_facts(
        [
            {"text": "The user moved to Perth.", "topic": "identity", "supersedes": ["f1"]},
            {"text": "", "topic": "identity"},
            {"text": "x" * 401, "topic": "identity"},
            {"text": "Has a cat.", "topic": "pets"},
            {"text": "Has a cat."},
            "not a fact",
            {
                "text": "Exam on 3 Nov.",
                "topic": "ongoing",
                "occurred_at": "2026-11-03",
                "valid_until": "soon",
                "source": "overheard",
            },
            {"text": "y" * 400, "topic": "work"},
        ],
        conversation_id=None,
        source="imported",
    )
    assert [(fact.id, fact.text[:20]) for fact in added] == [
        ("f3", "The user moved to Pe"),
        ("f4", "Exam on 3 Nov."),
        ("f5", "y" * 20),
    ]
    assert (added[1].occurred_at, added[1].valid_until, added[1].source) == (
        "2026-11-03",
        None,
        "imported",  # its own source is not one of the three
    )
    assert store.facts(include_superseded=True)[0] == superseded
    assert [fact_id for fact_id, _ in _indexed_facts(store)] == ["f1", "f2", "f3", "f4", "f5"]
    with pytest.raises(ValueError, match="source"):
        store.add_facts([], conversation_id=None, source="edited")


def test_merge_and_expire_facts(store: MemoryStore, clock: FakeClock) -> None:
    store.add_facts(
        [
            {"text": "The user is vegetarian.", "topic": "preferences"},
            {"text": "The user doesn't eat meat.", "topic": "preferences"},
            {"text": "The user said they are vegetarian.", "topic": "preferences"},
            {
                "text": "The user flies to Tokyo on 3 Nov.",
                "topic": "ongoing",
                "valid_until": "2026-11-10",
            },
            {"text": "The user has exams this week.", "topic": "ongoing"},
            {"text": "The user's lease ended.", "topic": "ongoing", "valid_until": "2026-09-30"},
        ],
        conversation_id="c1",
        source="said",
    )
    clock.now = T0 + timedelta(days=1)  # 2026-10-22
    store.merge_facts("f1", ["f2", "f3", "f1", "f404"])
    store.expire_facts(["f4", "f5", "f6", "f404"])
    facts = {fact.id: fact for fact in store.facts(include_superseded=True)}
    # Merged and expired facts stay, as history: none is deleted.
    assert sorted(facts) == ["f1", "f2", "f3", "f4", "f5", "f6"]
    assert [fact.id for fact in store.facts()] == ["f1", "f4", "f5", "f6"]
    assert [(facts[i].superseded_by, facts[i].superseded_at) for i in ("f1", "f2", "f3")] == [
        (None, None),
        ("f1", at(86400)),
        ("f1", at(86400)),
    ]
    # Over by the clock's date: valid until the day before, unless it ended earlier.
    assert [facts[i].valid_until for i in ("f4", "f5", "f6")] == [
        "2026-10-21",
        "2026-10-21",
        "2026-09-30",
    ]

    store.merge_facts("f5", ["f2"])  # superseded already: f2 keeps f1
    store.forget("vegetarian")  # hides f1 and f3
    store.merge_facts("f1", ["f5"])  # nothing is merged into a forgotten fact...
    store.expire_facts(["f1"])  # ...and a forgotten one is left as it is
    assert store._query(
        "SELECT id, superseded_by, valid_until, hidden_at FROM facts ORDER BY rowid"
    ) == [
        ("f1", None, None, at(86400)),
        ("f2", "f1", None, None),
        ("f3", "f1", None, at(86400)),
        ("f4", None, "2026-10-21", None),
        ("f5", None, "2026-10-21", None),
        ("f6", None, "2026-09-30", None),
    ]


def test_replace_entries_keeps_ids_and_locked_verbatim(
    store: MemoryStore, clock: FakeClock
) -> None:
    store.apply_ops(
        [
            add("Works as a designer.", topic="work"),
            add("Likes tea."),
            add("Has a dog called " + WORD + ".", topic="people"),
            add("Moving house soon.", topic="ongoing"),
        ]
    )
    locked = store.edit_entry("e2", text="Likes green tea.")
    designer = store.entries()[0]
    clock.now = T0 + timedelta(days=1)
    store.purge()  # the batch that added e3, and its copy of e3's text, are past undo
    store.apply_ops([{"op": "remove", "id": "e4"}])  # hidden, in its undo window
    assert _in_files(store, WORD)
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": "Works as a senior designer.", "locked": False},
            {"id": "e2", "topic": "preferences", "text": "Hates tea.", "locked": False},
            {"id": None, "topic": "ongoing", "text": "Studying video editing.", "locked": True},
            {"id": "e9", "topic": "work", "text": "Removed on the device meanwhile."},
            "not an entry",
            {"id": "e1", "topic": "work", "text": "A second item for e1."},
            {"id": None, "topic": "pets", "text": "Breaks a rule."},
            {"id": "e4", "topic": "ongoing", "text": "Hidden: left alone."},
        ]
    )
    studying = MemoryEntry(
        id="e5",
        topic="ongoing",
        text="Studying video editing.",
        created_at=at(86400),
        updated_at=at(86400),
        valid_until=None,
        source="inferred",
        locked=False,
        source_conversation_id=None,
    )
    senior = replace(designer, text="Works as a senior designer.", updated_at=at(86400))
    # e3 was left out, so it is deleted (and scrubbed from the files); e4 is still hidden.
    assert store.entries() == [senior, locked, studying]
    assert [entry.id for entry in store.entries(include_hidden=True)] == ["e1", "e2", "e4", "e5"]
    assert not _in_files(store, WORD)

    # An existing entry whose item breaks a rule stays as it was; one unchanged keeps its
    # updated_at; a locked one left out stays.
    clock.now = T0 + timedelta(days=2)
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": "x" * 301},
            {
                "id": "e5",
                "topic": "ongoing",
                "text": "Studying video editing.",
                "valid_until": None,
            },
        ]
    )
    assert store.entries() == [senior, locked, studying]

    # The engine sends only the fields the model set, so an item without valid_until
    # leaves the date as it was, as an update op does.
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text, "valid_until": "2027-06-30"},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    dated = replace(senior, valid_until="2027-06-30", updated_at=at(2 * 86400))
    assert store.entries() == [dated, locked, studying]
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    assert store.entries() == [dated, locked, studying]


def test_needs_consolidation_when_over_caps(store: MemoryStore) -> None:
    store.apply_ops([add("Entry number {}.".format(n)) for n in range(40)])
    assert store.needs_consolidation() is False  # 40 entries: at the cap
    store.apply_ops([add("Entry number 40.")])
    assert store.needs_consolidation() is True
    store.apply_ops([{"op": "remove", "id": "e41"}])
    assert store.needs_consolidation() is False  # a hidden entry doesn't count
    store.apply_ops([{"op": "remove", "id": entry.id} for entry in store.entries()])

    # 6,000 code points of text is at the cap: 20 entries of 300 code points, 150
    # graphemes each. One code point more is over it.
    accented = "e" + chr(0x301)
    store.apply_ops([add(accented * 149 + "x" + chr(0x41 + n)) for n in range(20)])
    assert store.needs_consolidation() is False
    store.apply_ops([add("y")])
    assert store.needs_consolidation() is True


def test_needs_consolidation_after_ten_changes_and_seven_days(tmp_path: Path, backend: str) -> None:
    clock = FakeClock()
    path = tmp_path / "u1.sqlite3"
    store = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        store.apply_ops([add("Entry number {}.".format(n)) for n in range(9)])
        clock.now = T0 + timedelta(days=8)
        assert store.needs_consolidation() is False  # 9 changes
        store.add_facts([{"text": "A fact.", "topic": "work"}], conversation_id=None, source="said")
        assert store.needs_consolidation() is True  # 10 changes, 8 days after the store began
        clock.now = T0 + timedelta(days=6, hours=23)
        assert store.needs_consolidation() is False
        clock.now = T0 + timedelta(days=7)
        assert store.needs_consolidation() is True

        store.mark_consolidated()
        store.apply_ops([add("Entry number {}.".format(n)) for n in range(10, 20)])
        clock.now = T0 + timedelta(days=13, hours=23)
        assert store.needs_consolidation() is False  # 10 changes, under 7 days since
    finally:
        store.close()
    # The dates are in the file: a store opened later counts from them.
    clock.now = T0 + timedelta(days=14)
    store = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        assert store.needs_consolidation() is True
    finally:
        store.close()


def test_changes_counter_and_mark_consolidated(store: MemoryStore) -> None:
    assert store.changes_since_consolidation() == 0
    store.apply_ops([add("Entry number {}.".format(n)) for n in range(6)])
    store.apply_ops(
        [
            {"op": "update", "id": "e1", "text": "Changed."},
            {"op": "remove", "id": "e2"},
            add("Entry number 3."),  # a duplicate: dropped, not a change
        ]
    )
    store.forget("number 3")
    store.forget("matches nothing")  # applied, but changed nothing
    store.edit_entry("e5", text="Edited.")
    assert store.changes_since_consolidation() == 10
    store.mark_consolidated()
    assert store.changes_since_consolidation() == 0

    # Facts count one each; undoing a batch takes its changes back.
    store.add_facts(
        [{"text": "Fact one.", "topic": "work"}, {"text": "Fact two.", "topic": "work"}],
        conversation_id=None,
        source="said",
    )
    batch = store.apply_ops([add("Entry number 9."), {"op": "remove", "id": "e6"}])
    assert store.changes_since_consolidation() == 4
    store.undo(batch.id)
    assert store.changes_since_consolidation() == 2
    # Consolidation's own writes don't count.
    store.merge_facts("f1", ["f2"])
    store.expire_facts(["f1"])
    store.replace_entries([{"id": "e1", "topic": "preferences", "text": "Rewritten."}])
    assert store.changes_since_consolidation() == 2
    # The user's own delete counts, and so does a forget that cleared only the log.
    store.delete_entry("e1")
    store.record_round("c1", "Talked about zebras.", "Noted.")
    store.forget("zebras")
    assert store.changes_since_consolidation() == 4


# ── the store: settings, export and reset ──


def test_paused_and_include_sensitive_are_kept_in_the_file(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "u1.sqlite3"
    store = MemoryStore(path, backend=backend)  # type: ignore[arg-type]
    try:
        assert (store.paused, store.include_sensitive) == (False, False)
        store.paused = True
        store.include_sensitive = True
    finally:
        store.close()
    store = MemoryStore(path, backend=backend)  # type: ignore[arg-type]
    try:
        assert (store.paused, store.include_sensitive) == (True, True)
        store.paused = False
        assert (store.paused, store.include_sensitive) == (False, True)
    finally:
        store.close()


def test_export_json_and_markdown_contain_every_entry_and_fact(store: MemoryStore) -> None:
    store.apply_ops(
        [
            add("Prefers metric units."),
            add("Works as a radiologist.", topic="work", valid_until="2027-06-30"),
            add("Lives in Hu" + chr(0x1EBF) + "."),  # Hue, with its tone mark
            add("Was going to forget this " + WORD + "."),
        ]
    )
    store.edit_entry("e1", text="Prefers metric units, always.")
    store.add_facts(
        [
            {"text": "The user lived in Melbourne.", "topic": "identity"},
            {"text": "The user moved to Sydney.", "topic": "identity", "supersedes": ["f1"]},
            {"text": "The user's " + WORD + " fact.", "topic": "work"},
        ],
        conversation_id="c1",
        source="said",
    )
    store.forget(WORD)
    entries, facts = store.entries(), store.facts(include_superseded=True)
    assert len(entries) == 3 and len(facts) == 2

    exported = json.loads(store.export_json())
    assert exported["exported_at"] == NOW
    assert [MemoryEntry(**item) for item in exported["entries"]] == entries
    assert [Fact(**item) for item in exported["facts"]] == facts

    markdown = store.export_markdown()
    for item in (*entries, *facts):
        assert item.text in markdown
    assert WORD not in markdown and WORD not in store.export_json()  # forgotten
    assert "### Preferences\n\n- Prefers metric units, always. (edited by you)\n" in markdown
    assert "- Works as a radiologist. (until 2027-06-30)" in markdown
    assert "- 2026-10-21: The user lived in Melbourne. (superseded)" in markdown


def test_reset_leaves_an_empty_schema(store: MemoryStore, clock: FakeClock) -> None:
    store.apply_ops([add("My locker code is " + WORD + "."), add("Likes tea.")])
    store.add_facts(
        [{"text": "The " + WORD + " fact.", "topic": "work"}], conversation_id="c1", source="said"
    )
    store.record_round("c1", "Remember " + WORD, "Noted.", at=T0, title="Locker")
    store.set_summary("c1", "Locker", "The user's locker code is " + WORD + ".")
    store.apply_ops([{"op": "remove", "id": "e2"}])
    store.paused = True
    store.include_sensitive = True
    assert _in_files(store, WORD)
    tables = {
        name for (name,) in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
    }

    clock.now = T0 + timedelta(days=1)
    store.reset()

    assert {
        name for (name,) in store._query("SELECT name FROM sqlite_master WHERE type = 'table'")
    } == tables
    for table in ("entries", "facts", "conversations", "rounds", "undo_batches"):
        assert store._query("SELECT count(*) FROM " + table) == [(0,)], table  # noqa: S608
    assert (_indexed_facts(store), _indexed_rounds(store)) == ([], [])
    assert not _in_files(store, WORD)
    assert "## Entries\n\nNone.\n\n## Facts\n\nNone.\n" in store.export_markdown()
    # The user's switches stay; consolidation starts afresh.
    assert (store.paused, store.include_sensitive) == (True, True)
    assert store.changes_since_consolidation() == 0
    # Ids carry on: none is ever used twice.
    batch = store.apply_ops([add("Likes tea.")])
    assert batch.applied[0]["id"] == "e3"
