"""End to end: the real Talk2View + ChatController behind UIServer, driven over HTTP the way
the embed page drives it, against the stateful fake engine.

The page signs in, chats, approves a client tool, uploads a file and sends it, picks a
model, starts a new session and signs out. Its state, rebuilt only from SSE events, must
equal the hub's mirror, and its chat half must equal ``controller.snapshot()``. Then one
account follows another, and nothing of the first reaches a stream resumed afterwards, and
a platform tool runs start to finish while a page is reading the stream.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List

import httpx
import pytest
from pytest_httpserver import HTTPServer

from talk2view import ChatController, Talk2View, tool
from talk2view.controller import Capabilities
from talk2view.ui import GenericHost, UIServer

from .fake_engine import _STOP, _data, _text
from .ui_engine import AccountEngine
from .ui_helpers import EventStream, read_data_event

STUB = Path(__file__).parent / "fixtures" / "ui_static"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))


def test_the_real_controller_behind_the_ui_server(account_engine: AccountEngine) -> None:
    ran: List[str] = []

    @tool(permission=True)
    def delete_volume(name: str) -> str:
        """Delete a volume.

        Args:
            name: The volume.
        """
        ran.append(name)
        return "deleted"

    t2v = Talk2View("pk_test", base_url=account_engine.url())
    t2v.tools.register_from_functions([delete_volume])
    controller = ChatController(t2v)  # the default worker-thread executor
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    token = server.start().split("#t=", 1)[1]
    page = EventStream(server.port, token)

    def post(path: str, body: bytes, content_type: str) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + token, "Content-Type": content_type,
        })
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        assert resp.status in (200, 202), payload
        return payload

    def command(**body: Any) -> None:
        post("/command", json.dumps(body).encode(), "application/json")

    def result(command_id: str) -> Dict[str, Any]:
        assert page.wait_until(lambda s: any(r["id"] == command_id for r in s["results"]))
        found: Dict[str, Any] = next(r for r in page.state()["results"] if r["id"] == command_id)
        return found

    try:
        assert page.wait_until(lambda s: "auth" in s and "messages" in s)
        command(type="login", email="ana@example.com", password="pw", id="login")
        assert page.wait_until(lambda s: s["auth"]["isAuthenticated"] and s["models"]["list"])

        command(type="send", text="hello", id="s1")
        assert page.wait_until(
            lambda s: [m["content"] for m in s["messages"]] == ["hello", "ok"]
            and not s["isLoading"]
        )

        command(type="send", text="delete CT", id="s2")
        assert page.wait_until(lambda s: s["pendingApproval"] is not None)
        pending = page.state()["pendingApproval"]
        assert pending["toolName"] == "delete_volume" and ran == []
        command(type="approve", toolCallId=pending["toolCallId"], action="once", id="a")
        assert page.wait_until(
            lambda s: s["pendingApproval"] is None
            and not s["isLoading"]
            and s["messages"][-1]["content"].endswith("done")
        )
        assert ran == ["CT"]

        request = httpx.Request(
            "POST", "http://127.0.0.1/upload", files={"file": ("scan.png", PNG, "image/png")}
        )
        attachment = post("/upload", request.read(), request.headers["Content-Type"])
        command(type="send", text="look", attachmentIds=[attachment["id"]], id="s3")
        assert result("s3")["ok"] is True
        assert page.wait_until(
            lambda s: s["messages"][-2].get("attachments") == [attachment] and not s["isLoading"]
        )
        last_turn = account_engine.histories_of("user-ana")[-1][-1]
        assert attachment["id"] in json.dumps(last_turn)

        command(type="selectModel", modelId="m-deep", id="m")
        assert result("m")["ok"] is True
        command(type="newSession", id="n")
        assert page.wait_until(lambda s: s["messages"] == [] and not s["isLoading"])
        assert result("n")["ok"] is True
        outcomes = {r["id"]: r["ok"] for r in page.state()["results"]}
        assert all(outcomes[i] for i in ("login", "s1", "s2", "a", "s3", "m", "n"))
        command(type="logout", id="o")
        assert page.wait_until(
            lambda s: not s["auth"]["isAuthenticated"] and s["models"]["list"] == []
        )
        assert result("o")["ok"] is True

        # The thread list follows the sign-out on its own worker, after logout() returns:
        # let it finish (and write what the debounce holds, so nothing more is published),
        # then wait for the page to reach the hub before comparing the two.
        assert server._threads is not None and server._threads._settle(flush=True)
        assert page.wait_until(lambda s: s == server._hub.snapshot()["state"])
        state = page.state()
        assert page.error is None
        assert state == server._hub.snapshot()["state"]
        chat = dict(controller.snapshot())
        chat.pop("seq")
        assert {key: state[key] for key in chat} == chat
        assert [r["id"] for r in state["results"]] == ["o"]  # the sign-out emptied the rest
        assert page.page.kinds.count("snapshot") == 1
    finally:
        server.stop()
        controller.close()
        t2v.close()


class Wire:
    """What a page holding the token can do: one live stream, commands, uploads, and
    resuming a second stream from any ``Last-Event-ID``."""

    def __init__(self, server: UIServer) -> None:
        self.port = server.port
        self.token = server.url.split("#t=", 1)[1]
        self.page = EventStream(self.port, self.token)

    def post(self, path: str, body: bytes, content_type: str) -> Any:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=body, headers={
            "Authorization": "Bearer " + self.token, "Content-Type": content_type,
        })
        resp = conn.getresponse()
        payload = json.loads(resp.read())
        conn.close()
        assert resp.status in (200, 202), payload
        return payload

    def command(self, **body: Any) -> None:
        self.post("/command", json.dumps(body).encode(), "application/json")

    def result(self, command_id: str) -> Dict[str, Any]:
        def find(state: Dict[str, Any]) -> Any:
            return next((r for r in state["results"] if r["id"] == command_id), None)

        assert self.page.wait_until(lambda s: find(s) is not None), command_id
        found: Dict[str, Any] = find(self.page.state())
        return found

    def upload(self, filename: str) -> Dict[str, Any]:
        request = httpx.Request(
            "POST", "http://127.0.0.1/upload", files={"file": (filename, PNG, "image/png")}
        )
        uploaded: Dict[str, Any] = self.post(
            "/upload", request.read(), request.headers["Content-Type"]
        )
        return uploaded

    def resume(self, last_event_id: int) -> Dict[str, str]:
        """The first event of a stream opened with ``Last-Event-ID: last_event_id``."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/events", headers={
            "Authorization": "Bearer " + self.token, "Last-Event-ID": str(last_event_id),
        })
        resp = conn.getresponse()
        assert resp.status == 200
        event = read_data_event(resp)
        conn.close()
        assert event is not None
        return event


