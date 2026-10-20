"""The one-shot loopback listener behind ``T2VAuth.sign_in_with_provider`` (RFC 8252 §7.3).

It binds ``127.0.0.1`` on a free port and waits for the engine's redirect of the
end-user's browser to ``http://127.0.0.1:<port>/t2v/callback?code=...&state=...`` (or
``?error=...&state=...``). Only that path, with a ``Host`` naming this listener, one
well-formed ``code`` or ``error``, and the ``state`` this attempt made, ends the wait; it
is answered ``303 See Other`` to the engine's static ``done`` page, so the code leaves the
address bar, and the listener closes. Browsers hide a redirect's source from their history
pages but may still store its URL in the history database; the code is single-use,
expires in 60 s and is useless without this attempt's PKCE verifier. Anything else gets
a short refusal page and the listener keeps waiting, so a stray request (the browser's
``/favicon.ico``, another program on this computer, a page on another name that resolves
here) cannot end or spoil the sign-in. Each connection has its own thread and
``REQUEST_TIMEOUT_S`` to send its whole request, so a browser's idle preconnect socket
never delays the callback, and ``close`` cuts every connection still open.

Nothing it receives is printed or logged: the request line carries a one-time code.
"""

from __future__ import annotations

import hmac
import io
import re
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional, Set, Type
from urllib.parse import parse_qs, urlsplit

__all__ = ["CALLBACK_PATH", "LoopbackListener", "LoopbackOutcome"]

#: Where the engine sends the browser (``auth_oauth_desktop.LOOPBACK_PATH``).
CALLBACK_PATH = "/t2v/callback"
#: How long one connection may take to send its request.
REQUEST_TIMEOUT_S = 2.0
_CODE = re.compile(r"[A-Za-z0-9_-]{43}")
_ERROR = re.compile(r"[a-z_]{1,64}")

_REFUSED = (
    "<!doctype html><html lang='en'><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Talk2View</title>"
    "<body style='margin:3rem auto;max-width:26rem;padding:0 1.5rem;background:#F2F0EC;"
    "color:#021C25;font:16px/1.5 system-ui,-apple-system,sans-serif'>"
    "<h1 style='font-size:1.25rem'>This isn’t part of a sign-in</h1>"
    "<p>You can close this tab.</p></body></html>"
)
_HEADERS = (
    ("Cache-Control", "no-store"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                                "frame-ancestors 'none'"),
)


@dataclass(frozen=True, repr=False)
class LoopbackOutcome:
    """What the engine sent: a one-time ``code``, or an ``error``."""

    code: Optional[str] = None
    error: Optional[str] = None

    def __repr__(self) -> str:
        return "LoopbackOutcome(code={}, error={!r})".format(
            "***" if self.code is not None else None, self.error
        )

    __str__ = __repr__


class _DeadlineRaw(io.RawIOBase):
    """A connection's reads, all bounded by one deadline: a client that trickles a byte
    at a time cannot hold its thread past it."""

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        left = self._deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        self._sock.settimeout(left)
        return self._sock.recv_into(buffer)


class _Server(socketserver.ThreadingMixIn, HTTPServer):
    # SO_REUSEADDR (HTTPServer's default) lets another process bind the same port on
    # Windows; SO_EXCLUSIVEADDRUSE forbids it there. Elsewhere, no reuse at all.
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False  # close() never waits on a stalled connection's thread

    def __init__(self, address: Any, handler: Type[BaseHTTPRequestHandler]) -> None:
        self._connections_lock = threading.Lock()
        self._connections: Set[socket.socket] = set()
        self._cut = False
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._connections_lock:
            cut = self._cut
            if not cut:
                self._connections.add(request)
        if cut:
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        with self._connections_lock:
            self._connections.discard(request)
        super().shutdown_request(request)

    def cut_connections(self) -> None:
        """Hang up on every open connection, and on any accepted from now on."""
        with self._connections_lock:
            self._cut = True
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # Not HTTPServer.server_bind: its socket.getfqdn() is a reverse DNS lookup that
        # can stall for seconds on a machine with slow DNS.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # never print: a reset connection's traceback could quote its request


