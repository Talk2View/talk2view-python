"""The memory wire (spec 8.1, 8.3, 8.4): the memory capability flags, the turn's ``memory``
object, the engine's ``memory`` event and the engine-defined ``search_memory`` client tool.

A real ``Talk2View`` against ``FakeEngine``, which here keeps each chat and tool-register
request body as the bytes it received.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import ChatController, Talk2View
from talk2view.streaming import _parse_chunk
from talk2view.types import Capabilities, ChatEvent, ClientToolSchema, Decision

from . import test_contract as contract
from .fake_engine import _STOP, FakeEngine, _data, _text

DONE = "data: [DONE]\n\n"

# One turn's memory object, as a device sends it. The en dash goes out escaped, so the
# byte comparisons cover the escaping too.
PAYLOAD: Dict[str, Any] = {
    "status": "on",
    "entries": [
        {
            "id": "e12",
            "topic": "ongoing",
            "text": "Trip to Tokyo 3" + chr(0x2013) + "10 Nov",
            "updated_at": "2026-10-20",
            "valid_until": "2026-11-10",
        }
    ],
    "recent": [
        {
            "id": "c1",
            "title": "Packing",
            "summary": "A packing list for Tokyo.",
            "last_active_at": "2026-10-19",
        }
    ],
    "recalled": [
        {
            "kind": "fact",
            "date": "2026-10-01",
            "conversation_title": "Flights",
            "text": "Booked JL 772.",
        }
    ],
}

# The ops of one update_memory call, as the engine's memory chunk carries them.
OPS: List[Dict[str, Any]] = [
    {"op": "add", "topic": "preferences", "text": "Prefers metric units"},
    {"op": "remove", "id": "e7"},
]

# What SDK 0.3.0 sends for _turn(): no memory flag and no memory object.
TODAY = [
    b'{"messages": [{"role": "user", "content": "delete it"}], "stream": true}',
    b'{"tool_call_id": "tc1", "result": "12 mm", "is_error": false}',
    b'{"tool_call_id": "tc2", "result": "deleted", "is_error": false}',
    b'{"tool_call_id": "ap1", "approved": true}',
]

Frame = Union[Dict[str, Any], str]  # a chunk, or a raw SSE line such as DONE


class MemoryEngine(FakeEngine):
    """``FakeEngine`` that keeps each chat and tool-register request as ``(path, raw
    body)``, and answers each /messages or /resume call with the next stream queued in
    ``streams``, while there is one."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.requests: List[Tuple[str, bytes]] = []
        self.streams: List[List[Frame]] = []

    def _handle(self, request: Request) -> Response:
        response = super()._handle(request)  # auth and the session, checked as before
        path = request.path
        chat = path.endswith(("/messages", "/resume"))
        if not chat and path != "/v1/tools/register":
            return response
        with self.lock:
            self.requests.append((path, request.get_data()))
            if not chat or response.status_code != 200 or not self.streams:
                return response
            stream = self.streams.pop(0)
        return self._sse(iter([f if isinstance(f, str) else _data(f) for f in stream]))

    def bodies(self, *suffixes: str) -> List[bytes]:
        """Raw bodies of the requests whose path ends with one of ``suffixes``, in order."""
        with self.lock:
            return [body for path, body in self.requests if path.endswith(suffixes)]


@pytest.fixture
def memory_engine(fake_engine_server: HTTPServer) -> Iterator[MemoryEngine]:
    fake = MemoryEngine(fake_engine_server).start()
    try:
        yield fake
    finally:
        fake.stop()


def _signed_in(engine: MemoryEngine, capabilities: Optional[Capabilities]) -> Talk2View:
    t2v = Talk2View("pk_test", base_url=engine.url(), capabilities=capabilities)
    t2v.auth.login("ann@example.com", "pw")
    return t2v


def _interrupt(tool_name: str, tool_call_id: str, arguments: Any = None) -> Dict[str, Any]:
    return {
        "id": "c",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "interrupt": {
            "type": "tool_call",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "arguments": {"name": "CT"} if arguments is None else arguments,
        },
    }


def _approval(tool_call_id: str) -> Dict[str, Any]:
    return {
        "id": "c",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        "approval": {
            "type": "approval_request",
            "tool_name": "gmail_send_email",
            "tool_call_id": tool_call_id,
            "arguments": {"to": "alex@example.com"},
            "summary": "Send an email to alex@example.com",
            "details": [],
        },
    }


def _memory_chunk(tool_call_id: str) -> Dict[str, Any]:
    """The engine's memory chunk. No finish_reason key: it is serialised without nulls."""
    return {
        "id": "c",
        "choices": [{"index": 0, "delta": {}}],
        "memory": {"ops": OPS, "tool_call_id": tool_call_id},
    }


