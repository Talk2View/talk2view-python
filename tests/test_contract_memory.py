"""Replay contract/memory: the memory store's op, search and payload cases, and its word lists.

The cases pin the on-device store (spec §7.1 to §7.3, §8.1) for every port: the Swift and
TypeScript stores replay the same files. Here ``apply_entry_ops`` and ``MemoryStore`` replay
each case, the store on each backend, so the scan backend must find and rank what FTS5 does.
A case's days are in its ``timezone``, or in UTC when it gives none: the system's zone, as
``days`` reads it, is pinned to UTC. word_lists.json ships the lists the cases turn on, and
must stay the store's own.
"""

from __future__ import annotations

import itertools
import json
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from talk2view.memory import Backend, MemoryStore, days
from talk2view.memory.models import Fact, MemoryEntry
from talk2view.memory.ops import FORGET_VAGUE_WORDS, apply_entry_ops
from talk2view.memory.search import CJK_STOP_BIGRAMS, result_json
from talk2view.memory.text import STOP_WORDS

from .test_memory_search import _loaded_zone
from .test_memory_store import BACKENDS, FakeClock

CONTRACT = Path(__file__).resolve().parents[1] / "contract"


def _load(name: str) -> Dict[str, Any]:
    fixture: Dict[str, Any] = json.loads((CONTRACT / "memory" / name).read_text(encoding="utf-8"))
    return fixture


OPS = _load("op_cases.json")
SEARCHES = _load("search_cases.json")
PAYLOADS = _load("payload_cases.json")
FIXTURES = {"op_cases.json": OPS, "search_cases.json": SEARCHES, "payload_cases.json": PAYLOADS}


def _ids(fixture: Dict[str, Any]) -> List[str]:
    return [case["id"] for case in fixture["cases"]]


@pytest.fixture(autouse=True)
def _system_zone_is_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """A case without a timezone runs in UTC on any machine: ``days`` reads the system's
    zone through ``_system_zone`` alone."""
    monkeypatch.setattr(days, "_system_zone", lambda: timezone.utc)


def test_every_case_has_one_outcome() -> None:
    for name, fixture in FIXTURES.items():
        ids = _ids(fixture)
        assert fixture["version"] == 1, name
        assert len(ids) == len(set(ids)), name
    for case in OPS["cases"]:
        assert set(case["expect"]) == {"entries", "dropped"}, case["id"]
    for case in SEARCHES["cases"]:
        assert all(isinstance(ref, str) for ref in case["expect"]), case["id"]
        assert len(case.get("expect_results", case["expect"])) == len(case["expect"]), case["id"]
    tool_ids = [case["id"] for case in SEARCHES["tool_cases"]]
    assert len(set(tool_ids)) == len(tool_ids) and not set(tool_ids) & set(_ids(SEARCHES))
    for case in SEARCHES["tool_cases"]:
        assert len({"expect", "expect_count"} & set(case)) == 1, case["id"]
    for case in PAYLOADS["cases"]:
        assert set(case["expect"]) == {"status", "entries", "recent", "recalled"}, case["id"]


def test_units_note_present() -> None:
    """Every cap counts code points, never graphemes or UTF-16 units: the README's row for
    each file says so, as the file's ``about`` does."""
    readme = (CONTRACT / "README.md").read_text(encoding="utf-8")
    for name, fixture in FIXTURES.items():
        rows = [line for line in readme.splitlines() if line.startswith("| `memory/" + name)]
        assert len(rows) == 1, name
        assert "code points" in rows[0], name
        assert "code points" in fixture["about"], name


def test_word_lists_are_the_stores() -> None:
    """word_lists.json holds the store's own lists, sorted, so a port's lists and this
    store's can't drift apart (R80)."""
    lists = _load("word_lists.json")
    assert lists["version"] == 1
    assert lists["stop_words"] == sorted(STOP_WORDS)
    assert lists["cjk_stop_bigrams"] == sorted(CJK_STOP_BIGRAMS)
    assert lists["forget_vague_words"] == sorted(FORGET_VAGUE_WORDS)


# ── op_cases.json ──


