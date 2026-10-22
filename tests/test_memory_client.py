"""T2VMemory and Talk2View(memory_path=...) (spec §7.4, §10.2): the turn's memory field, the
memory event, search_memory answered from the store, the round log, and the extraction and
consolidation the device asks the engine for.

``MemoryCallsEngine`` is test_memory_wire's ``MemoryEngine`` plus the engine's two memory
routes, which keep each request body and answer with canned results. Most tests drive a
``T2VMemory(..., inline=True)`` on a simulated clock, so its engine calls run in order in
the test's thread; those through ``Talk2View`` get the background worker it builds.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sqlite3
import stat
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import ChatController, Talk2View, _memory_file
from talk2view.auth import T2VAuth
from talk2view.cancel import CancelToken
from talk2view.client import T2VClient
from talk2view.memory import AppliedBatch, MemoryStore, T2VMemory, bodies, days
from talk2view.memory import client as memory_client
from talk2view.memory.worker import Cooldown, Worker
from talk2view.storage import MemoryStorage
from talk2view.types import Capabilities, ChatEvent, Decision

from .fake_engine import _STOP, _error, _json, _text
from .test_memory_store import T0, FakeClock
from .test_memory_wire import DONE, OPS, MemoryEngine, _approval, _interrupt, _memory_chunk

# The engine's extract answer when a test queues none.
EXTRACTED: Dict[str, Any] = {
    "summary": {"title": "Weekend plans", "text": "Talked about plans for the weekend."},
    "facts": [
        {
            "text": "Plans a hike on Saturday.",
            "topic": "ongoing",
            "occurred_at": "2026-10-24",
            "supersedes": [],
            "source": "said",
        }
    ],
    "entry_ops": [{"op": "add", "topic": "ongoing", "text": "Hiking on Saturday"}],
    "model": "m",
}

# An extract answer about a peanut allergy: all of it is what a forget of "peanut" removes.
PEANUT_ANSWER: Dict[str, Any] = {
    "summary": {"title": "Peanut allergy", "text": "The user is allergic to peanuts."},
    "facts": [
        {"text": "Is allergic to peanuts.", "topic": "identity", "supersedes": [], "source": "said"}
    ],
    "entry_ops": [{"op": "add", "topic": "identity", "text": "Allergic to peanuts"}],
    "model": "m",
}

NO_RESULTS = '{"results":[]}'


def _echoed(request: Dict[str, Any]) -> Dict[str, Any]:
    """A consolidate answer that keeps every entry the request carried, as it was."""
    entries = [
        {
            "id": entry["id"],
            "topic": entry["topic"],
            "text": entry["text"],
            "valid_until": entry.get("valid_until"),
            "locked": entry["locked"],
        }
        for entry in request["entries"]
    ]
    return {"entries": entries, "merge_facts": [], "expire_facts": [], "model": "m"}


class MemoryCallsEngine(MemoryEngine):
    """``MemoryEngine`` plus POST /v1/memory/extract and /v1/memory/consolidate. Each call
    is kept in ``calls`` as ``(kind, body)`` and answers the next reply queued for its kind
    in ``replies``: a ``(status, type[, message])`` error, a float (seconds to wait before
    the default answer), a callable (the answer for the request body), a ``Response`` or
    the answer itself.
    With none queued, extract answers EXTRACTED and consolidate keeps every entry.
    ``during``, when set, runs inside each call, before it answers."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.replies: Dict[str, List[Any]] = {"extract": [], "consolidate": []}
        self.during: Optional[Callable[[str], None]] = None

    def _handle(self, request: Request) -> Response:
        match = re.fullmatch(r"/v1/memory/(extract|consolidate)", request.path)
        if match is None:
            return super()._handle(request)
        kind = match.group(1)
        auth = request.headers.get("Authorization", "")
        with self.lock:
            if auth[len("Bearer ") :] not in self.tokens:
                return _error(401, "authentication_error", "Invalid token")
            body = json.loads(request.get_data())
            self.calls.append((kind, body))
            reply = self.replies[kind].pop(0) if self.replies[kind] else None
            during = self.during
        if during is not None:
            during(kind)
        if isinstance(reply, float):
            time.sleep(reply)
            reply = None
        if callable(reply):
            reply = reply(body)
        if isinstance(reply, Response):
            return reply
        if isinstance(reply, tuple):
            return _error(*reply)
        if reply is None:
            reply = EXTRACTED if kind == "extract" else _echoed(body)
        return _json(reply)

    def bodies_of(self, kind: str) -> List[Dict[str, Any]]:
        with self.lock:
            return [body for called, body in self.calls if called == kind]

    def kinds(self) -> List[str]:
        with self.lock:
            return [kind for kind, _ in self.calls]


@pytest.fixture
def engine(fake_engine_server: HTTPServer) -> Iterator[MemoryCallsEngine]:
    fake = MemoryCallsEngine(fake_engine_server).start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture(autouse=True)
def _system_zone_is_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The system's zone, as days reads it, is UTC here on any platform."""
    monkeypatch.setattr(days, "_system_zone", lambda: timezone.utc)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(engine: MemoryCallsEngine) -> Iterator[T2VClient]:
    """A client signed in as ann (user-ann)."""
    t2v_client = T2VClient("pk_test", MemoryStorage(), base_url=engine.url())
    T2VAuth(t2v_client).login("ann@example.com", "pw")
    try:
        yield t2v_client
    finally:
        t2v_client.close()


@pytest.fixture
def memory(client: T2VClient, tmp_path: Path, clock: FakeClock) -> Iterator[T2VMemory]:
    """ann's memory, inline, on the simulated clock."""
    store = MemoryStore(tmp_path / "user-ann.sqlite3", clock=clock)
    inline = T2VMemory(client, store, clock=clock, inline=True)
    try:
        yield inline
    finally:
        inline.close()


def _talk2view(engine: MemoryCallsEngine, tmp_path: Path, **kwargs: Any) -> Talk2View:
    """A Talk2View with memory, signed in as ann."""
    t2v = Talk2View("pk_test", base_url=engine.url(), memory_path=tmp_path / "memory", **kwargs)
    t2v.auth.login("ann@example.com", "pw")
    return t2v


def _memory_of(t2v: Talk2View) -> T2VMemory:
    memory = t2v.memory
    assert memory is not None
    return memory


def add(text: str, topic: str = "preferences") -> Dict[str, Any]:
    return {"op": "add", "topic": topic, "text": text}


def _texts(memory: T2VMemory) -> List[str]:
    return [entry.text for entry in memory.store.entries()]


def _rounds(memory: T2VMemory, conversation_id: Optional[str] = None) -> List[Tuple[str, str]]:
    """The conversation's rounds (the current one by default), as (user, assistant)."""
    rounds = memory.store.rounds(conversation_id or memory.conversation_id)
    return [(user_text, reply) for _, user_text, reply, _ in rounds]


def _sent(engine: MemoryCallsEngine, *suffixes: str) -> List[Dict[str, Any]]:
    return [json.loads(raw) for raw in engine.bodies(*suffixes)]


def _logged(caplog: pytest.LogCaptureFixture) -> List[Tuple[int, str]]:
    """The SDK's log lines (the fake server logs too), as (level, message)."""
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name.startswith("talk2view")
    ]