def _turn(engine: MemoryEngine, capabilities: Optional[Capabilities], **send: Any) -> None:
    """One turn through every body a session sends: /messages, then the /resume of a client
    tool that runs by itself, of one the end-user approves, and of an engine approval."""
    engine.streams[:] = [
        [_interrupt("measure", "tc1")],
        [_interrupt("delete_volume", "tc2")],
        [_approval("ap1")],
        [_text("ok"), _STOP, DONE],
    ]
    with _signed_in(engine, capabilities) as t2v:
        t2v.tools.handle("measure", lambda name: "12 mm")
        t2v.tools.handle("delete_volume", lambda name: "deleted", permission=True)
        session = t2v.create_session()
        asked = list(session.send_message("delete it", **send))[-1]
        assert (asked.type, asked.kind) == ("approval_required", "client")
        asked = list(session.respond_to_approval(asked, Decision("once")))[-1]
        assert (asked.type, asked.kind) == ("approval_required", "engine")
        assert list(session.respond_to_approval(asked, Decision("once")))[-1].type == "done"


@pytest.mark.parametrize("capabilities", [None, Capabilities()], ids=["none", "defaults"])
def test_default_bodies_are_byte_identical_to_today(
    memory_engine: MemoryEngine, capabilities: Optional[Capabilities]
) -> None:
    _turn(memory_engine, capabilities)

    assert memory_engine.bodies("/messages", "/resume") == TODAY


@pytest.mark.parametrize(
    ("flags", "sent"),
    [
        (
            {"memory": True, "memory_search": True},
            {"supports_memory": True, "supports_memory_search": True},
        ),
        ({"memory": True}, {"supports_memory": True}),
        ({"memory_search": True}, {"supports_memory_search": True}),
        ({}, {}),
    ],
    ids=["memory-and-search", "memory", "search", "neither"],
)
def test_flags_sent_only_when_capabilities_set(
    memory_engine: MemoryEngine, flags: Dict[str, bool], sent: Dict[str, bool]
) -> None:
    """On /messages AND on every /resume: LangGraph replays the interrupted tool there, and
    the engine checks search_memory's gate against the resume body again."""
    _turn(memory_engine, Capabilities(**flags))

    bodies = [json.loads(raw) for raw in memory_engine.bodies("/messages", "/resume")]
    assert len(bodies) == 4
    for body in bodies:
        assert {k: v for k, v in body.items() if k.startswith("supports_memory")} == sent
        assert "memory" not in body


def test_memory_field_rides_on_messages_and_resume(memory_engine: MemoryEngine) -> None:
    """The turn's memory object goes on /messages and, byte for byte, on every /resume of
    the turn: a resume without it would make the engine's search_memory gate refuse."""
    _turn(memory_engine, Capabilities(memory=True, memory_search=True), memory=PAYLOAD)

    bodies = memory_engine.bodies("/messages", "/resume")
    assert len(bodies) == 4
    sent = json.dumps(PAYLOAD).encode()
    for raw in bodies:
        assert json.loads(raw)["memory"] == PAYLOAD
        assert sent in raw


def test_a_new_send_message_replaces_the_memory(memory_engine: MemoryEngine) -> None:
    """A manual resume_tool_call() carries its turn's memory; the next send_message()
    replaces it, with an object of its own or with none."""
    paused = {"status": "paused"}
    memory_engine.streams[:] = [
        [_interrupt("measure", "tc1")],
        [_text("ok"), _STOP, DONE],
        [_interrupt("measure", "tc2")],
        [_text("ok"), _STOP, DONE],
        [_interrupt("measure", "tc3")],
        [_text("ok"), _STOP, DONE],
    ]
    with _signed_in(memory_engine, Capabilities(memory=True)) as t2v:
        session = t2v.create_session()  # no handler for measure: the caller resumes by hand
        for tool_call_id, memory in (("tc1", PAYLOAD), ("tc2", paused), ("tc3", None)):
            send = {} if memory is None else {"memory": memory}
            assert list(session.send_message("measure it", **send))[-1].type == "tool_call"
            assert list(session.resume_tool_call(tool_call_id, "12 mm"))[-1].type == "done"

    bodies = [json.loads(raw) for raw in memory_engine.bodies("/messages", "/resume")]
    assert [body.get("memory", "absent") for body in bodies] == [
        PAYLOAD,
        PAYLOAD,
        paused,
        paused,
        "absent",
        "absent",
    ]


def test_memory_chunk_becomes_a_memory_event(memory_engine: MemoryEngine) -> None:
    """The memory chunk is a memory event, and the stream goes on to stop and [DONE]."""
    memory_engine.streams[:] = [[_text("Noted. "), _memory_chunk("c1"), _STOP, DONE]]
    with _signed_in(memory_engine, Capabilities(memory=True)) as t2v:
        events = list(t2v.create_session().send_message("Remember that I prefer metric units"))

    assert events == [
        ChatEvent(type="text", content="Noted. "),
        ChatEvent(type="memory", tool_call_id="c1", memory_ops=OPS),
        ChatEvent(type="done", thread_id="th"),
    ]


