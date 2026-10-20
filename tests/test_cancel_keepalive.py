"""Cancellation against a KEEP-ALIVE HTTP/1.1 server, like the production ALB.

``pytest_httpserver`` answers HTTP/1.0 without keep-alive, so every request in
those tests gets a fresh socket and the whole class of "this cancel shut down a
socket another request was already using" bugs is invisible there. These tests
use a stdlib ``ThreadingHTTPServer`` that really does pool connections, and
record the client port of every request so connection reuse is asserted, not
assumed.

Synchronisation is by ``threading.Event`` with bounded waits — never a sleep —
so a regression fails instead of hanging.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest

from talk2view.cancel import Cancelled, CancelToken
from talk2view.client import T2VClient
from talk2view.storage import MemoryStorage

NEW_TOKENS = {
    "access_token": "new_tok",
    "refresh_token": "new_ref",
    "token_type": "bearer",
    "expires_in": 3600,
}

SSE_CHUNK = (
    'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"},'
    '"finish_reason":null}]}\n\n'
)
SSE_BODY = SSE_CHUNK + "data: [DONE]\n\n"
# A stream that pauses for a tool call or an approval ends WITHOUT [DONE]: the
# body simply runs out, so the reader consumes it fully and httpx pools the
# connection for the /resume round-trip that follows.
SSE_PAUSED_BODY = SSE_CHUNK


def _send(handler: BaseHTTPRequestHandler, status: int, body: bytes, content_type: str) -> None:
    """Write a Content-Length framed response, so the connection stays alive."""
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    _send(handler, status, json.dumps(payload).encode(), "application/json")


def send_sse(handler: BaseHTTPRequestHandler, body: str) -> None:
    _send(handler, 200, body.encode(), "text/event-stream")


def unauthorized(handler: BaseHTTPRequestHandler) -> None:
    send_json(
        handler, 401, {"error": {"type": "authentication_error", "message": "Expired"}}
    )


Route = Callable[[BaseHTTPRequestHandler], None]


class KeepAliveServer:
    """A threaded HTTP/1.1 server with per-path routes and a request log."""

    def __init__(self) -> None:
        self.routes: Dict[str, Route] = {}
        # (path, client port, Authorization header) per request, in order.
        self.log: List[Tuple[str, int, Optional[str]]] = []
        self.bodies: Dict[str, List[Any]] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                """Keep the test output pristine."""

            def do_POST(self) -> None:
                server._dispatch(self)

            def do_GET(self) -> None:
                server._dispatch(self)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self.url = "http://127.0.0.1:{}".format(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def route(self, path: str, handler: Route) -> None:
        self.routes[path] = handler

    def _dispatch(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length", "0"))
        raw = handler.rfile.read(length) if length else b""
        path = handler.path
        self.log.append((path, handler.client_address[1], handler.headers.get("Authorization")))
        if raw:
            try:
                self.bodies.setdefault(path, []).append(json.loads(raw))
            except ValueError:  # pragma: no cover — bodies here are always JSON
                self.bodies.setdefault(path, []).append(raw)
        route = self.routes.get(path)
        if route is None:  # pragma: no cover — every test routes what it calls
            send_json(handler, 404, {"error": {"type": "not_found", "message": path}})
            return
        route(handler)

    def paths(self) -> List[str]:
        return [entry[0] for entry in self.log]

    def ports(self) -> List[int]:
        return [entry[1] for entry in self.log]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(5)


@pytest.fixture()
def server() -> Iterator[KeepAliveServer]:
    keep_alive = KeepAliveServer()
    yield keep_alive
    keep_alive.close()


def _make_client(server: KeepAliveServer) -> Tuple[T2VClient, MemoryStorage]:
    storage = MemoryStorage()
    storage.set("access_token", "old_tok")
    storage.set("refresh_token", "old_ref")
    return T2VClient("pk_test", storage, server.url), storage


class TestCancelDuringRefresh:
    """A stream cancel must never abort the token refresh it triggered.

    The 401'd stream and the refresh share one pooled connection. While the
    stream's abort closer stayed registered across the refresh, a ``cancel()``
    landing in that window shut the socket down *after* the server had already
    rotated the refresh token: the client kept the consumed pair and the next
    refresh was doomed — a permanent transient loop, or a sign-out.
    """

    def test_cancel_during_the_refresh_still_stores_the_rotated_pair(
        self, server: KeepAliveServer
    ) -> None:
        in_refresh = threading.Event()
        cancel_landed = threading.Event()

        def refresh(handler: BaseHTTPRequestHandler) -> None:
            in_refresh.set()
            assert cancel_landed.wait(5), "cancel() never landed"
            send_json(handler, 200, dict(NEW_TOKENS))

        server.route("/v1/sessions/s1/messages", unauthorized)
        server.route("/v1/auth/refresh", refresh)
        client, storage = _make_client(server)
        token = CancelToken()
        outcome: Dict[str, Any] = {}

        def consume() -> None:
            try:
                list(
                    client.stream_request(
                        "/v1/sessions/s1/messages", {"messages": []}, cancel=token
                    )
                )
                outcome["ended"] = "clean"
            except Cancelled:
                outcome["ended"] = "cancelled"
            except Exception as exc:  # pragma: no cover — failure detail only
                outcome["ended"] = repr(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert in_refresh.wait(5), "the refresh request never arrived"
            token.cancel()
            cancel_landed.set()
            worker.join(5)

            assert not worker.is_alive(), "the stream never ended"
            # The 401 and the refresh shared ONE pooled connection: the setup
            # that made the abort reach the refresh's socket.
            assert server.paths() == ["/v1/sessions/s1/messages", "/v1/auth/refresh"]
            assert len(set(server.ports())) == 1, server.log
            # The server rotated, so the client MUST hold the new pair.
            assert server.bodies["/v1/auth/refresh"] == [{"refresh_token": "old_ref"}]
            assert storage.get("access_token") == "new_tok"
            assert storage.get("refresh_token") == "new_ref"
            # Only then is the cancel honoured — no retry stream was opened.
            assert outcome == {"ended": "cancelled"}
        finally:
            cancel_landed.set()
            client.close()

    def test_cancel_during_a_transient_refresh_ends_quietly(
        self, server: KeepAliveServer
    ) -> None:
        """A stream cancelled while its 401 refresh is in flight, and the
        refresh turns out transient (409 — already rotated elsewhere), must
        end with ``Cancelled`` rather than the "please retry" ``NetworkError``:
        the caller already asked to stop, so a cancelled turn should end
        quietly instead of surfacing a spurious network error.
        """
        in_refresh = threading.Event()
        cancel_landed = threading.Event()

        def refresh(handler: BaseHTTPRequestHandler) -> None:
            in_refresh.set()
            assert cancel_landed.wait(5), "cancel() never landed"
            send_json(
                handler,
                409,
                {"error": {"type": "refresh_conflict", "message": "already rotated"}},
            )

        server.route("/v1/sessions/s1/messages", unauthorized)
        server.route("/v1/auth/refresh", refresh)
        client, storage = _make_client(server)
        token = CancelToken()
        outcome: Dict[str, Any] = {}

        def consume() -> None:
            try:
                list(
                    client.stream_request(
                        "/v1/sessions/s1/messages", {"messages": []}, cancel=token
                    )
                )
                outcome["ended"] = "clean"
            except Cancelled:
                outcome["ended"] = "cancelled"
            except Exception as exc:  # pragma: no cover — failure detail only
                outcome["ended"] = repr(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert in_refresh.wait(5), "the refresh request never arrived"
            token.cancel()
            cancel_landed.set()
            worker.join(5)

            assert not worker.is_alive(), "the stream never ended"
            assert outcome == {"ended": "cancelled"}
            # A transient refresh never rotates anything — tokens are untouched.
            assert storage.get("access_token") == "old_tok"
            assert storage.get("refresh_token") == "old_ref"
        finally:
            cancel_landed.set()
            client.close()

    def test_401_refresh_and_retry_reuse_the_pooled_connection(
        self, server: KeepAliveServer
    ) -> None:
        """The uncancelled 401 path, over keep-alive: one connection, in order.

        The retry no longer opens while the 401 response is still held open.
        """
        first = {"done": False}

        def messages(handler: BaseHTTPRequestHandler) -> None:
            if not first["done"]:
                first["done"] = True
                unauthorized(handler)
                return
            send_sse(handler, SSE_BODY)

        server.route("/v1/sessions/s1/messages", messages)
        server.route("/v1/auth/refresh", lambda h: send_json(h, 200, dict(NEW_TOKENS)))
        client, storage = _make_client(server)

        chunks = list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))

        assert [c.choices[0].delta.content for c in chunks] == ["hi"]
        assert server.paths() == [
            "/v1/sessions/s1/messages",
            "/v1/auth/refresh",
            "/v1/sessions/s1/messages",
        ]
        assert len(set(server.ports())) == 1, server.log
        assert [entry[2] for entry in server.log] == [
            "Bearer old_tok",
            None,  # /v1/auth/refresh carries the partner key only
            "Bearer new_tok",
        ]
        assert storage.get("access_token") == "new_tok"
        client.close()


class TestLateCancelAndThePool:
    def test_a_cancel_after_the_stream_finished_leaves_the_pool_usable(
        self, server: KeepAliveServer
    ) -> None:
        """End to end: once the reader unregistered, nothing may touch
        the socket it pooled — the next request reuses it untouched.

        The stream pauses (no ``[DONE]``), which is when the connection really
        is pooled: the resume round-trip of every tool call rides on it.
        """
        server.route("/v1/sessions/s1/messages", lambda h: send_sse(h, SSE_PAUSED_BODY))
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        token = CancelToken()

        chunks = list(
            client.stream_request("/v1/sessions/s1/messages", {"messages": []}, cancel=token)
        )
        assert [c.choices[0].delta.content for c in chunks] == ["hi"]

        token.cancel()  # a stop() that lands just after the stream ended

        assert client.request("/v1/ping") == {"ok": True}
        assert len(set(server.ports())) == 1, "the ping did not reuse the pooled connection"
        client.close()