def _eventually(check: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def _memory_event(*ops: Dict[str, Any]) -> Dict[str, Any]:
    """The engine's memory chunk for ``ops``."""
    return {
        "id": "c",
        "choices": [{"index": 0, "delta": {}}],
        "memory": {"ops": list(ops), "tool_call_id": "c1"},
    }


# ── Talk2View(memory_path=...) ──


def test_memory_path_builds_a_per_user_store(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """D22: one file per user id under memory_path, a guest's included; a switch closes the
    previous user's memory at once, and the client's close closes the last."""
    root = tmp_path / "memory"
    with Talk2View("pk_test", base_url=engine.url(), memory_path=root) as t2v:
        assert t2v.memory is None  # nobody signed in
        t2v.auth.login("u1@example.com", "pw")
        first = _memory_of(t2v)
        assert first.store.path == root / "user-u1.sqlite3"
        assert t2v.memory is first
        first.store.record_round(first.conversation_id, "I moved to Perth", "Noted.")

        t2v.auth.login("u2@example.com", "pw")  # a switch, no sign-out between
        assert first.enabled is False  # closed by the switch, before any use
        second = _memory_of(t2v)
        assert second.store.path == root / "user-u2.sqlite3"
        assert second.store.unextracted() == []  # u1's round stays in u1's file

        t2v.auth.logout()
        assert second.enabled is False
        assert t2v.memory is None

        t2v.auth.start_anonymous()
        guest = _memory_of(t2v)
        assert guest.store.path == root / "anon-1.sqlite3"
    assert guest.enabled is False
    assert t2v.memory is None  # closed
    files = sorted(path.name for path in root.glob("*.sqlite3"))
    assert files == ["anon-1.sqlite3", "user-u1.sqlite3", "user-u2.sqlite3"]
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        for path in root.glob("*.sqlite3"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _memory_file("u1") == "u1.sqlite3"
    assert re.fullmatch(r"[0-9a-f]{32}\.sqlite3", _memory_file("../not a file name"))


def test_ids_that_differ_only_in_case_get_files_of_their_own() -> None:
    """Where the filesystem ignores case (macOS and Windows by default), User-A and user-a
    would share a file: only a lowercase id names its file as it is. The engine's ids, a
    lowercase UUID or anon-<n>, keep their names."""
    upper, lower = _memory_file("User-A"), _memory_file("user-a")

    assert lower == "user-a.sqlite3"
    assert re.fullmatch(r"[0-9a-f]{32}\.sqlite3", upper)
    assert upper.lower() != lower.lower()
    for engine_id in ("anon-42", "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"):
        assert _memory_file(engine_id) == engine_id + ".sqlite3"


def test_capabilities_gain_memory_flags(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """memory_path adds both flags to the caller's capabilities, keeps them across
    set_chat_defaults (a ChatController's included) and handles search_memory. Without it
    nothing changes: no flag, no memory field, no handler."""
    caps = Capabilities(reasoning=True, timezone="Australia/Perth")
    with _talk2view(engine, tmp_path, capabilities=caps) as t2v:
        assert t2v.create_session()._capability_fields(resume=False) == {
            "supports_reasoning": True,
            "supports_memory": True,
            "supports_memory_search": True,
            "timezone": "Australia/Perth",
        }
        t2v.set_chat_defaults(capabilities=Capabilities(tool_events=True))
        assert t2v.create_session()._capability_fields(resume=True) == {
            "supports_tool_events": True,
            "supports_memory": True,
            "supports_memory_search": True,
        }
        controller = ChatController(t2v, capabilities=Capabilities(), executor=lambda fn: fn())
        controller.close()
        assert t2v.create_session()._capability_fields(resume=False) == {
            "supports_memory": True,
            "supports_memory_search": True,
        }
        assert t2v.tools.has_handler("search_memory")

    with Talk2View("pk_test", base_url=engine.url(), capabilities=caps) as plain:
        plain.auth.login("ann@example.com", "pw")
        assert plain.memory is None
        assert not plain.tools.has_handler("search_memory")
        assert list(plain.chat("hi"))[-1].type == "done"
    (body,) = _sent(engine, "/messages")
    assert body == {
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "supports_reasoning": True,
        "timezone": "Australia/Perth",
    }


def test_chat_sends_the_payload_and_records_the_round(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Prefers metric units")], source="said")
        expected = memory.turn_payload("Which flight did I book?")
        assert expected is not None and expected["entries"]
        engine.streams[:] = [[_text("You booked "), _text("JL 772."), _STOP, DONE]]

        events = list(t2v.chat("Which flight did I book?"))

        assert [event.type for event in events] == ["text", "text", "done"]
        (body,) = _sent(engine, "/messages")
        assert body["memory"] == expected
        assert (body["supports_memory"], body["supports_memory_search"]) == (True, True)
        assert _rounds(memory) == [("Which flight did I book?", "You booked JL 772.")]


def test_incognito_sends_status_and_records_nothing(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    """Only {"status": "incognito"}: no round, no memory event applied, nothing found."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Lives in Perth", "identity")], source="said")
        memory.store.record_round("older", "I moved to Perth in October", "Noted.")
        memory.store.mark_extracted("older", 1)  # no background extraction to race
        memory.new_conversation(incognito=True)
        assert memory.incognito is True
        engine.streams[:] = [[_text("Noted. "), _memory_chunk("c1"), _STOP, DONE]]

        events = list(t2v.chat("Remember that I prefer metric units"))

        assert [event.type for event in events] == ["text", "memory", "done"]
        (body,) = _sent(engine, "/messages")
        assert body["memory"] == {"status": "incognito"}
        assert _rounds(memory) == []
        assert _texts(memory) == ["Lives in Perth"] and memory.last_applied is None
        assert memory.handle_search({"query": "Perth"}) == NO_RESULTS
        assert memory.new_conversation() and memory.incognito is False
        assert "Perth" in memory.handle_search({"query": "Perth"})


def test_paused_sends_status_paused(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """Only {"status": "paused"}: no round, no memory event applied, nothing found, and no
    engine memory call."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Lives in Perth", "identity")], source="said")
        memory.store.record_round("older", "I moved to Perth in October", "Noted.")
        memory.store.mark_extracted("older", 1)
        memory.store.paused = True
        engine.streams[:] = [[_text("Noted. "), _memory_chunk("c1"), _STOP, DONE]]

        list(t2v.chat("Remember that I prefer metric units"))

        (body,) = _sent(engine, "/messages")
        assert body["memory"] == {"status": "paused"}
        assert _rounds(memory) == []
        assert _texts(memory) == ["Lives in Perth"] and memory.last_applied is None
        assert memory.handle_search({"query": "Perth"}) == NO_RESULTS
        assert memory.run_due() == []
        assert memory.wait_idle(5)
        assert engine.calls == []


def test_memory_event_is_applied_and_batch_returned(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        engine.streams[:] = [[_text("Noted. "), _memory_chunk("c1"), _STOP, DONE]]

        events = list(t2v.chat("Remember that I prefer metric units"))

        assert events == [
            ChatEvent(type="text", content="Noted. "),
            ChatEvent(type="memory", tool_call_id="c1", memory_ops=OPS),
            ChatEvent(type="done", thread_id="th"),
        ]
        assert [(entry.text, entry.source) for entry in memory.store.entries()] == [
            ("Prefers metric units", "said")
        ]
        assert _rounds(memory) == [("Remember that I prefer metric units", "Noted. ")]

        batch = memory.on_event(
            ChatEvent(type="memory", tool_call_id="c2", memory_ops=[add("Works at Canva")])
        )
        assert batch is not None
        assert ([record["op"] for record in batch.applied], batch.dropped) == (["add"], [])
        assert _texts(memory) == ["Prefers metric units", "Works at Canva"]
        assert memory.on_event(ChatEvent(type="text", content="Works at Canva")) is None


def test_a_memory_event_with_no_ops_is_ignored(
    memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R62: no apply_ops([]), so no empty undo batch; the turn's round is still kept."""
    applied: List[Any] = []
    monkeypatch.setattr(memory.store, "apply_ops", lambda ops, **kwargs: applied.append(ops))
    empty = ChatEvent(type="memory", tool_call_id="c1", memory_ops=[])
    assert memory.on_event(empty) is None
    assert memory.on_event(ChatEvent(type="memory", tool_call_id="c1")) is None
    events = [ChatEvent(type="text", content="Noted."), empty, ChatEvent(type="done")]

    assert list(memory.observe_turn("Remember nothing", iter(events))) == events

    assert applied == []
    assert _rounds(memory) == [("Remember nothing", "Noted.")]


def test_a_turn_paused_midway_records_nothing(memory: T2VMemory) -> None:
    """Memory paused while the reply streams: the round isn't kept. A conversation the
    store marks incognito, whoever marked it, sends only its status."""

    def turn() -> Iterator[ChatEvent]:
        yield ChatEvent(type="text", content="Noted.")
        memory.store.paused = True
        yield ChatEvent(type="done")

    events = [event.type for event in memory.observe_turn("I moved to Perth", turn())]

    assert events == ["text", "done"]
    assert _rounds(memory) == []
    memory.store.paused = False
    memory.store.set_incognito(memory.conversation_id, True)
    assert memory.turn_payload("hi") == {"status": "incognito"}


# ── what a memory event applied, and its undo (R85) ──


def test_last_applied_holds_the_events_batch_and_undo_takes_it_back(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    """R85 (spec §10.1: "Memory updated", with an undo): a host that gets a memory event from
    chat() finds the batch it applied in last_applied already, and undo takes it back
    within the window, once. Each turn starts with None, so one with no memory event
    leaves it None."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        assert memory.last_applied is None
        engine.streams[:] = [[_text("Noted. "), _memory_event(add("Works at Canva")), _STOP, DONE]]
        seen: List[Optional[AppliedBatch]] = []

        for event in t2v.chat("I started at Canva"):
            if event.type == "memory":
                seen.append(memory.last_applied)

        assert len(seen) == 1
        batch = seen[0]
        assert batch is not None and memory.last_applied is batch
        assert [record["op"] for record in batch.applied] == ["add"]
        assert _texts(memory) == ["Works at Canva"]

        assert memory.undo(batch.id) is True

        assert _texts(memory) == []
        assert memory.undo(batch.id) is False  # undone already
        engine.streams[:] = [[_text("Hello."), _STOP, DONE]]
        assert list(t2v.chat("Hi"))[-1].type == "done"
        assert memory.last_applied is None


def test_undo_is_false_and_never_raises(
    memory: T2VMemory,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R85: undo is False, never raising, for a batch past the 30 s window or unknown, when
    the store fails (one WARNING naming the error's class, never its message), and once
    closed, when it never reaches the store, so nothing is logged."""
    turn = [
        ChatEvent(type="memory", tool_call_id="c1", memory_ops=[add("Works at Canva")]),
        ChatEvent(type="done"),
    ]
    assert list(memory.observe_turn("I started at Canva", iter(turn))) == turn
    old = memory.last_applied
    assert old is not None
    clock.now = T0 + timedelta(seconds=31)
    batch = memory.store.apply_ops([add("Lives in Perth")], source="said")

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("near 'Perth': peanut")

    with caplog.at_level(logging.DEBUG, logger="talk2view"):
        assert memory.undo(old.id) is False  # past the window
        assert memory.undo("b-unknown") is False
        with monkeypatch.context() as patch:
            patch.setattr(memory.store, "undo", failing)
            assert memory.undo(batch.id) is False
        assert _texts(memory) == ["Works at Canva", "Lives in Perth"]
        memory.close()
        assert memory.undo(batch.id) is False

    assert _logged(caplog) == [
        (logging.WARNING, "Memory: undoing a memory change failed (OperationalError)")
    ]


def test_an_approval_with_no_paused_turn_shows_no_earlier_batch(memory: T2VMemory) -> None:
    """The rest of a paused turn keeps the batch its first half applied, but an approval
    answered with no paused turn to go on with (another turn ran since, say) starts
    last_applied from None, as a turn does: an earlier turn's batch never shows."""
    paused = [
        ChatEvent(type="memory", tool_call_id="c1", memory_ops=[add("Works at Canva")]),
        ChatEvent(type="approval_required", tool_call_id="ap1"),
    ]
    rest = [ChatEvent(type="text", content="Sent."), ChatEvent(type="done")]
    list(memory.observe_turn("Email Alex that I work at Canva", iter(paused)))
    batch = memory.last_applied
    assert batch is not None
    assert [memory.last_applied for _ in memory.observe_approval(iter(rest))] == [batch, batch]

    other = [
        ChatEvent(type="memory", tool_call_id="c2", memory_ops=[add("Lives in Perth")]),
        ChatEvent(type="done"),
    ]
    list(memory.observe_turn("I live in Perth", iter(other)))
    assert memory.last_applied is not None

    assert [memory.last_applied for _ in memory.observe_approval(iter(rest))] == [None, None]


def test_last_applied_stays_none_when_every_op_was_dropped(memory: T2VMemory) -> None:
    """R87: a memory event whose ops were all dropped (a duplicate add) changed nothing, so
    last_applied is None and no host shows "Memory updated"; on_event still returns the
    batch, with the rule that dropped them. Such an event replaces an earlier event's
    batch with None, as every memory event replaces the one before."""
    memory.store.apply_ops([add("Works at Canva")], source="said")
    duplicate = ChatEvent(type="memory", tool_call_id="c1", memory_ops=[add("Works at Canva")])

    batch = memory.on_event(duplicate)

    assert batch is not None and (batch.applied, batch.dropped) == ([], ["duplicate"])
    turn = [
        ChatEvent(type="memory", tool_call_id="c2", memory_ops=[add("Lives in Perth")]),
        duplicate,
        ChatEvent(type="done"),
    ]
    seen = [
        memory.last_applied
        for event in memory.observe_turn("I live in Perth and work at Canva", iter(turn))
        if event.type == "memory"
    ]
    assert seen[0] is not None and [record["op"] for record in seen[0].applied] == ["add"]
    assert seen[1:] == [None] and memory.last_applied is None
    assert _texts(memory) == ["Works at Canva", "Lives in Perth"]


# ── search_memory ──


def test_search_memory_interrupt_is_answered_from_the_store(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    """The handler answers with the store's search_result_json; the engine's nulls (A7)
    count as left out; the resume carries the result and the turn's memory field."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.record_round("older", "I booked flight JL 772 to Tokyo", "Noted.")
        memory.store.mark_extracted("older", 1)  # no background extraction to race
        args = {"query": "Tokyo flight", "from_date": None, "to_date": None, "limit": None}
        engine.streams[:] = [
            [_interrupt("search_memory", "sm1", args)],
            [_text("JL 772."), _STOP, DONE],
        ]

        events = list(t2v.chat("Which flight did I book?"))

        assert [event.type for event in events] == ["tool_call", "text", "done"]
        expected = memory.store.search_result_json(
            "Tokyo flight", exclude_conversation=memory.conversation_id
        )
        assert "JL 772" in expected
        (message,) = _sent(engine, "/messages")
        (resume,) = _sent(engine, "/resume")
        assert (resume["result"], resume["is_error"]) == (expected, False)
        assert resume["memory"] == message["memory"]


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({}, "query must be text"),
        ({"query": None}, "query must be text"),
        ({"query": 42}, "query must be text"),
        ({"query": "a"}, "query must be at least 2 characters"),
        ({"query": ""}, "query must be at least 2 characters"),
        ({"query": "Perth", "from_date": "yesterday"}, "from_date must be a YYYY-MM-DD date"),
        ({"query": "Perth", "to_date": 20261021}, "to_date must be a YYYY-MM-DD date"),
        ({"query": "Perth", "limit": "8"}, "limit must be a whole number"),
        ({"query": "Perth", "limit": True}, "limit must be a whole number"),
        ({"query": "Perth", "limit": 2.5}, "limit must be a whole number"),
        (["Perth"], "the arguments must be an object"),
    ],
)
def test_handle_search_refuses_malformed_arguments(
    memory: T2VMemory, args: Any, problem: str
) -> None:
    """R40: a short error result, never an exception, that never quotes memory."""
    memory.store.record_round("older", "I moved to Perth", "Noted.")
    assert json.loads(memory.handle_search(args)) == {"error": problem}


def test_handle_search_reads_nulls_as_absent_and_keeps_limits(
    memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A7/R40: None is left out (limit 8); limit kept within 1-20; the query cut to 200 code
    points; the current conversation and the timezone go to the store."""
    calls: List[Tuple[str, Dict[str, Any]]] = []
    real = memory.store.search_result_json

    def spy(query: str, **kwargs: Any) -> str:
        calls.append((query, kwargs))
        return real(query, **kwargs)

    monkeypatch.setattr(memory.store, "search_result_json", spy)
    memory.timezone = "Australia/Perth"
    long_query = chr(0x1F600) * 250
    dates = {"from_date": "2026-10-01", "to_date": "2026-10-21"}

    memory.handle_search({"query": long_query, "limit": 0})
    memory.handle_search({"query": "Perth", "limit": 99, "from_date": None, "to_date": None})
    memory.handle_search({"query": "Perth", "limit": None})
    memory.handle_search({"query": "Perth"})
    memory.handle_search(dict(dates, query="Perth", limit=5))

    assert [(len(query), kwargs["limit"]) for query, kwargs in calls] == [
        (200, 1),
        (5, 20),
        (5, 8),
        (5, 8),
        (5, 5),
    ]
    assert calls[0][0] == chr(0x1F600) * 200
    assert [(kwargs["from_date"], kwargs["to_date"]) for _, kwargs in calls] == [
        (None, None)
    ] * 4 + [("2026-10-01", "2026-10-21")]
    for _, kwargs in calls:
        assert kwargs["exclude_conversation"] == memory.conversation_id
        assert kwargs["timezone"] == "Australia/Perth"


def test_handle_search_leaves_out_the_current_conversation(memory: T2VMemory) -> None:
    memory.store.record_round(memory.conversation_id, "My locker code is 4417", "Noted.")
    memory.store.record_round("older", "My bike lock code is 9921", "Noted.")

    found = memory.handle_search({"query": "code"})

    assert "9921" in found
    assert "4417" not in found


# ── extraction ──


def test_run_due_extracts_idle_conversations(
    engine: MemoryCallsEngine, memory: T2VMemory, clock: FakeClock
) -> None:
    """A conversation idle 10 minutes with 2 new rounds: one extract call with its new
    messages after up to 6 of context, the facts a search for its new lines finds, the
    entries, and the timezone; the result applied and the extracted point moved. run_due
    purges too."""
    store = memory.store
    assert memory.run_due() == []  # launch: nothing yet
    store.apply_ops([add("Lives in Perth", "identity"), add("Owns a bike")], source="said")
    store.add_facts(
        [
            {"text": "Is planning a trip to Tokyo.", "topic": "ongoing"},
            {"text": "Has a sister named Mia.", "topic": "people"},
            {"text": "Likes green tea.", "topic": "preferences"},
        ],
        conversation_id="c0",
        source="said",
    )
    for minutes, user_text, reply in (
        (40, "I'm planning a trip to Tokyo", "Nice!"),
        (39, "We fly on 3 November", "Noted."),
        (2, "Book a hotel near Shinjuku for the Tokyo trip", "Here are three options."),
        (1, "My sister Mia is coming too", "Great."),
    ):
        store.record_round("c1", user_text, reply, at=T0 - timedelta(minutes=minutes))
    store.mark_extracted("c1", 2)
    memory.timezone = "Australia/Perth"

    clock.now = T0 + timedelta(minutes=8)  # idle 9 minutes
    assert memory.run_due() == []
    store.apply_ops([{"op": "remove", "id": "e2"}], source="said")  # hidden for the undo window
    assert "e2" in [entry.id for entry in store.entries(include_hidden=True)]
    clock.now = T0 + timedelta(minutes=9)  # idle 10 minutes, and e2 hidden for 60 s
    assert memory.run_due() == ["extract:c1"]

    assert "e2" not in [entry.id for entry in store.entries(include_hidden=True)]  # purged
    (body,) = engine.bodies_of("extract")
    conversation = body["conversation"]
    assert conversation["id"] == "c1"
    assert (conversation["started_at"], conversation["last_active_at"]) == (
        "2026-10-21T09:20:00Z",
        "2026-10-21T09:59:00Z",
    )
    assert "title" not in conversation
    assert [(m["role"], m["text"]) for m in conversation["messages"]] == [
        ("user", "I'm planning a trip to Tokyo"),
        ("assistant", "Nice!"),
        ("user", "We fly on 3 November"),
        ("assistant", "Noted."),
        ("user", "Book a hotel near Shinjuku for the Tokyo trip"),
        ("assistant", "Here are three options."),
        ("user", "My sister Mia is coming too"),
        ("assistant", "Great."),
    ]
    assert conversation["messages"][4]["at"] == "2026-10-21T09:58:00Z"
    assert body["extracted_through"] == 4
    assert sorted(fact["text"] for fact in body["related_facts"]) == [
        "Has a sister named Mia.",
        "Is planning a trip to Tokyo.",
    ]
    assert set(body["related_facts"][0]) == {"id", "text", "topic", "recorded_at"}
    assert [entry["text"] for entry in body["entries"]] == ["Lives in Perth"]
    assert (body["mode"], body["timezone"]) == ("conversation", "Australia/Perth")
    assert "include_sensitive" not in body

    record = store.conversation("c1")
    assert record is not None
    assert record.extracted_through == 4
    assert (record.summary_title, record.summary_text) == (
        "Weekend plans",
        "Talked about plans for the weekend.",
    )
    (hike,) = [fact for fact in store.facts() if fact.text == "Plans a hike on Saturday."]
    assert (hike.conversation_id, hike.source, hike.occurred_at) == ("c1", "said", "2026-10-24")
    assert [(entry.text, entry.source) for entry in store.entries()] == [
        ("Lives in Perth", "said"),
        ("Hiking on Saturday", "inferred"),
    ]
    assert memory.run_due() == []  # nothing new


def test_extracted_through_counts_the_context_messages(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """A9: the wire's extracted_through counts the context messages in the request (up to
    6, a round giving one message when its reply is empty), so no new message is read as
    context and no context message as new; the store's mark counts rounds."""
    store = memory.store
    for n in range(1, 6):
        store.record_round(
            "c1", "User message {}.".format(n), "" if n == 4 else "Reply {}.".format(n)
        )
    store.mark_extracted("c1", 5)
    store.record_round("c1", "User message 6.", "")
    store.record_round("c1", "User message 7.", "Reply 7.")

    assert memory.extract("c1") is True
    store.record_round("c1", "User message 8.", "Reply 8.")
    assert memory.extract("c1") is True

    first, second = engine.bodies_of("extract")
    assert [m["text"] for m in first["conversation"]["messages"]] == [
        "Reply 2.",
        "User message 3.",
        "Reply 3.",
        "User message 4.",
        "User message 5.",
        "Reply 5.",
        "User message 6.",
        "User message 7.",
        "Reply 7.",
    ]
    assert first["extracted_through"] == 6
    assert [m["text"] for m in second["conversation"]["messages"]] == [
        "User message 4.",
        "User message 5.",
        "Reply 5.",
        "User message 6.",
        "User message 7.",
        "Reply 7.",
        "User message 8.",
        "Reply 8.",
    ]
    assert second["extracted_through"] == 6
    record = store.conversation("c1")
    assert record is not None and record.extracted_through == 8


def test_extraction_keeps_within_the_engine_caps(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """Each message is cut to 20,000 code points and a request holds at most 200 messages
    and 200,000 code points: the rounds left out go in the next request."""
    store = memory.store
    for _ in range(6):
        store.record_round("long", "a" * 25_000, "b" * 25_000)
    for n in range(101):
        store.record_round("many", "Message {}.".format(n), "Reply {}.".format(n))

    assert memory.extract("long") is True
    assert memory.extract("long") is True
    assert memory.extract("many") is True

    first, second, many = engine.bodies_of("extract")
    assert [len(m["text"]) for m in first["conversation"]["messages"]] == [20_000] * 10
    assert (first["extracted_through"], second["extracted_through"]) == (0, 6)
    assert sum(len(m["text"]) for m in second["conversation"]["messages"]) == 160_000
    assert len(many["conversation"]["messages"]) == 200
    assert [_conversation_mark(store, cid) for cid in ("long", "many")] == [6, 100]


def test_extraction_relates_at_most_60_facts(engine: MemoryCallsEngine, memory: T2VMemory) -> None:
    """One search per line said: the facts each finds, in order, each once, at most 60."""
    store = memory.store
    kyoto = ["Saw Kyoto temple number {}.".format(n) for n in range(40)]
    osaka = ["Ate at Osaka market stall {}.".format(n) for n in range(40)]
    store.add_facts(
        [{"text": text, "topic": "ongoing", "valid_until": "2030-01-01"} for text in kyoto]
        + [{"text": text, "topic": "ongoing"} for text in osaka],
        conversation_id=None,
        source="said",
    )
    store.record_round("c1", "Tell me about Kyoto\nAnd Osaka\nAnd Kyoto again", "Sure.")

    assert memory.extract("c1") is True

    (body,) = engine.bodies_of("extract")
    related = body["related_facts"]
    assert len(related) == 60
    assert sorted(fact["text"] for fact in related[:40]) == sorted(kyoto)
    assert {fact["text"] for fact in related[40:]} < set(osaka)
    assert {fact.get("valid_until") for fact in related[:40]} == {"2030-01-01"}
    assert "valid_until" not in related[40]


def test_extract_sends_nothing_it_has_no_use_for(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """No call for an unknown or incognito conversation, while paused, or for new rounds
    with nothing the user said (those are marked read); an unknown mode raises."""
    store = memory.store
    store.record_round("replies", "", "A reply with nothing before it.")
    store.record_round("private", "I moved to Perth", "Noted.")
    store.set_incognito("private", True)
    store.record_round("c1", "I moved to Perth", "Noted.")

    assert memory.extract("unknown") is False
    assert memory.extract("private") is False
    assert memory.extract("replies") is True
    assert _conversation_mark(store, "replies") == 1
    store.paused = True
    assert memory.extract("c1") is False
    with pytest.raises(ValueError):
        memory.extract("c1", mode="summary")
    assert engine.calls == []


def test_a_result_arriving_after_a_pause_a_delete_or_a_close_is_dropped(
    engine: MemoryCallsEngine, memory: T2VMemory, caplog: pytest.LogCaptureFixture
) -> None:
    """Paused, or the chat deleted, while the call ran: nothing of the result is written,
    and the conversation stays to extract. Closed meanwhile: dropped without a log line,
    even when the call failed."""
    store = memory.store
    store.apply_ops([add("Likes tea")], source="said")
    store.record_round("c1", "I moved to Perth", "Noted.")

    def pause(kind: str) -> None:
        store.paused = True

    engine.during = pause
    assert memory.extract("c1") is False
    assert _conversation_mark(store, "c1") == 0
    assert store.facts() == [] and _texts(memory) == ["Likes tea"]
    store.paused = False
    engine.replies["consolidate"].append(
        {"entries": [], "merge_facts": [], "expire_facts": [], "model": "m"}
    )
    assert memory.consolidate() is False
    assert _texts(memory) == ["Likes tea"]
    store.paused = False

    def delete(kind: str) -> None:
        store.delete_conversation("c1")

    engine.during = delete
    assert memory.extract("c1") is False
    assert store.conversation("c1") is None and store.facts() == []

    store.record_round("c2", "I like tea", "Noted.")

    def close(kind: str) -> None:
        memory.close()

    engine.during = close
    engine.replies["extract"].append((503, "credit_check_unavailable"))
    with caplog.at_level(logging.DEBUG, logger="talk2view"):
        assert memory.extract("c2") is False
    assert _logged(caplog) == []


def _conversation_mark(store: MemoryStore, conversation_id: str) -> int:
    record = store.conversation(conversation_id)
    assert record is not None
    return record.extracted_through


def test_new_conversation_triggers_extraction_of_the_previous(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    first = memory.conversation_id
    memory.store.record_round(first, "I moved to Perth", "Noted.")

    second = memory.new_conversation()

    assert second != first and memory.conversation_id == second
    assert re.fullmatch(r"[0-9a-f]{32}", second)
    assert [body["conversation"]["id"] for body in engine.bodies_of("extract")] == [first]
    assert _conversation_mark(memory.store, first) == 1
    # An empty or incognito conversation has nothing to extract.
    memory.new_conversation(incognito=True)
    memory.store.record_round(memory.conversation_id, "I like tea", "Noted.")  # never written
    memory.new_conversation()
    assert len(engine.bodies_of("extract")) == 1


def test_a_new_incognito_conversation_is_marked_as_it_starts(
    memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """new_conversation(incognito=True) switches to the new conversation and marks it in one
    step: whoever takes the memory's lock next, a turn on another thread say, never finds
    the new conversation unmarked."""
    first = memory.conversation_id
    lock = memory._lock
    seen: List[Tuple[str, bool]] = []  # the state each taker of the lock finds

    class Watched:
        def __enter__(self) -> bool:
            seen.append((memory.conversation_id, memory.incognito))
            return lock.__enter__()

        def __exit__(self, *exc: Any) -> None:
            lock.__exit__(*exc)

    monkeypatch.setattr(memory, "_lock", Watched())
    second = memory.new_conversation(incognito=True)

    assert seen[0] == (first, False)
    assert [incognito for conversation, incognito in seen if conversation == second] == [True]
    monkeypatch.undo()
    assert memory.turn_payload("hi") == {"status": "incognito"}
    assert memory.store.record_round(second, "I like tea", "Noted.") == 0  # never kept


def test_run_due_at_launch_extracts_unextracted_conversations(
    engine: MemoryCallsEngine, client: T2VClient, tmp_path: Path, clock: FakeClock
) -> None:
    """Spec §7.4 "the app launches": the first run_due extracts every unextracted
    conversation, idle or not, least recently active first; later ones only the idle."""
    store = MemoryStore(tmp_path / "user-ann.sqlite3", clock=clock)
    store.record_round("c2", "I started a new job at Canva", "Congrats!", at=T0)
    store.record_round("c1", "I moved to Perth", "Noted.", at=T0 - timedelta(minutes=1))
    store.record_round("c3", "I like tea", "Noted.", at=T0)
    store.mark_extracted("c3", 1)
    memory = T2VMemory(client, store, clock=clock, inline=True)
    try:
        assert memory.run_due() == ["extract:c1", "extract:c2"]
        store.record_round("c2", "It starts on Monday", "Good luck!", at=T0)
        assert memory.run_due() == []  # not idle
        clock.now = T0 + timedelta(minutes=10)
        assert memory.run_due() == ["extract:c2"]
    finally:
        memory.close()
    assert [body["conversation"]["id"] for body in engine.bodies_of("extract")] == [
        "c1",
        "c2",
        "c2",
    ]


def test_one_extraction_per_conversation_in_flight(
    engine: MemoryCallsEngine, client: T2VClient, tmp_path: Path, clock: FakeClock
) -> None:
    """While a conversation's extraction is queued or running, run_due sends nothing more
    for it and an explicit extract() is refused."""
    hold = threading.Event()
    engine.during = lambda kind: hold.wait(5) and None
    store = MemoryStore(tmp_path / "user-ann.sqlite3", clock=clock)
    store.record_round("c1", "I moved to Perth", "Noted.")
    memory = T2VMemory(client, store, clock=clock)  # the background worker
    try:
        assert memory.run_due() == ["extract:c1"]
        assert _eventually(lambda: len(engine.calls) == 1)
        clock.now = T0 + timedelta(minutes=10)
        assert memory.run_due() == []
        assert memory.extract("c1") is False
        hold.set()
        assert memory.wait_idle(5)
        assert _conversation_mark(store, "c1") == 1
        assert memory.run_due() == []
    finally:
        hold.set()
        memory.close()
    assert len(engine.bodies_of("extract")) == 1


@pytest.mark.parametrize(
    ("failure", "logged"),
    [
        ((402, "memory_budget_low"), "HTTP 402 memory_budget_low"),
        ((403, "memory_requires_account"), "HTTP 403 memory_requires_account"),
        ((429, "rate_limit_exceeded"), "HTTP 429 rate_limit_exceeded"),
        ((503, "credit_check_unavailable"), "HTTP 503 credit_check_unavailable"),
        ((504, "timeout"), "HTTP 504 timeout"),
        (1.0, "network_error"),  # the client's own timeout
        ({"summary": None, "facts": "none", "entry_ops": []}, "malformed answer"),
        ([], "malformed answer"),
        (lambda body: Response("not JSON"), "JSONDecodeError"),
    ],
    ids=["402", "403", "429", "503", "504", "timeout", "malformed", "not-an-object", "not-json"],
)
def test_extract_failures_are_silent_and_retried_later(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure: Any,
    logged: str,
) -> None:
    """False and one WARNING with the status or kind only; nothing moves, and a later
    trigger tries again."""
    monkeypatch.setattr(memory_client, "CALL_TIMEOUT_S", 0.2)
    memory.store.record_round("c1", "I moved to Perth", "Noted.")
    engine.replies["extract"][:] = [failure, failure]

    with caplog.at_level(logging.DEBUG, logger="talk2view"):
        assert memory.run_due() == ["extract:c1"]
        assert memory.extract("c1") is False

    assert (
        _logged(caplog) == [(logging.WARNING, "Memory: extraction failed ({})".format(logged))] * 2
    )
    assert _conversation_mark(memory.store, "c1") == 0
    assert memory.store.facts() == [] and memory.store.entries() == []
    clock.now = T0 + timedelta(minutes=10)
    assert memory.run_due() == ["extract:c1"]
    assert _conversation_mark(memory.store, "c1") == 1
    assert memory.enabled is True


def test_memory_disabled_backs_off_for_the_process(
    engine: MemoryCallsEngine,
    client: T2VClient,
    memory: T2VMemory,
    tmp_path: Path,
    clock: FakeClock,
) -> None:
    """403 memory_disabled: the partner has memory off, so no engine memory call goes out
    through this client again, from this memory or another; the turn payload still works."""
    memory.store.record_round("c1", "I moved to Perth", "Noted.")
    engine.replies["extract"].append((403, "memory_disabled"))

    assert memory.run_due() == ["extract:c1"]

    assert memory.enabled is False
    clock.now = T0 + timedelta(minutes=10)
    assert memory.run_due() == []
    assert memory.extract("c1") is False
    assert memory.consolidate() is False
    assert memory.import_text("I like tea.") is False
    other = T2VMemory(client, MemoryStore(tmp_path / "other.sqlite3", clock=clock), inline=True)
    try:
        other.store.record_round("c9", "I like coffee", "Noted.")
        assert other.enabled is False
        assert other.run_due() == []
    finally:
        other.close()
    assert len(engine.calls) == 1
    payload = memory.turn_payload("hi")
    assert payload is not None and payload["status"] == "on"


def _rate_limited(retry_after: int) -> Response:
    """The engine's 429, with a Retry-After header."""
    return Response(
        json.dumps({"error": {"type": "rate_limit_exceeded", "message": "slow down"}}),
        status=429,
        headers={"Retry-After": str(retry_after)},
        content_type="application/json",
    )


def test_a_failed_call_cools_run_due_down(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R81: after a failed call, run_due starts no engine call for 60 s on the injected
    clock, though it still purges, and logs nothing more; a second failure in a row waits
    120 s; a success ends the cooldown, so the next failure waits 60 s again."""
    store = memory.store
    store.record_round("c1", "I moved to Perth", "Noted.", at=T0 - timedelta(hours=1))
    failure = (503, "credit_check_unavailable")
    engine.replies["extract"][:] = [failure, failure]

    with caplog.at_level(logging.INFO, logger="talk2view"):
        assert memory.run_due() == ["extract:c1"]  # fails at T0
        store.apply_ops([add("Likes tea")], source="said")
        store.apply_ops([{"op": "remove", "id": "e1"}], source="said")  # hidden at T0
        clock.now = T0 + timedelta(seconds=59)
        assert memory.run_due() == []
        assert store.entries(include_hidden=True) == []  # purged all the same
        clock.now = T0 + timedelta(seconds=60)
        assert memory.run_due() == ["extract:c1"]  # fails again: 120 s from here
        clock.now = T0 + timedelta(seconds=179)
        assert memory.run_due() == []
        clock.now = T0 + timedelta(seconds=180)
        assert memory.run_due() == ["extract:c1"]  # succeeds
        assert _conversation_mark(store, "c1") == 1

        store.record_round("c2", "I like tea", "Noted.", at=T0 - timedelta(hours=1))
        engine.replies["extract"].append(failure)
        assert memory.run_due() == ["extract:c2"]  # fails: 60 s, not 240 s
        clock.now = T0 + timedelta(seconds=239)
        assert memory.run_due() == []
        clock.now = T0 + timedelta(seconds=240)
        assert memory.run_due() == ["extract:c2"]

    assert engine.kinds() == ["extract"] * 5
    failed = (logging.WARNING, "Memory: extraction failed (HTTP 503 credit_check_unavailable)")
    assert _logged(caplog) == [failed] * 3


def test_the_cooldown_stays_within_an_hour_and_honours_retry_after(
    engine: MemoryCallsEngine, memory: T2VMemory, clock: FakeClock
) -> None:
    """R81: direct calls go out during the cooldown, and their failures lengthen it,
    doubling to at most 3600 s. A Retry-After makes it at least that long, still at most
    an hour. The cooldown starts at run_due's now when it is given."""
    store = memory.store
    store.record_round("c1", "I moved to Perth", "Noted.", at=T0 - timedelta(hours=1))
    engine.replies["extract"][:] = [(503, "credit_check_unavailable")] * 7

    for _ in range(7):  # 60, 120, 240, 480, 960 and 1920 s, then 3840 s held to 3600
        assert memory.extract("c1") is False
    assert len(engine.calls) == 7
    assert memory.run_due(now=T0 + timedelta(seconds=3599)) == []
    assert memory.run_due(now=T0 + timedelta(seconds=3600)) == ["extract:c1"]  # succeeds

    later = T0 + timedelta(hours=2)
    for conversation, retry_after, wait in (("c2", 300, 300), ("c3", 7200, 3600)):
        store.record_round(conversation, "I like tea", "Noted.", at=T0 - timedelta(hours=1))
        engine.replies["extract"].append(lambda body, seconds=retry_after: _rate_limited(seconds))
        assert memory.run_due(now=later) == ["extract:" + conversation]  # fails at later
        assert memory.run_due(now=later + timedelta(seconds=wait - 1)) == []
        later += timedelta(seconds=wait)
        assert memory.run_due(now=later) == ["extract:" + conversation]  # succeeds
    assert len(engine.calls) == 12


def _cools_for(cooldown: Cooldown, start: datetime, seconds: int) -> bool:
    """Whether the cooldown runs ``seconds`` from ``start``: still at its last second, and
    over at its end."""
    end = start + timedelta(seconds=seconds)
    return cooldown.cooling(end - timedelta(seconds=1)) and not cooldown.cooling(end)


def test_cooldown_doubles_to_an_hour_and_a_success_ends_it(clock: FakeClock) -> None:
    """R86: Cooldown alone, on a clock and with no engine. 60 s after the first failure in a
    row, twice the last wait after each further one, at most 3600 s; a failure during the
    cooldown restarts it from its own time; a success ends it, and the next failure waits
    60 s again."""
    cooldown = Cooldown()
    assert not cooldown.cooling(clock())
    for wait in (60, 120, 240, 480, 960, 1920, 3600, 3600):
        cooldown.failed(clock())
        assert _cools_for(cooldown, clock(), wait), wait
        clock.now += timedelta(seconds=wait)
    cooldown.recovered()
    assert not cooldown.cooling(clock())
    cooldown.failed(clock())
    assert _cools_for(cooldown, clock(), 60)
    clock.now += timedelta(seconds=10)
    cooldown.failed(clock())  # during the cooldown: 120 s from this failure
    assert _cools_for(cooldown, clock(), 120)


def test_cooldown_honours_retry_after(clock: FakeClock) -> None:
    """R86: a retry_after makes that failure's wait at least so long, still at most 3600 s,
    and never shortens it; the waits after it double from where they were."""
    cooldown = Cooldown()
    cases: List[Tuple[Optional[float], int]] = [
        (300, 300),  # the first failure: 60 s, raised to 300
        (None, 120),  # doubling from 60, not from 300
        (7200, 3600),  # held to an hour
        (10, 480),  # shorter than the doubled wait: no effect
        (0, 960),
    ]
    for retry_after, wait in cases:
        cooldown.failed(clock(), retry_after)
        assert _cools_for(cooldown, clock(), wait), (retry_after, wait)
        clock.now += timedelta(seconds=wait)


@pytest.mark.parametrize("inline", [True, False], ids=["inline", "background"])
def test_queued_jobs_check_the_cooldown_as_they_start(
    engine: MemoryCallsEngine,
    client: T2VClient,
    tmp_path: Path,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    inline: bool,
) -> None:
    """R86: each job run_due or new_conversation queued checks the cooldown when it starts.
    Three idle conversations and a due consolidation against a failing engine: the first
    extraction fails and starts the cooldown, and the jobs after it, the extraction
    new_conversation queues included, make no call. One call and a 60 s cooldown, not five
    calls and one doubled four times; all of it stays due, and goes once the cooldown has
    run."""
    store = MemoryStore(tmp_path / "user-ann.sqlite3", clock=clock)
    for hours, conversation in ((3, "c1"), (2, "c2"), (1, "c3")):
        store.record_round(conversation, "I like tea", "Noted.", at=T0 - timedelta(hours=hours))
    store.apply_ops([add("Capped entry {}.".format(n)) for n in range(41)], source="said")
    assert len(store.entries()) == 41  # over ENTRY_MAX: consolidation is due
    failure = (503, "credit_check_unavailable")
    engine.replies["extract"][:] = [failure] * 4
    engine.replies["consolidate"][:] = [failure]
    memory = T2VMemory(client, store, clock=clock, inline=inline)
    conversations = ["c1", "c2", "c3", memory.conversation_id]
    try:
        with caplog.at_level(logging.INFO, logger="talk2view"):
            assert memory.run_due() == ["extract:c1", "extract:c2", "extract:c3", "consolidate"]
            store.record_round(
                conversations[3], "I like coffee", "Noted.", at=T0 - timedelta(minutes=30)
            )
            memory.new_conversation()  # queues the extraction of the one that ended
            assert memory.wait_idle(5)
            assert engine.kinds() == ["extract"]
            assert [_conversation_mark(store, c) for c in conversations] == [0, 0, 0, 0]

            engine.replies["extract"].clear()  # the engine is back
            engine.replies["consolidate"].clear()
            clock.now = T0 + timedelta(seconds=59)
            assert memory.run_due() == []
            clock.now = T0 + timedelta(seconds=60)
            due = ["extract:" + conversation for conversation in conversations]
            assert memory.run_due() == due + ["consolidate"]
            assert memory.wait_idle(5)
            assert [_conversation_mark(store, c) for c in conversations] == [1, 1, 1, 1]
    finally:
        memory.close()
    assert engine.kinds() == ["extract"] * 5 + ["consolidate"]
    extracted = [body["conversation"]["id"] for body in engine.bodies_of("extract")]
    assert extracted == ["c1"] + conversations
    failed = (logging.WARNING, "Memory: extraction failed (HTTP 503 credit_check_unavailable)")
    assert _logged(caplog) == [failed]


def test_a_guest_makes_no_memory_calls(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """The engine refuses extract and consolidate to guests, so a guest's memory never asks:
    its turns still send the payload."""
    with Talk2View("pk_test", base_url=engine.url(), memory_path=tmp_path) as t2v:
        t2v.auth.start_anonymous()
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Prefers metric units")], source="said")
        memory.store.record_round("c1", "I moved to Perth", "Noted.")

        assert memory.run_due() == []
        assert memory.extract("c1") is False
        assert memory.consolidate() is False
        assert memory.import_text("I like tea.") is False
        assert list(t2v.chat("hi"))[-1].type == "done"
        memory.new_conversation()
        assert memory.wait_idle(5)

        (body,) = _sent(engine, "/messages")
        assert body["memory"]["status"] == "on"
        assert [entry["text"] for entry in body["memory"]["entries"]] == ["Prefers metric units"]
    assert engine.calls == []


def test_memory_calls_wait_at_least_150_seconds(
    engine: MemoryCallsEngine,
    client: T2VClient,
    memory: T2VMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One engine memory call can take about 120 s; the chat's own timeouts stay as they
    were."""
    seen: List[Tuple[str, Any]] = []
    request = client.request

    def spy(endpoint: str, *args: Any, **kwargs: Any) -> Any:
        seen.append((endpoint, kwargs.get("timeout")))
        return request(endpoint, *args, **kwargs)

    monkeypatch.setattr(client, "request", spy)
    memory.store.record_round("c1", "I moved to Perth", "Noted.")

    assert memory.extract("c1") is True
    assert memory.consolidate() is True
    assert memory.import_text("I like tea.") is True

    assert [endpoint for endpoint, _ in seen] == [
        "/v1/memory/extract",
        "/v1/memory/consolidate",
        "/v1/memory/extract",
        "/v1/memory/consolidate",
    ]
    assert all(timeout is not None and timeout >= 150 for _, timeout in seen)
    assert client.request_timeout < 150


# ── consolidation ──


def test_consolidation_schedule(
    engine: MemoryCallsEngine, memory: T2VMemory, clock: FakeClock
) -> None:
    """< 10 changes: not sent; >= 10 and 7 days: sent once; >= 10 and 1 day: not; after an
    import: sent; over the caps: sent, and not again until memory changes."""
    store = memory.store
    store.apply_ops([add("Schedule entry {}.".format(n)) for n in range(9)], source="said")
    clock.now = T0 + timedelta(days=8)
    assert memory.run_due() == []
    store.apply_ops([add("Schedule entry 9.")], source="said")
    assert memory.run_due() == ["consolidate"]
    assert memory.run_due() == []

    store.apply_ops([add("Later entry {}.".format(n)) for n in range(10)], source="said")
    clock.now = T0 + timedelta(days=9)
    assert memory.run_due() == []
    clock.now = T0 + timedelta(days=15)
    assert memory.run_due() == ["consolidate"]
    assert engine.kinds() == ["consolidate", "consolidate"]

    assert memory.import_text("I go hiking most Saturdays.") is True
    assert engine.kinds()[2:] == ["extract", "consolidate"]
    assert memory.run_due() == []

    store.apply_ops([add("Capped entry {}.".format(n)) for n in range(20)], source="said")
    assert len(store.entries()) == 41  # over ENTRY_MAX
    assert memory.run_due() == ["consolidate"]
    assert memory.run_due() == []  # the answer kept all 41: nothing to do until a change
    store.apply_ops([add("One more entry.")], source="said")
    assert memory.run_due() == ["consolidate"]
    assert engine.kinds().count("consolidate") == 5


def test_consolidate_applies_entries_merges_and_expiries(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The request carries the snapshot (locked first, days in the user's zone); the answer
    replaces the entries, merges and expires facts in the zone the request named."""
    plus_fourteen = timezone(timedelta(hours=14))  # 2026-10-22 there at T0
    monkeypatch.setattr(
        days, "_named_zone", lambda name: plus_fourteen if name == "Plus/Fourteen" else None
    )
    memory.timezone = "Plus/Fourteen"
    store = memory.store
    store.apply_ops(
        [
            add("Lives in Perth", "identity"),
            add("Works at Canva", "work"),
            {"op": "add", "topic": "ongoing", "text": "In Tokyo", "valid_until": "2026-11-10"},
        ],
        source="said",
    )
    store.edit_entry("e2", text="Works at Canva as a designer")  # locked
    jl, flight, dentist = store.add_facts(
        [
            {"text": "Booked JL 772.", "topic": "ongoing"},
            {"text": "Booked flight JL 772 to Tokyo.", "topic": "ongoing"},
            {"text": "Has a dentist appointment on 21 Oct.", "topic": "ongoing"},
        ],
        conversation_id=None,
        source="said",
    )
    store.record_round("c1", "Plan my Tokyo trip", "Sure.")
    store.set_summary("c1", "Tokyo trip", "Planned the Tokyo trip.")
    engine.replies["consolidate"].append(
        {
            "entries": [
                {"id": "e1", "topic": "identity", "text": "Lives in Perth, Australia"},
                {"id": "e2", "topic": "work", "text": "Works at Canva", "locked": True},
                {"id": None, "topic": "ongoing", "text": "Trip to Tokyo", "valid_until": None},
            ],
            "merge_facts": [{"keep": jl.id, "drop": [flight.id]}],
            "expire_facts": [dentist.id],
            "model": "m",
        }
    )

    assert memory.consolidate() is True

    (body,) = engine.bodies_of("consolidate")
    assert body["entries"][0] == {
        "id": "e2",
        "topic": "work",
        "text": "Works at Canva as a designer",
        "updated_at": "2026-10-22",
        "locked": True,
    }
    assert [(entry["id"], entry["locked"]) for entry in body["entries"]] == [
        ("e2", True),
        ("e3", False),
        ("e1", False),
    ]
    assert body["entries"][1]["valid_until"] == "2026-11-10"
    assert sorted(fact["id"] for fact in body["facts"]) == sorted([jl.id, flight.id, dentist.id])
    assert body["summaries"] == [
        {
            "id": "c1",
            "title": "Tokyo trip",
            "text": "Planned the Tokyo trip.",
            "last_active_at": "2026-10-21T10:00:00Z",
        }
    ]
    assert (body["today"], body["timezone"]) == ("2026-10-22", "Plus/Fourteen")
    assert [(entry.id, entry.text) for entry in store.entries()] == [
        ("e1", "Lives in Perth, Australia"),
        ("e2", "Works at Canva as a designer"),  # locked: kept as the user wrote it
        ("e4", "Trip to Tokyo"),
    ]
    facts = {fact.id: fact for fact in store.facts()}
    assert flight.id not in facts
    assert facts[dentist.id].valid_until == "2026-10-21"  # the day before 22 Oct there
    assert store.needs_consolidation() is False


def test_consolidation_is_dropped_when_memory_changed_meanwhile(
    engine: MemoryCallsEngine, memory: T2VMemory, caplog: pytest.LogCaptureFixture
) -> None:
    """R55: an entry added while the call ran would be deleted by a result that never saw
    it, so nothing of the result is applied, one count-only line says so, and the next
    consolidation goes ahead."""
    memory.store.apply_ops([add("Likes tea")], source="said")

    def meanwhile(kind: str) -> None:
        memory.store.apply_ops([add("Works at Canva", "work")], source="said")

    engine.during = meanwhile
    engine.replies["consolidate"].append(
        {"entries": [], "merge_facts": [], "expire_facts": [], "model": "m"}
    )
    with caplog.at_level(logging.INFO, logger="talk2view"):
        assert memory.consolidate() is False

    assert _texts(memory) == ["Likes tea", "Works at Canva"]
    assert _logged(caplog) == [
        (
            logging.INFO,
            "Memory: memory changed during consolidation, so its result (0 entries) was not"
            " applied; consolidation stays due",
        )
    ]
    engine.during = None
    assert memory.consolidate() is True
    assert _texts(memory) == ["Likes tea", "Works at Canva"]


def test_a_call_keeps_the_zone_it_began_with(
    engine: MemoryCallsEngine, memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn may change the zone while a consolidation is being built: the request's
    today and its timezone both keep the zone the call began with, as its expiries do."""
    plus_fourteen = timezone(timedelta(hours=14))  # 2026-10-22 there at T0; 21 Oct in UTC
    monkeypatch.setattr(
        days, "_named_zone", lambda name: plus_fourteen if name == "Plus/Fourteen" else None
    )
    memory.timezone = "Plus/Fourteen"
    snapshot = memory.store.consolidation_snapshot

    def snapshot_then_switch(**kwargs: Any) -> Any:
        memory.timezone = None  # a turn elsewhere, in the system's zone (UTC here)
        return snapshot(**kwargs)

    monkeypatch.setattr(memory.store, "consolidation_snapshot", snapshot_then_switch)

    assert memory.consolidate() is True

    (body,) = engine.bodies_of("consolidate")
    assert (body["today"], body["timezone"]) == ("2026-10-22", "Plus/Fourteen")


def test_consolidation_carries_200_entries_and_reaches_the_rest_later(
    engine: MemoryCallsEngine, memory: T2VMemory, clock: FakeClock
) -> None:
    """R59: at most 200 entries, the most recently updated; the rest stay as they are and
    keep consolidation due, and the next consolidation, after memory next changes, carries
    them."""
    store = memory.store
    store.apply_ops([add("Old entry {}.".format(n)) for n in range(5)], source="said")
    clock.now = T0 + timedelta(hours=1)
    store.apply_ops([add("Entry {}.".format(n)) for n in range(200)], source="said")
    engine.replies["consolidate"].append(
        lambda request: dict(_echoed(request), entries=_echoed(request)["entries"][:10])
    )

    assert memory.run_due() == ["consolidate"]

    (first,) = engine.bodies_of("consolidate")
    assert [entry["id"] for entry in first["entries"]] == [
        "e{}".format(n) for n in range(205, 5, -1)
    ]
    kept = ["e{}".format(n) for n in range(1, 6)] + ["e{}".format(n) for n in range(196, 206)]
    assert [entry.id for entry in store.entries()] == kept
    assert store.needs_consolidation() is True
    assert memory.run_due() == []  # memory hasn't changed since
    store.apply_ops([add("A new entry.")], source="said")
    assert memory.run_due() == ["consolidate"]
    second = engine.bodies_of("consolidate")[1]
    assert sorted(entry["id"] for entry in second["entries"]) == sorted(kept + ["e206"])
    assert store.needs_consolidation() is False


def test_extraction_drops_entry_ops_when_the_chat_turned_incognito(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """A result for a chat that turned incognito while the call ran writes nothing back:
    the store drops the facts and the summary, T2VMemory the entry ops."""
    first = memory.conversation_id
    memory.store.record_round(first, "I moved to Perth", "Noted.")

    def turn_incognito(kind: str) -> None:
        memory.incognito = True

    engine.during = turn_incognito

    assert memory.extract(first) is True

    assert memory.store.entries() == [] and memory.store.facts() == []
    record = memory.store.conversation(first)
    assert record is not None
    assert (record.summary_title, record.extracted_through) == (None, 0)


def _forget_peanut(store: MemoryStore) -> Callable[[str], None]:
    """``during``: the user forgets their peanut allergy while the call runs."""

    def forget(kind: str) -> None:
        store.forget("peanut")

    return forget


def _peanut_free(memory: T2VMemory) -> bool:
    """Whether no fact, entry or summary holds the word."""
    store = memory.store
    texts = [fact.text for fact in store.facts(include_superseded=True)] + _texts(memory)
    for record in store.recent_conversations(100):
        texts += [record.summary_title or "", record.summary_text or ""]
    return not any("peanut" in text.lower() for text in texts)


def test_a_forget_during_extraction_drops_its_result(
    engine: MemoryCallsEngine, memory: T2VMemory, caplog: pytest.LogCaptureFixture
) -> None:
    """A forget while the extract call runs (the user's first message in the next chat
    can be "forget my peanut allergy"): the result was drawn from what it removed, so none
    of it is applied, one line with counts only says so, and the extracted point stays.
    The next extraction reads the log as the forget left it."""
    store = memory.store
    store.record_round("c1", "I'm allergic to peanuts", "Noted.")
    store.record_round("c1", "I live in Perth", "Nice.")
    engine.replies["extract"].append(PEANUT_ANSWER)
    engine.during = _forget_peanut(store)

    with caplog.at_level(logging.INFO, logger="talk2view"):
        assert memory.extract("c1") is False

    assert _peanut_free(memory)
    assert store.facts() == [] and store.entries() == []
    assert _conversation_mark(store, "c1") == 0
    assert _logged(caplog) == [
        (
            logging.INFO,
            "Memory: something was forgotten during the extraction, so its result (1 fact, 1"
            " entry op) was not applied",
        )
    ]
    engine.during = None
    assert memory.extract("c1") is True
    first, second = engine.bodies_of("extract")
    assert first["conversation"]["messages"][0]["text"] == "I'm allergic to peanuts"
    assert [m["text"] for m in second["conversation"]["messages"]] == ["I live in Perth", "Nice."]
    assert _conversation_mark(store, "c1") == 2
    assert _peanut_free(memory)


def test_a_forget_while_the_request_is_built_drops_the_result(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The forget revision is read before anything the request carries: a forget that lands
    after the log was read for the request, and before the request is sent, drops the
    result too. The counts in the log line read in the plural for any number but one."""
    store = memory.store
    store.record_round("c1", "I'm allergic to peanuts", "Noted.")
    engine.replies["extract"].append(
        dict(PEANUT_ANSWER, facts=PEANUT_ANSWER["facts"] * 2, entry_ops=[])
    )
    related = bodies._related_facts
    forget = _forget_peanut(store)

    def forget_then_relate(*args: Any, **kwargs: Any) -> Any:
        forget("extract")
        return related(*args, **kwargs)

    monkeypatch.setattr(bodies, "_related_facts", forget_then_relate)

    with caplog.at_level(logging.INFO, logger="talk2view"):
        assert memory.extract("c1") is False

    (body,) = engine.bodies_of("extract")
    assert body["conversation"]["messages"][0]["text"] == "I'm allergic to peanuts"
    assert _peanut_free(memory)
    assert _logged(caplog) == [
        (
            logging.INFO,
            "Memory: something was forgotten during the extraction, so its result (2 facts, 0"
            " entry ops) was not applied",
        )
    ]


def test_a_forget_from_another_thread_never_lands_between_the_check_and_the_writes(
    engine: MemoryCallsEngine, memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's own forget, on another thread, as the result is applied: it waits for the
    result's one transaction, then takes out what the result wrote too."""
    store = memory.store
    store.record_round("c1", "I'm allergic to peanuts", "Noted.")
    engine.replies["extract"].append(PEANUT_ANSWER)
    add_facts = store.add_facts
    host = threading.Thread(target=_forget_peanut(store), args=("host",))

    def forget_meanwhile(*args: Any, **kwargs: Any) -> Any:
        host.start()
        host.join(0.2)  # it can't finish while the result's transaction is open
        return add_facts(*args, **kwargs)

    monkeypatch.setattr(store, "add_facts", forget_meanwhile)

    assert memory.extract("c1") is True
    host.join(5)

    assert not host.is_alive()
    assert _peanut_free(memory)


def test_an_add_during_extraction_keeps_its_result(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """Only a write that takes text out of memory, or puts it back, drops a result: an
    entry a turn adds while the call runs, as any turn may, leaves it to be applied."""
    store = memory.store
    store.record_round("c1", "I moved to Perth", "Noted.")

    def add_an_entry(kind: str) -> None:
        store.apply_ops([add("Likes tea")], source="said")

    engine.during = add_an_entry

    assert memory.extract("c1") is True

    assert _texts(memory) == ["Likes tea", "Hiking on Saturday"]
    assert [fact.text for fact in store.facts()] == ["Plans a hike on Saturday."]
    assert _conversation_mark(store, "c1") == 1


def test_import_text_uses_import_mode_and_sensitive_switch(
    engine: MemoryCallsEngine, memory: T2VMemory
) -> None:
    """One extract call in import mode with the store's switch, its result tied to no
    conversation, then a consolidation."""
    memory.store.include_sensitive = True
    text = "I'm vegetarian.\nI live in Perth with my partner Sam."

    assert memory.import_text(text) is True

    assert engine.kinds() == ["extract", "consolidate"]
    (body,) = engine.bodies_of("extract")
    assert (body["mode"], body["include_sensitive"], body["extracted_through"]) == (
        "import",
        True,
        0,
    )
    conversation = body["conversation"]
    assert re.fullmatch(r"[0-9a-f]{32}", conversation["id"])
    assert conversation["messages"] == [
        {"role": "user", "text": text, "at": "2026-10-21T10:00:00Z"}
    ]
    assert [(entry.text, entry.source) for entry in memory.store.entries()] == [
        ("Hiking on Saturday", "imported")
    ]
    assert [fact.conversation_id for fact in memory.store.facts()] == [None]
    assert memory.store.recent_conversations() == []

    memory.store.include_sensitive = False
    assert memory.import_text("I prefer tea to coffee.") is True
    assert engine.bodies_of("extract")[1]["include_sensitive"] is False
    assert memory.import_text("x" * 200_001) is False
    assert memory.import_text(" \n ") is False
    assert len(engine.bodies_of("extract")) == 2


def test_an_import_keeps_its_consolidation_due_until_one_is_applied(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After an import, consolidation is due whatever the schedule, until one is applied,
    once a failed one's cooldown has run; a failed import or consolidation changes nothing,
    an import's failures are logged as the import's, and none goes out while paused."""
    engine.replies["consolidate"].append((429, "rate_limit_exceeded"))
    assert memory.import_text("I go hiking most Saturdays.") is True
    assert engine.kinds() == ["extract", "consolidate"]
    assert memory.run_due() == []  # the failed consolidation's cooldown
    clock.now = T0 + timedelta(seconds=60)
    assert memory.run_due() == ["consolidate"]
    assert memory.run_due() == []

    engine.replies["extract"][:] = [(503, "credit_check_unavailable"), []]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="talk2view"):
        assert memory.import_text("I prefer tea to coffee.") is False
        assert memory.import_text("I prefer tea to coffee.") is False
    assert _logged(caplog) == [
        (logging.WARNING, "Memory: import failed (HTTP 503 credit_check_unavailable)"),
        (logging.WARNING, "Memory: import failed (malformed answer)"),
    ]
    clock.now = T0 + timedelta(hours=1)
    assert memory.run_due() == []
    memory.store.paused = True
    assert memory.import_text("I prefer tea to coffee.") is False
    assert memory.consolidate() is False
    assert engine.kinds() == ["extract", "consolidate", "consolidate", "extract", "extract"]


def test_a_forget_during_an_import_discards_it(
    engine: MemoryCallsEngine, memory: T2VMemory, caplog: pytest.LogCaptureFixture
) -> None:
    """The same for an import: a forget while its call runs discards it whole, and no
    consolidation follows. import_text says False, as for a failed call; the host may
    import again."""
    store = memory.store
    store.apply_ops([add("Allergic to peanuts", "identity")], source="said")
    engine.replies["extract"].append(PEANUT_ANSWER)
    engine.during = _forget_peanut(store)

    with caplog.at_level(logging.INFO, logger="talk2view"):
        assert memory.import_text("I'm allergic to peanuts.") is False

    assert _peanut_free(memory)
    assert store.facts() == [] and store.entries() == []
    assert engine.kinds() == ["extract"]
    assert _logged(caplog) == [
        (
            logging.INFO,
            "Memory: something was forgotten during the import, so its result (1 fact, 1 entry"
            " op) was not applied",
        )
    ]
    engine.during = None
    assert memory.import_text("I prefer tea to coffee.") is True
    assert engine.kinds() == ["extract", "extract", "consolidate"]


# ── turns through Talk2View ──


def test_one_payload_rides_the_resend_and_the_resume(
    engine: MemoryCallsEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """chat() builds the field once: the same object goes on the /messages the engine lost,
    the resend's /messages and its /resume, and the round is recorded once."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Lives in Perth", "identity")], source="said")
        built: List[str] = []
        turn_payload = memory.turn_payload

        def counted(message: str) -> Optional[Dict[str, Any]]:
            built.append(message)
            return turn_payload(message)

        monkeypatch.setattr(memory, "turn_payload", counted)
        t2v.create_session()
        engine.sessions.clear()  # the engine lost the session
        engine.streams[:] = [
            [_interrupt("search_memory", "sm1", {"query": "Perth"})],
            [_text("In Perth."), _STOP, DONE],
        ]

        events = list(t2v.chat("Where do I live?"))

        assert events[-1].type == "done"
        assert built == ["Where do I live?"]
        lost, resent, resume = _sent(engine, "/messages", "/resume")
        assert lost["memory"] == resent["memory"] == resume["memory"]
        assert [entry["text"] for entry in lost["memory"]["entries"]] == ["Lives in Perth"]
        assert _rounds(memory) == [("Where do I live?", "In Perth.")]


def test_a_resend_never_carries_the_previous_users_memory(
    engine: MemoryCallsEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone else signs in once the turn's memory field is built: the send reaches the
    previous user's session, which the engine doesn't hold for the new user, and the turn
    ends with session_lost. Nothing is resent, so the previous user's memory field and
    history never go out in the new user's session."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Lives in Perth", "identity")], source="said")
        t2v.create_session()
        turn_payload = memory.turn_payload

        def build_then_switch(message: str) -> Optional[Dict[str, Any]]:
            field = turn_payload(message)
            t2v.auth.login("bob@example.com", "pw")
            return field

        monkeypatch.setattr(memory, "turn_payload", build_then_switch)

        events = list(t2v.chat("Where do I live?"))

        assert [(event.type, event.error_type) for event in events] == [("error", "session_lost")]
        (refused,) = _sent(engine, "/messages", "/resume")  # bob's token, ann's session
        assert [entry["text"] for entry in refused["memory"]["entries"]] == ["Lives in Perth"]
        assert engine.histories_of("user-bob") == []


def test_the_resend_check_needs_the_turns_user() -> None:
    """Who the turn began with is a required keyword of the send: left out, it would read as
    nobody signed in, and the check would refuse every resend of a signed-in user's turn."""
    send = inspect.signature(Talk2View._send_with_session_recovery)
    user_id = send.parameters["user_id"]
    assert user_id.kind is inspect.Parameter.KEYWORD_ONLY
    assert user_id.default is inspect.Parameter.empty


def test_no_round_after_an_error_or_a_stop(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    error = {"error": {"type": "upstream_error", "message": "boom"}}
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        engine.streams[:] = [[_text("Part of a reply"), error, _STOP, DONE]]
        events = [event.type for event in t2v.chat("I moved to Perth")]
        assert events == ["text", "error", "done"]

        token = CancelToken()
        seen: List[str] = []
        try:
            for event in t2v.chat("slow: I moved to Perth", cancel=token):
                seen.append(event.type)
                token.cancel()
        finally:
            engine.release_slow.set()

        assert seen == ["text"]
        assert _rounds(memory) == []


def test_an_approval_continues_the_turn(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """The turn an approval pauses goes on in respond_to_approval: its memory event is
    applied, its batch is last_applied as the event arrives (R85), its resume carries the
    same field, and its round is recorded once, whole."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        engine.streams[:] = [
            [_text("Sending. "), _approval("ap1")],
            [_memory_chunk("c1"), _text("Sent."), _STOP, DONE],
        ]
        asked = list(t2v.chat("Email Alex that I prefer metric units"))[-1]
        assert asked.type == "approval_required"
        assert _rounds(memory) == [] and memory.last_applied is None

        applied = [
            (event.type, memory.last_applied)
            for event in t2v.respond_to_approval(asked, Decision("once"))
            if event.type in ("memory", "done")
        ]

        batch = applied[0][1]
        assert batch is not None and applied == [("memory", batch), ("done", batch)]
        assert _rounds(memory) == [("Email Alex that I prefer metric units", "Sending. Sent.")]
        assert _texts(memory) == ["Prefers metric units"]
        message, resume = _sent(engine, "/messages", "/resume")
        assert resume["memory"] == message["memory"]


def test_no_round_for_a_turn_that_forgot(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """R67: the turn's own words name what was forgotten, so it leaves no round, and a
    search for them finds nothing (spec §4: no memory that survives forget)."""
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        memory.store.apply_ops([add("Is allergic to peanuts", "identity")], source="said")
        memory.store.record_round("older", "I'm allergic to peanuts", "Noted.")
        memory.store.mark_extracted("older", 1)  # no background extraction to race
        forget = _memory_event({"op": "forget", "text": "peanut"})
        engine.streams[:] = [[forget, _text("Done, that's forgotten."), _STOP, DONE]]

        assert list(t2v.chat("Forget that I'm allergic to peanuts"))[-1].type == "done"

        assert _rounds(memory) == []
        assert memory.store.entries() == []
        assert memory.store.search("peanut") == []
        assert memory.store.search("allergic") == []


def test_chat_goes_on_when_the_memory_cannot_open(
    engine: MemoryCallsEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A memory_path that can't hold the file: the turn goes without a memory field and
    search_memory finds nothing; one WARNING names the error's class."""
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    engine.streams[:] = [
        [_interrupt("search_memory", "sm1", {"query": "Perth"})],
        [_text("Nothing found."), _STOP, DONE],
    ]
    with Talk2View("pk_test", base_url=engine.url(), memory_path=blocked) as t2v:
        t2v.auth.login("ann@example.com", "pw")
        with caplog.at_level(logging.WARNING, logger="talk2view"):
            assert list(t2v.chat("Where do I live?"))[-1].type == "done"

    message, resume = _sent(engine, "/messages", "/resume")
    assert "memory" not in message and message["supports_memory"] is True
    assert resume["result"] == NO_RESULTS
    (logged,) = {message for _, message in _logged(caplog)}
    assert re.fullmatch(r"Memory: opening the memory failed \(\w+Error\)", logged)


def test_controller_clear_starts_a_new_conversation(
    engine: MemoryCallsEngine, tmp_path: Path
) -> None:
    """ChatController.clear() → clear_session() → new_conversation(): the ended one is
    extracted in the background, and the next turn's field names it."""
    with _talk2view(engine, tmp_path) as t2v:
        controller = ChatController(
            t2v, capabilities=Capabilities(reasoning=True), executor=lambda fn: fn()
        )
        try:
            memory = _memory_of(t2v)
            first = memory.conversation_id
            controller.send("I go hiking most Saturdays")
            assert _rounds(memory, first) == [("I go hiking most Saturdays", "ok")]

            controller.clear()

            assert memory.conversation_id != first
            assert memory.wait_idle(5)
            assert [body["conversation"]["id"] for body in engine.bodies_of("extract")] == [first]
            controller.send("What do I do on Saturdays?")
            last = _sent(engine, "/messages")[-1]
            assert [item["title"] for item in last["memory"]["recent"]] == ["Weekend plans"]
            assert last["supports_reasoning"] and last["supports_memory"]
            assert _rounds(memory) == [("What do I do on Saturdays?", "ok")]
        finally:
            controller.close()


# ── the background worker and the timer ──


def test_chat_never_waits_for_memory_calls(engine: MemoryCallsEngine, tmp_path: Path) -> None:
    """Not inline: the extraction new_conversation() queues takes 2 s, and neither it nor
    the next chat() waits for it. The 1 s bounds prove that without flaking on a busy
    runner."""
    engine.replies["extract"].append(2.0)
    with _talk2view(engine, tmp_path) as t2v:
        memory = _memory_of(t2v)
        previous = memory.conversation_id
        memory.store.record_round(previous, "I go hiking most Saturdays", "Nice.")

        started = time.monotonic()
        memory.new_conversation()
        assert time.monotonic() - started < 1.0

        engine.streams[:] = [[_text("ok"), _STOP, DONE]]
        started = time.monotonic()
        events = t2v.chat("hi")
        assert next(events).type == "text"
        assert time.monotonic() - started < 1.0
        assert [event.type for event in events] == ["done"]

        assert memory.wait_idle(5)
        assert len(engine.bodies_of("extract")) == 1  # in flight at the launch: not sent twice
        assert _conversation_mark(memory.store, previous) == 1
        assert "Hiking on Saturday" in _texts(memory)


def test_start_background_runs_run_due_and_stops(
    tmp_path: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = T2VClient("pk_test", MemoryStorage(), base_url="http://127.0.0.1:9")
    memory = T2VMemory(client, MemoryStore(tmp_path / "m.sqlite3", clock=clock), clock=clock)
    ticks = threading.Semaphore(0)
    threads: List[str] = []

    def run_due(now: Any = None) -> List[str]:
        threads.append(threading.current_thread().name)
        ticks.release()
        return []

    try:
        monkeypatch.setattr(memory, "run_due", run_due)
        with pytest.raises(ValueError):
            memory.start_background(0)
        memory.start_background(interval_s=0.01)
        memory.start_background(interval_s=0.01)  # one timer only
        assert ticks.acquire(timeout=5) and ticks.acquire(timeout=5)

        memory.stop_background()
        for thread in threading.enumerate():
            if thread.name == "talk2view-memory-timer":
                thread.join(5)
        count = len(threads)
        time.sleep(0.05)
        assert len(threads) == count
        assert set(threads) == {"talk2view-memory-timer"}

        memory.start_background(interval_s=0.01)
        assert ticks.acquire(timeout=5)
        memory.close()  # stops it too
        for thread in threading.enumerate():
            if thread.name == "talk2view-memory-timer":
                thread.join(5)
        assert not any(t.name == "talk2view-memory-timer" for t in threading.enumerate())
        memory.start_background(interval_s=0.01)  # closed: no timer
        assert not any(t.name == "talk2view-memory-timer" for t in threading.enumerate())
    finally:
        memory.close()
        client.close()


def test_a_start_racing_close_leaves_no_timer(
    memory: T2VMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start_background() that lands while close() runs, just after close() stopped the
    timer, starts none: the memory is closed first."""
    stop = memory.stop_background

    def stop_then_start() -> None:
        stop()
        memory.start_background(interval_s=0.01)

    monkeypatch.setattr(memory, "stop_background", stop_then_start)
    try:
        memory.close()
        assert not any(t.name == "talk2view-memory-timer" for t in threading.enumerate())
    finally:
        stop()


def test_a_job_raising_a_base_exception_never_wedges_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A BaseException out of a job gets past keyed() and ends its thread, but the job
    queued behind it still runs, the worker takes more, and wait_idle returns."""

    class Halt(BaseException):
        pass

    escaped: List[Any] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_type))
    go = threading.Event()
    ran: List[str] = []

    def halt() -> None:
        go.wait(5)
        raise Halt()

    worker = Worker()
    assert worker.run("a", halt)
    assert worker.run("b", lambda: ran.append("b"))  # queued behind it
    go.set()
    assert worker.wait_idle(5)
    assert worker.run("a", lambda: ran.append("a"))  # its key was released
    assert worker.wait_idle(5)

    assert ran == ["b", "a"]
    assert _eventually(lambda: escaped == [Halt])


def test_a_closed_memory_does_nothing(engine: MemoryCallsEngine, memory: T2VMemory) -> None:
    memory.store.record_round("c1", "I moved to Perth", "Noted.")
    memory.close()
    memory.close()

    assert memory.enabled is False
    assert memory.turn_payload("hi") is None
    assert memory.handle_search({"query": "Perth"}) == NO_RESULTS
    assert memory.on_event(ChatEvent(type="memory", tool_call_id="c1", memory_ops=OPS)) is None
    assert memory.run_due() == []
    assert memory.extract("c1") is False
    assert memory.consolidate() is False
    assert memory.import_text("I like tea.") is False
    memory.incognito = True
    assert memory.new_conversation()
    events = [ChatEvent(type="text", content="ok"), ChatEvent(type="done")]
    assert list(memory.observe_turn("hi", iter(events))) == events
    assert list(memory.observe_approval(iter(events))) == events
    assert engine.calls == []


@pytest.mark.parametrize("call", ["extract", "consolidate", "import"])
def test_nothing_goes_out_once_closed(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
) -> None:
    """R82: closed once its body is built (the user signed out, say), a call sends nothing
    and logs nothing: the body holds the memory of a user who has gone."""
    memory.store.record_round("c1", "I moved to Perth", "Noted.")
    name = {"extract": "extract_body", "consolidate": "consolidate_body", "import": "import_body"}
    build = getattr(bodies, name[call])

    def build_then_close(*args: Any, **kwargs: Any) -> Any:
        built = build(*args, **kwargs)
        memory.close()
        return built

    monkeypatch.setattr(bodies, name[call], build_then_close)
    with caplog.at_level(logging.DEBUG, logger="talk2view"):
        if call == "extract":
            assert memory.extract("c1") is False
        elif call == "consolidate":
            assert memory.consolidate() is False
        else:
            assert memory.import_text("I like tea.") is False

    assert engine.calls == []
    assert _logged(caplog) == []


def test_store_failures_never_raise(
    engine: MemoryCallsEngine,
    client: T2VClient,
    memory: T2VMemory,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every entry point stays quiet when the store fails: one WARNING naming the error's
    class, and no engine call it couldn't build. A guest check that fails counts as a
    guest."""
    memory.store.record_round("c1", "I moved to Perth", "Noted.")

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    with caplog.at_level(logging.WARNING, logger="talk2view"):
        with monkeypatch.context() as patch:
            patch.setattr(MemoryStore, "paused", property(failing))
            events = [ChatEvent(type="text", content="Noted."), ChatEvent(type="done")]
            assert list(memory.observe_turn("hi", iter(events))) == events
        with monkeypatch.context() as patch:
            for name in ("unextracted", "conversation", "consolidation_snapshot", "set_incognito"):
                patch.setattr(memory.store, name, failing)
            patch.setattr(MemoryStore, "include_sensitive", property(failing))
            assert memory.run_due() == []
            assert memory.extract("c1") is False
            assert memory.consolidate() is False
            assert memory.import_text("I like tea.") is False
            memory.new_conversation()
            memory.incognito = True

    assert _logged(caplog) == [
        (logging.WARNING, "Memory: reading the pause switch failed (OperationalError)"),
        (logging.WARNING, "Memory: checking what is due failed (OperationalError)"),
        (logging.WARNING, "Memory: extraction failed (OperationalError)"),
        (logging.WARNING, "Memory: consolidation failed (OperationalError)"),
        (logging.WARNING, "Memory: import failed (OperationalError)"),
        (logging.WARNING, "Memory: queueing an extraction failed (OperationalError)"),
        (logging.WARNING, "Memory: marking the chat incognito failed (OperationalError)"),
    ]
    # Failing closed: the store never marked the chat, yet nothing of it is sent or found.
    assert memory.incognito is True
    assert memory.turn_payload("Where do I live?") == {"status": "incognito"}
    assert memory.handle_search({"query": "Perth"}) == NO_RESULTS
    assert _rounds(memory, "c1") == [("I moved to Perth", "Noted.")]

    unsure = T2VMemory(
        client, MemoryStore(tmp_path / "unsure.sqlite3"), inline=True, is_anonymous=failing
    )
    try:
        unsure.store.record_round("c9", "I like coffee", "Noted.")
        assert unsure.run_due() == []
    finally:
        unsure.close()
    assert engine.calls == []


# ── logs ──

SECRETS = ("zanzibar", "x1234567", "peanut")


def test_no_memory_text_in_sdk_logs(
    engine: MemoryCallsEngine,
    memory: T2VMemory,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every logger under talk2view, at DEBUG, through every memory path that logs, the
    failures included, with errors whose messages quote memory: no memory text reaches a
    log line."""
    caplog.set_level(logging.DEBUG, logger="talk2view")
    store = memory.store
    said = "I fly to Zanzibar with passport X1234567"
    store.apply_ops([add("Allergic to peanuts", "identity")], source="said")

    # A turn: an op applied, two dropped, the round recorded; a search; a bad search.
    event = ChatEvent(
        type="memory",
        tool_call_id="c1",
        memory_ops=[
            add("Trip to Zanzibar", "ongoing"),
            add("Trip to Zanzibar", "ongoing"),
            {"op": "update", "id": "e99", "text": "Passport X1234567"},
        ],
    )
    turn = [event, ChatEvent(type="text", content="Zanzibar it is."), ChatEvent(type="done")]
    list(memory.observe_turn(said, iter(turn)))
    memory.handle_search({"query": "Zanzibar passport"})
    memory.handle_search({"query": "Z"})

    # Extraction and an import: an engine error quoting memory, a malformed answer, success.
    conversation = memory.conversation_id
    engine.replies["extract"][:] = [(502, "upstream_error", "the model said: " + said), []]
    assert memory.extract(conversation) is False
    assert memory.extract(conversation) is False
    assert memory.extract(conversation) is True
    assert memory.import_text("x" * 200_001) is False
    assert memory.import_text("Zanzibar is my favourite island.") is True

    # Consolidation: memory changed meanwhile, then a malformed answer.
    engine.during = lambda kind: store.apply_ops([add("Peanut butter is banned", "identity")])
    assert memory.consolidate() is False
    engine.during = None
    engine.replies["consolidate"].append({"entries": "Zanzibar"})
    assert memory.consolidate() is False

    # The store failing with messages that quote memory.
    def failing(*args: Any, **kwargs: Any) -> Any:
        raise sqlite3.OperationalError("near 'Zanzibar X1234567': peanut")

    for name in ("search_result_json", "turn_payload", "record_round", "apply_ops", "purge"):
        monkeypatch.setattr(store, name, failing)
    memory.handle_search({"query": "Zanzibar"})
    assert memory.turn_payload(said) is None
    list(memory.observe_turn(said, iter(turn)))
    memory.run_due()

    # And a Talk2View turn with a memory event and a search.
    with _talk2view(engine, tmp_path) as t2v:
        engine.streams[:] = [
            [
                _memory_event(add("Trip to Zanzibar", "ongoing")),
                _interrupt("search_memory", "sm1", {"query": "Zanzibar"}),
            ],
            [_text("Zanzibar, with passport X1234567."), _STOP, DONE],
        ]
        assert list(t2v.chat(said))[-1].type == "done"

    records = [record for record in caplog.records if record.name.startswith("talk2view")]
    messages = [record.getMessage() for record in records]
    assert "Memory: extraction failed (HTTP 502 upstream_error)" in messages
    assert "Memory: import failed (200001 code points, over the 200000 one call takes)" in messages
    assert "Memory: recording the round failed (OperationalError)" in messages
    assert "Memory: a memory event applied 1 ops and dropped 2 (duplicate, missing)" in messages
    assert all(record.exc_info is None for record in records)
    for message in messages:
        assert not any(secret in message.lower() for secret in SECRETS), message