def test_memory_is_read_from_its_key_alone() -> None:
    """No choices or finish_reason needed. The ops pass through as sent: the store judges
    each op and counts what it drops. Ops that are not a list read as none."""
    odd = OPS + [{"op": "explode"}]
    chunk = _parse_chunk({"memory": {"ops": odd, "tool_call_id": "c1"}})
    assert chunk.choices == []
    assert chunk.memory is not None
    assert (chunk.memory.tool_call_id, chunk.memory.ops) == ("c1", odd)

    not_a_list = _parse_chunk({"memory": {"ops": {"op": "add"}, "tool_call_id": "c1"}})
    assert not_a_list.memory is not None
    assert not_a_list.memory.ops == []
    assert _parse_chunk({"memory": None}).memory is None


def test_a_null_tool_call_id_reads_as_empty() -> None:
    """The field is a string: a JSON null reads as "", as a missing key does."""
    for raw in ({"ops": OPS, "tool_call_id": None}, {"ops": OPS}):
        chunk = _parse_chunk({"memory": raw})
        assert chunk.memory is not None
        assert chunk.memory.tool_call_id == ""


def test_unknown_chunk_keys_still_ignored(memory_engine: MemoryEngine) -> None:
    """A key this SDK doesn't know changes nothing, even one named like memory."""
    future = dict(_text("Hello"), memory_v2={"ops": OPS, "tool_call_id": "c1"})
    assert _parse_chunk(future) == _parse_chunk(_text("Hello"))

    memory_engine.streams[:] = [[future, _STOP, DONE]]
    with _signed_in(memory_engine, Capabilities(memory=True)) as t2v:
        events = list(t2v.create_session().send_message("hi"))

    assert events == [
        ChatEvent(type="text", content="Hello"),
        ChatEvent(type="done", thread_id="th"),
    ]


def test_contract_memory_event_replays(httpserver: HTTPServer) -> None:
    """The engine-generated memory_event fixture, replayed as tests/test_contract.py replays
    every fixture: through chat() for the events, through ChatController for the snapshot."""
    fixture = next((f for f in contract.FIXTURES if f["id"] == "memory_event"), None)
    assert fixture is not None, "contract/manifest.json has no memory_event fixture for python"

    engine = contract.Engine(httpserver, fixture)
    with contract._client(httpserver, fixture) as t2v:
        events = [contract.normalise_event(e) for e in t2v.chat(fixture["prompt"])]
    assert {"phases": [events]} == contract._expected("memory_event", "events")
    (call,) = engine.calls
    assert call.path.endswith("/messages")
    assert call.body["supports_memory"] is True
    assert "supports_memory_search" not in call.body

    httpserver.clear()
    contract.Engine(httpserver, fixture)
    with contract._client(httpserver, fixture) as t2v:
        controller = ChatController(t2v, executor=lambda fn: fn())
        controller.send(fixture["prompt"])
        snapshot = controller.snapshot()
    assert snapshot["isLoading"] is False
    assert contract.normalise_snapshot(dict(snapshot)) == contract._expected(
        "memory_event", "snapshot"
    )


def test_search_memory_handler_runs_without_a_registered_schema(
    memory_engine: MemoryEngine,
) -> None:
    """search_memory is engine-defined: the SDK never registers its schema (the engine
    refuses a partner tool of that name while memory is on), and tools.handle() runs it."""
    seen: List[Dict[str, Any]] = []
    result = json.dumps(
        {
            "results": [
                {
                    "date": "2026-10-01",
                    "conversation": "Flights",
                    "kind": "fact",
                    "text": "Booked JL 772.",
                }
            ]
        }
    )

    def search_memory(**args: Any) -> str:
        seen.append(args)
        return result

    args = {"query": "Tokyo flights", "from_date": "2026-10-01", "limit": 8}
    memory_engine.streams[:] = [
        [_interrupt("search_memory", "sm1", args)],
        [_text("JL 772."), _STOP, DONE],
    ]
    with _signed_in(memory_engine, Capabilities(memory=True, memory_search=True)) as t2v:
        t2v.tools.register([ClientToolSchema(name="measure", description="Measure a volume.")])
        t2v.tools.handle("measure", lambda name: "12 mm")
        t2v.tools.handle("search_memory", search_memory)
        session = t2v.create_session()
        events = list(session.send_message("Which flight did I book?", memory=PAYLOAD))

    assert seen == [args]
    assert events == [
        ChatEvent(type="tool_call", tool_name="search_memory", tool_call_id="sm1", arguments=args),
        ChatEvent(type="text", content="JL 772."),
        ChatEvent(type="done", thread_id="th"),
    ]
    (resume,) = memory_engine.bodies("/resume")
    assert json.loads(resume) == {
        "tool_call_id": "sm1",
        "result": result,
        "is_error": False,
        "supports_memory": True,
        "supports_memory_search": True,
        "memory": PAYLOAD,
    }
    # The partner's own tool went to /v1/tools/register (twice: register() and the new
    # session's re-register), and search_memory never did.
    registers = memory_engine.bodies("/v1/tools/register")
    assert len(registers) == 2
    for raw in registers:
        assert [tool["name"] for tool in json.loads(raw)["tools"]] == ["measure"]
