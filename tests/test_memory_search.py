"""Memory search and the turn payload (spec §7.2, §8.1, §8.4): the query, BM25 and fusion,
excerpts, the search_memory result, pre-selection and the ``memory`` field a turn sends.

Every test that takes ``store`` runs on each real backend, as test_memory_store's do: the
scan backend must find what FTS5 finds, and rank it the same way."""

from __future__ import annotations

import ast
import inspect
import itertools
import json
import os
import random
import re
import sqlite3
import statistics
import time
import unicodedata
import zoneinfo
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import pytest

from talk2view.memory import TOPICS, MemoryStore, SearchHit, days, index_tokens, normalise_for_index
from talk2view.memory import payload as payload_module
from talk2view.memory import search as search_module
from talk2view.memory import store as store_module
from talk2view.memory.models import FACT_TEXT_MAX, RECALLED_TEXT_MAX, RESULT_CHARS_MAX
from talk2view.memory.payload import keep_within, recalled_items
from talk2view.memory.search import (
    CJK_STOP_BIGRAMS,
    ELLIPSIS,
    PRESELECT_FLOOR,
    QUERY_BIGRAMS_MAX,
    QUERY_TERMS_MAX,
    best_first,
    bm25_scan,
    build_fts_query,
    conversation_title,
    excerpt,
    first_match,
    fts_query,
    fts_term,
    fuse,
    preselected,
    query_terms,
    round_hit,
    round_text,
    rrf,
    strong_match,
    term_frequency,
)
from talk2view.memory.text import fold_latin, index_token_spans, is_segmented

from .test_memory_ops import _in_files
from .test_memory_store import (
    BACKENDS,
    SQLITE3_HAS_FTS5,
    T0,
    FakeClock,
    _conversation,
    _entry,
    _fact,
    _indexed_rounds,
)

WORD = "quokkazanzibar"  # no other text holds it
EAST, CAPITAL = chr(0x4E1C), chr(0x4EAC)
TOKYO = EAST + CAPITAL  # 东京
TOKYO_PHRASE = EAST + " " + CAPITAL  # as a query term: a phrase of its characters
WANT_TO_GO = chr(0x6211) + chr(0x60F3) + chr(0x53BB)  # 我想去, "I want to go"
TRAVEL = chr(0x65C5) + chr(0x884C)  # 旅行
CA_PHE = "c" + chr(0xE0) + " ph" + chr(0xEA)  # cà phê, Vietnamese "coffee"
FATHA = chr(0x064E)  # an Arabic haraka, a combining mark
KATABA = chr(0x0643) + chr(0x062A) + chr(0x0628)  # كتب, "he wrote", without harakat
THAI_EAT = chr(0x0E01) + chr(0x0E34) + chr(0x0E19)  # กิน: a vowel sign (Mn) between consonants
DATE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)


def _chars(*points: int) -> str:
    return "".join(chr(point) for point in points)


