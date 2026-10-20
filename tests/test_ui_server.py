"""talk2view.ui.server: the whole wire over real sockets, against the FakeController."""

from __future__ import annotations

import http.client
import http.server
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import httpx
import pytest

import talk2view.ui.server as ui_server
import talk2view.ui.testing as ui_testing
from talk2view import Talk2View
from talk2view.ui import AppState, GenericHost, StaticAssetsMissingError, Theme, UIServer
from talk2view.ui.app_state import _identity
from talk2view.ui.static import BUILD_INSTRUCTIONS
from talk2view.ui.testing import FakeController

from .ui_engine import AccountEngine
from .ui_helpers import EventStream, PageModel, read_data_event, read_event, wait_for

STUB = Path(__file__).parent / "fixtures" / "ui_static"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def inline(job: Callable[[], None]) -> None:
    job()


class Rig:
    def __init__(self, server: UIServer, controller: FakeController, host: GenericHost) -> None:
        self.server, self.controller, self.host = server, controller, host
        self.url = server.start()
        self.port = server.port
        self.token = self.url.split("#t=", 1)[1]

    def conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)

    def request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
        auth: bool = True,
    ) -> Tuple[int, http.client.HTTPMessage, bytes]:
        conn = self.conn()
        sent = dict(headers or {})
        if auth:
            sent.setdefault("Authorization", "Bearer " + self.token)
        conn.request(method, path, body=body, headers=sent)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp.headers, data

    def command(self, **payload: Any) -> Tuple[int, Any]:
        status, _, data = self.request(
            "POST", "/command", json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )
        return status, json.loads(data)

    def result(self, command_id: str) -> Dict[str, Any]:
        def find() -> Optional[Dict[str, Any]]:
            for item in self.server._hub.snapshot()["state"]["results"]:
                if item["id"] == command_id:
                    found: Dict[str, Any] = item
                    return found
            return None

        assert wait_for(lambda: find() is not None), command_id
        value = find()
        assert value is not None
        return value

    def stream(
        self, headers: Optional[Dict[str, str]] = None, path: str = "/events"
    ) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = self.conn()
        sent = {"Authorization": "Bearer " + self.token}
        sent.update(headers or {})
        conn.request("GET", path, headers=sent)
        return conn, conn.getresponse()


def make_rig(
    t2v: Optional[Talk2View] = None, host: Optional[GenericHost] = None, **options: Any
) -> Rig:
    controller = FakeController(executor=inline, reply="Hello there world")
    real_host = host or GenericHost()
    app = AppState(t2v, real_host) if t2v is not None else None
    server = UIServer(controller, real_host, app=app, static_dir=STUB, **options)
    return Rig(server, controller, real_host)


@pytest.fixture
def rig() -> Iterator[Rig]:
    made = make_rig()
    yield made
    made.server.stop()


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url())
    yield client
    client.close()


def raw(port: int, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(request)
        reply = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return reply
            reply += chunk


def declared_only(rig: "Rig", path: str, content_type: str, length: int) -> bytes:
    """Headers that declare ``length`` body bytes, and no body: a refusal must come unread."""
    return raw(
        rig.port,
        "POST {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n"
        "Content-Type: {}\r\nContent-Length: {}\r\n\r\n".format(
            path, rig.port, rig.token, content_type, length
        ).encode(),
    )


def upload_body(
    data: bytes = PNG, filename: str = "scan.png", part_type: str = "image/png"
) -> Tuple[bytes, str]:
    request = httpx.Request(
        "POST", "http://127.0.0.1/upload", files={"file": (filename, data, part_type)}
    )
    return request.read(), request.headers["Content-Type"]


def post_closing(rig: "Rig", path: str, content_type: str, body: bytes) -> bytes:
    """A POST the server closes after answering, read raw: a request whose handler died
    comes back as ``b""`` instead of raising here."""
    head = (
        "POST {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n"
        "Content-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n"
    ).format(path, rig.port, rig.token, content_type, len(body))
    return raw(rig.port, head.encode() + body)


def status_and_json(reply: bytes) -> Tuple[int, Any]:
    head, _, body = reply.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 "), reply[:200]
    return int(head.split(b" ", 2)[1]), json.loads(body)


def upload(rig: "Rig") -> Tuple[int, bytes]:
    body, content_type = upload_body()
    status, _, data = rig.request("POST", "/upload", body, {"Content-Type": content_type})
    return status, data


def statuses(reply: bytes) -> List[str]:
    """The status code of every response in a raw reply, in order."""
    return [
        line.split(b" ")[1].decode()
        for line in reply.split(b"\r\n")
        if line.startswith(b"HTTP/1.1 ")
    ]


def parse_reply(reply: bytes) -> Tuple[int, Dict[str, str], bytes]:
    """``(status, lower-cased headers, body)`` of a raw reply holding one response."""
    assert reply.startswith(b"HTTP/1.1 "), reply[:200]
    head, _, body = reply.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(lines[0].split(" ")[1]), headers, body


# ── Lifecycle ──


def test_start_binds_loopback_and_returns_the_tokened_url(rig: Rig) -> None:
    assert rig.url == "http://127.0.0.1:{}/#t={}".format(rig.port, rig.token)
    assert rig.port > 0 and len(rig.token) >= 43
    bound = rig.server._httpd
    assert bound is not None and bound.server_address[0] == "127.0.0.1"


def test_url_before_start_start_twice_and_start_after_stop_raise() -> None:
    server = UIServer(FakeController(), GenericHost(), static_dir=STUB)
    assert server.port == 0
    with pytest.raises(RuntimeError):
        server.url
    with pytest.raises(RuntimeError):
        server.show()  # on the caller's thread, not later on the host's
    server.stop()
    with pytest.raises(RuntimeError):
        server.start()
    running = make_rig()
    try:
        with pytest.raises(RuntimeError):
            running.server.start()
    finally:
        running.server.stop()


def test_a_second_server_cannot_reuse_a_stopped_servers_app_state(t2v: Talk2View) -> None:
    host = GenericHost()
    app = AppState(t2v, host)
    first = UIServer(FakeController(executor=inline), host, app=app, static_dir=STUB)
    first.start()
    first.stop()
    second = UIServer(FakeController(executor=inline), host, app=app, static_dir=STUB)
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError, match="single use"):
        second.start()  # loudly, not a server whose account slices never fill
    assert ui_workers(before) == []  # the failed start unwound
    assert second.port == 0
    second.stop()