class LoopbackListener:
    """One sign-in's listener. The port is bound when it is built; ``start`` serves it on
    a ``t2v-oauth-loopback`` daemon thread. ``abort`` and ``close`` may be called from
    any thread, any number of times."""

    def __init__(self, state: str, done_url: str) -> None:
        self._state = state.encode("utf-8")
        self._done_url = done_url
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._outcome: Optional[LoopbackOutcome] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self._server = _Server(("127.0.0.1", 0), self._handler_class())
        self._server.timeout = 0.2  # handle_request() returns this often to see `_done`
        self.port: int = int(self._server.server_address[1])

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._closed:
                return
            # Started under the lock, so close() never joins a thread not yet started.
            # The serve thread does not take the lock to start.
            self._thread = threading.Thread(
                target=self._serve, name="t2v-oauth-loopback", daemon=True
            )
            self._thread.start()

    def wait(self, timeout: float) -> Optional[LoopbackOutcome]:
        """Block until the engine's answer, ``abort()``, or ``timeout`` seconds."""
        self._done.wait(timeout)
        with self._lock:
            return self._outcome

    def abort(self) -> None:
        self._done.set()

    def close(self) -> None:
        self._done.set()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        self._server.cut_connections()
        if thread is None:
            self._server.server_close()
        elif thread is not threading.current_thread():
            thread.join(2.0)

    def _serve(self) -> None:
        try:
            while not self._done.is_set():
                self._server.handle_request()
        finally:
            self._server.server_close()

    def _finish(self, outcome: LoopbackOutcome) -> bool:
        """Claim the outcome for this request, once. ``_done`` is set by the handler only
        after its 303 is written: set here, ``wait`` would return and ``close`` would cut
        the connection still carrying the 303, leaving the code in the address bar."""
        with self._lock:
            if self._done.is_set() or self._outcome is not None:
                return False
            self._outcome = outcome
            return True

    def _read(self, raw_path: str, host: str) -> Optional[LoopbackOutcome]:
        """The engine's answer in this request, or None for anything else."""
        if host != "127.0.0.1:{}".format(self.port):
            return None
        parts = urlsplit(raw_path)
        if parts.path != CALLBACK_PATH:
            return None
        query = parse_qs(parts.query, keep_blank_values=True)
        if set(query) - {"state", "code", "error"}:
            return None
        states = query.get("state", [])
        codes = query.get("code", [])
        errors = query.get("error", [])
        if len(states) != 1 or not hmac.compare_digest(states[0].encode("utf-8"), self._state):
            return None
        if len(codes) == 1 and not errors and _CODE.fullmatch(codes[0]):
            return LoopbackOutcome(code=codes[0])
        if len(errors) == 1 and not codes and _ERROR.fullmatch(errors[0]):
            return LoopbackOutcome(error=errors[0])
        return None

    def _handler_class(self) -> Type[BaseHTTPRequestHandler]:
        listener = self

        class Handler(BaseHTTPRequestHandler):
            timeout = REQUEST_TIMEOUT_S
            server_version = "Talk2View"
            sys_version = ""

            def setup(self) -> None:
                super().setup()
                self.rfile.close()  # the makefile() reader: an open one keeps the socket's fd
                deadline = time.monotonic() + REQUEST_TIMEOUT_S
                self.rfile = io.BufferedReader(_DeadlineRaw(self.connection, deadline))

            def log_message(self, format: str, *args: Any) -> None:
                pass  # never: the request line holds a one-time code

            def do_GET(self) -> None:  # noqa: N802 — http.server's name
                outcome = listener._read(self.path, self.headers.get("Host", ""))
                if outcome is None or not listener._finish(outcome):
                    self._refuse()
                    return
                try:
                    failed = "" if outcome.code is not None else "?failed=1"
                    self.send_response(303)
                    self.send_header("Location", listener._done_url + failed)
                    self._common_headers(0)
                finally:
                    listener._done.set()  # only now: the 303 is on its way

            def _refuse(self) -> None:
                body = _REFUSED.encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self._common_headers(len(body))
                self.wfile.write(body)

            def _common_headers(self, length: int) -> None:
                for name, value in _HEADERS:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(length))
                self.send_header("Connection", "close")
                self.end_headers()

        return Handler