# R63's sentences. 我下周去东京的行程是什么时候, "when is my trip to Tokyo next week?"
TRIP_QUESTION = _chars(
    0x6211, 0x4E0B, 0x5468, 0x53BB, 0x4E1C, 0x4EAC, 0x7684, 0x884C, 0x7A0B, 0x662F, 0x4EC0,
    0x4E48, 0x65F6, 0x5019,
)  # fmt: skip
# 用户11月3日到10日去东京旅行, "the user goes to Tokyo from 3 to 10 November": 去东 and 东京
# are bigrams of the question too.
TRIP_FACT = (
    _chars(0x7528, 0x6237) + "11" + _chars(0x6708) + "3" + _chars(0x65E5, 0x5230) + "10"
    + _chars(0x65E5, 0x53BB, 0x4E1C, 0x4EAC, 0x65C5, 0x884C)
)  # fmt: skip
# 不知道什么时候搬家, "doesn't know when they will move house": it shares only stop bigrams
# with the question (什么, 么时, 时候).
MOVING_FACT = _chars(0x4E0D, 0x77E5, 0x9053, 0x4EC0, 0x4E48, 0x65F6, 0x5019, 0x642C, 0x5BB6)
# 来週の東京出張はいつですか, "when is next week's business trip to Tokyo?"
JA_QUESTION = _chars(
    0x6765, 0x9031, 0x306E, 0x6771, 0x4EAC, 0x51FA, 0x5F35, 0x306F, 0x3044, 0x3064, 0x3067,
    0x3059, 0x304B,
)  # fmt: skip
# 11月3日に東京へ出張する, "goes to Tokyo on business on 3 November": 東京 and 出張.
JA_FACT = (
    "11" + _chars(0x6708) + "3"
    + _chars(0x65E5, 0x306B, 0x6771, 0x4EAC, 0x3078, 0x51FA, 0x5F35, 0x3059, 0x308B)
)  # fmt: skip
# 週末は京都で買い物, "shopping in Kyoto at the weekend": 週, は and 京 are in the question,
# but none of its bigrams.
JA_OTHER = _chars(0x9031, 0x672B, 0x306F, 0x4EAC, 0x90FD, 0x3067, 0x8CB7, 0x3044, 0x7269)
# ไปเชียงใหม่วันไหน, "which day do I go to Chiang Mai?"
TH_QUESTION = _chars(
    0x0E44, 0x0E1B, 0x0E40, 0x0E0A, 0x0E35, 0x0E22, 0x0E07, 0x0E43, 0x0E2B, 0x0E21, 0x0E48,
    0x0E27, 0x0E31, 0x0E19, 0x0E44, 0x0E2B, 0x0E19,
)  # fmt: skip
# จองโรงแรมที่เชียงใหม่แล้ว, "booked a hotel in Chiang Mai already": เชียงใหม่, Chiang Mai.
TH_FACT = _chars(
    0x0E08, 0x0E2D, 0x0E07, 0x0E42, 0x0E23, 0x0E07, 0x0E41, 0x0E23, 0x0E21, 0x0E17, 0x0E35,
    0x0E48, 0x0E40, 0x0E0A, 0x0E35, 0x0E22, 0x0E07, 0x0E43, 0x0E2B, 0x0E21, 0x0E48, 0x0E41,
    0x0E25, 0x0E49, 0x0E27,
)  # fmt: skip
# กินข้าวกับแม่, "ate with mum": no bigram of the question.
TH_OTHER = _chars(
    0x0E01, 0x0E34, 0x0E19, 0x0E02, 0x0E49, 0x0E32, 0x0E27, 0x0E01, 0x0E31, 0x0E1A, 0x0E41,
    0x0E21, 0x0E48,
)  # fmt: skip
# 我用Python写脚本, "I write scripts in Python".
CODING = _chars(0x6211, 0x7528) + "Python" + _chars(0x5199, 0x811A, 0x672C)
# Python脚本每天早上运行, "a Python script runs every morning": "python" and 脚本.
SCRIPT_FACT = "Python" + _chars(0x811A, 0x672C, 0x6BCF, 0x5929, 0x65E9, 0x4E0A, 0x8FD0, 0x884C)


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
def _system_zone_is_utc(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The system's local zone is UTC for these tests, wherever they run: with no timezone
    given, a day is the system's (R64), and their dates are written for a user on UTC.
    Without time.tzset (Windows), the machine's zone stays."""
    if not hasattr(time, "tzset"):
        yield
        return
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def system_zone(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Sets the system's local zone, as the TZ variable does (Unix only); the autouse
    fixture puts it back."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is Unix only")

    def set_zone(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    return set_zone


def _stamp(days_ago: float = 0.0, *, hours_ago: float = 0.0) -> str:
    """The time ``days_ago`` days and ``hours_ago`` hours before T0, as the store writes it."""
    return (T0 - timedelta(days=days_ago, hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _say(
    store: MemoryStore,
    conversation_id: str,
    user_text: str,
    reply: str = "",
    *,
    days_ago: float = 0.0,
    title: Optional[str] = None,
) -> int:
    """Record a round said ``days_ago`` days before T0."""
    at = T0 - timedelta(days=days_ago)
    return store.record_round(conversation_id, user_text, reply, at=at, title=title)


def _know(
    store: MemoryStore, fact_id: str, text: str, *, days_ago: float = 0.0, **fields: Any
) -> None:
    """Record a fact ``days_ago`` days before T0, or at ``recorded_at``."""
    values: Dict[str, Any] = {"recorded_at": _stamp(days_ago)}
    values.update(fields)
    store._insert_fact(_fact(fact_id, text, **values))


def _refs(hits: Sequence[SearchHit]) -> List[str]:
    return [hit.ref for hit in hits]


def _words(count: int, start: int = 0) -> List[str]:
    """Filler words of 7 code points each: word000, word001, ..."""
    return ["word{:03d}".format(number) for number in range(start, start + count)]


def _document(results: List[Dict[str, Any]], *, note: bool) -> str:
    """A search_memory result as compact JSON, as search.result_json writes it."""
    value: Dict[str, Any] = {"results": results}
    if note:
        value["note"] = "truncated"
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _values(value: Any) -> Iterator[Any]:
    """``value`` and every value nested in it."""
    yield value
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            yield from _values(item)


# ── the query (D17) ──


def test_query_builder_quotes_and_prefixes() -> None:
    assert query_terms("Tokyo flights") == ["tokyo", "flights"]
    assert build_fts_query("Tokyo flights") == '"tokyo"* OR "flights"*'
    # Stop words and single letters are left out: a query of nothing else has no terms.
    assert build_fts_query("the and of") is None
    assert build_fts_query("I a x") is None
    assert build_fts_query("") is None
    # The index's own tokens, casefolded and without diacritics, each once, in order.
    assert build_fts_query("Caf" + chr(0xE9) + " CAFE cafe, Tokyo!") == '"cafe"* OR "tokyo"*'
    words = ["term" + letter * 2 for letter in "abcdefghijklm"]  # 13 distinct terms
    assert build_fts_query(" ".join(words)) == " OR ".join('"' + w + '"*' for w in words[:12])


HOSTILE_QUERIES = (
    'foo" OR NEAR(x y) *',
    '"',
    '""',
    "*",
    "^foo",
    "foo*",
    "foo:bar",
    "seg_text:foo",
    "{seg_text fact_id}: foo",
    "fact_id:f1",
    "foo AND bar",
    "foo NOT bar",
    "NOT",
    "NEAR(foo bar, 2)",
    "(foo",
    "foo)",
    "-foo +bar",
    "foo'bar",
    'foo"bar',
)
QUOTED_TERM = re.compile(r'"(?:[^"]|"")+"\*?')


def test_query_builder_cannot_inject(store: MemoryStore) -> None:
    """Nothing in a query becomes FTS5 syntax: each term is a quoted string, so FTS5 reads
    an operator, a column filter or a stray quote as text. A search for any of them runs
    without error and finds only text holding its words."""
    _know(store, "f1", "Plays in a foo fighters tribute band.")
    _know(store, "f2", "Solved x y z puzzles.")
    _know(store, "f3", "Lives near the station.")
    # "or" is a stop word; x and y are single letters.
    assert build_fts_query('foo" OR NEAR(x y) *') == '"foo"* OR "near"*'
    assert sorted(_refs(store.search('foo" OR NEAR(x y) *'))) == ["f1", "f3"]
    for text in HOSTILE_QUERIES:
        query = build_fts_query(text)
        if query is not None:
            assert all(QUOTED_TERM.fullmatch(part) for part in query.split(" OR ")), text
        terms = query_terms(text)
        for hit in store.search(text):
            tokens = index_tokens(hit.text)
            assert any(token.startswith(term) for term in terms for token in tokens), text
    # A quote in a term would be doubled, as FTS5 escapes one; no term holds one.
    assert fts_term('a"b') == '"a""b"*'


def test_a_cjk_run_becomes_its_overlapping_bigrams(store: MemoryStore) -> None:
    """R63: a run of n CJK or Thai characters gives its n - 1 overlapping bigrams, each a
    phrase of two characters, which the index holds one to a token. A run of one is a term
    of one, and a Latin word written against a run is a prefix term of its own."""
    assert query_terms(TOKYO) == [TOKYO_PHRASE]
    assert build_fts_query(TOKYO) == '"' + TOKYO_PHRASE + '"'
    assert build_fts_query(TOKYO + "Tower") == '"' + TOKYO_PHRASE + '" OR "tower"*'
    i, want, go = WANT_TO_GO
    assert query_terms(WANT_TO_GO + "Google" + TOKYO) == [
        i + " " + want,
        want + " " + go,
        "google",
        TOKYO_PHRASE,
    ]
    # A repeated bigram is one term.
    assert query_terms(TOKYO + TOKYO) == [TOKYO_PHRASE, CAPITAL + " " + EAST]
    # Anything but a combining mark ends a run, and a lone character is a term of one.
    assert query_terms(EAST + ", " + CAPITAL) == [EAST, CAPITAL]
    assert build_fts_query(EAST) == '"' + EAST + '"'
    assert query_terms(EAST + "Tower") == [EAST, "tower"]  # a word ends a run too
    assert query_terms(THAI_EAT) == [THAI_EAT[0] + " " + THAI_EAT[2]]

    _say(store, "c1", WANT_TO_GO + TOKYO + TRAVEL)  # "I want to go to Tokyo to travel"
    _say(store, "c2", CAPITAL + EAST)  # the same characters the other way round
    _say(store, "c3", chr(0x6211) + chr(0x5728) + "Google" + chr(0x5DE5) + chr(0x4F5C))
    # The phrase finds the run inside longer text, its characters in order only.
    assert _refs(store.search(TOKYO)) == ["r:c1:1"]
    assert sorted(_refs(store.search(EAST))) == ["r:c1:1", "r:c2:1"]
    assert _refs(store.search("goog")) == ["r:c3:1"]


def test_words_and_bigrams_have_caps_of_their_own() -> None:
    """R63: words keep their cap of 12 and CJK or Thai terms have their own of 24, each
    taken in the query's order; one cap filling up never stops the other."""
    words = ["term" + letter * 2 for letter in "abcdefghijklm"]  # 13 distinct words
    run = _chars(*range(0x4E00, 0x4E1E))  # 30 characters: 29 bigrams
    bigrams = [run[at] + " " + run[at + 1] for at in range(len(run) - 1)]
    assert not {pair.replace(" ", "") for pair in bigrams} & CJK_STOP_BIGRAMS
    assert (QUERY_TERMS_MAX, QUERY_BIGRAMS_MAX) == (12, 24)
    assert query_terms(" ".join(words) + " " + run) == words[:12] + bigrams[:24]
    assert query_terms(run + " " + " ".join(words)) == bigrams[:24] + words[:12]
    assert query_terms(run + "zulu") == bigrams[:24] + ["zulu"]


def test_stop_bigrams_are_left_out() -> None:
    """R63: a stop bigram is not a term, as a stop word is not; the rest of its run stays."""
    we_go = _chars(0x6211, 0x4EEC, 0x53BB)  # 我们去, "we go": 我们 is a stop bigram
    assert query_terms(we_go) == [we_go[1] + " " + we_go[2]]
    assert build_fts_query(_chars(0x4EC0, 0x4E48, 0x65F6, 0x5019)) is None  # 什么时候, "when"
    assert len(CJK_STOP_BIGRAMS) <= 40
    assert all(len(pair) == 2 and all(map(is_segmented, pair)) for pair in CJK_STOP_BIGRAMS)


def test_a_chinese_question_preselects_a_fact_sharing_two_bigrams(store: MemoryStore) -> None:
    """R63: the question and the trip fact share two bigrams (去东, 东京), so the fact is
    found and pre-selected. A fact sharing one bigram is found but too weak to go with the
    turn, and one sharing only stop bigrams is not found at all."""
    _know(store, "f1", TRIP_FACT, days_ago=20)
    _know(store, "f2", MOVING_FACT, days_ago=1)
    _know(store, "f3", TOKYO + _chars(0x5F88, 0x5927), days_ago=2)  # 东京很大, "Tokyo is big"
    assert _refs(store.search(TRIP_QUESTION)) == ["f1", "f3"]
    assert strong_match(index_tokens(TRIP_FACT), query_terms(TRIP_QUESTION))
    assert [item["text"] for item in store.turn_payload(TRIP_QUESTION)["recalled"]] == [TRIP_FACT]


def test_japanese_and_thai_questions_find_a_fact_sharing_a_word(store: MemoryStore) -> None:
    """R63: a Japanese and a Thai question, each written without spaces, find the fact that
    shares a word with it, and the fact goes with the turn. A fact holding some of the
    question's characters, but none of its bigrams, is not found."""
    _know(store, "f1", JA_FACT)
    _know(store, "f2", JA_OTHER)
    _know(store, "f3", TH_FACT)
    _know(store, "f4", TH_OTHER)
    for question, fact, ref in ((JA_QUESTION, JA_FACT, "f1"), (TH_QUESTION, TH_FACT, "f3")):
        assert _refs(store.search(question)) == [ref]
        assert [item["text"] for item in store.turn_payload(question)["recalled"]] == [fact]


def test_a_mixed_query_has_the_latin_word_and_the_bigrams(store: MemoryStore) -> None:
    """R63: Latin written against Chinese is a prefix word between the runs' bigrams, and a
    fact matching the word and one bigram has two terms, enough to be pre-selected."""
    wo, yong, xie, jiao, ben = CODING[:2] + CODING[-3:]  # 我用 and 写脚本, in pinyin
    assert query_terms(CODING) == [wo + " " + yong, "python", xie + " " + jiao, jiao + " " + ben]
    assert build_fts_query(CODING) == '"{} {}" OR "python"* OR "{} {}" OR "{} {}"'.format(
        wo, yong, xie, jiao, jiao, ben
    )
    _know(store, "f1", SCRIPT_FACT)
    assert _refs(store.search(CODING)) == ["f1"]
    assert [item["text"] for item in store.turn_payload(CODING)["recalled"]] == [SCRIPT_FACT]


# ── ranking and fusion (D18) ──


def test_rrf_k60_and_recency_tiebreak(store: MemoryStore) -> None:
    assert rrf([["f1", "f2"], ["r:c1:1"]]) == {"f1": 1 / 61, "f2": 1 / 62, "r:c1:1": 1 / 61}
    assert rrf([["x"], ["y", "x"]]) == {"x": 1 / 61 + 1 / 62, "y": 1 / 61}
    times = {
        "f1": "2026-10-19T09:00:00Z",
        "f2": "2026-10-21T09:00:00Z",
        "r:c1:1": "2026-10-20T09:00:00Z",
    }
    # f1 and the round tie: the newer goes first. f2 is newer still, but scored lower.
    assert fuse([["f1", "f2"], ["r:c1:1"]], times) == [
        ("r:c1:1", 1 / 61),
        ("f1", 1 / 61),
        ("f2", 1 / 62),
    ]
    # On the same time too, the earlier list (the facts) goes first.
    same = dict.fromkeys(["f1", "r:c1:1"], "2026-10-20T09:00:00Z")
    assert [ref for ref, _ in fuse([["f1"], ["r:c1:1"]], same)] == ["f1", "r:c1:1"]

    _know(store, "f1", "Booked a kayak lesson.", days_ago=2)
    _say(store, "c1", "Booked a kayak lesson.", days_ago=1, title="Lessons")
    _say(store, "c2", "Booked a kayak lesson.", days_ago=3, title="More lessons")
    hits = store.search("kayak lesson")
    # The two rounds tie on BM25, so the newer ranks first in its list.
    assert _refs(hits) == ["r:c1:1", "f1", "r:c2:1"]
    assert [hit.score for hit in hits] == [1 / 61, 1 / 61, 1 / 62]


def test_superseded_facts_excluded_unless_nothing_else(store: MemoryStore) -> None:
    _know(
        store,
        "f1",
        "Lives in Sydney.",
        days_ago=30,
        superseded_by="f2",
        superseded_at=_stamp(1),
    )
    _know(store, "f2", "Lives in Melbourne.", days_ago=1)
    _know(store, "f3", "Saw an opera in Sydney.", hidden_at=_stamp())  # in its undo window
    assert _refs(store.search("lives")) == ["f2"]
    # Nothing else matches, so the superseded fact shows; a hidden one never does.
    assert _refs(store.search("Sydney")) == ["f1"]
    assert store.search("Sydney", from_date="2026-10-01") == []  # the filters still apply
    _say(store, "c1", "We flew to Sydney for the weekend.", days_ago=5)
    assert _refs(store.search("Sydney")) == ["r:c1:1"]


def test_date_filter_applies_to_both_corpora(store: MemoryStore) -> None:
    """The filter takes the day, in the user's timezone, a fact was recorded or a round
    said, both ends included. The user here is on UTC (R64's tests try other zones)."""
    _know(store, "f1", "Took a kayak lesson.", recorded_at="2026-10-10T23:30:00Z")
    _know(store, "f2", "Entered a kayak race.", recorded_at="2026-10-20T00:10:00Z")
    _say(store, "c1", "Kayak lesson today.", days_ago=11)  # 2026-10-10
    _say(store, "c2", "Kayak race today.", days_ago=1)  # 2026-10-20

    def found(**dates: str) -> List[str]:
        return sorted(_refs(store.search("kayak", timezone="UTC", **dates)))

    assert found() == ["f1", "f2", "r:c1:1", "r:c2:1"]
    assert found(from_date="2026-10-15") == ["f2", "r:c2:1"]
    assert found(to_date="2026-10-10") == ["f1", "r:c1:1"]
    assert found(from_date="2026-10-20", to_date="2026-10-20") == ["f2", "r:c2:1"]
    assert found(from_date="2026-10-11", to_date="2026-10-19") == []
    hits = store.search("kayak", from_date="2026-10-20", timezone="UTC")
    assert {hit.date for hit in hits} == {"2026-10-20"}
    for bad in ("2026-10-2", "20/10/2026", "2026-10-20T00:00:00Z", ""):
        with pytest.raises(ValueError, match="from_date must be a YYYY-MM-DD date"):
            store.search("kayak", from_date=bad)
    with pytest.raises(ValueError, match="to_date must be a YYYY-MM-DD date"):
        store.search("kayak", to_date="tomorrow")


def test_search_folds_diacritics_and_marks_as_the_index_does(store: MemoryStore) -> None:
    """Vietnamese with or without its diacritics, and Arabic with or without harakat, find
    each other, precomposed or not."""
    _know(store, "f1", "Drinks " + CA_PHE + " s" + chr(0x1EEF) + "a every morning.")
    _know(store, "f2", "Ca phe muoi is from Hue.")
    harakat = "".join(letter + FATHA for letter in KATABA)
    _know(store, "f3", harakat + " " + chr(0x0631) + chr(0x0633) + chr(0x0627) + chr(0x0644))
    _know(store, "f4", KATABA)
    for query in ("ca phe", CA_PHE, unicodedata.normalize("NFD", CA_PHE)):
        assert sorted(_refs(store.search(query))) == ["f1", "f2"]
    for query in (KATABA, harakat):
        assert sorted(_refs(store.search(query))) == ["f3", "f4"]


def test_no_terms_find_nothing(store: MemoryStore) -> None:
    _know(store, "f1", "Has a kayak.")
    assert store.search("") == [] and store.search("the of a") == []
    assert bm25_scan([("f1", "has a kayak.")], []) == []
    assert first_match("Has a kayak.", []) is None


def test_term_frequency_counts_as_fts5_does() -> None:
    assert term_frequency(["kayak", "kayaks", "kay", "okay"], "kay") == 3
    assert term_frequency([EAST, CAPITAL, EAST, CAPITAL], TOKYO_PHRASE) == 2
    laugh = chr(0x54C8)  # 哈
    assert term_frequency([laugh] * 3, laugh + " " + laugh) == 2  # overlapping instances count
    assert term_frequency([CAPITAL, EAST], TOKYO_PHRASE) == 0


def test_best_first_orders_as_the_fts5_sql_does() -> None:
    """By score (lower is better), then the newer, then the later row."""
    rows = [
        (-1.0, "2026-10-01T00:00:00Z", 1, "a"),
        (-2.0, "2026-09-01T00:00:00Z", 2, "b"),
        (-1.0, "2026-10-05T00:00:00Z", 3, "c"),
        (-1.0, "2026-10-05T00:00:00Z", 4, "d"),
    ]
    assert best_first(rows, 3) == [
        ("b", "2026-09-01T00:00:00Z"),
        ("d", "2026-10-05T00:00:00Z"),
        ("c", "2026-10-05T00:00:00Z"),
    ]


@pytest.mark.skipif(not SQLITE3_HAS_FTS5, reason="sqlite3 lacks FTS5")
def test_bm25_scan_gives_fts5s_own_scores() -> None:
    """The scan backend's BM25 is FTS5's: the same rows match, with the same scores, for
    prefix terms, bigrams and lone characters, repeated terms, folded diacritics, and an
    IDF at or under 0."""
    texts = [
        "Booked a kayak trip to Norway.",
        "Kayak, kayak, kayaks: the kayaking club meets on Sunday.",
        "Bought " + CA_PHE + " at the market.",
        "Ca phe sua da, every morning.",
        WANT_TO_GO + TOKYO + TRAVEL,
        TOKYO + TOKYO + "Tower",
        CAPITAL + EAST,
        "".join(letter + FATHA for letter in KATABA) + " " + KATABA,
        THAI_EAT + "kayak",
        "A note with nothing in common.",
        "Trip trip trip trip trip.",
        "Short.",
        TRIP_FACT,
        MOVING_FACT,
        JA_FACT,
        TH_FACT,
        SCRIPT_FACT,
    ]
    queries = [
        "kayak",
        "kay",
        "kayak trip",
        CA_PHE,
        "ca phe",
        TOKYO,
        EAST,
        TOKYO + " kayak",
        KATABA,
        THAI_EAT,
        "trip",
        "zzz nothing",
        WANT_TO_GO + TOKYO + TRAVEL,
        TRIP_QUESTION,
        JA_QUESTION,
        TH_QUESTION,
        CODING,
    ]
    corpora = [
        [("d{}".format(number), normalise_for_index(text)) for number, text in enumerate(texts)],
        [("k1", "kayak one"), ("k2", "kayak two"), ("k3", "kayak three")],  # every row: IDF < 0
    ]
    for docs in corpora:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute(
                "CREATE VIRTUAL TABLE t USING fts5("
                "key UNINDEXED, seg_text, tokenize='unicode61 remove_diacritics 2')"
            )
            conn.executemany("INSERT INTO t (key, seg_text) VALUES (?, ?)", docs)
            for query in queries:
                terms = query_terms(query)
                expected = dict(
                    conn.execute(
                        "SELECT key, bm25(t) FROM t WHERE t MATCH ?", (fts_query(terms),)
                    ).fetchall()
                )
                scored = dict(bm25_scan(docs, terms))
                assert scored.keys() == expected.keys(), query
                for key, score in expected.items():
                    assert scored[key] == pytest.approx(score, rel=1e-9), (query, key)


# ── hits and excerpts (D19) ──


def test_excerpt_is_600_around_the_first_match() -> None:
    words = _words(400)
    words[200] = words[300] = "zebracorn"
    text = " ".join(words)
    piece = excerpt(text, ["zebra"])
    assert piece.startswith(ELLIPSIS) and piece.endswith(ELLIPSIS)
    assert len(piece) <= 600 + 2
    inner = piece[1:-1]
    assert inner == inner.strip() and inner.count("zebracorn") == 1  # around the first
    at = text.index(inner)
    # Snapped to whitespace, so whole words at both ends, and nearly the whole width.
    assert text[at - 1] == " " and text[at + len(inner)] == " "
    assert len(inner) > 600 - 2 * len("word000 ")
    assert abs(inner.index("zebracorn") - 300) <= len("word000 ")  # about halfway in

    assert excerpt("A short text.", ["zebra"]) == "A short text."
    early = excerpt(" ".join(["zebracorn"] + _words(399)), ["zebracorn"])
    assert early.startswith("zebracorn ") and early.endswith(ELLIPSIS) and len(early) <= 601
    late = excerpt(" ".join(_words(399) + ["zebracorn"]), ["zebracorn"])
    assert late.startswith(ELLIPSIS) and late.endswith(" zebracorn") and len(late) <= 601
    # No term found: the start of the text.
    assert excerpt(text, ["nowhere"]).startswith("word000 ")
    # A prefix term matches where a token starts only.
    assert first_match(text, ["corn"]) is None
    # Text written without spaces is cut exactly: 600 code points, and the ellipses.
    han = chr(0x4E00) * 700 + TOKYO + chr(0x4E00) * 700
    piece = excerpt(han, [TOKYO_PHRASE])
    assert len(piece) == 602 and TOKYO in piece


def test_a_round_hit_is_an_excerpt_saying_who_said_what(store: MemoryStore) -> None:
    long_text = " ".join(_words(150) + ["zebracorn"] + _words(150, 150))
    _say(store, "c1", long_text, "Sounds good.", days_ago=1, title="Notes")
    _say(store, "c2", "Is a zebracorn real?", "No, it is made up.", days_ago=2)
    store.set_summary("c2", "Made-up animals", "Asked about a zebracorn.")
    _know(store, "f1", "Drew a zebracorn for the school fair.", conversation_id="c1")
    hits = {hit.ref: hit for hit in store.search("zebracorn")}
    assert hits["r:c2:1"] == SearchHit(
        kind="excerpt",
        date="2026-10-19",
        conversation="Made-up animals",  # no title: the summary's stands in
        text="User: Is a zebracorn real?\nAssistant: No, it is made up.",
        score=hits["r:c2:1"].score,
        ref="r:c2:1",
    )
    long = hits["r:c1:1"]
    assert long.text.startswith(ELLIPSIS + " User: word") and long.text.endswith(ELLIPSIS)
    assert "zebracorn" in long.text and len(long.text) <= 602 + len(" User: ")
    assert (long.kind, long.date, long.conversation) == ("excerpt", "2026-10-20", "Notes")
    fact = hits["f1"]
    assert (fact.kind, fact.date, fact.conversation, fact.text) == (
        "fact",
        "2026-10-21",
        "Notes",
        "Drew a zebracorn for the school fair.",
    )


def test_an_excerpt_centres_on_what_was_said_not_its_labels(store: MemoryStore) -> None:
    """A round hit's labels ("User: ", "Assistant: ") are not what was said, so a term
    that begins one, such as "use", is looked for in the round's own text only."""
    _say(store, "c1", " ".join(_words(150)) + " Then I use the kayak.", "Fine.", title="Notes")
    [hit] = store.search("use")
    assert hit.text.startswith(ELLIPSIS) and "I use the kayak." in hit.text


def test_an_excerpt_says_whose_words_it_starts_in(store: MemoryStore) -> None:
    """Review Minor 2: an excerpt cut after the start of its round gives the label of the
    side it starts in after the ellipsis, once, so the model can tell the user's words
    from the reply's. Where it is cut is excerpt's choice, as it was."""
    deep = " ".join(_words(150)) + " zebracorn " + " ".join(_words(150, 150))
    terms = ["zebracorn"]

    def shown(user_text: str, assistant_text: str) -> str:
        return round_hit("r:c1:1", 0.0, user_text, assistant_text, "2026-10-21", "", terms).text

    reply = shown("Tell me about zebras.", deep)
    text = round_text("Tell me about zebras.", deep)
    assert reply == ELLIPSIS + " Assistant: " + excerpt(text, terms)[1:]
    assert reply.startswith(ELLIPSIS + " Assistant: word") and reply.endswith(ELLIPSIS)
    assert shown(deep, "Sounds good.").startswith(ELLIPSIS + " User: word")
    assert shown("", deep).startswith(ELLIPSIS + " Assistant: word")
    # Cut where the reply's own label begins: the label once.
    at_label = shown(" ".join(_words(10)) + " " + "x" * 600, " ".join(_words(10)) + " zebracorn")
    assert at_label.startswith(ELLIPSIS + " Assistant: word000 ")
    assert at_label.count("Assistant:") == 1
    # Not cut: the round's own labels, as before.
    assert shown("Is a zebracorn real?", "No.") == "User: Is a zebracorn real?\nAssistant: No."

    _say(store, "c1", "Tell me about zebras.", deep, title="Zebras")
    [hit] = store.search("zebracorn")
    assert hit.text == reply


# ── the search_memory result (§8.4) ──


def test_result_json_capped_at_8000_with_note(store: MemoryStore) -> None:
    for number in range(30):
        text = " ".join(["kayak"] + _words(120))
        _say(store, "c{}".format(number), text, days_ago=number, title="Chat {}".format(number))
    document = store.search_result_json("kayak", limit=30)  # 20 at most
    result = json.loads(document)
    assert len(document) <= RESULT_CHARS_MAX
    assert set(result) == {"results", "note"} and result["note"] == "truncated"
    kept = result["results"]
    assert 0 < len(kept) < 20
    assert all(set(item) == {"date", "conversation", "kind", "text"} for item in kept)
    # Only the results that had to go: the next one would take it over the cap.
    every = [
        {"date": hit.date, "conversation": hit.conversation, "kind": hit.kind, "text": hit.text}
        for hit in store.search("kayak", limit=20)
    ]
    assert kept == every[: len(kept)]
    assert len(_document(every[: len(kept) + 1], note=True)) > RESULT_CHARS_MAX
    assert [item["date"] for item in kept] == sorted((item["date"] for item in kept), reverse=True)


def test_search_result_json_limits_and_shape(store: MemoryStore) -> None:
    for number in range(1, 26):
        _know(
            store,
            "f{}".format(number),
            "Paddled kayak route {}.".format(number),
            days_ago=number - 1,
        )
    assert store.search_result_json("kayak", limit=1) == (
        '{"results":[{"date":"2026-10-21","conversation":"","kind":"fact",'
        '"text":"Paddled kayak route 1."}]}'
    )
    assert len(json.loads(store.search_result_json("kayak"))["results"]) == 8
    # The limit is kept within 1 to 20.
    assert len(json.loads(store.search_result_json("kayak", limit=100))["results"]) == 20
    assert len(json.loads(store.search_result_json("kayak", limit=0))["results"]) == 1
    assert store.search("kayak", limit=0) == []
    assert json.loads(store.search_result_json("the of")) == {"results": []}
    # Characters as themselves, not escaped: the cap counts code points.
    _know(store, "f99", "Flew to " + TOKYO + " in May.")
    assert TOKYO in store.search_result_json(TOKYO)


# ── pre-selection and the turn payload (§8.1) ──


def test_preselection_floor(store: MemoryStore) -> None:
    """D18: a hit goes with a turn unasked when its fused score is at least 1/64 (rank 4 or
    better in its list) and it matches two distinct terms, in any script: a CJK or Thai
    bigram is one term (R63)."""
    terms = ["kayak", "trip"]
    tokens = index_tokens("A kayak trip.")
    assert PRESELECT_FLOOR == 1 / 64
    assert preselected(1 / 64, tokens, terms) and not preselected(1 / 65, tokens, terms)
    assert not strong_match(index_tokens("A kayak."), terms)
    trip = index_tokens(WANT_TO_GO + TOKYO + TRAVEL)
    assert not strong_match(trip, [TOKYO_PHRASE])  # one bigram, as one word, is too weak
    assert strong_match(trip, query_terms(TOKYO + TRAVEL))  # 东京, 京旅 and 旅行
    assert not strong_match(index_tokens(WANT_TO_GO + EAST), [EAST])

    _know(store, "f1", "Booked a kayak trip to Norway in June.", days_ago=2)
    assert store.turn_payload("kayak")["recalled"] == []  # one term matched: too weak
    assert store.turn_payload("Should I pack a drybag for the kayak trip?")["recalled"] == [
        {
            "kind": "fact",
            "date": "2026-10-19",
            "conversation_title": "",
            "text": "Booked a kayak trip to Norway in June.",
        }
    ]
    _say(store, "c1", WANT_TO_GO + TOKYO + TRAVEL, days_ago=1, title="Trip")
    assert store.turn_payload(TOKYO, exclude_conversation="c2")["recalled"] == []  # one bigram
    assert store.turn_payload(TOKYO + TRAVEL, exclude_conversation="c2")["recalled"] == [
        {
            "kind": "excerpt",
            "date": "2026-10-20",
            "conversation_title": "Trip",
            "text": "User: " + WANT_TO_GO + TOKYO + TRAVEL,
        }
    ]
    # The current conversation's own rounds are in the turn's context already.
    assert store.turn_payload(TOKYO + TRAVEL, exclude_conversation="c1")["recalled"] == []


def test_the_current_conversations_facts_are_not_recalled(store: MemoryStore) -> None:
    """Review Minor 3: a turn's recalled hits come from older conversations, so the facts
    drawn from the current one are left out with its rounds: they are in the turn's context
    already. A fact drawn from no conversation stays, and a search with no current
    conversation finds every one, superseded ones too when only those match."""
    booked, june = "Booked a kayak trip to Norway.", "The kayak trip to Norway is in June."
    packed = "Packed a drybag for the kayak trip."
    _know(store, "f1", booked, conversation_id="c1")
    _know(store, "f2", june, conversation_id="c2")
    _know(store, "f3", packed)  # drawn from no conversation
    question = "When is the kayak trip to Norway?"

    def recalled(text: str, current: Optional[str]) -> List[str]:
        items = store.turn_payload(text, exclude_conversation=current)["recalled"]
        return sorted(item["text"] for item in items)

    assert recalled(question, "c1") == sorted([june, packed])
    assert recalled(question, "c2") == sorted([booked, packed])
    assert recalled(question, None) == sorted([booked, june, packed])
    assert sorted(_refs(store.search(question))) == ["f1", "f2", "f3"]
    # Superseded facts, which count only when nothing else matches, are left out alike.
    gone = {"superseded_by": "f9", "superseded_at": "2026-10-21T09:00:00Z"}
    _know(store, "f4", "Sketched a zebracorn in pencil.", conversation_id="c1", **gone)
    _know(store, "f5", "Painted a zebracorn in pencil.", **gone)
    assert recalled("zebracorn in pencil", "c1") == ["Painted a zebracorn in pencil."]
    assert len(recalled("zebracorn in pencil", None)) == 2


def test_one_word_written_two_ways_is_one_term(store: MemoryStore) -> None:
    """Review Minor 1: a word that another word of the query begins is left out, as
    "kayak"* matches "kayaking" already, so one word written two ways counts once toward
    the floor and takes one place under the cap. Words in every script but CJK and Thai are
    prefixes alike; a CJK or Thai term is a phrase, and stays."""
    question = "I kayak and I love kayaking"
    assert query_terms(question) == ["kayak", "love"]
    assert query_terms("kayaking kayaks kayak") == ["kayak"]
    book = _chars(0x043A, 0x043D, 0x0438, 0x0433, 0x0430)  # книга, Russian "book"
    assert query_terms(book + _chars(0x043C, 0x0438) + " " + book) == [book]  # книгами книга
    assert query_terms(EAST + ", " + TOKYO) == [EAST, TOKYO_PHRASE]
    words = _words(11)
    assert query_terms(" ".join(["kayaking", *words, "kayak", "zebra"])) == [*words, "kayak"]
    assert not strong_match(index_tokens("Went kayaking on Sunday."), query_terms(question))

    _know(store, "f1", "Went kayaking on Sunday.")
    assert _refs(store.search(question)) == ["f1"]
    assert store.turn_payload(question)["recalled"] == []  # one word only
    _know(store, "f2", "Would love to go kayaking again.")
    recalled = store.turn_payload(question)["recalled"]
    assert [item["text"] for item in recalled] == ["Would love to go kayaking again."]


def test_turn_payload_shape_and_caps(store: MemoryStore) -> None:
    old = _stamp(100)
    store._insert_entry(_entry("e0", "Prefers metric units.", locked=True, updated_at=old))
    for number in range(1, 46):  # e1 the least recently updated
        store._insert_entry(
            _entry(
                "e{}".format(number),
                "Note {} about the user.".format(number),
                updated_at=_stamp(hours_ago=46 - number),
            )
        )
    for number in range(1, 13):  # c12 the most recently active
        conversation_id = "c{:02d}".format(number)
        title = "Chat {}".format(number)
        _say(
            store,
            conversation_id,
            "Message {}".format(number),
            "Reply",
            days_ago=13 - number,
            title=title,
        )
    store.set_incognito("c11", True)
    for number in range(1, 7):
        _know(store, "f{}".format(number), "Kayak trip number {}.".format(number), days_ago=number)

    payload = store.turn_payload("Another kayak trip soon?", exclude_conversation="c12")
    assert list(payload) == ["status", "entries", "recent", "recalled"]
    assert payload["status"] == "on"
    # Over ENTRY_MAX, the least recently updated unlocked entries are left out; a locked
    # one stays however old. In the store's order.
    entries = payload["entries"]
    assert [item["id"] for item in entries] == ["e0"] + ["e{}".format(n) for n in range(7, 46)]
    assert entries[0] == {
        "id": "e0",
        "topic": "identity",
        "text": "Prefers metric units.",
        "updated_at": "2026-07-13",
    }
    # The 8 most recently active, newest first, leaving out the current and incognito ones.
    recent = payload["recent"]
    assert [item["id"] for item in recent] == ["c{:02d}".format(n) for n in range(10, 2, -1)]
    assert recent[0] == {
        "id": "c10",
        "title": "Chat 10",
        "summary": "",
        "last_active_at": "2026-10-18",
    }
    # f1 to f4 clear the floor; f5 and f6, ranked 5 and 6 among the facts, fall under it.
    recalled = payload["recalled"]
    assert [item["text"] for item in recalled] == [
        "Kayak trip number {}.".format(n) for n in range(1, 5)
    ]
    assert recalled[0] == {
        "kind": "fact",
        "date": "2026-10-20",
        "conversation_title": "",
        "text": "Kayak trip number 1.",
    }
    dates = (
        [item["updated_at"] for item in entries]
        + [item["last_active_at"] for item in recent]
        + [item["date"] for item in recalled]
    )
    assert all(DATE.fullmatch(date) for date in dates)


def test_turn_payload_keeps_each_list_within_its_code_points(store: MemoryStore) -> None:
    for number in range(1, 26):  # 25 entries of 300 code points
        text = "Note {:02d} ".format(number) + "x" * 292
        store._insert_entry(
            _entry("e{}".format(number), text, updated_at=_stamp(hours_ago=26 - number))
        )
    for number in range(1, 9):  # 8 conversations of 6 + 600 code points
        conversation_id = "c{}".format(number)
        _say(
            store,
            conversation_id,
            "Hello",
            "Hi",
            days_ago=9 - number,
            title="Chat {}".format(number),
        )
        store.set_summary(conversation_id, "Summary", "s" * 600)
    payload = store.turn_payload(None)
    entries = payload["entries"]
    assert [item["id"] for item in entries] == ["e{}".format(n) for n in range(6, 26)]
    assert sum(len(item["text"]) for item in entries) == 6000
    recent = payload["recent"]
    assert [item["id"] for item in recent] == ["c8", "c7", "c6", "c5"]
    assert sum(len(item["title"]) + len(item["summary"]) for item in recent) <= 3000


def _recalled_hit(number: int, text: str, score: float = 1 / 61) -> Tuple[SearchHit, str]:
    """A pre-selection candidate: a hit and the text it was indexed by, here its own."""
    hit = SearchHit(
        kind="fact",
        date="2026-10-20",
        conversation="Trip {}".format(number),
        text=text,
        score=score,
        ref="f{}".format(number),
    )
    return hit, text


def test_recalled_items_keep_to_the_engines_rules() -> None:
    terms = ["kayak", "trip"]
    long_text = "kayak trip " + "y" * 989  # RECALLED_TEXT_MAX
    hits = [_recalled_hit(number, long_text) for number in range(1, 5)]
    # 3 x (6 + 1,000) code points fit in RECALLED_CHARS_MAX (4,000); a fourth would not.
    assert [item["conversation_title"] for item in recalled_items(hits, terms)] == [
        "Trip 1",
        "Trip 2",
        "Trip 3",
    ]
    passed_over = [
        _recalled_hit(1, long_text + "y"),  # over RECALLED_TEXT_MAX
        _recalled_hit(2, "kayak trip", score=1 / 65),  # under the floor
        _recalled_hit(3, "kayak only"),  # one term
        (replace(_recalled_hit(4, "kayak trip")[0], date="2026-10-20T09:00:00Z"), "kayak trip"),
        (_recalled_hit(5, "")[0], "kayak trip"),  # no text
    ]
    assert recalled_items(passed_over + [_recalled_hit(6, "kayak trip")], terms) == [
        {"kind": "fact", "date": "2026-10-20", "conversation_title": "Trip 6", "text": "kayak trip"}
    ]


def test_keep_within_is_the_engines_longest_prefix() -> None:
    assert keep_within([5, 5, 5], [2, 0, 1], 2, 100) == [0, 2]
    assert keep_within([5, 5, 5], [0, 1, 2], 3, 10) == [0, 1]
    # It stops at the first that doesn't fit, as the engine trims, rather than skip it.
    assert keep_within([8, 1], [0, 1], 5, 5) == []
    assert keep_within([], [], 4, 100) == []


def test_conversation_title_and_round_text() -> None:
    assert conversation_title("Trip", "Summary") == "Trip"
    assert conversation_title(None, "Summary") == "Summary"
    assert conversation_title("  ", "Summary") == "Summary"
    assert conversation_title("", None) == ""
    assert conversation_title("T" * 200, None) == "T" * 120
    assert round_text("Hi", "Hello") == "User: Hi\nAssistant: Hello"
    assert round_text("Hi", "") == "User: Hi"
    assert round_text("", "Hello") == "Assistant: Hello"


def test_turn_payload_status_paused_and_incognito(store: MemoryStore) -> None:
    store._insert_entry(_entry("e1", "Has a dog."))
    _say(store, "c1", "Walked the dog.", "Nice.", days_ago=1, title="Dog walk")
    nothing = {"entries": [], "recent": [], "recalled": []}
    store.paused = True
    assert store.turn_payload("dog walk") == {"status": "paused", **nothing}
    store.paused = False
    store.set_incognito("c2", True)
    assert store.turn_payload("dog walk", exclude_conversation="c2") == {
        "status": "incognito",
        **nothing,
    }
    payload = store.turn_payload("dog walk", exclude_conversation="c3")  # no record yet
    assert payload["status"] == "on"
    assert [item["id"] for item in payload["entries"]] == ["e1"]
    assert [item["id"] for item in payload["recent"]] == ["c1"]
    assert [item["kind"] for item in payload["recalled"]] == ["excerpt"]


def test_expired_entries_are_excluded_from_payload_not_deleted(store: MemoryStore) -> None:
    """R5: an entry whose valid_until has passed isn't sent, but stays in the store."""
    store._insert_entry(_entry("e1", "Is in Tokyo this week.", valid_until="2026-10-20"))
    store._insert_entry(_entry("e2", "Is training for a marathon.", valid_until="2026-10-21"))
    store._insert_entry(_entry("e3", "Has a dog."))
    assert store.turn_payload(None)["entries"] == [
        {
            "id": "e2",
            "topic": "identity",
            "text": "Is training for a marathon.",
            "updated_at": "2026-10-20",
            "valid_until": "2026-10-21",  # valid through that day
        },
        # No valid_until: the key is left out, never sent as null.
        {"id": "e3", "topic": "identity", "text": "Has a dog.", "updated_at": "2026-10-20"},
    ]
    assert [entry.id for entry in store.entries()] == ["e1", "e2", "e3"]


def test_recent_passes_over_a_conversation_the_engine_would_drop(store: MemoryStore) -> None:
    """R56: the engine drops a recent conversation without a title, or with an id it can't
    take, so none is sent. The summary's title stands in for a missing chat title."""
    _say(store, "c1", "Hello", "Hi", days_ago=1)  # no title, no summary
    _say(store, "c2", "Hello", "Hi", days_ago=2)
    store.set_summary("c2", "Weekend plans", "Talked about the weekend.")
    _say(store, "c3", "Hello", "Hi", days_ago=3, title="   ")
    _say(store, "c" * 33, "Hello", "Hi", days_ago=4, title="Long id")
    _say(store, "c5", "Hello", "Hi", days_ago=5, title="Recipes")
    _say(store, "c6", "Hello", "Hi", days_ago=6, title="T" * 200)
    assert store.turn_payload(None)["recent"] == [
        {
            "id": "c2",
            "title": "Weekend plans",
            "summary": "Talked about the weekend.",
            "last_active_at": "2026-10-19",
        },
        {"id": "c5", "title": "Recipes", "summary": "", "last_active_at": "2026-10-16"},
        {"id": "c6", "title": "T" * 120, "summary": "", "last_active_at": "2026-10-15"},
    ]


# The engine's rules for the memory field, as t2v/core/memory/schemas.py in the Platform
# repo states them: read from there, not imported, so the SDK's tests need no engine.
_ENGINE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def _engine_id(value: Any) -> bool:
    return isinstance(value, str) and _ENGINE_ID.fullmatch(value) is not None


def _engine_date(value: Any) -> bool:
    return isinstance(value, str) and DATE.fullmatch(value) is not None


def _engine_text(value: Any, cap: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= cap


def _engine_entry(item: Dict[str, Any]) -> bool:
    valid_until = item.get("valid_until")
    return (
        _engine_id(item.get("id"))
        and item.get("topic") in TOPICS
        and _engine_text(item.get("text"), 300)
        and _engine_date(item.get("updated_at"))
        and (valid_until is None or _engine_date(valid_until))
    )


def _engine_recent(item: Dict[str, Any]) -> bool:
    summary = item.get("summary")
    return (
        _engine_id(item.get("id"))
        and _engine_text(item.get("title"), 120)
        and isinstance(summary, str)
        and len(summary) <= 600
        and _engine_date(item.get("last_active_at"))
    )


def _engine_recalled(item: Dict[str, Any]) -> bool:
    return (
        item.get("kind") in ("fact", "excerpt")
        and _engine_date(item.get("date"))
        and isinstance(item.get("conversation_title"), str)
        and _engine_text(item.get("text"), 1000)
    )


# Each list: its rule, the date it is trimmed by (oldest first; recalled loses its tail
# instead), the fields its total counts, and its count and total.
_ENGINE_LISTS: Tuple[Tuple[str, Any, Optional[str], Tuple[str, ...], int, int], ...] = (
    ("entries", _engine_entry, "updated_at", ("text",), 40, 6000),
    ("recent", _engine_recent, "last_active_at", ("title", "summary"), 8, 3000),
    ("recalled", _engine_recalled, None, ("conversation_title", "text"), 4, 4000),
)


def _engine_counts(payload: Dict[str, Any]) -> Dict[str, int]:
    """How many items of each list normalise_memory_field would drop (malformed, over a
    per-item cap, or with an id an earlier item took) and trim (over the list's count or
    total), as it counts them."""
    counts: Dict[str, int] = {}
    for name, rule, date, fields, max_items, max_chars in _ENGINE_LISTS:
        items = payload[name]
        valid = [item for item in items if isinstance(item, dict) and rule(item)]
        ranked: Sequence[int] = range(len(valid))
        if date is not None:
            first: List[Dict[str, Any]] = []
            for item in valid:
                if all(item["id"] != other["id"] for other in first):
                    first.append(item)
            valid = first
            dates = [item[date] for item in valid]
            ranked = sorted(range(len(valid)), key=dates.__getitem__, reverse=True)
        kept = chars = 0
        for position in ranked:
            chars += sum(len(valid[position][field]) for field in fields)
            if kept == max_items or chars > max_chars:
                break
            kept += 1
        counts["dropped " + name] = len(items) - len(valid)
        counts["trimmed " + name] = len(valid) - kept
    return counts


def test_a_built_payload_passes_the_engines_rules_whole(store: MemoryStore) -> None:
    """The engine drops and trims nothing of a payload this store builds, measured in code
    points as it measures them, even with every list over its caps in the store."""
    for number in range(1, 51):  # 50 entries of 150 code points; e1 the newest
        store._insert_entry(
            _entry(
                "e{}".format(number),
                "Entry {:02d} ".format(number) + "e" * 141,
                updated_at=_stamp(hours_ago=number),
                locked=number % 10 == 0,
                valid_until="2026-12-31" if number % 7 == 0 else None,
            )
        )
    for number in range(1, 13):
        conversation_id = "c{:02d}".format(number)
        _say(
            store,
            conversation_id,
            "Planning the kayak trip, part {}. ".format(number) + " ".join(_words(120)),
            "Noted.",
            days_ago=number,
            title="Conversation number {:02d}".format(number),
        )
        store.set_summary(conversation_id, "Summary", "s" * 400)
    payload = store.turn_payload("Is the kayak trip still on?", exclude_conversation="c99")

    counts = _engine_counts(payload)
    assert counts == dict.fromkeys(counts, 0)
    # Each list was over a cap in the store, and is at it now.
    assert len(payload["entries"]) == 40
    assert sum(len(item["text"]) for item in payload["entries"]) == 6000
    assert len(payload["recent"]) == 7  # 7 x (22 + 400) code points of 3,000
    assert len(payload["recalled"]) == 4
    # Only the wire's names, dates as dates, and no nulls.
    for item in payload["entries"]:
        assert {"id", "topic", "text", "updated_at"} <= set(item)
        assert set(item) <= {"id", "topic", "text", "updated_at", "valid_until"}
    assert any("valid_until" in item for item in payload["entries"])
    assert all(
        set(item) == {"id", "title", "summary", "last_active_at"} for item in payload["recent"]
    )
    assert all(
        set(item) == {"kind", "date", "conversation_title", "text"} for item in payload["recalled"]
    )
    assert all(value is not None for value in _values(payload))
    assert json.loads(json.dumps(payload)) == payload
    # The rules bite: one entry more would be trimmed, and a time for a date dropped.
    extra = dict(payload["entries"][0], id="e999")
    assert (
        _engine_counts(dict(payload, entries=payload["entries"] + [extra]))["trimmed entries"] == 1
    )
    timed = dict(payload["recent"][0], last_active_at="2026-10-20T10:00:00Z")
    assert _engine_counts(dict(payload, recent=[timed]))["dropped recent"] == 1


# ── the user's days (R64) ──

LA, SYDNEY = "America/Los_Angeles", "Australia/Sydney"
# Noon on 10 November in Los Angeles (UTC-8), 07:00 on the 11th in Sydney (UTC+11).
EVENING = T0 + timedelta(days=20, hours=10)
EVENING_AT = EVENING.strftime("%Y-%m-%dT%H:%M:%SZ")  # 2026-11-10T20:00:00Z
UNKNOWN_ZONES = ("Mars/Olympus_Mons", "", "../../etc/passwd", "America", "not a zone")


def test_an_entry_is_sent_until_its_last_day_ends_where_the_user_is(
    store: MemoryStore, clock: FakeClock
) -> None:
    """R64: an entry valid until 2026-11-10 is still sent at noon on the 10th in Los Angeles
    and no longer at 07:00 on the 11th in Sydney, the same instant. The day each entry was
    updated, and each conversation last active, is the user's day too."""
    clock.now = EVENING
    trip = _entry("e1", "Is in Tokyo until 10 Nov.", topic="ongoing", valid_until="2026-11-10")
    store._insert_entry(replace(trip, updated_at=EVENING_AT))
    store._insert_entry(_entry("e2", "Has a dog.", updated_at=EVENING_AT))
    store.record_round("c1", "Hello.", "Hi.", at=EVENING, title="Greetings")
    dog = {"id": "e2", "topic": "identity", "text": "Has a dog."}
    in_la = store.turn_payload(None, timezone=LA)
    assert in_la["entries"] == [
        {
            "id": "e1",
            "topic": "ongoing",
            "text": "Is in Tokyo until 10 Nov.",
            "updated_at": "2026-11-10",
            "valid_until": "2026-11-10",
        },
        dict(dog, updated_at="2026-11-10"),
    ]
    in_sydney = store.turn_payload(None, timezone=SYDNEY)
    assert in_sydney["entries"] == [dict(dog, updated_at="2026-11-11")]
    assert [item["last_active_at"] for item in (*in_la["recent"], *in_sydney["recent"])] == [
        "2026-11-10",
        "2026-11-11",
    ]


def test_a_fact_gives_its_valid_until_and_is_recalled_past_it(
    store: MemoryStore, clock: FakeClock
) -> None:
    """R65: a fact's valid_until never keeps it out of a search or a turn, wherever the user
    is: facts stay true as history. A fact hit with one, past or to come, gives it before
    its text, in a search_memory result and in a recalled item, within the engine's cap."""
    clock.now = EVENING  # the 11th in Sydney, the 10th in Los Angeles
    flight = "Flies home from Tokyo on 10 November."
    again = "Flies home from Tokyo again on 3 December."
    _know(store, "f1", flight, recorded_at="2026-10-01T09:00:00Z", valid_until="2026-11-10")
    _know(store, "f2", again, recorded_at="2026-10-02T09:00:00Z", valid_until="2026-12-03")
    shown = ["(until 2026-11-10) " + flight, "(until 2026-12-03) " + again]
    question = "When is my flight home from Tokyo?"
    for zone in (SYDNEY, LA):
        recalled = store.turn_payload(question, timezone=zone)["recalled"]
        assert sorted(item["text"] for item in recalled) == shown
        result = json.loads(store.search_result_json(question, timezone=zone))
        assert sorted(item["text"] for item in result["results"]) == shown
    # The longest fact the store keeps, with its bound, is within the engine's cap whole.
    longest = (flight + " " + WORD * 40)[:FACT_TEXT_MAX]
    _know(store, "f3", longest, recorded_at="2026-10-03T09:00:00Z", valid_until="2026-11-10")
    recalled = store.turn_payload(question, timezone=SYDNEY)["recalled"]
    assert "(until 2026-11-10) " + longest in [item["text"] for item in recalled]
    assert FACT_TEXT_MAX + len("(until 2026-11-10) ") <= RECALLED_TEXT_MAX


def test_the_date_filter_and_hit_dates_are_the_users_days(store: MemoryStore) -> None:
    """R64: the 11th in Sydney runs from 2026-11-10T13:00Z, included, to 2026-11-11T13:00Z,
    so what was recorded or said at 2026-11-10T20:00Z is on it, and dated it. In Los Angeles
    and in UTC the same instant is on the 10th."""
    _know(store, "f1", "Booked a kayak lesson.", recorded_at=EVENING_AT)
    _know(store, "f2", "Paid for the kayak.", recorded_at="2026-11-10T12:59:59Z")  # 23:59:59
    _know(store, "f3", "The kayak arrived.", recorded_at="2026-11-10T13:00:00Z")  # midnight
    store.record_round("c1", "Kayak lesson booked.", "", at=EVENING)
    store.record_round("c2", "Kayak at dawn.", "", at=T0 + timedelta(days=20, hours=3))  # midnight

    def found(day: str, zone: str) -> List[Tuple[str, str]]:
        hits = store.search("kayak", from_date=day, to_date=day, timezone=zone)
        return sorted((hit.ref, hit.date) for hit in hits)

    on_the_11th = ["f1", "f3", "r:c1:1", "r:c2:1"]
    assert found("2026-11-11", SYDNEY) == [(ref, "2026-11-11") for ref in on_the_11th]
    assert found("2026-11-10", SYDNEY) == [("f2", "2026-11-10")]
    result = json.loads(store.search_result_json("booked", timezone=SYDNEY))
    assert [item["date"] for item in result["results"]] == ["2026-11-11", "2026-11-11"]
    for zone in (LA, "UTC"):
        assert found("2026-11-11", zone) == []
        assert {date for _, date in found("2026-11-10", zone)} == {"2026-11-10"}
        assert len(found("2026-11-10", zone)) == 5
    # A day the calendar lacks reads as the text compares: November, the 0th to the 31st.
    whole = store.search("kayak", from_date="2026-11-00", to_date="2026-11-31", timezone=SYDNEY)
    assert len(whole) == 5


def test_an_unknown_zone_falls_back_to_the_systems_without_raising(
    store: MemoryStore, clock: FakeClock, system_zone: Callable[[str], None]
) -> None:
    """R64: a name zoneinfo can't use, unknown, empty, malformed or a folder of zones, means
    the system's local zone, and nothing raises."""
    clock.now = EVENING
    store._insert_entry(_entry("e1", "Is in Tokyo until 10 Nov.", valid_until="2026-11-10"))
    _know(store, "f1", "Booked a kayak lesson.", recorded_at=EVENING_AT)
    system_zone(SYDNEY)
    for name in UNKNOWN_ZONES:
        assert store.turn_payload(None, timezone=name)["entries"] == []  # over on the 11th
        [hit] = store.search("kayak", from_date="2026-11-11", timezone=name)
        assert hit.date == "2026-11-11"
    system_zone(LA)
    for name in UNKNOWN_ZONES:
        assert [item["id"] for item in store.turn_payload(None, timezone=name)["entries"]] == ["e1"]
        assert store.search("kayak", from_date="2026-11-11", timezone=name) == []


def test_with_no_tz_data_a_named_zone_falls_back_to_the_systems(
    store: MemoryStore,
    clock: FakeClock,
    system_zone: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R64: on a platform with no tz data, such as Windows without tzdata, zoneinfo knows no
    zone at all; the system's local zone stands in for every name."""

    def no_tz_data(key: str) -> Any:
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found with key " + key)

    clock.now = EVENING
    store._insert_entry(_entry("e1", "Is in Tokyo until 10 Nov.", valid_until="2026-11-10"))
    system_zone(LA)
    monkeypatch.setattr(days, "zoneinfo", SimpleNamespace(ZoneInfo=no_tz_data))
    days._named_zone.cache_clear()
    try:
        # Sydney is past the 10th, but the system's zone, Los Angeles, is not.
        assert len(store.turn_payload(None, timezone=SYDNEY)["entries"]) == 1
        assert days.day(EVENING_AT, SYDNEY) == "2026-11-10"
    finally:
        days._named_zone.cache_clear()


def test_no_zone_means_the_systems(
    store: MemoryStore, clock: FakeClock, system_zone: Callable[[str], None]
) -> None:
    """R64: with timezone None, the default, every day is the system's local zone's."""
    clock.now = EVENING
    store._insert_entry(_entry("e1", "Is in Tokyo until 10 Nov.", valid_until="2026-11-10"))
    _know(store, "f1", "Booked a kayak lesson.", recorded_at=EVENING_AT)
    system_zone(SYDNEY)
    assert store.turn_payload(None)["entries"] == []
    assert [hit.date for hit in store.search("kayak")] == ["2026-11-11"]
    assert _refs(store.search("kayak", from_date="2026-11-11")) == ["f1"]
    system_zone(LA)
    assert [item["id"] for item in store.turn_payload(None)["entries"]] == ["e1"]
    assert [hit.date for hit in store.search("kayak")] == ["2026-11-10"]
    assert store.search("kayak", from_date="2026-11-11") == []


def test_expiring_facts_and_the_markdown_export_use_the_users_days(
    store: MemoryStore, clock: FakeClock
) -> None:
    """R64: an expired fact is valid until the day before the user's today, and the export
    dates each fact by the user's day."""
    clock.now = EVENING
    _know(store, "f1", "Is in Tokyo this week.", recorded_at=EVENING_AT)
    _know(store, "f2", "Has exams this week.", recorded_at=EVENING_AT)
    store.expire_facts(["f1"], timezone=SYDNEY)  # the 11th there
    store.expire_facts(["f2"], timezone=LA)  # the 10th there
    assert [fact.valid_until for fact in store.facts()] == ["2026-11-10", "2026-11-09"]
    tokyo = ": Is in Tokyo this week. (until 2026-11-10)"
    assert "- 2026-11-11" + tokyo in store.export_markdown(timezone=SYDNEY)
    assert "- 2026-11-10" + tokyo in store.export_markdown(timezone=LA)


def test_day_bounds_read_impossible_and_far_dates_as_the_text_compares() -> None:
    """``days.bounds``: the start of the user's first day and of the day after the last. A
    day the calendar lacks is the nearest real one on its side; no real day there finds
    nothing (None); a day whose start a datetime can't hold leaves its side open."""
    assert days.bounds("2026-11-11", "2026-11-11", SYDNEY) == (
        "2026-11-10T13:00:00Z",
        "2026-11-11T13:00:00Z",
    )
    assert days.bounds(None, None, SYDNEY) == (None, None)

    def start(day: str) -> Tuple[Optional[str], Optional[str]]:
        return day + "T00:00:00Z", None

    def end(day: str) -> Tuple[Optional[str], Optional[str]]:
        return None, day + "T00:00:00Z"

    # Each text as a from_date and as a to_date.
    cases = [
        ("2026-04-31", start("2026-05-01"), end("2026-05-01")),  # from 1 May; to 30 April
        ("2026-02-30", start("2026-03-01"), end("2026-03-01")),
        ("2026-03-00", start("2026-03-01"), end("2026-03-01")),  # from 1 March; to 28 February
        ("2026-00-15", start("2026-01-01"), end("2026-01-01")),  # to 31 December 2025
        ("2026-13-01", start("2027-01-01"), end("2027-01-01")),  # to 31 December 2026
        ("0000-05-05", start("0001-01-01"), None),  # no day comes before year 1
        ("0001-01-00", start("0001-01-01"), None),
        ("9999-12-32", None, (None, None)),  # none after 9999-12-31, whose end is open
    ]
    for text, as_from, as_to in cases:
        assert days.bounds(text, None, "UTC") == as_from, text
        assert days.bounds(None, text, "UTC") == as_to, text
    assert days.bounds("0001-01-01", "9999-12-31", SYDNEY) == (None, None)
    # A day that starts in a daylight-saving gap starts when the clocks jump.
    assert days.bounds("2018-11-04", None, "America/Sao_Paulo") == ("2018-11-04T03:00:00Z", None)
    # A time's day, and the UTC day for a time with no day there or a malformed one.
    assert [days.day(EVENING_AT, zone) for zone in (SYDNEY, LA, "UTC")] == [
        "2026-11-11",
        "2026-11-10",
        "2026-11-10",
    ]
    assert days.day("9999-12-31T23:00:00Z", SYDNEY) == "9999-12-31"
    assert days.day("2026-11-10", SYDNEY) == "2026-11-10"


# ── incognito (R44) ──


def test_marking_a_conversation_incognito_deletes_its_rounds_and_summary(
    store: MemoryStore,
) -> None:
    """R44: an incognito chat's rounds are never kept, so marking a chat incognito deletes
    the rounds it has already, with their index rows and its summary, and scrubs the file
    of them. Its title stays, and so do facts drawn from it, which take no name from it
    while it is incognito."""
    _say(store, "c1", "Planning the " + WORD + " trip.", "Sounds fun.", days_ago=1, title="Plans")
    _say(store, "c1", "More about " + WORD + ".", "Sure.", days_ago=1)
    store.set_summary("c1", "About " + WORD, "Talked about " + WORD + ".")
    store.mark_extracted("c1", 2)
    _know(store, "f1", "Plans a kayak trip.", conversation_id="c1")
    assert sorted(_refs(store.search(WORD))) == ["r:c1:1", "r:c1:2"]

    store.set_incognito("c1", True)
    record = _conversation(store, "c1")
    assert (record.incognito, record.title, record.summary_title, record.summary_text) == (
        True,
        "Plans",
        None,
        None,
    )
    assert record.extracted_through == 0
    assert store._query("SELECT count(*) FROM rounds")[0][0] == 0
    assert _indexed_rounds(store) == []
    assert store.search(WORD) == []
    assert not _in_files(store, WORD)
    assert [(hit.ref, hit.conversation) for hit in store.search("kayak")] == [("f1", "")]
    store.set_incognito("c1", False)  # nothing comes back
    assert store.rounds("c1") == []
    assert [(hit.ref, hit.conversation) for hit in store.search("kayak")] == [("f1", "Plans")]


def test_search_and_the_payload_pass_over_an_incognito_conversation(store: MemoryStore) -> None:
    """R44's second guard: rounds under an incognito flag, as a file from an older build
    might hold them, are never found or sent."""
    _say(store, "c1", "Planning a " + WORD + " trip.", "Fun!", days_ago=1, title="Trip")
    _know(store, "f1", "Is planning a " + WORD + " trip.", conversation_id="c1")
    with store._write():  # the flag alone, the rounds left in place
        store._db.execute("UPDATE conversations SET incognito = 1 WHERE id = 'c1'")
    hits = store.search(WORD + " trip")
    assert [(hit.ref, hit.conversation) for hit in hits] == [("f1", "")]
    payload = store.turn_payload(WORD + " trip", exclude_conversation="c2")
    assert [(item["kind"], item["conversation_title"]) for item in payload["recalled"]] == [
        ("fact", "")
    ]
    assert payload["recent"] == []
    assert len(json.loads(store.search_result_json(WORD))["results"]) == 1


# ── forget (R56, R57) ──


def test_after_forget_neither_search_nor_the_payload_finds_it(
    store: MemoryStore, clock: FakeClock
) -> None:
    """R56: once forget("peanut") has run, no search finds a fact, round or summary with
    it, and no turn sends it, as an entry, a recent conversation or a recalled hit. Undo
    keeps it in the file for its window, and the purge after that scrubs it, from the scan
    backend's search cache too."""
    _say(
        store,
        "c1",
        "I am allergic to peanut butter.",
        "Noted: no peanut sauces.",
        days_ago=2,
        title="Peanut allergy",
    )
    _say(store, "c1", "What about cashews?", "Those are tree nuts.", days_ago=2)
    store.set_summary("c1", "Allergies", "The user is allergic to peanuts.")
    _say(store, "c2", "Thai satay with peanut sauce tonight?", "Sounds tasty.", days_ago=1)
    store.set_summary("c2", "Dinner ideas", "Satay for dinner.")
    _know(store, "f1", "Is allergic to peanuts.", conversation_id="c1")
    store.apply_ops([{"op": "add", "topic": "identity", "text": "Allergic to peanuts."}])
    question = "Is the peanut satay safe for me?"
    assert "peanut" in json.dumps(store.turn_payload(question, exclude_conversation="c3")).lower()

    store.forget("peanut")
    assert store.search("peanut") == []
    assert json.loads(store.search_result_json("peanut")) == {"results": []}
    payload = store.turn_payload(question, exclude_conversation="c3")
    assert "peanut" not in json.dumps(payload, ensure_ascii=False).lower()
    # c1 lost its title and its summary, so it has no name to be sent by.
    assert [item["id"] for item in payload["recent"]] == ["c2"]

    clock.now = T0 + timedelta(seconds=31)  # past the undo window
    store.purge()
    assert not any("peanut" in text for cache in store._scan_cache.values() for text in cache)
    assert not _in_files(store, "peanut")
    assert store.search("peanut") == []


def test_forgetting_part_of_a_word_leaves_the_word_findable(store: MemoryStore) -> None:
    """R57: forget("pea") clears "peas" from the log, a whole word, but not "peanut"."""
    _say(store, "c1", "Peanut butter on toast?", "A classic.", days_ago=1, title="Breakfast")
    _say(store, "c2", "Peas or beans with dinner?", "Peas.", days_ago=1, title="Dinner")
    store.forget("pea")
    assert _refs(store.search("peanut")) == ["r:c1:1"]
    assert store.search("peas") == []
    recalled = store.turn_payload("peanut butter again", exclude_conversation="c3")["recalled"]
    assert [item["text"] for item in recalled] == [
        "User: Peanut butter on toast?\nAssistant: A classic."
    ]


# ── reads (R61) ──


def test_a_read_inside_a_read_joins_it(tmp_path: Path, backend: str) -> None:
    """R61: a ``_read`` inside a ``_read`` starts no transaction: it sees the snapshot the
    outer one took, whatever another connection commits meanwhile, and leaves the outer
    read working. A ``_read`` inside a ``_write`` sees the write's own changes."""
    path = tmp_path / "u1.sqlite3"
    first = MemoryStore(path, backend=backend, clock=FakeClock())  # type: ignore[arg-type]
    second = MemoryStore(path, backend=backend, clock=FakeClock())  # type: ignore[arg-type]
    try:
        _know(first, "f1", "Has a kayak.")
        with first._read():
            assert _refs(first.search("kayak canoe")) == ["f1"]
            _know(second, "f2", "Has a canoe.")  # another connection commits
            with first._read():
                assert [fact.id for fact in first.facts()] == ["f1"]
                assert _refs(first.search("kayak canoe")) == ["f1"]
            assert [fact.id for fact in first.facts()] == ["f1"]  # still the outer snapshot
        assert sorted(_refs(first.search("kayak canoe"))) == ["f1", "f2"]
        with first._write():
            _know(first, "f3", "Has a raft.")
            with first._read():
                assert [fact.id for fact in first.facts()] == ["f1", "f2", "f3"]
    finally:
        first.close()
        second.close()


def test_a_write_inside_a_read_raises_before_writing(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R61: a read never turns into a write. A ``_write`` inside a ``_read`` raises
    RuntimeError before it runs a statement, and the read goes on."""
    _know(store, "f1", "Has a kayak.")
    statements: List[str] = []
    execute = store._db.execute

    def spy(sql: str, params: Sequence[Any] = ()) -> List[Tuple[Any, ...]]:
        statements.append(sql)
        return execute(sql, params)

    monkeypatch.setattr(store._db, "execute", spy)
    with store._read():
        assert _refs(store.search("kayak")) == ["f1"]
        statements.clear()
        with pytest.raises(RuntimeError, match="a write can't start inside a read"):
            with store._write():
                store._db.execute("DELETE FROM facts")  # never runs
        with pytest.raises(RuntimeError, match="a write can't start inside a read"):
            store.record_round("c1", "Hello", "Hi")
        assert statements == []  # not even BEGIN IMMEDIATE
        assert _refs(store.search("kayak")) == ["f1"]  # the read goes on
    assert [fact.id for fact in store.facts()] == ["f1"]
    assert store.conversation("c1") is None
    assert store.record_round("c1", "Hello", "Hi") == 1  # once the read is over, writes work


# ── text helpers ──


def test_index_token_spans_give_index_tokens_and_their_places() -> None:
    samples = [
        "Deep-dish pizza, NYC's best!",
        CA_PHE + " s" + chr(0x1EEF) + "a " + chr(0x111) + chr(0xE1),  # cà phê sữa đá
        unicodedata.normalize("NFD", CA_PHE),  # marks inside the tokens
        "".join(letter + FATHA for letter in KATABA) + " " + KATABA,
        WANT_TO_GO + "Google" + TOKYO + "2026",
        THAI_EAT + " " + chr(0x0E02),
        chr(0xFB01) + "ne " + chr(0xFF21) + chr(0xFF22),  # a ligature, full-width letters
        "snake_case and" + chr(0xE000) + "private",  # private use is a token character
        "x" + chr(0x2474) + "y",  # ⑴ is "(1)" in NFKC: its parentheses part the tokens
        "2" + chr(0x32C0) + "x",  # ㋀ is "1月": the 1 joins the 2, the 月 stands apart
        "",
        "   ",
    ]
    for text in samples:
        spans = list(index_token_spans(text))
        assert [token for token, _, _ in spans] == index_tokens(text), text
        for token, start, end in spans:  # one code point can hold two tokens, as ㋀ does
            assert token in index_tokens(text[start:end]), text
    # A combining mark belongs to the token it follows.
    assert list(index_token_spans(unicodedata.normalize("NFD", CA_PHE))) == [
        ("ca", 0, 3),
        ("phe", 4, 8),
    ]
    with_harakat = "".join(letter + FATHA for letter in KATABA)
    assert list(index_token_spans(with_harakat + "!")) == [(KATABA, 0, 6)]


def _ranges(*bounds: Tuple[int, int]) -> List[str]:
    """The code points of each inclusive range, as characters."""
    return [chr(point) for low, high in bounds for point in range(low, high + 1)]


# What the property check draws from, script by script: the forms NFKC rewrites as well.
_ASCII_POOLS = [
    _ranges((0x41, 0x5A), (0x61, 0x7A)),  # Latin
    _ranges((0x30, 0x39)),  # digits
    list(" -_,.!?'\"()/:;\n\t"),  # punctuation and spaces
]
_POOLS = _ASCII_POOLS + [
    _ranges((0xC0, 0xD6), (0xD8, 0xF6), (0x100, 0x17F), (0x1EA0, 0x1EF9)),  # with diacritics
    _ranges((0x300, 0x36F)),  # combining diacritical marks
    _ranges((0x4E00, 0x4E3F), (0x3041, 0x3096), (0x30A1, 0x30FA), (0xFF66, 0xFF9D)),  # CJK
    _ranges((0xF900, 0xF90F)),  # CJK compatibility ideographs
    _ranges((0x0E01, 0x0E3A), (0x0E40, 0x0E4E), (0x0E50, 0x0E59)),  # Thai, with its marks
    _ranges((0xAC00, 0xAC3F), (0x1100, 0x1112), (0x1161, 0x1175), (0x3131, 0x318E)),  # Hangul
    _ranges((0x0621, 0x064A), (0x064B, 0x0652), (0x0660, 0x0669), (0xFE80, 0xFE8F)),  # Arabic
    _ranges((0xFF10, 0xFF19), (0xB2, 0xB3), (0x2460, 0x2473)),  # full-width, super, circled
    _ranges((0xA0, 0xA0), (0x3000, 0x3002), (0x300C, 0x300D), (0x2026, 0x2026)),  # spaces
    _ranges((0xE000, 0xE00F)),  # private use, which unicode61 counts as letters
]


def test_index_token_spans_agree_with_index_tokens_on_random_text() -> None:
    """Review Minor 6: index_token_spans is a second tokenizer, kept in step with
    index_tokens. Over 3,000 seeded random strings in NFKC, drawn from Latin, Latin with
    diacritics, CJK, Thai, Hangul, Arabic, digits, punctuation and private use, they give
    the same tokens, and each token's span holds it."""
    rng = random.Random(20261021)  # noqa: S311 - test data, not cryptography
    for number in range(3000):
        pools = _ASCII_POOLS if number % 5 == 0 else _POOLS
        raw = "".join(rng.choice(rng.choice(pools)) for _ in range(rng.randint(0, 40)))
        text = unicodedata.normalize("NFKC", raw)
        spans = list(index_token_spans(text))
        assert [token for token, _, _ in spans] == index_tokens(text), ascii(text)
        for token, start, end in spans:
            assert 0 <= start < end <= len(text), ascii(text)
            assert token in index_tokens(text[start:end]), ascii(text)
        assert [start for _, start, _ in spans] == sorted(start for _, start, _ in spans)


def test_fold_latin_and_is_segmented() -> None:
    assert fold_latin(CA_PHE) == "ca phe"
    assert fold_latin("plain ASCII") == "plain ASCII"
    for ch in (EAST, chr(0x0E01), chr(0x3042), chr(0x30A2), chr(0xFF71), chr(0x20000)):
        assert is_segmented(ch)
    for ch in ("a", "1", chr(0xE0), chr(0x0643), chr(0xAC00)):  # Hangul spaces its words
        assert not is_segmented(ch)


# ── what search.py exports ──


def test_search_exports_the_plans_interfaces_and_what_the_store_uses() -> None:
    """Review Minor 5: each name search.py exports is a contract a port or a fixture may
    pin, so it exports the plan's interfaces and what store.py and payload.py take from it,
    and nothing else."""
    used = {"build_fts_query", "query_terms", "rrf", "bm25_scan", "excerpt"}
    used |= {"PRESELECT_FLOOR", "PRESELECT_MIN_TERMS"}
    for module in (store_module, payload_module):
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == "search":
                used |= {alias.name for alias in node.names}
    assert sorted(search_module.__all__) == sorted(used)


# ── speed ──


def _vocabulary(rng: random.Random, size: int) -> List[str]:
    """``size`` made-up words of two to four syllables, in a random order."""
    syllables = [consonant + vowel for consonant in "bdfgklmnprstvz" for vowel in "aeiou"]
    words: Set[str] = set()
    while len(words) < size:
        words.add("".join(rng.choice(syllables) for _ in range(rng.randint(2, 4))))
    ordered = sorted(words)
    rng.shuffle(ordered)
    return ordered


@pytest.mark.skipif(bool(os.environ.get("T2V_CI_SLOW")), reason="T2V_CI_SLOW: a slow runner")
def test_search_is_fast_enough(tmp_path: Path, backend: str) -> None:
    """The plan's bound, informational: over 5,000 rounds and 2,000 facts of Zipf-distributed
    words, a search's median time is under 50 ms with FTS5 and under 500 ms on the scan
    backend. A slow runner sets T2V_CI_SLOW to skip it."""
    rng = random.Random(20261021)  # noqa: S311 - test data, not cryptography
    vocabulary = _vocabulary(rng, 3000)
    weights = list(itertools.accumulate(1 / rank for rank in range(1, len(vocabulary) + 1)))

    def sentence(low: int, high: int) -> str:
        count = rng.randint(low, high)
        return " ".join(rng.choices(vocabulary, cum_weights=weights, k=count))

    memory = MemoryStore(tmp_path / "u1.sqlite3", backend=backend, clock=FakeClock())  # type: ignore[arg-type]
    try:
        with memory._write():  # one transaction: seconds to build, not minutes
            for number in range(5000):
                at = T0 - timedelta(minutes=number)
                memory.record_round(
                    "c{}".format(number // 10), sentence(8, 40), sentence(20, 80), at=at
                )
            facts = [{"text": sentence(6, 30), "topic": "ongoing"} for _ in range(2000)]
            memory.add_facts(facts, conversation_id=None, source="said")
        queries = [" ".join(rng.sample(vocabulary[10:500], 3)) for _ in range(21)]
        memory.search(queries[0])  # the scan backend fills its cache
        timings = []
        for query in queries[1:]:
            started = time.perf_counter()
            memory.search(query)
            timings.append(time.perf_counter() - started)
    finally:
        memory.close()
    assert statistics.median(timings) < (0.5 if backend == "scan" else 0.05)
