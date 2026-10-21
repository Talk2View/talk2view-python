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
import sys
import threading
import time
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


class _QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that aborts a stream resets the socket the handler waits on for its
        next request: expected here, so not printed. Anything else still is."""
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


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

        self._httpd = _QuietHTTPServer(("127.0.0.1", 0), Handler)
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


# --- Connection reuse ------------------------------------------------------------------
#
# The engine streams with chunked transfer encoding (a FastAPI StreamingResponse), so a
# stream's last bytes on the wire come AFTER the event the reader stops at: the zero-length
# chunk that ends the body follows `[DONE]`, and the end of a paused stream follows its
# interrupt event, which is where the session stops reading to run the tool. A response
# closed before those bytes are read cannot go back to the pool, and every later request
# of the turn paid a new TCP + TLS handshake (~0.5 s from Vietnam to Sydney).


def _interrupt_event(tool_call_id: str) -> str:
    return "data: " + json.dumps(
        {
            "id": "c1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "interrupt": {
                "type": "tool_call",
                "tool_name": "ping_tool",
                "tool_call_id": tool_call_id,
                "arguments": {},
            },
        }
    ) + "\n\n"


STOP_EVENT = (
    'data: {"id":"c1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
)


def send_chunked_sse(
    handler: BaseHTTPRequestHandler,
    events: List[str],
    before_end: Optional[threading.Event] = None,
    held: Optional[List[str]] = None,
) -> None:
    """Stream ``events`` one HTTP chunk each, like the engine; ``before_end`` holds the
    ``held`` events and the closing zero-length chunk back until it is set (bounded)."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    try:
        for event in events:
            data = event.encode()
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            handler.wfile.flush()
        if before_end is not None:
            before_end.wait(10)
        for event in held or []:
            data = event.encode()
            handler.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        handler.wfile.write(b"0\r\n\r\n")
        handler.wfile.flush()
    except OSError:
        handler.close_connection = True  # the client aborted the stream


_PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _no_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """No proxy from the environment, in either case, nor from this machine's system
    settings (which ``urllib.request.getproxies`` reads when the environment names none)."""
    import urllib.request

    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)


def _keepalive_is_on(client: T2VClient) -> bool:
    import socket

    response = client.request("/v1/ping", raw=True)
    sock = response.extensions["network_stream"].get_extra_info("socket")
    return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))


def _contents(chunks: List[Any]) -> List[str]:
    return [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]


