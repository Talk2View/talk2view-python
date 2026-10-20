"""Replay contract/ against the real client stack.

The ``.sse`` files are byte-exact engine output (generated and guarded by the engine's
own tests). They are served over HTTP so the
real ``T2VClient`` + SSE parser + session event flow run; the TS suite replays the same bytes
(the TypeScript SDK's ``tests/contract/contract.test.ts``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import talk2view.errors as errors_module
from talk2view import Talk2View
from talk2view.controller import Capabilities, ChatController, Decision
from talk2view.errors import T2VError, error_for_type, is_session_gone
from talk2view.types import ChatEvent, ClientToolSchema

CONTRACT = Path(__file__).resolve().parents[1] / "contract"
MANIFEST: Dict[str, Any] = json.loads((CONTRACT / "manifest.json").read_text(encoding="utf-8"))
FIXTURES = [f for f in MANIFEST["fixtures"] if "python" in f["sdks"]]
SESSION = MANIFEST["session"]


def _expected(fixture_id: str, kind: str) -> Any:
    path = CONTRACT / "expected" / "{}.{}.json".format(fixture_id, kind)
    return json.loads(path.read_text(encoding="utf-8"))


def normalise_event(event: ChatEvent) -> Dict[str, Any]:
    """ChatEvent -> the language-neutral dict in expected/*.events.json (TS key names)."""
    out: Dict[str, Any] = {"type": event.type}
    if event.type in ("text", "reasoning"):
        out["content"] = event.content
    elif event.type == "tool_call":
        out.update(
            toolName=event.tool_name, toolCallId=event.tool_call_id, arguments=event.arguments
        )
    elif event.type == "approval_required":
        out.update(
            toolName=event.tool_name,
            toolCallId=event.tool_call_id,
            arguments=event.arguments,
            description=event.description,
        )
        if event.kind == "engine":
            out.update(kind="engine", details=event.details)
    elif event.type == "approval_result":
        out.update(toolName=event.tool_name, decision=event.decision)
    elif event.type == "tool_event" and event.tool_event is not None:
        tool_event = event.tool_event
        out.update(event=tool_event.type, toolName=event.tool_name, toolCallId=event.tool_call_id)
        if tool_event.arguments is not None:
            out["arguments"] = tool_event.arguments
        if tool_event.status is not None:
            out["status"] = tool_event.status
        if tool_event.display is not None:
            out["display"] = tool_event.display
    elif event.type == "status":
        out.update(status=event.status, message=event.message)
    elif event.type == "todos":
        out["content"] = event.todos
    elif event.type == "done":
        out["threadId"] = event.thread_id
    elif event.type == "error":
        out["message"] = event.message
        if event.error_type is not None:
            out["errorType"] = event.error_type
        if event.detail is not None:
            out["detail"] = event.detail
    return out


def normalise_snapshot(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Drop what cannot be byte-stable (ids, timestamps) and keep what the page renders."""
    messages: List[Dict[str, Any]] = []
    for message in snapshot["messages"]:
        messages.append({k: v for k, v in message.items() if k not in ("id", "timestamp")})
    return {
        "messages": messages,
        "error": snapshot["error"],
        "threadId": snapshot["threadId"],
        "pendingApproval": snapshot["pendingApproval"],
    }


class Call(NamedTuple):
    """One chat call the SDK made: which route it went to, and what it posted."""

    path: str
    body: Dict[str, Any]


class Engine:
    """Serves one fixture: the session, the scripted streams in order, and records calls."""

    def __init__(self, httpserver: HTTPServer, fixture: Dict[str, Any]) -> None:
        self.calls: List[Call] = []
        self._streams: Iterator[str] = iter(fixture["streams"])
        session_id = SESSION["session_id"]
        httpserver.expect_request("/v1/sessions", method="POST").respond_with_json(SESSION)
        httpserver.expect_request("/v1/tools/register", method="POST").respond_with_json(
            {"registered": list(fixture.get("tools", {})), "count": len(fixture.get("tools", {}))}
        )
        for route in ("messages", "resume"):
            httpserver.expect_request(
                "/v1/sessions/{}/{}".format(session_id, route), method="POST"
            ).respond_with_handler(self._stream)

    def _stream(self, request: Request) -> Response:
        self.calls.append(Call(request.path, json.loads(request.get_data(as_text=True))))
        payload = (CONTRACT / "sse" / next(self._streams)).read_bytes()
        return Response(payload, status=200, content_type="text/event-stream")

    @property
    def bodies(self) -> List[Dict[str, Any]]:
        return [call.body for call in self.calls]


def _client(httpserver: HTTPServer, fixture: Dict[str, Any]) -> Talk2View:
    t2v = Talk2View(
        partner_key="pk_test_contract",
        base_url=httpserver.url_for("").rstrip("/"),
        capabilities=Capabilities(**fixture.get("capabilities", {})),
    )
    t2v._client.storage.set("access_token", "tok_contract")
    tools = fixture.get("tools", {})
    if tools:
        for name, result in tools.items():
            t2v.tools.handle(name, lambda _result=result, **_kwargs: _result)
        t2v.tools.register(
            [ClientToolSchema(name=name, description="contract tool") for name in tools]
        )
    return t2v


def _assert_resume_calls(engine: Engine, fixture: Dict[str, Any]) -> None:
    """Routing, not just body shape (mirrors the TS suite's ``expectResumeBodies``).

    An approval that resumed by posting a second ``/messages`` would send the turn again
    instead of continuing it, and every body assertion below would still pass.
    """
    expected = fixture.get("resumeBodies", [])
    assert engine.calls, "the fixture made no chat call at all"
    # The opening turn always posts to .../messages, never .../resume.
    assert engine.calls[0].path.endswith("/messages")
    resumes = engine.calls[1:]
    # A fixture with no resume bodies makes exactly one chat call and nothing else.
    assert len(resumes) == len(expected)
    for actual, wanted in zip(resumes, expected):
        assert actual.path.endswith("/resume")
        # Subset match: capability flags may ride along on /resume.
        assert {k: actual.body.get(k) for k in wanted} == wanted
        # ...but never `supports_approval`: it is a session-creation flag, not a turn flag.
        assert "supports_approval" not in actual.body


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_events_match_the_contract(httpserver: HTTPServer, fixture: Dict[str, Any]) -> None:
    engine = Engine(httpserver, fixture)
    with _client(httpserver, fixture) as t2v:
        phases = [list(t2v.chat(fixture["prompt"]))]
        if "approve" in fixture:
            request = phases[0][-1]
            assert request.type == "approval_required"
            phases.append(
                list(t2v.respond_to_approval(request, Decision(action=fixture["approve"])))
            )
    actual = {"phases": [[normalise_event(e) for e in phase] for phase in phases]}
    assert actual == _expected(fixture["id"], "events")
    _assert_resume_calls(engine, fixture)


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["id"] for f in FIXTURES])
def test_final_snapshot_matches_the_contract(
    httpserver: HTTPServer, fixture: Dict[str, Any]
) -> None:
    engine = Engine(httpserver, fixture)
    with _client(httpserver, fixture) as t2v:
        controller = ChatController(t2v, executor=lambda fn: fn())
        controller.send(fixture["prompt"])
        if "approve" in fixture:
            assert controller.snapshot()["pendingApproval"] is not None
            controller.approve(Decision(action=fixture["approve"]))
        snapshot = controller.snapshot()
    assert snapshot["isLoading"] is False
    assert normalise_snapshot(dict(snapshot)) == _expected(fixture["id"], "snapshot")
    _assert_resume_calls(engine, fixture)


def test_capability_flags_reach_the_messages_body(httpserver: HTTPServer) -> None:
    fixture = next(f for f in FIXTURES if f["id"] == "tool_events_image")
    engine = Engine(httpserver, fixture)
    with _client(httpserver, fixture) as t2v:
        list(t2v.chat(fixture["prompt"]))
    assert engine.bodies[0]["supports_tool_events"] is True
    assert engine.bodies[0]["supports_image_generation"] is True
    assert "supports_approval" not in engine.bodies[0]


@pytest.mark.parametrize("entry", MANIFEST["http"], ids=[e["file"] for e in MANIFEST["http"]])
def test_http_error_bodies_map_to_error_classes(entry: Dict[str, Any]) -> None:
    fixture = json.loads((CONTRACT / "http" / entry["file"]).read_text(encoding="utf-8"))
    body = fixture["body"]["error"]

    error = error_for_type(body["message"], body["type"], fixture["status"])

    assert type(error) is getattr(errors_module, entry["pythonClass"])
    assert error.type == body["type"]
    assert error.status_code == fixture["status"]
    assert is_session_gone(error) is entry["sessionGone"]


@pytest.mark.parametrize(
    "entry",
    [e for e in MANIFEST["http"] if not e["file"].endswith("_401.json")],
    ids=[e["file"] for e in MANIFEST["http"] if not e["file"].endswith("_401.json")],
)
def test_http_error_bodies_survive_the_client(
    httpserver: HTTPServer, entry: Dict[str, Any]
) -> None:
    """Over real HTTP (401s are excluded: they enter the token-refresh path first)."""
    fixture = json.loads((CONTRACT / "http" / entry["file"]).read_text(encoding="utf-8"))
    httpserver.expect_request("/v1/contract-probe", method="POST").respond_with_json(
        fixture["body"], status=fixture["status"]
    )
    with Talk2View(
        partner_key="pk_test_contract", base_url=httpserver.url_for("").rstrip("/")
    ) as t2v:
        t2v._client.storage.set("access_token", "tok_contract")
        with pytest.raises(T2VError) as caught:
            t2v._client.request("/v1/contract-probe", method="POST", body={})

    error = caught.value
    assert type(error) is getattr(errors_module, entry["pythonClass"])
    assert error.type == fixture["body"]["error"]["type"]
    assert error.status_code == fixture["status"]
    assert is_session_gone(error) is entry["sessionGone"]
