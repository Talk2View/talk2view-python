"""The client's own part of a chat's first turn, against a fake engine that takes its time
the way the real one does over a WAN link, measured with the turn timing
(``talk2view.metrics``): ``setup_ms`` (the turn accepted to its request sent), the chat
page's ``queue_ms``, and the engine's wait. Also the engine's ``Server-Timing`` and
``X-Request-Id`` on each streamed request.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import ChatController, Skill, Talk2View, TurnMetrics, TurnTiming
from talk2view.metrics import MAX_SERVER_TIMINGS, parse_server_timing
from talk2view.ui import GenericHost, UIServer
from talk2view.ui.options import ChatOptions

from .fake_engine import TOOL, _error, _json
from .test_metrics import PRIVATE_MESSAGE, Host
from .ui_engine import AccountEngine
from .ui_helpers import EventStream, wait_for

STUB = Path(__file__).parent / "fixtures" / "ui_static"
SETUP_S = 0.3  # each of the create, the tools and the skills
MINT_S = 1.2  # a new end-user's virtual key, minted on their first AI call


class TimedEngine(AccountEngine):
    """``AccountEngine`` whose routes take ``delays[path]`` seconds (``"messages"`` for every
    chat message), which mints a user's key once, on their first ``POST
    /v1/account/warm`` or message, in ``mint_s`` (a request that arrives mid-mint waits for
    the rest, as on the engine), and which puts ``stream_headers`` on every stream. It
    keeps the tool names a session was sent (``tools_of``), and ``before_create``, when
    set, runs once when a ``POST /v1/sessions`` arrives, before it is answered."""

    def __init__(self, server: Optional[HTTPServer] = None) -> None:
        super().__init__(server)
        self.delays: Dict[str, float] = {}
        self.mint_s = 0.0
        self.stream_headers: Dict[str, str] = {}
        self._mints: Dict[str, threading.Lock] = {}
        self._minted: set[str] = set()
        self.before_create: Optional[Callable[[str], None]] = None

    def tools_of(self, user_id: str) -> Optional[List[str]]:
        with self.lock:
            session = self.sessions.get(user_id)
            return list(session.get("tools", [])) if session is not None else None

    def _user(self, request: Request) -> str:
        auth = request.headers.get("Authorization", "")
        with self.lock:
            return self.tokens.get(auth[len("Bearer "):], "") if auth else ""

    def _mint(self, user_id: str) -> None:
        with self.lock:
            lock = self._mints.setdefault(user_id, threading.Lock())
        with lock:
            if user_id not in self._minted:
                time.sleep(self.mint_s)
                self._minted.add(user_id)

    def _handle(self, request: Request) -> Response:
        path = request.path
        delay = self.delays.get("messages" if path.endswith("/messages") else path, 0.0)
        if delay:
            time.sleep(delay)
        if path == "/v1/sessions" and request.method == "POST" and self.before_create:
            hook, self.before_create = self.before_create, None
            hook(self._user(request))
        if path == "/v1/tools/register":
            user_id = self._user(request)
            if user_id:
                names = [t["name"] for t in (request.get_json(silent=True) or {})["tools"]]
                with self.lock:
                    self.log.append("POST {} {}".format(path, user_id))
                    self._session(user_id)["tools"] = names
                return _json({"registered": names, "count": len(names)})
        if path == "/v1/account/warm" or path.endswith("/messages"):
            user_id = self._user(request)
            if path == "/v1/account/warm":
                with self.lock:
                    self.log.append("POST {} {}".format(path, user_id or "-"))
            if user_id and self.mint_s:
                self._mint(user_id)
            if path == "/v1/account/warm":
                if not user_id:
                    return _error(401, "authentication_error", "Invalid token")
                return _json({"ready": True})
        response = super()._handle(request)
        if response.mimetype == "text/event-stream":
            response.headers.update(self.stream_headers)
        return response


@pytest.fixture
def timed_engine(fake_engine_server: HTTPServer) -> Iterator[TimedEngine]:
    fake = TimedEngine(fake_engine_server)
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


# ── setup_ms: after the create, the tools and the skills go out together ────────


def test_the_first_turn_sends_its_tools_and_skills_together_after_the_create(
    timed_engine: TimedEngine,
) -> None:
    for path in ("/v1/sessions", "/v1/tools/register", "/v1/skills/register"):
        timed_engine.delays[path] = SETUP_S
    host = Host(timed_engine)  # a tool registered, signed in
    try:
        host.t2v.skills.add(Skill(name="measure", content="how"))
        host.controller.send("hello")
        timing = host.wait_timing()
        assert timing.outcome == "done"
        assert timing.setup_ms is not None
        # Two round trips (the create, then both registers together), not three.
        assert timing.setup_ms < 1000 * SETUP_S * 2.6, timing.setup_ms
        user_id = host.t2v.auth.get_user().id  # type: ignore[union-attr]
        assert timed_engine.skills_of(user_id) == ["measure"]
        paths = timed_engine.paths()
        assert "POST /v1/tools/register " + user_id in paths
        # And the second turn pays none of it.
        host.controller.send("again")
        second = host.wait_timing(2)
        assert second.setup_ms is not None and second.setup_ms < 1000 * SETUP_S / 2
    finally:
        host.close()


def test_a_session_dropped_before_the_create_answers_still_gets_the_tools_and_skills(
    timed_engine: TimedEngine,
) -> None:
    # The engine drops the end-user's session just as the create arrives (an eviction, a
    # DELETE from their other client, a deploy): the tools and the skills must reach the
    # session the create then makes, not one that is gone, or the turn runs without them.
    def drop(user_id: str) -> None:
        time.sleep(0.05)  # anything sent beside the create has landed by now
        with timed_engine.lock:
            timed_engine.sessions.pop(user_id, None)

    timed_engine.before_create = drop
    host = Host(timed_engine)
    try:
        host.t2v.skills.add(Skill(name="measure", content="how"))
        host.controller.send("hello")
        assert host.wait_timing().outcome == "done"
        user_id = host.t2v.auth.get_user().id  # type: ignore[union-attr]
        assert timed_engine.tools_of(user_id) == [TOOL]
        assert timed_engine.skills_of(user_id) == ["measure"]
    finally:
        host.close()


# ── A guest started for a send is warmed while its turn sets up ────────────────


class Chat:
    """A signed-out ``Talk2View`` + ``ChatController`` behind a started ``UIServer`` that
    allows guests, as the 3D Slicer extension runs it, and every turn timing it delivers."""

    def __init__(self, engine: TimedEngine) -> None:
        self.t2v = Talk2View("pk_test", base_url=engine.url())
        self.t2v.metrics = TurnMetrics(page_grace_s=0.05)
        self.timings: List[TurnTiming] = []
        self.t2v.metrics.on_turn(self.timings.append)
        self.controller = ChatController(self.t2v)
        self.server = UIServer(
            self.controller, GenericHost(), static_dir=STUB,
            options=ChatOptions(allow_anonymous=True),
        )
        self.token = self.server.start().split("#t=", 1)[1]
        self.page = EventStream(self.server.port, self.token)
        assert self.page.wait_until(lambda s: "auth" in s and "messages" in s)

    def command(self, **body: Any) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        conn.request("POST", "/command", body=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.token, "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        resp.read()
        conn.close()
        assert resp.status == 202

    def close(self) -> None:
        self.server.stop()
        self.controller.close()
        self.t2v.close()


def test_a_guest_started_for_a_send_is_warmed_while_its_turn_sets_up(
    timed_engine: TimedEngine,
) -> None:
    timed_engine.delays["/v1/auth/anonymous"] = 0.2
    timed_engine.delays["/v1/sessions"] = 0.5
    timed_engine.mint_s = MINT_S
    chat = Chat(timed_engine)
    try:
        # What the page sends when a signed-out end-user presses Send, guests allowed.
        chat.command(type="startAnonymous", id="a1")
        chat.command(type="send", text="hello", timing=True, id="s1")
        assert wait_for(lambda: len(chat.timings) == 1, timeout=10.0)
        timing = chat.timings[0]
        assert timing.outcome == "done"
        paths = timed_engine.paths()
        warm = paths.index("POST /v1/account/warm anon-1")
        message = next(i for i, p in enumerate(paths) if p.endswith("/messages anon-1"))
        assert warm < message
        # The key was minting while the session was set up, so the message waited only for
        # the rest of the mint (without the warm-up it waits for all of it: MINT_S).
        assert timing.engine_wait_ms is not None
        assert timing.engine_wait_ms < 1000 * (MINT_S - 0.3), timing.engine_wait_ms
        # What stays: the send waits for the guest start it needs (the engine's time).
        assert timing.ui is not None and timing.ui.queue_ms is not None
        assert timing.ui.queue_ms >= 150
    finally:
        chat.close()


# ── The engine's Server-Timing and request id, on each request ─────────────────


def test_each_streamed_request_keeps_the_engines_server_timing_and_request_id(
    timed_engine: TimedEngine,
) -> None:
    timed_engine.stream_headers = {
        "Server-Timing": 'auth;dur=12.5, graph;dur=3;desc="first, token", cache',
        "X-Request-Id": "req-7",
    }
    host = Host(timed_engine)
    try:
        host.controller.send(PRIVATE_MESSAGE)  # the engine calls the client tool
        timing = host.wait_timing()
        assert timing.outcome == "done"
        assert timing.request.server_timing == {"auth": 12.5, "graph": 3.0}
        assert timing.request.request_id == "req-7"
        (tool,) = timing.tools
        assert tool.name == TOOL and tool.resume is not None
        assert tool.resume.server_timing == {"auth": 12.5, "graph": 3.0}
        assert tool.resume.request_id == "req-7"
        plain = json.loads(json.dumps(timing.as_dict()))
        assert plain["request"]["server_timing"] == {"auth": 12.5, "graph": 3.0}
        assert plain["tools"][0]["resume"]["server_timing"] == {"auth": 12.5, "graph": 3.0}

        timed_engine.stream_headers = {}
        host.controller.send("hello")
        second = host.wait_timing(2)
        assert second.request.server_timing is None
        assert second.as_dict()["request"]["server_timing"] is None
    finally:
        host.close()


@pytest.mark.parametrize(
    "header, parsed",
    [
        (None, None),
        ("", None),
        ("cache", None),  # no dur
        ("auth;dur=12.5", {"auth": 12.5}),
        ("auth;dur=12.54;desc=Auth", {"auth": 12.5}),
        ('db;desc="a, b; c=1";dur="4"', {"db": 4.0}),  # separators inside a quoted desc
        ("a;dur=1, a;dur=2", {"a": 1.0}),  # the first of a name wins
        ("a;DUR=1", {"a": 1.0}),
        ("a;dur=abc, b;dur=-1, c;dur=nan, d;dur=inf, e;dur=2", {"e": 2.0}),
        ("a;dur, b;dur=, =x;dur=1, ;dur=1, c;dur=3", {"c": 3.0}),
        (";;,,", None),
    ],
)
def test_parse_server_timing(header: Optional[str], parsed: Optional[Dict[str, float]]) -> None:
    assert parse_server_timing(header) == parsed


def test_parse_server_timing_is_bounded() -> None:
    many = ", ".join("m{};dur=1".format(i) for i in range(MAX_SERVER_TIMINGS + 10))
    timings = parse_server_timing(many)
    assert timings is not None and len(timings) == MAX_SERVER_TIMINGS
    long = parse_server_timing("{};dur=1".format("n" * 500))
    assert long is not None and [len(k) for k in long] == [64]


def test_parse_server_timing_keeps_the_first_of_names_alike_once_clipped() -> None:
    first, second = "p" * 64 + "-first", "p" * 64 + "-second"
    timings = parse_server_timing("{};dur=1, {};dur=2".format(first, second))
    assert timings == {"p" * 64: 1.0}
