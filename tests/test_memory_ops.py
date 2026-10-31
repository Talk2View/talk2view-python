"""Memory op rules (spec §7.3): ``apply_entry_ops``, the pure function every client's
store mirrors and the contract fixtures pin, and the store methods built on it.

Every test that takes ``store`` runs on each real backend, as test_memory_store's do."""

from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import sqlite3
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Sequence, Tuple

import pytest

from talk2view.memory import (
    STOP_WORDS,
    ConsolidationSnapshot,
    Fact,
    MemoryEntry,
    MemoryStore,
    days,
    index_tokens,
    normalise_for_index,
)
from talk2view.memory.ops import FORGET_VAGUE_WORDS, apply_entry_ops, forget_pattern, token_line

from .test_memory_store import (
    BACKENDS,
    T0,
    FakeClock,
    _conversation,
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


@pytest.fixture(autouse=True)
def _system_zone_is_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system's local zone, as days reads it, is UTC for these tests on any platform: a
    store method given no timezone takes the system's days (R64), as consolidation does
    until it passes the user's, and these tests' dates are UTC days."""
    monkeypatch.setattr(days, "_system_zone", lambda: timezone.utc)


def _rows(store: MemoryStore, table: str) -> List[Tuple[Any, ...]]:
    """Every row of ``table`` (see _ALL_ROWS)."""
    return store._query(_ALL_ROWS[table])


def _fail_last_write(store: MemoryStore) -> None:
    """Make every update of the count of changes fail, as a full disk would. A batch and a
    delete make one as their last write, and an undo one just before it raises the
    revision. A temporary trigger on the store's own connection, kept out of the file, so
    real SQLite rolls the transaction back. ``_stop_failing`` drops it."""
    store._db.execute(
        "CREATE TEMP TRIGGER fail BEFORE UPDATE ON meta"
        " WHEN NEW.key = 'changes_since_consolidation'"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )


def _fail_marking_consolidated(store: MemoryStore) -> None:
    """Make recording a consolidation fail, as a full disk would: the last write
    ``apply_consolidation`` makes. ``_stop_failing`` drops it."""
    store._db.execute(
        "CREATE TEMP TRIGGER fail BEFORE INSERT ON meta"
        " WHEN NEW.key = 'changes_since_consolidation'"
        " BEGIN SELECT RAISE(ABORT, 'disk full'); END"
    )


def _stop_failing(store: MemoryStore) -> None:
    store._db.execute("DROP TRIGGER fail")


def _round_texts(store: MemoryStore) -> List[Tuple[Any, ...]]:
    """Every round as stored, blank ones included: (conversation_id, idx, user, reply)."""
    return store._query(
        "SELECT conversation_id, idx, user_text, assistant_text FROM rounds ORDER BY rowid"
    )


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
            # And it must name something (R57): stop words and pro-forms alone, or no word
            # at all, are too vague. "Forget it" is how people say "never mind".
            {"op": "forget", "text": "it"},
            {"op": "forget", "text": "That one."},
            {"op": "forget", "text": "#$%"},
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
        "forget text too vague",
        "forget text too vague",
        "forget text too vague",
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


def test_forget_hides_entries_whose_words_hold_it() -> None:
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
    # Matched on the index's words (R68): case, spacing and punctuation fold away, and a
    # plural matches, but a word is matched whole, so "te" leaves "tea". The user's own
    # (locked) entry is passed by, and reported.
    assert entries == [
        replace(peanuts, hidden_at=NOW),
        replace(butter, hidden_at=NOW),
        tea,
        sauce,
    ]
    # A forget that matches nothing is applied, and says so.
    assert applied == [
        {"op": "forget", "text": "Peanut.", "entry_ids": ["e1", "e2"]},
        {"op": "forget", "text": "coffee", "entry_ids": []},
        {"op": "forget", "text": "TE", "entry_ids": []},
    ]
    assert dropped == ["locked"]


def test_a_forget_must_name_something() -> None:
    """R57: a forget whose tokens (``index_tokens``) are all FORGET_VAGUE_WORDS, the
    STOP_WORDS and the pronouns and pro-forms that stand for something instead of naming it,
    names nothing. It is dropped whole as "forget text too vague", so "it" never hides every
    entry holding "it", as "Likes Italian food." does. One word that names something is
    enough."""
    italian = _entry("e1", "Likes Italian food.")
    sam = _entry("e2", "Her ex is called Sam.")
    entries, applied, dropped = apply_entry_ops(
        [italian, sam],
        [
            {"op": "forget", "text": "it"},
            {"op": "forget", "text": "That one!"},
            {"op": "forget", "text": "all of them"},
            {"op": "forget", "text": "Everything."},
            {"op": "forget", "text": "her ex"},
        ],
        now=NOW,
        next_id=ids(),
    )
    assert dropped == ["forget text too vague"] * 4
    assert applied == [{"op": "forget", "text": "her ex", "entry_ids": ["e2"]}]
    assert entries == [italian, replace(sam, hidden_at=NOW)]
    # The words are written as index_tokens gives them, so every port can copy the set.
    assert STOP_WORDS < FORGET_VAGUE_WORDS
    assert {"one", "them", "everything"} <= FORGET_VAGUE_WORDS
    for word in FORGET_VAGUE_WORDS:
        assert index_tokens(word) == [word]


def test_a_forget_matches_entries_as_whole_words() -> None:
    """R68: an entry holds a forget's phrase when its tokens (``index_tokens``) hold the
    phrase's, in order and whole, the last allowed an "s" or "es": the log's rule (R57). The
    engine forgets by one distinctive word (R66), and a word must never hide the longer ones
    it begins: "Sam" leaves "Same time next week." and "Samsung", which a substring would
    hide, silently once undo's 30 seconds are up. Plurals, possessives (with a straight or
    a curly apostrophe), hyphenated compounds and a CJK run, whose characters segment_cjk
    sets apart, still match."""
    tokyo = chr(0x4E1C) + chr(0x4EAC)  # 东京
    trip = chr(0x53BB) + tokyo + chr(0x65C5) + chr(0x884C)  # 去东京旅行, "a trip to Tokyo"
    entries = [
        _entry("e1", "Same time next week."),
        _entry("e2", "Has a Samsung phone."),
        _entry("e3", "Sam's birthday is in May."),
        _entry("e4", "Allergic to peanuts."),
        _entry("e5", "Keeps a peanut-free kitchen."),
        _entry("e6", "Visits Brightwave" + chr(0x2019) + "s office."),
        _entry("e7", "Plans a " + trip + "."),
        _entry("e8", "Works at Brightwave."),
    ]
    after, applied, dropped = apply_entry_ops(
        entries,
        [
            {"op": "forget", "text": "Sam"},
            {"op": "forget", "text": "peanut"},
            {"op": "forget", "text": "Brightwave"},
            {"op": "forget", "text": tokyo},
        ],
        now=NOW,
        next_id=ids(),
    )
    assert dropped == []
    hid = [record["entry_ids"] for record in applied]
    assert hid == [["e3"], ["e4", "e5"], ["e6", "e8"], ["e7"]]
    assert [entry.id for entry in after if entry.hidden_at is None] == ["e1", "e2"]
    # The match is the log's own (forget_pattern over token_line): a word it begins is
    # passed by, and a phrase of two words needs both, in order.
    assert forget_pattern("Sam").search(token_line("Samsung")) is None
    assert forget_pattern("box").search(token_line("Two boxes")) is not None
    assert forget_pattern("peanut butter").search(token_line("Butter, peanut.")) is None
    # A phrase with no tokens, which apply_entry_ops drops as too vague, finds nothing at
    # all, rather than the texts with an "s" token or none.
    assert forget_pattern("#$%").search(token_line("Its 5 s")) is None


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
            "cleared_titles": 0,
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


def test_forget_hides_facts_that_hold_it_as_whole_words(store: MemoryStore) -> None:
    """R68: a fact holds a forget's phrase as an entry does, as whole words (forget_pattern),
    superseded facts included: "Sam" hides "Sam's" and "Sam", never "Samsung" or "same"."""
    store.add_facts(
        [
            {"text": "The user bought a Samsung phone.", "topic": "work"},
            {"text": "The user meets Sam's team on Fridays.", "topic": "people"},
            {"text": "The user meets Sam on Fridays.", "topic": "people", "supersedes": ["f2"]},
            {"text": "The user is the same age as their sister.", "topic": "people"},
        ],
        conversation_id=None,
        source="said",
    )
    batch = store.forget("Sam")
    assert batch.applied[0]["fact_ids"] == ["f2", "f3"]
    assert [fact.id for fact in store.facts(include_superseded=True)] == ["f1", "f4"]


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
            "cleared_titles": 0,
        },
    ]
    assert batch.removed_entries == store.entries(include_hidden=True)
    assert store._query("SELECT id, hidden_at, hidden_batch FROM entries") == [
        ("e1", NOW, batch.id)
    ]
    assert store.undo(batch.id) is True
    assert store.entries(include_hidden=True) == []


def test_forget_clears_the_memory_log(store: MemoryStore, clock: FakeClock) -> None:
    """A forget reaches the store's own log of the chats too, which search and the turn
    payload read: both texts of each round whose texts hold the phrase are blanked and the
    round's index row deleted, each summary whose title or text holds it is cleared, and
    each chat title that holds it is blanked. The batch keeps what was there, for undo,
    through the window; then purge deletes it from the files."""
    store.record_round("c1", "My locker code is " + WORD + ".", "Noted.", at=T0, title="Locker")
    store.record_round("c1", "Find me a dentist.", "Here are three.", at=T0)
    store.record_round(
        "c2", "Hello", "Hi. Your " + WORD + " code is safe.", at=T0, title="The " + WORD + " code"
    )
    store.record_round("c3", "", "", at=T0)  # a round without text has no index row
    store.set_summary("c1", "Locker", "The user's locker code is " + WORD + ".")
    store.set_summary("c2", WORD + " chat", "The user said hello.")
    store.set_summary("c3", "Dentist", "The user asked for a dentist.")
    tables = ("rounds", "conversations")
    before = [_rows(store, table) for table in tables]
    index_before = _indexed_rounds(store)
    assert [(c, i) for c, i, _ in index_before] == [("c1", 1), ("c1", 2), ("c2", 1)]

    def chat(conversation_id: str) -> Tuple[Any, Any, Any]:
        record = store.conversation(conversation_id)
        assert record is not None
        return record.title, record.summary_title, record.summary_text

    batch = store.forget(WORD.upper() + ".")  # matched once normalised, as entries are
    assert (batch.cleared_rounds, batch.cleared_summaries, batch.cleared_titles) == (2, 2, 1)
    assert batch.applied == [
        {
            "op": "forget",
            "text": WORD.upper() + ".",
            "entry_ids": [],
            "fact_ids": [],
            "cleared_rounds": 2,
            "cleared_summaries": 2,
            "cleared_titles": 1,
        }
    ]
    assert _round_texts(store) == [
        ("c1", 1, "", ""),
        ("c1", 2, "Find me a dentist.", "Here are three."),
        ("c2", 1, "", ""),
        ("c3", 1, "", ""),
    ]
    assert _indexed_rounds(store) == [
        ("c1", 2, normalise_for_index("Find me a dentist.\nHere are three."))
    ]
    assert _found(store, WORD) == ([], [])
    assert [chat(c) for c in ("c1", "c2", "c3")] == [
        ("Locker", None, None),  # a title without the phrase stays
        ("", None, None),
        (None, "Dentist", "The user asked for a dentist."),
    ]
    recent = store.recent_conversations()
    assert [(record.id, record.title) for record in recent] == [
        ("c3", None),
        ("c2", ""),
        ("c1", "Locker"),
    ]
    # The index holds what rebuilding it from the rounds gives.
    indexed = _indexed_rounds(store)
    with store._write():
        store._rebuild_index()
    assert _indexed_rounds(store) == indexed

    # Undo brings every text back exactly, and the index rows with them.
    assert store.undo(batch.id) is True
    assert [_rows(store, table) for table in tables] == before
    assert _indexed_rounds(store) == index_before

    # A round, summary or title changed since is a later change: the undo refuses, whole.
    again = store.forget(WORD)
    store.delete_conversation("c2")  # a round, a summary and a title the batch cleared
    assert store.undo(again.id) is False
    assert _round_texts(store)[0] == ("c1", 1, "", "")  # nothing came back
    assert chat("c1") == ("Locker", None, None)
    store.set_summary("c3", "Dentist", "Booked with Dr Ngata.")
    third = store.forget("ngata")
    assert (third.cleared_rounds, third.cleared_summaries, third.cleared_titles) == (0, 1, 0)
    store.set_summary("c3", "Dentist", "Booked.")  # a new summary since
    assert store.undo(third.id) is False
    assert chat("c3") == (None, "Dentist", "Booked.")
    store.record_round("c3", "Ask about wombats.", "Noted.", at=T0)
    fourth = store.forget("wombats")
    assert (fourth.cleared_rounds, fourth.cleared_summaries, fourth.cleared_titles) == (1, 0, 0)
    # The chat's rounds deleted while the chat stays, as marking it incognito will: the
    # undo refuses rather than index rounds that are gone.
    store._db.execute("DELETE FROM rounds WHERE conversation_id = 'c3'")
    assert store.undo(fourth.id) is False
    store.record_round("c4", "Hi.", "Hello.", at=T0, title="Okapi club")
    fifth = store.forget("okapi")
    assert (fifth.cleared_rounds, fifth.cleared_summaries, fifth.cleared_titles) == (0, 0, 1)
    assert chat("c4") == ("", None, None)
    assert store.undo(fifth.id) is True
    assert chat("c4") == ("Okapi club", None, None)
    sixth = store.forget("okapi")
    store._db.execute("UPDATE conversations SET title = 'Club' WHERE id = 'c4'")  # renamed
    assert store.undo(sixth.id) is False
    assert chat("c4") == ("Club", None, None)
    assert [(c, i) for c, i, _ in _indexed_rounds(store)] == [("c1", 2), ("c4", 1)]
    assert _in_files(store, WORD)  # the batch keeps the texts through the window
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 0  # no entry or fact went, only the batches
    assert store._query("SELECT count(*) FROM undo_batches") == [(0,)]
    assert not _in_files(store, WORD)


def test_forget_matches_a_round_on_its_two_texts_joined(store: MemoryStore) -> None:
    """A round is matched on the tokens of its user text and its reply together, as the
    index holds them: a phrase the user started and the reply finished is found, and the
    same words apart are not. Punctuation parts tokens, so "Deep-dish" holds deep, dish."""
    store.record_round("c1", "I could eat deep", "Dish pizza, then?", at=T0)
    store.record_round("c1", "Deep breaths.", "Dish soap is under the sink.", at=T0)
    store.record_round("c1", "Deep-dish pizza.", "Noted.", at=T0)

    batch = store.forget("Deep dish")
    assert batch.cleared_rounds == 2
    assert _round_texts(store) == [
        ("c1", 1, "", ""),
        ("c1", 2, "Deep breaths.", "Dish soap is under the sink."),
        ("c1", 3, "", ""),
    ]
    assert [(c, i) for c, i, _ in _indexed_rounds(store)] == [("c1", 2)]


def test_forget_finds_its_phrase_in_the_log_as_whole_words(store: MemoryStore) -> None:
    """R57: in the log (rounds, summaries and chat titles) a forget's phrase matches as its
    tokens in order, whole (``index_tokens``, as the index splits text), the last allowed an
    "s" or "es". So "ex" never blanks "for example", nor "cat" "category". A CJK phrase
    matches inside a run, since segment_cjk sets each character apart, and Latin diacritics
    fold away, as the index folds them. Entries and facts match the same way (R68)."""
    ca_phe = "c" + chr(0xE0) + " ph" + chr(0xEA)  # cà phê
    tokyo = chr(0x4E1C) + chr(0x4EAC)  # 东京
    store.record_round("c1", "My ex called.", "How did it go?", at=T0, title="My ex")
    store.record_round("c1", "For example, this one.", "Sure.", at=T0)
    store.set_summary("c1", "Exams", "The user's ex called about exams.")
    store.record_round("c2", "I fed my cats.", "Good.", at=T0, title="Categories")
    store.record_round("c2", "Which category?", "Books.", at=T0)
    store.record_round("c2", "Pack the boxes.", "Done.", at=T0)
    store.set_summary("c2", "Category", "The user asked about a category of books.")
    store.record_round("c3", chr(0x6211) + chr(0x53BB) + tokyo + chr(0x4E86), "OK.", at=T0)
    store.record_round("c3", tokyo[::-1], "OK.", at=T0)  # the same characters, reversed
    store.record_round("c4", "Mot ly " + ca_phe + ".", "Ngon.", at=T0)
    store.record_round("c4", "Uong ca phe sua da.", "Ngon.", at=T0)
    store.apply_ops([add("Exercises daily.")])

    def cleared(text: str) -> Tuple[int, int, int]:
        batch = store.forget(text)
        assert batch.dropped == []
        return batch.cleared_rounds, batch.cleared_summaries, batch.cleared_titles

    vague = store.forget("this one")  # names nothing: the log is left as it is
    assert (vague.dropped, vague.applied) == (["forget text too vague"], [])
    assert cleared("ex") == (1, 1, 1)
    assert [entry.text for entry in store.entries()] == ["Exercises daily."]  # whole words
    assert cleared("cat") == (1, 0, 0)
    assert cleared("box") == (1, 0, 0)
    assert cleared(tokyo) == (1, 0, 0)
    assert cleared(ca_phe) == (2, 0, 0)
    assert _round_texts(store) == [
        ("c1", 1, "", ""),
        ("c1", 2, "For example, this one.", "Sure."),
        ("c2", 1, "", ""),
        ("c2", 2, "Which category?", "Books."),
        ("c2", 3, "", ""),
        ("c3", 1, "", ""),
        ("c3", 2, tokyo[::-1], "OK."),
        ("c4", 1, "", ""),
        ("c4", 2, "", ""),
    ]
    assert [
        (record.title, record.summary_title)
        for record in (_conversation(store, "c1"), _conversation(store, "c2"))
    ] == [("", None), ("Categories", "Category")]


def test_blank_rounds_are_left_out_of_rounds_and_unextracted(store: MemoryStore) -> None:
    """A round with neither text, as a forget leaves one, is nothing to read: rounds()
    leaves it out, and unextracted() lists no conversation whose only rounds extraction
    has not seen are blank."""
    store.record_round("c1", "Hello", "Hi.", at=T0)
    store.record_round("c1", "My code is " + WORD + ".", "Noted.", at=T0)
    store.record_round("c2", "Hello", "Hi.", at=T0)
    store.record_round("c2", "Also " + WORD + ".", "Noted.", at=T0)
    store.record_round("c2", "Thanks.", "You're welcome.", at=T0)
    store.record_round("c3", "", "", at=T0)  # recorded blank
    store.mark_extracted("c1", 1)
    store.mark_extracted("c2", 1)
    assert [record.id for record in store.unextracted()] == ["c1", "c2"]

    store.forget(WORD)
    assert store.rounds("c1") == [(1, "Hello", "Hi.", NOW)]
    assert store.rounds("c2", after=1) == [(3, "Thanks.", "You're welcome.", NOW)]
    assert store.rounds("c3") == []
    assert [record.id for record in store.unextracted()] == ["c2"]


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


def test_a_scrub_that_fails_after_its_write_committed_is_retried(
    store: MemoryStore, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred scrub runs once its write has committed, so its failure must not fail the
    method: what the write did stands. It logs a warning, without text or counts, and the
    scrub stays due: the next write that commits retries it, and one that rolls back keeps
    it due. Every method that deletes text defers its scrub so."""
    batch = store.apply_ops([add("My locker code is " + WORD + ".")])
    store.record_round("c1", "Hello", "Hi.", at=T0)
    execute = store._db.execute

    def checkpoint_fails(sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        if sql.startswith("PRAGMA wal_checkpoint"):
            raise sqlite3.OperationalError("disk I/O error")
        return execute(sql, params)

    monkeypatch.setattr(store._db, "execute", checkpoint_fails)
    with caplog.at_level(logging.WARNING, logger="talk2view.memory"):
        assert store.undo(batch.id) is True
        store.delete_conversation("c1")
    assert store.entries(include_hidden=True) == []
    assert store.conversation("c1") is None
    assert [record.getMessage() for record in caplog.records] == [
        "Memory: scrubbing the store failed (OperationalError); the next write retries it"
    ] * 2
    monkeypatch.undo()
    assert _in_files(store, WORD)  # not scrubbed yet
    with pytest.raises(RuntimeError):
        with store._write():
            raise RuntimeError("disk full")
    assert _in_files(store, WORD)
    store.paused = True
    assert not _in_files(store, WORD)


def test_close_runs_a_scrub_left_due(
    tmp_path: Path,
    backend: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scrub left due lives in the store object alone, so close runs it: the file it leaves
    holds none of the text deleted before. (SQLite's own checkpoint at close takes the WAL,
    but not a deleted round's words in an index segment that never merged.) A scrub that
    fails again there is logged, and the file closes all the same."""

    def scrub_fails(store: MemoryStore) -> None:
        execute = store._db.execute

        def failing(sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
            if "'optimize'" in sql or sql.startswith("PRAGMA wal_checkpoint"):
                raise sqlite3.OperationalError("disk I/O error")
            return execute(sql, params)

        monkeypatch.setattr(store._db, "execute", failing)

    store = MemoryStore(tmp_path / "u1.sqlite3", backend=backend)  # type: ignore[arg-type]
    store.record_round("c1", "My locker code is " + WORD + ".", "Noted.", at=T0)
    scrub_fails(store)
    store.delete_conversation("c1")
    monkeypatch.undo()
    assert _in_files(store, WORD)  # not scrubbed yet
    store.close()
    assert not _in_files(store, WORD)

    store = MemoryStore(tmp_path / "u2.sqlite3", backend=backend)  # type: ignore[arg-type]
    execute = store._db.execute
    store.record_round("c1", "Hello", "Hi.", at=T0)
    scrub_fails(store)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="talk2view.memory"):
        store.delete_conversation("c1")
        store.close()
    assert [record.getMessage() for record in caplog.records] == [
        "Memory: scrubbing the store failed (OperationalError); the next write retries it",
        "Memory: scrubbing the store failed (OperationalError) as it closed; deleted text may"
        " stay in the file until the next scrub",
    ]
    with pytest.raises(Exception, match="(?i)closed"):
        execute("SELECT 1")


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
    # None leaves a field as it is, so clear_valid_until is how the date comes off; asked
    # for with a date as well, it is refused.
    with pytest.raises(ValueError, match="^both valid_until and clear_valid_until$"):
        store.edit_entry("e1", valid_until="2027-02-01", clear_valid_until=True)
    assert store.entries()[0] == edited
    clock.now = T0 + timedelta(minutes=2)
    undated = store.edit_entry("e1", clear_valid_until=True)
    assert undated == replace(edited, valid_until=None, updated_at=at(120))
    assert store.entries()[0] == undated
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
    store.merge_facts("f2", ["f4"])  # nor is anything merged into a superseded fact
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

    # An item is the entry's whole state, unlike an update op: one without valid_until, or
    # with null, gives the entry no date.
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text, "valid_until": "2027-06-30"},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    dated = replace(senior, valid_until="2027-06-30", updated_at=at(2 * 86400))
    assert store.entries() == [dated, locked, studying]
    clock.now = T0 + timedelta(days=3)
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    assert store.entries() == [replace(senior, updated_at=at(3 * 86400)), locked, studying]
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text, "valid_until": "2027-06-30"},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    clock.now = T0 + timedelta(days=4)
    store.replace_entries(
        [
            {"id": "e1", "topic": "work", "text": senior.text, "valid_until": None},
            {"id": "e5", "topic": "ongoing", "text": "Studying video editing."},
        ]
    )
    assert store.entries() == [replace(senior, updated_at=at(4 * 86400)), locked, studying]


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
    store.record_round("c2", "Hi.", "Hello.", title="Okapi club")
    store.forget("okapi")  # a title only
    assert store.changes_since_consolidation() == 5


def test_revision_rises_with_every_change_to_memory(tmp_path: Path, backend: str) -> None:
    """R55: a consolidation applies only against the revision its request was built from,
    so every committed change to memory raises it, and nothing else does. A failed write
    doesn't, and nothing lowers it: it is kept in the file, and reset raises it too."""
    clock = FakeClock()
    path = tmp_path / "u1.sqlite3"
    store = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    seen = store.revision()

    def rise() -> int:
        """How far the revision rose since the last call."""
        nonlocal seen
        before, seen = seen, store.revision()
        return seen - before

    def facts(*texts: str) -> None:
        store.add_facts(
            [{"text": text, "topic": "people"} for text in texts],
            conversation_id=None,
            source="said",
        )

    try:
        assert seen == 0
        # Each write that changes the visible entries or facts raises it...
        store.apply_ops([add("Likes tea."), add("Has a dog.")])
        assert rise() == 1
        store.edit_entry("e1", text="Likes green tea.")
        assert rise() == 1
        deleted = store.delete_entry("e2")
        assert rise() == 1
        assert deleted is not None and store.undo(deleted) is True
        assert rise() == 1
        facts("The dog is called Rex.", "The user has a cat.")
        assert rise() == 1
        facts("The dog is called Max.")
        store.merge_facts("f3", ["f1"])
        store.expire_facts(["f2"])
        assert rise() == 3
        store.replace_entries(
            [
                {"id": "e1", "topic": "preferences", "text": "Locked: left as it is."},
                {"id": "e2", "topic": "people", "text": "Has a dog."},
            ]
        )
        assert rise() == 1
        store.forget("dog")
        assert rise() == 1
        # So does every forget, one that cleared only the log (rounds, summaries and titles)
        # or matched nothing included (R55a): a consolidation built before it may hold the
        # phrase, in a summary or a title, and would write it back.
        store.record_round("c1", "Talked about zebras.", "Noted.", at=T0, title="Zebras")
        store.set_summary("c1", "Zebras", "The user talked about zebras.")
        assert rise() == 0
        batch = store.forget("zebras")
        assert (batch.cleared_rounds, batch.cleared_summaries, batch.cleared_titles) == (1, 1, 1)
        assert rise() == 1
        store.forget("matches nothing")
        assert rise() == 1
        # ...once per committed write, however many changes it holds.
        with store._write():
            store.apply_ops([add("Likes coffee.")])
            facts("The user drinks coffee.")
            store.forget("coffee")
        assert rise() == 1

        # Writes that change no entry or fact leave it: a forget dropped as too short or too
        # vague among them, since it is never applied.
        store.apply_ops([add("Likes green tea."), {"op": "remove", "id": "e1"}])
        store.forget("x")
        store.forget("that one")
        assert store.delete_entry("e404") is None
        store.add_facts([{"text": "", "topic": "people"}], conversation_id=None, source="said")
        store.merge_facts("f3", ["f2"])  # f3 is forgotten
        store.merge_facts("f2", ["f1", "f404"])  # f1 is forgotten; f404 never was
        store.expire_facts(["f2"])  # ended already
        store.replace_entries([{"id": "e1", "topic": "preferences", "text": "Likes tea."}])
        assert store.undo("b404") is False
        store.record_round("c2", "Hello", "Hi.", at=T0, title="Hi")
        store.set_summary("c2", "Hi", "The user said hello.")
        store.mark_extracted("c2", 1)
        store.set_incognito("c3", True)
        store.delete_conversation("c2")
        store.paused = True
        store.include_sensitive = True
        store.mark_consolidated()
        clock.now = T0 + timedelta(seconds=31)
        assert store.purge() == 5  # e2, e3, f1, f3 and f4: hidden already
        assert rise() == 0
        _fail_last_write(store)
        with pytest.raises(Exception, match="disk full"):
            store.apply_ops([add("Likes coffee.")])
        _stop_failing(store)
        assert rise() == 0

        store.reset()
        assert rise() == 1
    finally:
        store.close()
    store = MemoryStore(path, backend=backend, clock=clock)  # type: ignore[arg-type]
    try:
        assert store.revision() == seen
    finally:
        store.close()


def test_forget_revision_rises_only_with_writes_that_take_text_out(
    store: MemoryStore, clock: FakeClock
) -> None:
    """An extraction or an import applies its result only while the forget revision it read
    before building its request holds: the result was drawn from memory as it was then,
    and could write back what went while its call ran. So each committed write that can
    take text out of memory, or put it back, raises it: a batch that applied a forget or a
    remove, delete_entry, undo and reset. Nothing else does, so the adds and edits of a
    busy store never cost an extraction its result."""
    seen = store.forget_revision()

    def rose() -> bool:
        """Whether it rose since the last call."""
        nonlocal seen
        before, seen = seen, store.forget_revision()
        return seen > before

    assert seen == 0
    # Writes that add, change or tidy leave it...
    store.apply_ops([add("Likes tea."), add("Has a dog."), add("Owns a bike.")])
    store.apply_ops([{"op": "update", "id": "e1", "text": "Likes green tea."}])
    store.edit_entry("e2", text="Has a dog called Rex.")
    store.add_facts(
        [
            {"text": "The dog is called Rex.", "topic": "people"},
            {"text": "The user's dog is called Rex.", "topic": "people"},
        ],
        conversation_id=None,
        source="said",
    )
    store.merge_facts("f2", ["f1"])
    store.expire_facts(["f2"])
    store.replace_entries([{"id": "e1", "topic": "preferences", "text": "Likes tea."}])
    assert [entry.id for entry in store.entries()] == ["e1", "e2"]  # consolidation's e3 went
    store.record_round("c1", "Talked about zebras.", "Noted.", at=T0, title="Zebras")
    store.set_summary("c1", "Zebras", "The user talked about zebras.")
    store.mark_extracted("c1", 1)
    store.set_incognito("c2", True)
    store.paused = True
    store.paused = False
    # ...as do a forget or a remove the rules drop, and a delete or an undo of nothing.
    store.forget("x")
    store.apply_ops([{"op": "remove", "id": "e404"}])
    assert store.delete_entry("e404") is None
    assert store.undo("b404") is False
    assert not rose()

    # Each write that takes text out, or puts it back, raises it.
    batch = store.apply_ops([{"op": "remove", "id": "e1"}])
    assert rose()
    assert store.undo(batch.id) is True
    assert rose()
    store.forget("zebras")  # the log alone held it
    assert rose()
    store.forget("matches nothing")
    assert rose()
    assert store.delete_entry("e2") is not None  # locked: the user's own delete all the same
    assert rose()
    # A chat's own delete, and the purge once the undo window has passed, leave it.
    store.delete_conversation("c1")
    clock.now = T0 + timedelta(seconds=31)
    assert store.purge() == 1  # e2
    assert not rose()
    # A write that fails leaves it; a reset raises it.
    _fail_last_write(store)
    with pytest.raises(Exception, match="disk full"):
        store.forget("tea")
    _stop_failing(store)
    assert not rose()
    store.reset()
    assert rose()


def test_apply_consolidation_is_all_or_nothing(store: MemoryStore, clock: FakeClock) -> None:
    """R55: a consolidation's result applies whole, in one transaction, and only against
    the revision its request was built from, so a change made while the call runs is never
    undone by a result that never saw it."""
    store.apply_ops([add("Works as a designer.", topic="work"), add("Likes tea.")])
    store.add_facts(
        [
            {"text": "The user is vegetarian.", "topic": "preferences"},
            {"text": "The user doesn't eat meat.", "topic": "preferences"},
            {"text": "The user has exams this week.", "topic": "ongoing"},
        ],
        conversation_id="c1",
        source="said",
    )

    def entry(entry_id: str, topic: str, text: str) -> Dict[str, Any]:
        return {"id": entry_id, "topic": topic, "text": text, "valid_until": None, "locked": False}

    designer = entry("e1", "work", "Works as a senior designer.")
    tea = entry("e2", "preferences", "Likes tea.")
    merges: List[Any] = [{"keep": "f1", "drop": ["f2"]}]
    tables = ("entries", "facts")

    def refused(result: List[Dict[str, Any]], snapshot: ConsolidationSnapshot) -> None:
        """The result is refused, and nothing is written."""
        rows = [_rows(store, table) for table in tables]
        meta = store._query("SELECT key, value FROM meta ORDER BY key")
        assert store.apply_consolidation(result, merges, ["f3"], snapshot=snapshot) is False
        assert [_rows(store, table) for table in tables] == rows
        assert store._query("SELECT key, value FROM meta ORDER BY key") == meta

    # An entry update_memory adds while the call runs survives: the result never saw it.
    snapshot = store.consolidation_snapshot()  # read as the request is built
    dog = store.apply_ops([add("Has a dog.", topic="people")])
    refused([designer, tea], snapshot)
    assert store.entries()[-1].text == "Has a dog."

    # So does one added while an older batch is undone, which leaves the count of changes
    # where it was: the revision still moved.
    snapshot = store.consolidation_snapshot()
    changes = store.changes_since_consolidation()
    store.apply_ops([add("Has a cat.", topic="people")])
    assert store.undo(dog.id) is True
    assert store.changes_since_consolidation() == changes
    refused([designer, tea, entry("e3", "people", "Has a dog.")], snapshot)
    assert store.entries()[-1].text == "Has a cat."

    # Built again, the result names every entry. A failure at its last write leaves
    # nothing behind either.
    result = [designer, tea, entry("e4", "people", "Has a cat.")]
    snapshot = store.consolidation_snapshot()
    before = [_rows(store, table) for table in tables]
    meta = store._query("SELECT key, value FROM meta ORDER BY key")
    _fail_marking_consolidated(store)
    with pytest.raises(Exception, match="disk full"):
        store.apply_consolidation(result, merges, ["f3"], snapshot=snapshot)
    _stop_failing(store)
    assert [_rows(store, table) for table in tables] == before
    assert store._query("SELECT key, value FROM meta ORDER BY key") == meta

    # Then it applies whole, as one write: a malformed merge group is skipped, as a
    # malformed fact id is.
    clock.now = T0 + timedelta(days=1)
    malformed = [{"keep": "f1"}, "not a group", {"keep": ["f3"], "drop": ["f2"]}]
    assert (
        store.apply_consolidation(result, malformed + merges, ["f3", 7], snapshot=snapshot) is True
    )
    assert [entry.text for entry in store.entries()] == [
        "Works as a senior designer.",
        "Likes tea.",
        "Has a cat.",
    ]
    assert [
        (fact.id, fact.superseded_by, fact.valid_until)
        for fact in store.facts(include_superseded=True)
    ] == [("f1", None, None), ("f2", "f1", None), ("f3", None, "2026-10-21")]
    assert store.changes_since_consolidation() == 0
    assert store._meta_value("last_consolidated_at") == at(86400)
    assert store.revision() == snapshot.revision + 1


def _items(entries: Sequence[MemoryEntry]) -> List[Dict[str, Any]]:
    """``entries`` as a consolidation result's items: each one's whole state."""
    return [
        {"id": entry.id, "topic": entry.topic, "text": entry.text, "valid_until": entry.valid_until}
        for entry in entries
    ]


def test_consolidation_snapshot_is_one_read(
    store: MemoryStore, clock: FakeClock, backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consolidate request is built from one read of the store: the revision, the visible
    entries (locked ones first, then the most recently updated), the visible facts no newer
    fact superseded, newest first, and the summaries of the most recently active
    conversations, newest first, never an incognito one's. So the revision is the one its
    contents were read at: a write after it, even from another connection between the
    snapshot's own reads, makes apply_consolidation refuse the result."""
    store.apply_ops([add("Works as a designer.", topic="work"), add("Likes tea.")])
    store.edit_entry("e2", text="Likes green tea.")
    store.add_facts(
        [
            {"text": "The user lives in Melbourne.", "topic": "identity"},
            {"text": "The user has a cat.", "topic": "people"},
            {"text": "The user's password hint is " + WORD + ".", "topic": "identity"},
        ],
        conversation_id="c1",
        source="said",
    )
    store.forget(WORD)  # f3 is hidden
    clock.now = T0 + timedelta(hours=1)
    store.add_facts(
        [
            {"text": "The user moved to Sydney.", "topic": "identity", "supersedes": ["f1"]},
            {"text": "The user's cat is called Miso.", "topic": "people"},
        ],
        conversation_id="c2",
        source="said",
    )
    for minutes, conversation_id in ((10, "c1"), (40, "c2"), (30, "c3"), (50, "c4"), (20, "c5")):
        store.record_round(conversation_id, "Hello", "Hi.", at=T0 + timedelta(minutes=minutes))
    for conversation_id in ("c1", "c2", "c3", "c5"):
        store.set_summary(conversation_id, "Chat " + conversation_id, "About " + conversation_id)
    store.set_incognito("c3", True)  # its summary stays out; c4 has none
    entries = {entry.id: entry for entry in store.entries()}
    facts = {fact.id: fact for fact in store.facts()}

    snapshot = store.consolidation_snapshot(facts_max=2, summaries_max=2)
    assert snapshot == ConsolidationSnapshot(
        revision=store.revision(),
        entries=[entries["e2"], entries["e1"]],
        facts=[facts["f5"], facts["f4"]],
        summaries=[_conversation(store, "c2"), _conversation(store, "c5")],
    )
    assert [entry.locked for entry in snapshot.entries] == [True, False]
    whole = store.consolidation_snapshot()  # 400 facts and 30 summaries at most
    assert [fact.id for fact in whole.facts] == ["f5", "f4", "f2"]
    assert [record.id for record in whole.summaries] == ["c2", "c5", "c1"]
    with store._write():  # inside a write, its transaction serves
        assert store.consolidation_snapshot() == whole
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.revision = 0  # type: ignore[misc]

    # A write after the snapshot: the result built from it is refused, and the entry the
    # write added survives.
    store.apply_ops([add("Has a dog.", topic="people")])
    result = _items(snapshot.entries)
    assert store.apply_consolidation(result, [], [], snapshot=snapshot) is False
    assert store.entries()[-1].text == "Has a dog."

    # Another connection writes after the snapshot's first read: the reads that follow see
    # the store as that first read did, so the revision still matches what was read.
    other = MemoryStore(store.path, backend=backend, clock=clock)  # type: ignore[arg-type]
    first_read = store._query
    wrote: List[str] = []

    def write_after_first_read(sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        rows = first_read(sql, params)
        if not wrote:
            wrote.append(other.apply_ops([add("Has a cat.", topic="people")]).id)
        return rows

    revision = store.revision()
    monkeypatch.setattr(store, "_query", write_after_first_read)
    try:
        snapshot = store.consolidation_snapshot()
    finally:
        monkeypatch.undo()
        other.close()
    assert wrote and store.revision() == revision + 1
    assert snapshot.revision == revision
    assert [entry.text for entry in snapshot.entries] == [
        "Likes green tea.",
        "Has a dog.",
        "Works as a designer.",
    ]
    result = _items(snapshot.entries)
    assert store.apply_consolidation(result, [], [], snapshot=snapshot) is False
    assert store.entries()[-1].text == "Has a cat."

    # A read that fails ends the snapshot's transaction with it: the next write can begin.
    def unreadable(sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "_query", unreadable)
    with pytest.raises(sqlite3.OperationalError):
        store.consolidation_snapshot()
    monkeypatch.undo()
    store.apply_ops([add("Has a fish.", topic="people")])
    assert store.entries()[-1].text == "Has a fish."


def test_replace_entries_adds_no_copy_of_an_entry_it_keeps(store: MemoryStore) -> None:
    """A new item (id null) is skipped when a visible entry the call keeps has its
    ``normalise_dedupe`` text, whichever item comes first, as is a second new item with the
    same text: apply_entry_ops' rule for an add. One like an entry the call deletes is
    added. apply_consolidation applies its result the same way."""
    store.apply_ops(
        [
            add("Works as a designer.", topic="work"),
            add("Likes tea."),
            add("Has a dog.", topic="people"),
        ]
    )
    store.edit_entry("e2", text="Likes green tea.")  # locked: kept, though no item names it
    store.replace_entries(
        [
            {"id": None, "topic": "work", "text": "works as a senior designer"},
            {"id": "e1", "topic": "work", "text": "Works as a senior designer."},
            {"id": None, "topic": "preferences", "text": "Likes green tea"},
            {"id": None, "topic": "people", "text": "Has a dog."},  # e3 is deleted
            {"id": None, "topic": "ongoing", "text": "Studying."},
            {"id": None, "topic": "ongoing", "text": "studying"},
        ]
    )
    kept = [
        ("e1", "Works as a senior designer."),
        ("e2", "Likes green tea."),
        ("e4", "Has a dog."),
        ("e5", "Studying."),
    ]
    assert [(entry.id, entry.text) for entry in store.entries()] == kept

    result = _items(store.entries()) + [{"id": None, "topic": "people", "text": "has a dog"}]
    snapshot = store.consolidation_snapshot()
    assert store.apply_consolidation(result, [], [], snapshot=snapshot) is True
    assert [(entry.id, entry.text) for entry in store.entries()] == kept


def test_apply_consolidation_leaves_the_entries_its_snapshot_left_out(
    store: MemoryStore, clock: FakeClock
) -> None:
    """R59: a consolidate request carries at most 200 entries (CONSOLIDATE_ENTRIES_MAX),
    the locked ones first, then the most recently updated, and its result changes only
    those. An entry the snapshot left out is neither deleted nor rewritten, a new item
    with its text is not added beside it, and it keeps a consolidation due until one
    carries it. A reset starts consolidation afresh."""
    store.apply_ops([add("Old entry number {}.".format(n)) for n in range(5)])  # e1-e5
    clock.now = T0 + timedelta(minutes=30)
    store.edit_entry("e1", text="Old entry number 0, edited.")  # locked
    clock.now = T0 + timedelta(hours=1)
    store.apply_ops([add("Entry number {}.".format(n)) for n in range(200)])  # e6-e205
    left_out = [entry for entry in store.entries() if entry.id in ("e2", "e3", "e4", "e5", "e6")]

    snapshot = store.consolidation_snapshot()
    # e6 is the earliest of the 200 entries updated last, so it is the one they lose.
    newest = ["e" + str(n) for n in range(205, 6, -1)]
    assert [entry.id for entry in snapshot.entries] == ["e1"] + newest
    result = [
        {"id": "e1", "topic": "preferences", "text": "Rewritten."},  # locked: verbatim
        {"id": "e7", "topic": "preferences", "text": "Entries 1 to 199, merged."},
        {"id": "e2", "topic": "preferences", "text": "Not in the request."},  # skipped
        {"id": None, "topic": "preferences", "text": "Old entry number 4."},  # e5 stays
        {"id": None, "topic": "work", "text": "A new entry."},
    ]
    assert store.apply_consolidation(result, [], [], snapshot=snapshot) is True

    assert [entry for entry in store.entries() if entry.id in ("e2", "e3", "e4", "e5", "e6")] == (
        left_out
    )
    assert [(entry.id, entry.text) for entry in store.entries()] == [
        ("e1", "Old entry number 0, edited."),
        ("e2", "Old entry number 1."),
        ("e3", "Old entry number 2."),
        ("e4", "Old entry number 3."),
        ("e5", "Old entry number 4."),
        ("e6", "Entry number 0."),
        ("e7", "Entries 1 to 199, merged."),
        ("e206", "A new entry."),
    ]
    # Under the caps, with no changes since: due only for the entries it left out.
    assert store.changes_since_consolidation() == 0
    assert store.needs_consolidation() is True

    snapshot = store.consolidation_snapshot()
    assert len(snapshot.entries) == 8
    assert store.apply_consolidation(_items(snapshot.entries), [], [], snapshot=snapshot) is True
    assert store.needs_consolidation() is False

    snapshot = store.consolidation_snapshot(entries_max=1)
    assert store.apply_consolidation(_items(snapshot.entries), [], [], snapshot=snapshot) is True
    assert store.needs_consolidation() is True
    store.reset()
    assert store.needs_consolidation() is False


def test_apply_consolidation_expires_facts_in_the_requests_timezone(
    store: MemoryStore, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R65: a fact consolidation expires ends the day before the user's today, in the
    timezone the request was sent with; with none, the system's."""
    plus_fourteen = timezone(timedelta(hours=14))
    monkeypatch.setattr(
        days, "_named_zone", lambda name: plus_fourteen if name == "Plus/Fourteen" else None
    )
    store.add_facts(
        [
            {"text": "The user has exams this week.", "topic": "ongoing"},
            {"text": "The user is moving house.", "topic": "ongoing"},
        ],
        conversation_id=None,
        source="said",
    )
    clock.now = T0 + timedelta(hours=12)  # 22:00 on the 21st in UTC: the 22nd at UTC+14

    snapshot = store.consolidation_snapshot()
    assert store.apply_consolidation([], [], ["f1"], snapshot=snapshot, timezone="Plus/Fourteen")
    snapshot = store.consolidation_snapshot()
    assert store.apply_consolidation([], [], ["f2"], snapshot=snapshot)
    assert [fact.valid_until for fact in store.facts()] == ["2026-10-21", "2026-10-20"]


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

    revision = store.revision()
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
    # The user's switches stay; consolidation starts afresh; the revision rises, as it
    # never falls.
    assert (store.paused, store.include_sensitive) == (True, True)
    assert store.changes_since_consolidation() == 0
    assert store.revision() == revision + 1
    # Ids carry on: none is ever used twice.
    batch = store.apply_ops([add("Likes tea.")])
    assert batch.applied[0]["id"] == "e3"