@pytest.mark.parametrize("streaming", [False, True], ids=["replied", "streaming"])
@pytest.mark.parametrize("change", ["switch", "sign-out"])
def test_nothing_of_the_previous_account_survives_a_sign_out_or_a_switch(
    account_engine: AccountEngine, change: str, streaming: bool
) -> None:
    """Ana chats, then bo signs in: straight over ana ("switch"), or after she signed out.
    With ``streaming`` her reply is still arriving when that happens. A stream resumed from
    any seq of ana's, from 0 on, gets a snapshot that holds nothing of hers, and ana's
    upload is not bo's to send."""
    t2v = Talk2View("pk_test", base_url=account_engine.url())
    controller = ChatController(t2v)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    server.start()
    wire = Wire(server)
    try:
        assert wire.page.wait_until(lambda s: "auth" in s and "messages" in s)
        wire.command(type="login", email="ana@example.com", password="pw", id="ana-login")
        assert wire.page.wait_until(lambda s: s["auth"]["isAuthenticated"])
        scan = wire.upload("ana-scan.png")
        text = "ANA-PRIVATE-NOTE patient 1234"
        wire.command(
            type="send", text="slow " + text if streaming else text,
            attachmentIds=[scan["id"]], id="ana-send",
        )
        if streaming:
            assert account_engine.slow_started.wait(10)
            assert wire.page.wait_until(
                lambda s: len(s["messages"]) == 2 and s["messages"][1]["content"] == "partial"
            )
        else:
            assert wire.page.wait_until(lambda s: len(s["messages"]) == 2 and not s["isLoading"])
        ana_seq = wire.page.page.seq
        assert ana_seq is not None
        if change == "sign-out":
            wire.command(type="logout", id="ana-logout")
            assert wire.page.wait_until(
                lambda s: not s["auth"]["isAuthenticated"] and s["messages"] == []
            )
        wire.command(type="login", email="bo@example.com", password="pw2", id="bo-login")
        assert wire.result("bo-login")["ok"] is True
        assert wire.page.wait_until(
            lambda s: (s["auth"]["user"] or {}).get("email") == "bo@example.com"
            and s["messages"] == []
        )
        account_engine.release_slow.set()  # ana's abandoned stream ends: nothing more lands
        # The thread list follows the sign-in on its own worker, a moment after the chat
        # empties: once it has, no slice holds ana's titles either.
        assert server._threads is not None and server._threads._settle(flush=True)
        traces = [
            "ANA-PRIVATE-NOTE", "partial", "ana@example.com", "user-ana", "ana-scan.png",
            scan["id"], "ana-login", "ana-send", "ana-logout",
        ]
        for since in (0, ana_seq):
            event = wire.resume(since)
            assert event["event"] == "snapshot", since
            assert [t for t in traces if t in event["data"]] == [], since
            results = json.loads(event["data"])["state"]["results"]
            assert [r["id"] for r in results] == ["bo-login"], since
        live = json.dumps(wire.page.state())
        assert [t for t in traces if t in live] == []
        assert wire.page.error is None

        wire.command(type="send", text="look", attachmentIds=[scan["id"]], id="ana-scan-as-bo")
        assert wire.result("ana-scan-as-bo")["error"]["type"] == "unknown_attachment"
        mine = wire.upload("bo-scan.png")
        wire.command(type="send", text="look", attachmentIds=[mine["id"]], id="bo-send")
        assert wire.result("bo-send")["ok"] is True
        assert wire.page.wait_until(
            lambda s: [m["content"] for m in s["messages"]] == ["look", "ok"]
            and not s["isLoading"]
        )
        assert wire.page.state()["messages"][0]["attachments"] == [mine]
    finally:
        account_engine.release_slow.set()
        server.stop()
        controller.close()
        t2v.close()