def test_a_missing_bundle_is_refused_at_construction(tmp_path: Path) -> None:
    with pytest.raises(StaticAssetsMissingError):
        UIServer(FakeController(), GenericHost(), static_dir=tmp_path)


def test_show_and_set_theme() -> None:
    opened: List[str] = []
    made = make_rig(host=GenericHost(open_url=opened.append))
    try:
        made.server.show()
        assert opened == []  # posted: HostAdapter.show runs on the host's main thread only
        assert made.host.pump() == 1
        assert opened == [made.url]
        made.server.set_theme(Theme(mode="dark"))
        assert made.server._hub.snapshot()["state"]["theme"] == {"mode": "dark", "tokens": {}}
    finally:
        made.server.stop()


def test_stop_ends_open_streams_and_unsubscribes() -> None:
    made = make_rig()
    conn, resp = made.stream()
    assert read_data_event(resp) is not None  # the snapshot
    started = time.monotonic()
    made.server.stop()
    assert resp.read() == b""  # the stream ended
    assert time.monotonic() - started < 3.0
    conn.close()
    assert made.controller._subscribers == []
    made.server.stop()  # idempotent
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", made.port), timeout=1).close()


def ui_workers(exclude: Set[threading.Thread]) -> List[str]:
    return [
        t.name for t in threading.enumerate()
        if t not in exclude and t.is_alive() and t.name in ("t2v-ui-router", "t2v-ui-refresh")
    ]