class TestConnectionReuse:
    def test_a_turn_rides_one_connection(self, server: KeepAliveServer) -> None:
        """A chat stream read to [DONE], a resume stream the reader leaves at its
        interrupt (as the session does, to run the tool), a JSON request and another chat
        stream: all on the first connection."""
        server.route("/v1/sessions/s1/messages", lambda h: send_chunked_sse(h, [SSE_BODY]))
        server.route(
            "/v1/sessions/s1/resume",
            lambda h: send_chunked_sse(h, [SSE_CHUNK, _interrupt_event("tc1")]),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            chunks = list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))
            assert _contents(chunks) == ["hi"]

            stream = client.stream_request("/v1/sessions/s1/resume", {"result": "x"})
            for chunk in stream:
                if chunk.interrupt is not None:
                    break  # the session stops here to run the tool
            stream.close()

            assert client.request("/v1/ping") == {"ok": True}
            chunks = list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))
            assert _contents(chunks) == ["hi"]

            assert server.paths() == [
                "/v1/sessions/s1/messages",
                "/v1/sessions/s1/resume",
                "/v1/ping",
                "/v1/sessions/s1/messages",
            ]
            assert len(set(server.ports())) == 1, server.log
        finally:
            client.close()

    def test_a_session_turn_with_two_client_tools_rides_one_connection(
        self, server: KeepAliveServer
    ) -> None:
        """End to end through ``T2VSession``: message -> tool -> resume -> tool -> resume
        -> the reply, and the next JSON request after it."""
        from talk2view.sessions import T2VSession
        from talk2view.tools import T2VTools

        resumes = {"n": 0}

        def resume(handler: BaseHTTPRequestHandler) -> None:
            resumes["n"] += 1
            if resumes["n"] == 1:
                send_chunked_sse(handler, [_interrupt_event("tc2")])
            else:
                send_chunked_sse(handler, [SSE_CHUNK, STOP_EVENT, "data: [DONE]\n\n"])

        server.route(
            "/v1/sessions/s1/messages", lambda h: send_chunked_sse(h, [_interrupt_event("tc1")])
        )
        server.route("/v1/sessions/s1/resume", resume)
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        tools = T2VTools(client)
        tools.handle("ping_tool", lambda args: "pong")
        session = T2VSession("s1", "t1", "m", client, tools)
        try:
            events = list(session.send_message("hello"))
            assert [e.type for e in events if e.type in ("tool_call", "text", "done")] == [
                "tool_call",
                "tool_call",
                "text",
                "done",
            ]
            assert client.request("/v1/ping") == {"ok": True}
            assert server.paths() == [
                "/v1/sessions/s1/messages",
                "/v1/sessions/s1/resume",
                "/v1/sessions/s1/resume",
                "/v1/ping",
            ]
            assert len(set(server.ports())) == 1, server.log
        finally:
            client.close()

    def test_a_cancel_mid_stream_drops_that_connection(self, server: KeepAliveServer) -> None:
        """Stop still interrupts a blocked read at once, and the half-read connection is
        never pooled: the next request opens a new one and succeeds."""
        release = threading.Event()
        server.route(
            "/v1/sessions/s1/messages",
            lambda h: send_chunked_sse(h, [SSE_CHUNK], before_end=release),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        token = CancelToken()
        first_chunk = threading.Event()
        outcome: Dict[str, Any] = {}

        def consume() -> None:
            try:
                for _ in client.stream_request(
                    "/v1/sessions/s1/messages", {"messages": []}, cancel=token
                ):
                    first_chunk.set()
                outcome["ended"] = "clean"
            except Cancelled:
                outcome["ended"] = "cancelled"
            except Exception as exc:  # pragma: no cover — failure detail only
                outcome["ended"] = repr(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert first_chunk.wait(5), "the first chunk never arrived"
            token.cancel()
            worker.join(2)
            assert not worker.is_alive(), "cancel() did not interrupt the blocked read"
            assert outcome == {"ended": "cancelled"}

            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 2, server.log
        finally:
            release.set()
            client.close()

    def test_an_abandoned_stream_that_never_ends_is_dropped_within_the_drain_bound(
        self, server: KeepAliveServer
    ) -> None:
        """A reader that stops early waits only briefly for the rest of the body: a stream
        that keeps going is closed, not pooled, and closing it does not hang."""
        release = threading.Event()
        server.route(
            "/v1/sessions/s1/messages",
            lambda h: send_chunked_sse(h, [_interrupt_event("tc1")], before_end=release),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            stream = client.stream_request("/v1/sessions/s1/messages", {"messages": []})
            assert next(stream).interrupt is not None
            started = time.monotonic()
            stream.close()
            assert time.monotonic() - started < 3.0

            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 2, server.log
        finally:
            release.set()
            client.close()

    def test_threads_sharing_the_client_all_succeed(self, server: KeepAliveServer) -> None:
        """The tools/skills register threads, the warm-up and the chat worker share one
        client: concurrent streams and requests all complete, on pooled connections."""
        server.route("/v1/sessions/s1/messages", lambda h: send_chunked_sse(h, [SSE_BODY]))
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        errors: List[str] = []
        start = threading.Barrier(6)

        def work() -> None:
            try:
                start.wait(5)
                for _ in range(5):
                    chunks = list(
                        client.stream_request("/v1/sessions/s1/messages", {"messages": []})
                    )
                    assert _contents(chunks) == ["hi"]
                    assert client.request("/v1/ping") == {"ok": True}
            except Exception as exc:  # pragma: no cover — failure detail only
                errors.append(repr(exc))

        workers = [threading.Thread(target=work, daemon=True) for _ in range(6)]
        for worker in workers:
            worker.start()
        try:
            for worker in workers:
                worker.join(10)
            assert not any(w.is_alive() for w in workers)
            assert errors == []
            assert len(server.log) == 60
            # Each thread holds at most one connection at a time.
            assert len(set(server.ports())) <= 6, server.log
        finally:
            client.close()

    def test_an_idle_connection_outlives_a_users_think_time(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx drops a pooled connection after 5 s idle by default, so the first request
        of every message paid a new handshake. The client now keeps one for
        ``KEEPALIVE_EXPIRY`` (300 s), under the ALB's 900 s and most NAT timers."""
        import httpcore._sync.http11 as http11

        from talk2view.client import KEEPALIVE_EXPIRY

        assert KEEPALIVE_EXPIRY == 300.0
        skew = {"s": 0.0}

        class _Clock:
            @staticmethod
            def monotonic() -> float:
                return time.monotonic() + skew["s"]

        monkeypatch.setattr(http11, "time", _Clock)
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            assert client.request("/v1/ping") == {"ok": True}
            skew["s"] = 290.0  # a long read of a report between two messages
            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 1, server.log
            skew["s"] = 290.0 + 310.0  # idle for longer than the expiry
            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 2, server.log
        finally:
            client.close()

    def test_pooled_sockets_send_tcp_keepalives(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NAT or firewall that drops an idle mapping silently would leave a pooled
        socket that swallows the next request until a timeout. Keepalive probes refresh
        the mapping, and a dead peer turns the socket readable, so the pool discards it."""
        import socket

        _no_proxy_settings(monkeypatch)
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            response = client.request("/v1/ping", raw=True)
            sock = response.extensions["network_stream"].get_extra_info("socket")
            assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0
            idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
            if idle is not None:
                assert sock.getsockopt(socket.IPPROTO_TCP, idle) == 60
            if hasattr(socket, "TCP_KEEPINTVL"):
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 10
            if hasattr(socket, "TCP_KEEPCNT"):
                assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 3
        finally:
            client.close()

    def test_a_proxy_from_the_environment_is_still_used(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx ignores HTTP(S)_PROXY for a client given its own transport, which the
        keepalive socket options need; behind a proxy the client must still go through it.
        The "proxy" here is the local server, which sees the absolute-form request."""
        _no_proxy_settings(monkeypatch)
        monkeypatch.setenv("HTTP_PROXY", server.url)
        server.route("http://engine.invalid/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        storage = MemoryStorage()
        client = T2VClient("pk_test", storage, "http://engine.invalid")
        try:
            assert client.request("/v1/ping") == {"ok": True}
            assert server.paths() == ["http://engine.invalid/v1/ping"]
        finally:
            client.close()

    def test_no_proxy_alone_keeps_tcp_keepalives(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NO_PROXY is often set with no proxy at all (Docker, CI, login scripts): it
        names no proxy, so the keepalive transport stays."""
        _no_proxy_settings(monkeypatch)
        monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            assert _keepalive_is_on(client)
        finally:
            client.close()

    def test_no_proxy_star_turns_every_proxy_off(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx ignores every proxy variable under NO_PROXY=*; so does the choice of
        transport: the request goes direct, with keepalives."""
        _no_proxy_settings(monkeypatch)
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.setenv("NO_PROXY", "*")
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            assert _keepalive_is_on(client)
            assert server.paths() == ["/v1/ping"]
        finally:
            client.close()

    def test_streams_ask_for_an_uncompressed_body(self, server: KeepAliveServer) -> None:
        """A compressed stream could only be read through httpx's ``iter_text()``, which
        pools the connection before the stream's closers are unregistered."""
        seen: List[Optional[str]] = []

        def messages(handler: BaseHTTPRequestHandler) -> None:
            seen.append(handler.headers.get("Accept-Encoding"))
            send_chunked_sse(handler, [SSE_BODY])

        server.route("/v1/sessions/s1/messages", messages)
        client, _ = _make_client(server)
        try:
            list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))
            assert seen == ["identity"]
        finally:
            client.close()

    def test_the_connection_is_pooled_only_after_every_closer_is_gone(
        self, server: KeepAliveServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A closer shuts the socket down. One still armed when the connection goes back
        to the pool could land on a request another thread has just started on it, so the
        pool may only get the connection once the stream's cancel closer and the drain's
        deadline are both unregistered."""
        import httpcore._sync.http11 as http11

        import talk2view.client as client_module

        live = {"n": 0}

        class _Tracked(CancelToken):
            def on_cancel(self, closer: Callable[[], None]) -> Callable[[], None]:
                unregister = super().on_cancel(closer)
                live["n"] += 1
                done = {"v": False}

                def tracked_unregister() -> None:
                    unregister()
                    if not done["v"]:
                        done["v"] = True
                        live["n"] -= 1

                return tracked_unregister

        armed_at_pool: List[int] = []
        original = http11.HTTP11Connection._response_closed

        def response_closed(self: Any) -> None:
            armed_at_pool.append(live["n"])
            original(self)

        monkeypatch.setattr(client_module, "CancelToken", _Tracked)
        monkeypatch.setattr(http11.HTTP11Connection, "_response_closed", response_closed)
        server.route("/v1/sessions/s1/messages", lambda h: send_chunked_sse(h, [SSE_BODY]))
        server.route(
            "/v1/sessions/s1/resume",
            lambda h: send_chunked_sse(h, [SSE_CHUNK, _interrupt_event("tc1")]),
        )
        client, _ = _make_client(server)
        try:
            list(
                client.stream_request(
                    "/v1/sessions/s1/messages", {"messages": []}, cancel=_Tracked()
                )
            )
            stream = client.stream_request(
                "/v1/sessions/s1/resume", {"result": "x"}, cancel=_Tracked()
            )
            for chunk in stream:
                if chunk.interrupt is not None:
                    break
            stream.close()

            assert len(set(server.ports())) == 1, server.log
            assert armed_at_pool == [0, 0]
        finally:
            client.close()

    def test_a_terminating_chunk_that_arrives_after_the_reader_stopped(
        self, server: KeepAliveServer
    ) -> None:
        """The drain really reads the socket: here the end of the body is not yet buffered
        when the reader stops at the interrupt."""
        release = threading.Event()
        server.route(
            "/v1/sessions/s1/messages",
            lambda h: send_chunked_sse(h, [_interrupt_event("tc1")], before_end=release),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        timer = threading.Timer(0.1, release.set)
        try:
            stream = client.stream_request("/v1/sessions/s1/messages", {"messages": []})
            assert next(stream).interrupt is not None
            timer.start()
            stream.close()

            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 1, server.log
        finally:
            timer.cancel()
            release.set()
            client.close()

    def test_a_session_that_stops_before_a_late_done_keeps_the_connection(
        self, server: KeepAliveServer
    ) -> None:
        """``T2VSession`` returns at ``finish_reason: stop``, before ``[DONE]``; here the
        ``[DONE]`` and the end of the body come only after it has."""
        from talk2view.sessions import T2VSession
        from talk2view.tools import T2VTools

        release = threading.Event()
        server.route(
            "/v1/sessions/s1/messages",
            lambda h: send_chunked_sse(
                h, [SSE_CHUNK, STOP_EVENT], before_end=release, held=["data: [DONE]\n\n"]
            ),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        session = T2VSession("s1", "t1", "m", client, T2VTools(client))
        timer = threading.Timer(0.1, release.set)
        try:
            events = session.send_message("hello")
            for event in events:
                if event.type == "done":
                    timer.start()
            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 1, server.log
        finally:
            timer.cancel()
            release.set()
            client.close()

    def test_a_stream_that_runs_past_the_drain_bound_is_dropped(
        self, server: KeepAliveServer
    ) -> None:
        """More than 64 KiB after the event the reader stopped at: not worth reading, so
        the connection is closed, never pooled half-read."""
        filler = ": " + "x" * 1022 + "\n"  # 1 KiB SSE comment lines
        server.route(
            "/v1/sessions/s1/messages",
            lambda h: send_chunked_sse(h, [_interrupt_event("tc1")] + [filler] * 200),
        )
        server.route("/v1/ping", lambda h: send_json(h, 200, {"ok": True}))
        client, _ = _make_client(server)
        try:
            stream = client.stream_request("/v1/sessions/s1/messages", {"messages": []})
            assert next(stream).interrupt is not None
            stream.close()

            assert client.request("/v1/ping") == {"ok": True}
            assert len(set(server.ports())) == 2, server.log
        finally:
            client.close()