@pytest.mark.parametrize("case", OPS["cases"], ids=_ids(OPS))
def test_apply_entry_ops_replays_the_op_case(case: Dict[str, Any]) -> None:
    entries = [MemoryEntry(**item) for item in case["entries"]]
    after, _, dropped = apply_entry_ops(
        entries, case["ops"], now=OPS["now"], next_id=_next_id(entries)
    )
    assert [asdict(entry) for entry in after] == case["expect"]["entries"]
    assert dropped == case["expect"]["dropped"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("case", OPS["cases"], ids=_ids(OPS))
def test_the_store_replays_the_op_case(
    case: Dict[str, Any], backend: Backend, tmp_path: Path
) -> None:
    with closing(_store(tmp_path, backend, OPS["now"])) as store:
        for item in case["entries"]:
            store._insert_entry(MemoryEntry(**item))
        batch = store.apply_ops(case["ops"])
        after = store.entries(include_hidden=True)
    assert [asdict(entry) for entry in after] == case["expect"]["entries"]
    assert batch.dropped == case["expect"]["dropped"]


def _next_id(entries: List[MemoryEntry]) -> Callable[[], str]:
    """The ids the store gives added entries: e1, e2, ... in turn, passing over those in
    use."""
    taken = {entry.id for entry in entries}
    candidates = ("e" + str(number) for number in itertools.count(1))
    return lambda: next(candidate for candidate in candidates if candidate not in taken)


# ── search_cases.json ──


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("case", SEARCHES["cases"], ids=_ids(SEARCHES))
def test_the_store_replays_the_search_case(
    case: Dict[str, Any], backend: Backend, tmp_path: Path
) -> None:
    zone = _zone(case)
    with closing(_built(tmp_path, backend, SEARCHES["corpus"])) as store:
        hits = store.search(
            case["query"],
            from_date=case.get("from_date"),
            to_date=case.get("to_date"),
            limit=case.get("limit", 8),
            exclude_conversation=case.get("exclude_conversation"),
            timezone=zone,
        )
    assert [hit.ref for hit in hits] == case["expect"]
    if "expect_results" in case:
        assert json.loads(result_json(hits))["results"] == case["expect_results"]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "case", SEARCHES["tool_cases"], ids=[case["id"] for case in SEARCHES["tool_cases"]]
)
def test_the_store_replays_the_search_memory_case(
    case: Dict[str, Any], backend: Backend, tmp_path: Path
) -> None:
    """A search_memory call's whole result: its limit, its dates and the fallback when they
    find nothing, and its note."""
    zone = _zone(case)
    arguments = case["arguments"]
    with closing(_built(tmp_path, backend, SEARCHES["corpus"])) as store:
        result = json.loads(
            store.search_result_json(
                arguments["query"],
                from_date=arguments.get("from_date"),
                to_date=arguments.get("to_date"),
                limit=arguments.get("limit"),
                exclude_conversation=case.get("exclude_conversation"),
                timezone=zone,
            )
        )
    if "expect_count" in case:
        assert len(result["results"]) == case["expect_count"]
    else:
        assert result == case["expect"]


# ── payload_cases.json ──


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("case", PAYLOADS["cases"], ids=_ids(PAYLOADS))
def test_the_store_replays_the_payload_case(
    case: Dict[str, Any], backend: Backend, tmp_path: Path
) -> None:
    zone = _zone(case)
    with closing(_built(tmp_path, backend, case["corpus"], case["now"])) as store:
        payload = store.turn_payload(
            case["message"], exclude_conversation=case["conversation"], timezone=zone
        )
    assert payload == case["expect"]


# ── building a case's store ──


def _zone(case: Dict[str, Any]) -> Optional[str]:
    """The case's timezone, None for UTC. The test skips, saying why, when this platform
    can't load the zone."""
    name: Optional[str] = case.get("timezone")
    if name is not None:
        _loaded_zone(name)
    return name


def _moment(timestamp: str) -> datetime:
    """A time as the store writes it, "2026-10-21T10:00:00Z", as an aware datetime."""
    return datetime.fromisoformat(timestamp[:-1]).replace(tzinfo=timezone.utc)


def _store(tmp_path: Path, backend: Backend, now: Optional[str]) -> MemoryStore:
    """A store whose clock stands at ``now``, or at the tests' T0 for a search, which reads
    no clock."""
    clock = FakeClock() if now is None else FakeClock(_moment(now))
    return MemoryStore(tmp_path / "memory.sqlite3", backend=backend, clock=clock)


def _built(
    tmp_path: Path, backend: Backend, corpus: Dict[str, Any], now: Optional[str] = None
) -> MemoryStore:
    """A store at ``now`` holding ``corpus`` as the fixtures' ``about`` describes it: each
    round recorded in order, with its conversation's title, then the conversations marked
    incognito and the summaries, then the facts and the entries, each field left out null."""
    store = _store(tmp_path, backend, now)
    conversations = {item["id"]: item for item in corpus.get("conversations", [])}
    for item in corpus.get("rounds", []):
        conversation_id = item["conversation_id"]
        idx = store.record_round(
            conversation_id,
            item["user_text"],
            item["assistant_text"],
            at=_moment(item["at"]),
            title=conversations[conversation_id].get("title"),
        )
        assert idx == item["idx"], "the corpus lists a conversation's rounds in order"
    for conversation in conversations.values():
        if conversation.get("incognito"):
            store.set_incognito(conversation["id"], True)
        if "summary_title" in conversation or "summary_text" in conversation:
            store.set_summary(
                conversation["id"],
                conversation.get("summary_title", ""),
                conversation.get("summary_text", ""),
            )
    for item in corpus.get("facts", []):
        store._insert_fact(
            Fact(
                id=item["id"],
                text=item["text"],
                topic=item["topic"],
                recorded_at=item["recorded_at"],
                occurred_at=None,
                valid_until=item.get("valid_until"),
                conversation_id=item.get("conversation_id"),
                superseded_by=item.get("superseded_by"),
                superseded_at=None,
                source="said",
            )
        )
    for item in corpus.get("entries", []):
        locked = item.get("locked", False)
        store._insert_entry(
            MemoryEntry(
                id=item["id"],
                topic=item["topic"],
                text=item["text"],
                created_at=item["updated_at"],
                updated_at=item["updated_at"],
                valid_until=item.get("valid_until"),
                source="edited" if locked else "said",
                locked=locked,
                source_conversation_id=None,
            )
        )
    return store