class ToolEventEngine(AccountEngine):
    """``AccountEngine`` plus one platform tool that runs without an approval.

    A message holding "search" streams ``tool_event: tool_start``, waits for
    ``release_tool``, then streams ``tool_event: tool_end`` and the reply after it. The
    gate makes the turn's two halves separately observable without a sleep: the test sees
    the page on "running", opens the gate, and sees it reach "used".
    """

    def __init__(self, server: Any) -> None:
        super().__init__(server)
        self.release_tool = threading.Event()
        self.tool_started = threading.Event()

    def _handle(self, request: Any) -> Any:
        if request.path.endswith("/messages"):
            body = request.get_json(silent=True) or {}
            messages = body.get("messages") or [{}]
            if "search" in str(messages[-1].get("content", "")):
                assert body.get("supports_tool_events"), "Capabilities.tool_events off"
                return self._sse(self._platform_tool())
        return super()._handle(request)

    def _platform_tool(self) -> Iterator[str]:
        event = {
            "type": "tool_start",
            "tool_name": "web_search",
            "tool_call_id": "call-search",
            "arguments": {"query": "segmentation preset"},
        }
        yield _data({
            "id": "c",
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            "tool_event": event,
        })
        self.tool_started.set()
        self.release_tool.wait(10)
        yield _data({
            "id": "c",
            "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            "tool_event": {
                "type": "tool_end",
                "tool_name": "web_search",
                "tool_call_id": "call-search",
                "status": "ok",
            },
        })
        yield _data(_text("Here is what I found."))
        yield _data(_STOP)


def test_tool_end_reaches_a_live_events_reader_and_the_state_agrees(
    fake_engine_server: HTTPServer,
) -> None:
    """``tool_event: tool_end`` reaching a LIVE ``/events`` reader, start to finish.

    A platform tool (``Capabilities.tool_events``) that runs without an approval: the step
    goes running -> used on the page's own SSE-rebuilt state, the reply after it arrives,
    the hub's mirror and the controller's snapshot agree, and every route still answers
    on the same process afterwards.

    The reply text lands in a NEW assistant message, not the one holding the tool step:
    text after a tool call opens a new segment (``_reducer._on_text``), which is why the
    page ends this turn with two assistant messages and the step in the first of them.
    """
    engine = ToolEventEngine(fake_engine_server).start()
    t2v = Talk2View("pk_test", base_url=engine.url())
    controller = ChatController(t2v, capabilities=Capabilities(approval=True, tool_events=True))
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    server.start()
    wire = Wire(server)

    def steps(state: Dict[str, Any]) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = [s for m in state["messages"] for s in (m.get("steps") or [])]
        return found

    try:
        assert wire.page.wait_until(lambda s: "auth" in s and "messages" in s)
        wire.command(type="login", email="ana@example.com", password="pw", id="login")
        assert wire.page.wait_until(lambda s: s["auth"]["isAuthenticated"])

        wire.command(type="send", text="search the web please", id="s1")
        assert wire.page.wait_until(
            lambda s: [st["status"] for st in steps(s)] == ["running"]
        ), "the page never saw the tool start"
        assert engine.tool_started.wait(10)
        assert wire.page.error is None
        running = steps(wire.page.state())[0]
        assert running["name"] == "web_search" and running["source"] == "platform"

        # tool_end, flushed to the stream this page is reading.
        engine.release_tool.set()
        assert wire.page.wait_until(
            lambda s: [st["status"] for st in steps(s)] == ["used"] and not s["isLoading"]
        ), "the tool never finished on the page"
        assert wire.result("s1")["ok"] is True
        assert wire.page.error is None

        # The account slices (models, config, capabilities) are refreshed on their own
        # thread after the login and may still be on their way: wait, bounded, for the
        # page to catch up with the hub before comparing.
        assert wire.page.wait_until(lambda s: s == server._hub.snapshot()["state"]), (
            "the page never caught up with the hub"
        )
        state = wire.page.state()
        assert [m["content"] for m in state["messages"]] == [
            "search the web please", "", "Here is what I found."
        ]
        assert [bool(m.get("steps")) for m in state["messages"]] == [False, True, False]
        chat = dict(controller.snapshot())
        chat.pop("seq")
        assert {key: state[key] for key in chat} == chat

        # Still one live process, still serving: the page's stream, a command, and a
        # stream resumed from the seq the tool_end batch landed on.
        conn = http.client.HTTPConnection("127.0.0.1", wire.port, timeout=10)
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
        wire.command(type="send", text="hello", id="s2")
        assert wire.result("s2")["ok"] is True
        assert wire.page.wait_until(lambda s: s["messages"][-1]["content"] == "ok")
        assert wire.resume(server._hub.seq)["event"] == "ops"
        assert wire.page.error is None
    finally:
        engine.release_tool.set()
        server.stop()
        controller.close()
        t2v.close()
        engine.stop()