@pytest.mark.parametrize("failure", ["subscribe", "http-thread"])
def test_a_start_that_fails_part_way_unwinds_everything(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    controller = FakeController(executor=inline)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    bound: List[Any] = []

    class Recording(ui_server._Server):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            bound.append(self)

    monkeypatch.setattr(ui_server, "_Server", Recording)
    if failure == "subscribe":
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("subscribe failed")

        monkeypatch.setattr(controller, "subscribe", refuse)
    else:
        real_start = threading.Thread.start

        def start(thread: threading.Thread) -> None:
            if thread.name == "t2v-ui-http":
                raise RuntimeError("can't start new thread")
            real_start(thread)

        monkeypatch.setattr(threading.Thread, "start", start)
    before = set(threading.enumerate())
    with pytest.raises(RuntimeError):
        server.start()
    monkeypatch.undo()
    assert ui_workers(before) == []
    assert controller._subscribers == []  # the hub's subscription is gone too
    assert bound[0].socket.fileno() == -1  # the listening socket is closed
    assert server.port == 0
    with pytest.raises(RuntimeError):
        server.url
    stopped = threading.Event()
    threading.Thread(target=lambda: (server.stop(), stopped.set()), daemon=True).start()
    assert stopped.wait(1.0), "stop() after a failed start hung"


@pytest.mark.parametrize("hold", ["attach", "join"])
def test_a_stop_while_starting_leaves_nothing_running(
    monkeypatch: pytest.MonkeyPatch, hold: str
) -> None:
    # stop() on another thread lands while start() is part-way, the server not yet
    # published: at "attach" before the refresh thread starts (so stop()'s detach runs
    # first), at "join" with every worker running. start() must not go on to serve.
    controller = FakeController(executor=inline)
    server = UIServer(controller, GenericHost(), static_dir=STUB)
    bound: List[Any] = []

    class Recording(ui_server._Server):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            bound.append(self)

    at_join, may_join = threading.Event(), threading.Event()
    owner: Any = server._app if hold == "attach" else controller
    name = "attach" if hold == "attach" else "subscribe"
    real = getattr(owner, name)

    def held(*args: Any, **kwargs: Any) -> Any:
        at_join.set()
        may_join.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(ui_server, "_Server", Recording)
    monkeypatch.setattr(owner, name, held)
    outcome: Dict[str, Any] = {}

    def starting() -> None:
        try:
            outcome["url"] = server.start()
        except RuntimeError as exc:
            outcome["error"] = exc

    before = set(threading.enumerate())
    starter = threading.Thread(target=starting, daemon=True)
    starter.start()
    try:
        assert at_join.wait(5)  # the router and refresh threads run; nothing is published
        stopper = threading.Thread(target=server.stop, daemon=True)
        stopper.start()
        stopper.join(5)
        assert not stopper.is_alive(), "stop() hung while start() was part-way"
        may_join.set()
        starter.join(5)
        assert not starter.is_alive(), "start() hung after a racing stop()"
        assert "url" not in outcome and isinstance(outcome.get("error"), RuntimeError)
        alive = [
            t.name for t in threading.enumerate()
            if t not in before and t.is_alive() and t.name.startswith("t2v-ui")
        ]
        assert alive == []
        assert controller._subscribers == []
        assert server.port == 0
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", bound[0].server_address[1]), timeout=1)
    finally:
        may_join.set()
        leaked = server._httpd
        if leaked is not None:  # only when start() went on to serve: stop that server here
            leaked.shutdown()
            leaked.server_close()


def test_stop_ends_a_stream_that_attached_while_it_was_shutting_down() -> None:
    # A page whose stream passed the gate before stop() and attaches during
    # httpd.shutdown(), then blocks writing a large frame the page does not read: only
    # shutting its socket unblocks the writer, so stop() must see it.
    controller = FakeController(executor=inline, reply="x" * (16 * 2**20))
    host = GenericHost()
    made = Rig(UIServer(controller, host, static_dir=STUB), controller, host)
    made.command(type="send", text="hi", id="s")
    made.result("s")  # a 16 MB message: every new page's snapshot carries it
    hub, httpd = made.server._hub, made.server._httpd
    assert httpd is not None
    real_attach, real_shutdown = hub.attach, httpd.shutdown
    at_attach, may_attach = threading.Event(), threading.Event()
    # The response too: it owns the socket once the connection hands it over, and the
    # socket closes (unblocking the writer) as soon as nothing refers to it.
    opened: List[Tuple[http.client.HTTPConnection, http.client.HTTPResponse]] = []

    def gated_attach(last_event_id: Optional[int] = None) -> Any:
        at_attach.set()
        may_attach.wait(5)
        return real_attach(last_event_id)

    def late_shutdown() -> None:
        may_attach.set()
        # Attached, tracked, and its snapshot taken: the writer is into a 16 MB write.
        assert wait_for(
            lambda: bool(made.server._streams) and any(not c.snapshot for c in hub._clients)
        )
        real_shutdown()

    def open_stream() -> None:
        opened.append(made.stream())  # reads the headers only: the snapshot stays unread

    hub.attach = gated_attach  # type: ignore[method-assign]
    httpd.shutdown = late_shutdown  # type: ignore[method-assign]
    page = threading.Thread(target=open_stream, daemon=True)
    page.start()
    try:
        assert at_attach.wait(5)  # past the gate, before stop()
        made.server.stop()
        assert wait_for(lambda: not made.server._streams, timeout=3.0), "the stream lingers"
    finally:
        may_attach.set()
        page.join(5)
        for conn, resp in opened:
            resp.close()
            conn.close()


def test_a_stopped_server_answers_503_on_a_connection_kept_alive_across_stop(
    t2v: Talk2View, account_engine: AccountEngine, caplog: pytest.LogCaptureFixture
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    command_conn, upload_conn = made.conn(), made.conn()
    for conn in (command_conn, upload_conn):
        conn.request("GET", "/healthz")
        conn.getresponse().read()  # kept alive
    made.server.stop()
    auth = {"Authorization": "Bearer " + made.token}
    body, content_type = upload_body()
    try:
        with caplog.at_level(logging.DEBUG, logger="talk2view.ui"):
            command_conn.request(
                "POST", "/command", json.dumps({"type": "cancel", "id": "late"}).encode(),
                dict(auth, **{"Content-Type": "application/json"}),
            )
            upload_conn.request(
                "POST", "/upload", body, dict(auth, **{"Content-Type": content_type})
            )
            replies = [conn.getresponse() for conn in (command_conn, upload_conn)]
            answers = [
                (r.status, r.getheader("Connection"), json.loads(r.read())["error"]["type"])
                for r in replies
            ]
    finally:
        command_conn.close()
        upload_conn.close()
    assert answers == [(503, "close", "stopped")] * 2
    assert made.controller.calls == [] and account_engine.uploads == []
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


# ── Static files and the gate ──


def test_healthz_and_static_files_carry_the_security_headers(rig: Rig) -> None:
    status, headers, body = rig.request("GET", "/healthz", auth=False)
    assert status == 200 and json.loads(body) == {"ok": True}
    status, headers, body = rig.request("GET", "/", auth=False)
    assert status == 200 and body == (STUB / "index.html").read_bytes()
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    status, headers, _ = rig.request("GET", "/assets/app.js", auth=False)
    assert status == 200 and headers["Content-Type"] == "text/javascript; charset=utf-8"
    assert rig.request("GET", "/nope.js", auth=False)[0] == 404


@pytest.mark.parametrize(
    "target", ["/../secret", "/%2e%2e/%2e%2e/etc/passwd", "/assets/..%5c..%5cx", "/index.html%00"]
)
def test_traversal_is_404(rig: Rig, target: str) -> None:
    reply = raw(
        rig.port,
        "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n".format(
            target, rig.port
        ).encode(),
    )
    assert reply.startswith(b"HTTP/1.1 404")


def test_protected_paths_need_the_token(rig: Rig) -> None:
    assert rig.request("GET", "/events", auth=False)[0] == 401
    assert rig.request("POST", "/command", b"{}", auth=False)[0] == 401
    assert rig.request("POST", "/upload", b"", auth=False)[0] == 401
    wrong = {"Authorization": "Bearer nope"}
    assert rig.request("POST", "/command", b"{}", wrong)[0] == 401
    assert rig.request("POST", "/command?t=" + rig.token, b"{}", auth=False)[0] == 401
    status, _, body = rig.request("GET", "/events", auth=False, headers={"Host": "x"})
    assert status == 403 and json.loads(body)["error"]["type"] == "forbidden"


def test_foreign_host_origin_and_options_are_403_and_never_cors(rig: Rig) -> None:
    replies = [
        rig.request("GET", "/", auth=False, headers={"Host": "evil.example:{}".format(rig.port)}),
        rig.request("POST", "/command", b"{}", {"Origin": "http://evil.example"}),
        rig.request("OPTIONS", "/command", auth=False),
    ]
    assert [status for status, _, _ in replies] == [403, 403, 403]
    for _, headers, _ in replies + [rig.request("GET", "/"), rig.request("GET", "/healthz")]:
        assert not [name for name in headers if name.lower().startswith("access-control-")]


CANCEL = '{"type":"cancel"}'


@pytest.mark.parametrize(
    "start, framing, body, status",
    [
        ("POST /command", "Content-Length: 17\r\nContent-Length: {both}\r\n", CANCEL, "400"),
        ("POST /command", "Transfer-Encoding: \r\nContent-Length: 17\r\n", CANCEL, "411"),
        ("POST /command", "Transfer-Encoding: chunked\r\n", "11\r\n" + CANCEL + "\r\n0\r\n\r\n",
         "411"),
        ("GET /", "Content-Length: {second}\r\n", "", "400"),
        ("HEAD /", "Content-Length: {second}\r\n", "", "400"),
        ("GET /healthz", "Transfer-Encoding: chunked\r\n", "0\r\n\r\n", "400"),
        ("OPTIONS /command", "Content-Length: {second}\r\n", "", "403"),
    ],
    ids=[
        "repeated-content-length", "empty-transfer-encoding", "chunked", "get-with-a-body",
        "head-with-a-body", "get-with-transfer-encoding", "options-with-a-body",
    ],
)
def test_ambiguous_framing_is_refused_and_the_connection_closed(
    rig: Rig, start: str, framing: str, body: str, status: str
) -> None:
    # Whatever follows the refused request's head is never read as a request of its own.
    second = "GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n".format(
        rig.port
    )
    head = (
        "{} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n"
        "Content-Type: application/json\r\n"
    ).format(start, rig.port, rig.token)
    head += framing.format(both=len(body) + len(second), second=len(second)) + "\r\n"
    assert statuses(raw(rig.port, (head + body + second).encode())) == [status]
    assert rig.controller.calls == []


SECURITY = {"cache-control": "no-store", "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer", "connection": "close"}


STDLIB_REFUSALS = [
    (b"GET /" + b"a" * 65532, 414, "request_uri_too_long"),  # 65,537 bytes, all read
    # A header line past the standard library's 65,536 bytes: 65,537 bytes, all read.
    (b"GET / HTTP/1.1\r\nX-Long: " + b"a" * (65537 - 8), 431, "request_header_fields_too_large"),
    (b"PUT / HTTP/1.1\r\nContent-Length: 0\r\n\r\n", 501, "not_implemented"),
    (b"GET / HTTP/1.x\r\n", 400, "bad_request"),
    (b"GET / HTTP/2.0\r\n", 505, "http_version_not_supported"),
    (b"GET / extra HTTP/1.1\r\n", 400, "bad_request"),
]
STDLIB_REFUSAL_IDS = ["414", "431", "501", "bad-version", "505", "bad-syntax"]


@pytest.mark.parametrize(
    "request_bytes, status, error_type", STDLIB_REFUSALS, ids=STDLIB_REFUSAL_IDS
)
def test_the_standard_librarys_own_errors_carry_json_and_the_security_headers(
    rig: Rig, request_bytes: bytes, status: int, error_type: str
) -> None:
    got, headers, body = parse_reply(raw(rig.port, request_bytes))
    assert got == status
    assert {name: headers.get(name) for name in SECURITY} == SECURITY
    assert headers["content-type"] == "application/json; charset=utf-8"
    answer = json.loads(body)
    assert answer["ok"] is False and answer["error"]["type"] == error_type
    assert b"HTTP/" not in body and b"extra" not in body  # the request line is never echoed


@pytest.mark.parametrize(
    "request_bytes, status, error_type", STDLIB_REFUSALS, ids=STDLIB_REFUSAL_IDS
)
def test_the_standard_librarys_error_types_never_follow_the_pythons_phrases(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, request_bytes: bytes, status: int,
    error_type: str,
) -> None:
    # CPython 3.13 renamed phrases (414 "Request-URI Too Long" became "URI Too Long"): the
    # page must get the same error.type whichever Python the host runs.
    renamed = SimpleNamespace(phrase="Renamed By Another Python")
    monkeypatch.setattr(ui_server, "HTTPStatus", lambda code: renamed)
    got, _, body = parse_reply(raw(rig.port, request_bytes))
    assert got == status
    assert json.loads(body)["error"] == {
        "type": error_type, "message": "Renamed By Another Python.",
    }


@pytest.mark.parametrize(
    "status, error_type, message",
    [
        (400, "bad_request", "Bad Request."),
        (413, "too_large", None),  # "Request Entity Too Large", "Content Too Large" on 3.13
        (414, "request_uri_too_long", None),  # "Request-URI Too Long", "URI Too Long" on 3.13
        (431, "request_header_fields_too_large", "Request Header Fields Too Large."),
        (501, "not_implemented", "Not Implemented."),
        (505, "http_version_not_supported", "HTTP Version Not Supported."),
        (418, "http_error", None),  # any other status: one stable type
        (599, "http_error", "Error."),  # not even an HTTPStatus
    ],
)
def test_every_standard_library_status_has_a_fixed_error_type(
    status: int, error_type: str, message: Optional[str]
) -> None:
    got_type, got_message = ui_server._stdlib_refusal(status)
    assert got_type == error_type
    assert got_message.endswith(".") and len(got_message) > 1
    if message is not None:
        assert got_message == message


def _stdlib_reply(request: bytes) -> bytes:
    """What a plain standard-library HTTP server on this interpreter answers ``request``."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return raw(server.server_address[1], request)
    finally:
        server.shutdown()
        server.server_close()


def _status(reply: bytes) -> Optional[bytes]:
    """The status code of a reply with a status line, or None for a bare HTTP/0.9 body."""
    return reply.split(b" ", 2)[1] if reply.startswith(b"HTTP/") else None


def test_a_genuine_http_0_9_request_keeps_the_standard_librarys_answer(rig: Rig) -> None:
    # HTTP/0.9 has no status line or headers. Python's own answer to a non-GET one changed
    # in a security release (3.13.7 answers with a bare HTML body, 3.13.16 with a full 400
    # response), so the UI server is held to whatever this interpreter's standard library
    # answers, not to one version's.
    request = b"POST /command\r\n"
    reply, stdlib = raw(rig.port, request), _stdlib_reply(request)
    assert _status(reply) == _status(stdlib), (reply[:60], stdlib[:60])
    if _status(stdlib) is None:
        assert reply.upper().startswith(b"<!DOCTYPE HTML")


def test_head_serves_static_files_and_healthz_and_refuses_the_rest(rig: Rig) -> None:
    status, headers, body = rig.request("HEAD", "/", auth=False)
    assert (status, body) == (200, b"")
    assert headers["Content-Length"] == str(len((STUB / "index.html").read_bytes()))
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert headers["Content-Security-Policy"].startswith("default-src 'self'")
    assert headers["X-Content-Type-Options"] == "nosniff"
    status, _, body = rig.request("HEAD", "/healthz", auth=False)
    assert (status, body) == (200, b"")
    assert rig.request("HEAD", "/nope.js", auth=False)[0] == 404
    for path, allowed in (("/events", "GET"), ("/command", "POST"), ("/upload", "POST")):
        status, headers, body = rig.request("HEAD", path)
        assert (status, headers["Connection"], headers["Allow"], body) == (
            405, "close", allowed, b""
        ), path


def test_expect_100_continue_runs_the_gate_first(rig: Rig) -> None:
    head = (
        "POST /command HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n{}Expect: 100-continue\r\n"
        "Content-Type: application/json\r\nContent-Length: 17\r\n\r\n"
    )
    refused = raw(rig.port, head.format(rig.port, "").encode())
    assert statuses(refused) == ["401"]  # never a 100 that invites the body
    allowed = "Authorization: Bearer {}\r\nConnection: close\r\n".format(rig.token)
    accepted = raw(rig.port, (head.format(rig.port, allowed) + CANCEL).encode())
    assert statuses(accepted) == ["100", "202"]


# ── Commands ──


def test_a_command_is_accepted_then_answered_in_the_results_slice(rig: Rig) -> None:
    status, body = rig.command(type="send", text="hi", id="c-1")
    assert (status, body) == (202, {"ok": True, "id": "c-1"})
    assert rig.result("c-1") == {"id": "c-1", "type": "send", "ok": True}
    status, body = rig.command(type="cancel")
    assert (status, body) == (202, {"ok": True, "id": None})
    status, _ = rig.command(type="edit", messageId="ghost", text="x", id="c-2")
    assert status == 202
    assert rig.result("c-2")["error"]["type"] == "not_found"


def test_command_rejections(rig: Rig) -> None:
    status, _, body = rig.request("POST", "/command", b'{"type":"cancel"}',
                                  {"Content-Type": "text/plain"})
    assert status == 415
    status, _, body = rig.request("POST", "/command", b"{nope",
                                  {"Content-Type": "application/json"})
    assert (status, json.loads(body)["error"]["type"]) == (400, "bad_request")
    status, body_json = rig.command(type="explode")
    assert (status, body_json["error"]["type"]) == (400, "bad_command")
    reply = declared_only(rig, "/command", "application/json", 2 * 2**20 + 1)
    assert reply.startswith(b"HTTP/1.1 413")
    reply = raw(
        rig.port,
        "POST /command HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nAuthorization: Bearer {}\r\n"
        "Content-Type: application/json\r\nConnection: close\r\n\r\n".format(
            rig.port, rig.token
        ).encode(),
    )
    assert reply.startswith(b"HTTP/1.1 411")


def test_a_full_command_queue_is_503_queue_full_and_keeps_the_connection() -> None:
    made = make_rig()
    made.server._router._max_queued = 2
    gate = threading.Event()
    made.server._router.submit_job(gate.wait)  # the router sits on it: commands queue up
    try:
        conn = made.conn()
        answers = []
        for i in range(3):
            conn.request("POST", "/command", json.dumps({"type": "cancel", "id": str(i)}),
                         {"Authorization": "Bearer " + made.token,
                          "Content-Type": "application/json"})
            resp = conn.getresponse()
            answers.append((resp.status, resp.getheader("Connection"), json.loads(resp.read())))
        conn.close()
        assert [(status, connection) for status, connection, _ in answers[:2]] == [
            (202, None), (202, None)
        ]
        assert answers[2] == (503, None, {"ok": False, "error": {
            "type": "queue_full", "message": "Too many commands are waiting; try again."
        }})  # the body was read: the connection stays open for the retry
        gate.set()
        assert [made.result(str(i))["ok"] for i in range(2)] == [True, True]
        assert [name for name, _ in made.controller.calls] == ["cancel", "cancel"]
    finally:
        gate.set()
        made.server.stop()


def test_a_router_halted_by_host_code_answers_503_stopped_not_queue_full(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A BaseException out of host code halts the router for good: a page told "try
    again" would retry for ever."""
    made = make_rig()
    made.controller.raise_on["clear_error"] = KeyboardInterrupt()
    try:
        with caplog.at_level(logging.ERROR, logger="talk2view.ui"):
            assert made.command(type="clearError", id="halts")[0] == 202
            assert made.result("halts")["error"]["type"] == "internal_error"
            assert wait_for(lambda: not made.server._router._thread.is_alive())  # type: ignore[union-attr]
        conn = made.conn()
        conn.request("POST", "/command", json.dumps({"type": "cancel", "id": "after"}),
                     {"Authorization": "Bearer " + made.token,
                      "Content-Type": "application/json"})
        resp = conn.getresponse()
        answer = (resp.status, resp.getheader("Connection"), json.loads(resp.read()))
        conn.close()
        assert answer == (503, "close", {"ok": False, "error": {
            "type": "stopped", "message": "This chat has stopped.",
        }})
        assert [name for name, _ in made.controller.calls] == ["clear_error"]
    finally:
        made.server.stop()


def test_a_chat_batch_the_hub_loses_is_recovered_over_the_wire() -> None:
    """The hub asks for a rebase when its chat line has a gap; the server's router answers
    it, and a page following the stream ends on the controller's state."""
    controller = FakeController(executor=inline, reply="one two three four")
    host = GenericHost()
    server = UIServer(controller, host, static_dir=STUB)
    hub = server._hub
    follow = hub.on_chat_batch
    lost: List[int] = []

    def losing_one(batch: Any) -> None:
        if not lost and any(op["type"] == "append-text" for op in batch["ops"]):
            lost.append(batch["seq"])  # never reaches the mirror: the next batch is a gap
            return
        follow(batch)

    hub.on_chat_batch = losing_one  # type: ignore[method-assign]  # before join() subscribes
    made = Rig(server, controller, host)
    page = EventStream(made.port, made.token)
    try:
        assert page.wait_until(lambda s: "messages" in s)
        made.command(type="send", text="hi", id="s")
        assert page.wait_until(
            lambda s: len(s["messages"]) == 2
            and s["messages"][1]["content"] == "one two three four"
            and not s["isLoading"],
            timeout=5.0,
        ), "the page never recovered the lost batch"
        assert lost and page.error is None
        assert page.state() == hub.snapshot()["state"]
        chat = {k: v for k, v in controller.snapshot().items() if k != "seq"}
        assert {k: page.state()[k] for k in chat} == chat
    finally:
        made.server.stop()


def test_http_threads_never_call_the_controller(rig: Rig) -> None:
    threads: List[str] = []
    for name in ("send", "cancel", "clear_error"):
        original = getattr(rig.controller, name)

        def wrapped(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            threads.append(threading.current_thread().name)
            return _original(*args, **kwargs)

        setattr(rig.controller, name, wrapped)
    rig.command(type="send", text="hi", id="a")
    rig.command(type="cancel", id="b")
    rig.command(type="clearError", id="c")
    rig.result("c")
    assert threads == ["t2v-ui-router"] * 3


@pytest.mark.parametrize(
    "body",
    [
        b'{"type":"cancel","id":NaN}',
        b'{"type":"cancel","id":Infinity}',
        b'{"type":"cancel","id":-Infinity}',
        # parse_command does not look inside updatedInput: only the JSON reader can refuse.
        b'{"type":"approve","toolCallId":"t","action":"once","updatedInput":{"x":NaN}}',
        # Valid JSON numbers that overflow a float to an infinity.
        b'{"type":"approve","toolCallId":"t","action":"once","updatedInput":{"x":1e999}}',
        b'{"type":"approve","toolCallId":"t","action":"once","updatedInput":{"x":-1e999}}',
        b'{"type":"approve","toolCallId":"t","action":"once","updatedInput":{"x":1E400}}',
        b"[" * 100_000 + b"]" * 100_000,
        b'{"type":"approve","toolCallId":"t","action":"once","updatedInput":'
        + b'{"a":' * 100_000 + b"1" + b"}" * 100_001,
    ],
    ids=[
        "nan", "infinity", "minus-infinity", "nan-inside", "1e999", "minus-1e999", "1E400",
        "deep-list", "deep-object",
    ],
)
def test_a_command_body_json_cannot_carry_is_a_bad_request(rig: Rig, body: bytes) -> None:
    # JSON.parse on the page has no NaN or Infinity, and deep nesting raises RecursionError.
    status, answer = status_and_json(post_closing(rig, "/command", "application/json", body))
    assert (status, answer["error"]["type"]) == (400, "bad_request")
    assert rig.controller.calls == []


def test_a_command_parse_failure_is_500_and_logs_its_type_only(
    rig: Rig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Until parse_command wraps it in a Secret, the password is plain text in the body and
    # the payload: no traceback (whose frames a tool may print with their locals) and no
    # exception message may reach the log.
    def broken(payload: Any) -> Any:
        raise RuntimeError("parser bug near " + payload["password"])

    monkeypatch.setattr("talk2view.ui.server.parse_command", broken)
    body = json.dumps({"type": "login", "email": "a@b.c", "password": "hunter2-secret"})
    with caplog.at_level(logging.DEBUG):
        reply = post_closing(rig, "/command", "application/json", body.encode())
    assert [record for record in caplog.records if record.exc_info] == []
    assert "hunter2-secret" not in caplog.text
    assert "RuntimeError" in caplog.text
    status, answer = status_and_json(reply)
    assert (status, answer["error"]["type"]) == (500, "internal_error")
    assert b"hunter2-secret" not in reply


def test_a_bad_command_is_answered_by_its_message_and_never_logged_with_a_traceback(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    body = {"type": "login", "email": "a@b.c", "password": "hunter2-secret", "extra": 1}
    with caplog.at_level(logging.DEBUG):
        status, answer = rig.command(**body)
    assert (status, answer["error"]) == (
        400, {"type": "bad_command", "message": "login does not take 'extra'"}
    )
    assert [record for record in caplog.records if record.exc_info] == []
    assert "hunter2-secret" not in caplog.text


def test_the_command_handler_never_holds_the_body_or_the_payload(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A traceback raised later in _command would show its frame's locals (Sentry, rich,
    # pytest -l): until parse_command wraps it in a Secret the password is plain text in the
    # body and the payload, so only the frame that parses them may hold them.
    seen: Dict[str, Any] = {}
    real_submit = rig.server._router.submit

    def submit(command: Any) -> bool:
        caller = sys._getframe(1)
        seen["function"] = caller.f_code.co_name
        seen["locals"] = dict(caller.f_locals)
        return real_submit(command)

    monkeypatch.setattr(rig.server._router, "submit", submit)
    status, _ = rig.command(type="login", email="a@b.c", password="hunter2-secret", id="l")
    assert status == 202 and seen["function"] == "_command"
    assert not {"body", "payload"} & set(seen["locals"])
    assert "hunter2-secret" not in repr(seen["locals"])


# ── The event stream ──


def test_a_stream_starts_with_a_snapshot_and_ops_rebuild_the_state(rig: Rig) -> None:
    conn, resp = rig.stream()
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream; charset=utf-8"
    assert resp.headers["Cache-Control"] == "no-store"
    first = read_event(resp)
    assert first is not None and first.get("comments") == ": t2v-ui stream open\n"
    page = PageModel()
    event = read_data_event(resp)
    assert event is not None and event["event"] == "snapshot"
    page.feed(event["event"], int(event["id"]), event["data"])
    assert page.state is not None
    assert {"messages", "isLoading", "auth", "models", "theme", "results"} <= set(page.state)
    rig.command(type="send", text="/approve go", id="s")
    while page.state is None or page.state.get("pendingApproval") is None:
        event = read_data_event(resp)
        assert event is not None, "the stream ended early"
        page.feed(event["event"], int(event["id"]), event["data"])
    pending = page.state["pendingApproval"]
    rig.command(type="approve", toolCallId=pending["toolCallId"], action="once", id="a")
    while not (page.state["results"] and page.state["results"][-1]["id"] == "a"):
        event = read_data_event(resp)
        assert event is not None
        page.feed(event["event"], int(event["id"]), event["data"])
    conn.close()
    assert page.state == rig.server._hub.snapshot()["state"]
    assert page.state["messages"][1]["steps"][0]["status"] == "used"
    chat = {k: v for k, v in rig.controller.snapshot().items() if k != "seq"}
    assert {k: page.state[k] for k in chat} == chat


def test_a_lone_surrogate_in_the_chat_reaches_the_page_as_a_replacement_character() -> None:
    # Engine text (a streamed reply, an error message) can carry a lone surrogate, which
    # UTF-8 cannot encode. The stream must not die on it: the page would resume from the
    # same event, and die on it again, forever.
    controller = FakeController(executor=inline, reply="bad \ud800 half")
    host = GenericHost()
    made = Rig(UIServer(controller, host, static_dir=STUB), controller, host)
    try:
        conn, resp = made.stream()
        page = PageModel()
        event = read_data_event(resp)  # read_event decodes every line as strict UTF-8
        assert event is not None
        page.feed(event["event"], int(event["id"]), event["data"])
        made.command(type="send", text="hi", id="s")
        made.command(type="clearError", id="next")
        while page.state is None or not (
            page.state["results"] and page.state["results"][-1]["id"] == "next"
        ):
            event = read_data_event(resp)
            assert event is not None, "the stream ended"
            page.feed(event["event"], int(event["id"]), event["data"])
        conn.close()
        assert page.state["messages"][1]["content"] == "bad \ufffd half"
        # A page connecting now gets it in its snapshot, as bytes that are strict UTF-8.
        conn, resp = made.stream()
        assert resp.readline() == b": t2v-ui stream open\n" and resp.readline() == b"\n"
        frame = b"".join(resp.readline() for _ in range(4))  # event, id, data, blank line
        conn.close()
        text = frame.decode("utf-8")
        assert text.startswith("event: snapshot\n") and "bad \ufffd half" in text
    finally:
        made.server.stop()


def test_heartbeats_keep_an_idle_stream_alive() -> None:
    made = make_rig(heartbeat_s=0.1)
    try:
        conn, resp = made.stream()
        read_event(resp)  # stream open
        read_data_event(resp)  # snapshot
        event = read_event(resp)
        assert event == {"comments": ": hb\n"}
        conn.close()
    finally:
        made.server.stop()


def test_resume_with_last_event_id_replays_instead_of_a_snapshot(rig: Rig) -> None:
    conn, resp = rig.stream()
    page = PageModel()
    event = read_data_event(resp)
    assert event is not None
    page.feed(event["event"], int(event["id"]), event["data"])
    conn.close()
    rig.command(type="send", text="while away", id="w")
    rig.result("w")
    conn, resp = rig.stream({"Last-Event-ID": str(page.seq)})
    event = read_data_event(resp)
    assert event is not None and event["event"] == "ops"
    assert json.loads(event["data"])["base"] == page.seq
    page.feed(event["event"], int(event["id"]), event["data"])
    conn.close()
    assert page.state == rig.server._hub.snapshot()["state"]


@pytest.mark.parametrize("header", ["abc", "-1", "99999999", "1e3"])
def test_an_unusable_last_event_id_gets_a_snapshot(rig: Rig, header: str) -> None:
    conn, resp = rig.stream({"Last-Event-ID": header})
    event = read_data_event(resp)
    conn.close()
    assert event is not None and event["event"] == "snapshot"


def test_the_query_token_opens_a_stream_for_eventsource(rig: Rig) -> None:
    conn = rig.conn()
    conn.request("GET", "/events?t=" + rig.token)
    resp = conn.getresponse()
    assert resp.status == 200
    event = read_data_event(resp)
    conn.close()
    assert event is not None and event["event"] == "snapshot"


def test_too_many_streams_is_503() -> None:
    made = make_rig(max_clients=1)
    try:
        conn, resp = made.stream()
        read_data_event(resp)
        status, _, body = made.request("GET", "/events")
        assert status == 503 and json.loads(body)["error"]["type"] == "too_many_streams"
        conn.close()
    finally:
        made.server.stop()


# ── Uploads ──


def test_upload_then_send_with_the_attachment(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    try:
        body, content_type = upload_body()
        status, _, data = made.request("POST", "/upload", body, {"Content-Type": content_type})
        assert status == 200, data
        attachment = json.loads(data)
        assert set(attachment) == {"id", "filename", "mime_type", "size_bytes"}
        assert attachment["filename"] == "scan.png" and attachment["size_bytes"] == len(PNG)
        assert account_engine.uploads[0]["data"] == PNG
        made.command(type="send", text="look", attachmentIds=[attachment["id"]], id="s")
        assert made.result("s")["ok"] is True
        sent = made.controller.calls[-1][1][1]
        assert [a.id for a in sent] == [attachment["id"]]
    finally:
        made.server.stop()


def test_uploads_run_on_the_upload_worker(t2v: Talk2View) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    seen: List[str] = []
    original = t2v.upload_attachment

    def recording(*args: Any, **kwargs: Any) -> Any:
        seen.append(threading.current_thread().name)
        return original(*args, **kwargs)

    t2v.upload_attachment = recording  # type: ignore[method-assign]
    try:
        body, content_type = upload_body()
        assert made.request("POST", "/upload", body, {"Content-Type": content_type})[0] == 200
        assert len(seen) == 1 and seen[0].startswith("t2v-ui-upload")
    finally:
        made.server.stop()


def test_upload_rejections(t2v: Talk2View) -> None:
    made = make_rig(t2v, max_upload_bytes=1000)
    try:
        body, content_type = upload_body()
        headers = {"Content-Type": content_type}
        # Signed out: the engine refuses, and its type comes through as a 502.
        status, _, data = made.request("POST", "/upload", body, headers)
        assert status == 502 and json.loads(data)["error"]["type"] == "authentication_error"
        t2v.auth.login("ana@example.com", "pw")
        big, big_type = upload_body(b"x" * 2000)
        assert made.request("POST", "/upload", big, {"Content-Type": big_type})[0] == 413
        huge = declared_only(made, "/upload", content_type, 1000 + 64 * 1024 + 1)
        assert huge.startswith(b"HTTP/1.1 413")  # refused unread
        html, html_type = upload_body(b"<p>", "page.html", "text/html")
        assert made.request("POST", "/upload", html, {"Content-Type": html_type})[0] == 415
        guessed, guessed_type = upload_body(PNG, "scan.png", "application/octet-stream")
        assert made.request("POST", "/upload", guessed, {"Content-Type": guessed_type})[0] == 200
        status, _, data = made.request("POST", "/upload", b"junk", headers)
        assert status == 400 and json.loads(data)["error"]["type"] == "bad_upload"
    finally:
        made.server.stop()


def test_upload_without_an_account_is_503(rig: Rig) -> None:
    body, content_type = upload_body()
    status, _, data = rig.request("POST", "/upload", body, {"Content-Type": content_type})
    assert status == 503 and json.loads(data)["error"]["type"] == "account_unavailable"


def test_an_attachment_is_sent_only_by_the_account_that_uploaded_it(t2v: Talk2View) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    try:
        status, data = upload(made)
        assert status == 200, data
        attachment_id = json.loads(data)["id"]
        made.command(type="send", text="look", attachmentIds=["ghost"], id="missing")
        missing = made.result("missing")["error"]
        t2v.auth.login("bob@example.com", "pw")  # another account, with no sign-out between
        made.command(type="send", text="look", attachmentIds=[attachment_id], id="as-bob")
        # Refused exactly as an id never uploaded: the page learns nothing of Ana's.
        assert made.result("as-bob")["error"] == {
            "type": "unknown_attachment",
            "message": missing["message"].replace("ghost", attachment_id),
        }
        assert made.controller.calls == []
        t2v.auth.login("ana@example.com", "pw")
        made.command(type="send", text="look", attachmentIds=[attachment_id], id="as-ana")
        assert made.result("as-ana")["ok"] is True
    finally:
        made.server.stop()


@pytest.mark.parametrize("change", ["switch", "sign-out", "unreadable"])
def test_an_upload_the_account_changed_under_is_refused(
    t2v: Talk2View, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    original = t2v.upload_attachment
    uploaded: List[Any] = []

    def unreadable() -> Any:
        raise RuntimeError("the token store cannot be read")

    def racing(*args: Any, **kwargs: Any) -> Any:
        uploaded.append(original(*args, **kwargs))
        if change == "switch":
            t2v.auth.login("bob@example.com", "pw")
        elif change == "sign-out":
            t2v.auth.logout()
        else:
            monkeypatch.setattr(t2v.auth, "get_user", unreadable)
        return uploaded[-1]

    monkeypatch.setattr(t2v, "upload_attachment", racing)
    try:
        status, data = upload(made)
        assert status == 409, data
        assert json.loads(data)["error"]["type"] == "account_changed"
        assert b"user-" not in data and b"example.com" not in data  # no identity named
        monkeypatch.undo()
        t2v.auth.login("ana@example.com", "pw")
        made.command(type="send", text="look", attachmentIds=[uploaded[0].id], id="s")
        assert made.result("s")["error"]["type"] == "unknown_attachment"  # never filed
    finally:
        made.server.stop()


def test_a_first_upload_may_start_a_guest_session(account_engine: AccountEngine) -> None:
    guest = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=True)
    made = make_rig(guest)
    try:
        status, data = upload(made)  # signed out before it, a guest after it: allowed
        assert status == 200, data
        assert guest.auth.is_anonymous()
        made.command(type="send", text="look", attachmentIds=[json.loads(data)["id"]], id="s")
        assert made.result("s")["ok"] is True
    finally:
        made.server.stop()
        guest.close()


@pytest.mark.parametrize("store", ["unreadable-throughout", "guest-then-unreadable", "no-user"])
def test_an_upload_no_signed_in_user_can_own_is_refused(
    account_engine: AccountEngine, monkeypatch: pytest.MonkeyPatch, store: str
) -> None:
    # "no-user": a stored token whose user is missing or garbled reads as (True, None),
    # which two different identities could share.
    guest = store == "guest-then-unreadable"
    client = Talk2View("pk_test", base_url=account_engine.url(), anonymous_auto_start=guest)
    if not guest:
        client.auth.login("ana@example.com", "pw")
    made = make_rig(client)
    original = client.upload_attachment
    uploaded: List[Any] = []

    def unreadable() -> Any:
        raise RuntimeError("the token store cannot be read")

    def uploading(*args: Any, **kwargs: Any) -> Any:
        uploaded.append(original(*args, **kwargs))  # a guest session starts in here
        if guest:
            monkeypatch.setattr(client.auth, "get_user", unreadable)
        return uploaded[-1]

    if store == "unreadable-throughout":
        monkeypatch.setattr(client.auth, "get_user", unreadable)
    elif store == "no-user":
        monkeypatch.setattr(client.auth, "get_user", lambda: None)
    monkeypatch.setattr(client, "upload_attachment", uploading)
    try:
        status, data = upload(made)
        assert status == 409, data
        assert json.loads(data)["error"]["type"] == "account_changed"
        monkeypatch.undo()
        made.command(type="send", text="look", attachmentIds=[uploaded[0].id], id="s")
        assert made.result("s")["error"]["type"] == "unknown_attachment"  # never filed
    finally:
        made.server.stop()
        client.close()


def test_uploads_are_checked_and_filed_on_the_upload_worker(
    t2v: Talk2View, monkeypatch: pytest.MonkeyPatch
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    made = make_rig(t2v)
    identity: List[str] = []
    filed: List[str] = []
    registry = made.server._uploads
    original_add = registry.add

    def reading(given: Any) -> Any:
        identity.append(threading.current_thread().name)
        return _identity(given)

    def filing(*args: Any, **kwargs: Any) -> None:
        filed.append(threading.current_thread().name)
        original_add(*args, **kwargs)

    monkeypatch.setattr("talk2view.ui.server._identity", reading, raising=False)
    registry.add = filing  # type: ignore[method-assign]
    try:
        assert upload(made)[0] == 200
        assert len(identity) == 2 and all(n.startswith("t2v-ui-upload") for n in identity)
        assert len(filed) == 1 and filed[0].startswith("t2v-ui-upload")
    finally:
        made.server.stop()


# ── The request log ──


def test_the_log_has_paths_and_statuses_but_never_a_query_or_the_token(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="talk2view.ui"):
        conn = rig.conn()
        conn.request("GET", "/events?t=" + rig.token)
        resp = conn.getresponse()
        read_data_event(resp)
        conn.close()
        rig.request("GET", "/healthz?t=" + rig.token, auth=False)
        for line in (
            b"GET /events?t=%s HTTP/1.x\r\n",  # 400: bad version
            b"GET /events?t=%s HTTP/2.0\r\n",  # 505
            b"GET /events?t=%s extra HTTP/1.1\r\n",  # 400: its error echoes the whole line
        ):
            raw(rig.port, line % rig.token.encode())
        assert wait_for(lambda: "http GET /healthz 200" in caplog.text)
    text = caplog.text
    assert "http GET /events 200" in text
    assert rig.token not in text
    assert "?" not in text


@pytest.mark.parametrize(
    "request_line", ["GET?t={token} / HTTP/1.1", "X{token} /events HTTP/1.1"],
    ids=["query-in-the-method", "made-up-method"],
)
def test_a_method_the_server_does_not_know_is_never_logged(
    rig: Rig, caplog: pytest.LogCaptureFixture, request_line: str
) -> None:
    # The method is the page's own text, as the query is: one the server does not know
    # (a 501) is logged as a placeholder, so the token can never reach a log that way.
    head = request_line.format(token=rig.token) + "\r\nHost: 127.0.0.1:{}\r\n".format(rig.port)
    with caplog.at_level(logging.DEBUG, logger="talk2view.ui"):
        reply = raw(rig.port, (head + "Connection: close\r\n\r\n").encode())
    assert statuses(reply) == ["501"]
    assert "http <other> " in caplog.text  # the request was logged, without its method
    for record in caplog.records:
        assert rig.token not in record.getMessage() and rig.token not in repr(record.args)
    assert rig.token not in caplog.text


# ── python -m talk2view.ui.testing ──


def test_the_launcher_docs_name_the_directory_the_build_writes() -> None:
    built = "src/talk2view/ui/static"
    assert built in BUILD_INSTRUCTIONS
    assert built in (ui_testing.__doc__ or "")
    assert "sdk-embed/dist" not in (ui_testing.__doc__ or "")


@pytest.mark.skipif(os.name == "nt", reason="SIGINT to a child process is POSIX-only here")
def test_the_testing_launcher_serves_a_page_until_interrupted() -> None:
    proc = subprocess.Popen(  # noqa: S603 — fixed interpreter and module, no shell
        [sys.executable, "-m", "talk2view.ui.testing", "--static-dir", str(STUB),
         "--no-browser", "--chunk-delay", "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        url = proc.stdout.readline().strip()
        assert url.startswith("http://127.0.0.1:") and "#t=" in url
        port = int(url.split(":")[2].split("/")[0])
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
        proc.send_signal(signal.SIGINT)
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
