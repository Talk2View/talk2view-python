"""UIServer: the localhost HTTP + SSE server a web view loads the chat page from.

    host = GenericHost()                      # or the host app's own adapter
    t2v = Talk2View(partner_key="pk_...", dispatcher=host)
    controller = ChatController(t2v)
    server = UIServer(controller, host)
    server.start()                            # http://127.0.0.1:<port>/#t=<token>
    server.show()                             # posted: the host shows the URL as it pumps
    host.run_forever()
    server.stop(); controller.close(); t2v.close()

Routes (the full wire contract is in ``wire.py``):

    GET  /          the page (static files; CSP)        no token
    GET  /healthz   {"ok": true}                        no token
    GET  /events    SSE: snapshot, ops, ": hb"          token (``?t=`` fallback)
    POST /command   one JSON command -> 202             token
    POST /upload    multipart, one file -> attachment   token

Threads. HTTP threads (one per connection) run the gate, validate, and move bytes: they
never call the controller, ``Talk2View`` or host code. Commands go to the one
``CommandRouter`` worker; uploads to the one ``t2v-ui-upload`` worker, which also reads the
signed-in identity and files the attachment, and whose answer the HTTP thread waits for;
streams read the ``EventHub``. The server stops with ``stop()`` and never closes the
controller, the ``Talk2View`` or the host: they belong to the host.

Uploads are filed per identity. The worker reads who is signed in before and after the
upload, and refuses it (409, ``account_changed``) when afterwards no signed-in user with an
id can own it, or it changed from one signed-in identity to another; signed out before is
fine, as the upload may start a guest session. A ``send`` resolves only the ids its own
identity uploaded.

Framing is never guessed. A body needs exactly one ``Content-Length`` and no
``Transfer-Encoding``; a ``GET``, ``HEAD`` or ``OPTIONS`` that declares one is refused; and
every refusal that leaves bytes unread closes the connection, so they are never read as the
next request. Errors the standard library answers itself get the same JSON body and security
headers as the server's own.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import socket
import socketserver
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, Callable, Iterable, Optional, Set, Tuple, Union

from ..constants import ATTACHMENT_TYPES_LABEL, is_allowed_attachment_type
from ..errors import T2VError
from ..types import Attachment
from .app_state import _SIGNED_OUT, AppState, _identity
from .host import HostAdapter, Theme
from .hub import MAX_CLIENTS, EventHub, TooManyClientsError
from .multipart import MultipartError, parse_upload
from .options import ChatOptions
from .router import QUEUE_FULL, CommandRouter, ControllerLike, UploadRegistry, _can_own
from .security import CSP, HEALTH_PATH, SECURITY_HEADERS, check_request, new_token, request_path
from .static import content_type_for, require_static_dir, resolve_static
from .wire import _LONE_SURROGATE, Command, CommandError, http_error, parse_command

if TYPE_CHECKING:
    from .. import Talk2View

__all__ = ["DEFAULT_MAX_UPLOAD_BYTES", "MAX_COMMAND_BYTES", "UIServer"]

logger = logging.getLogger("talk2view.ui")

DEFAULT_MAX_UPLOAD_BYTES = 25 * 2**20
MAX_COMMAND_BYTES = 2 * 2**20
#: Room for the multipart envelope around the file itself.
_MULTIPART_OVERHEAD = 64 * 1024
_LAST_EVENT_ID = re.compile(r"^[0-9]{1,18}$")
#: By extension, when the part carries no useful Content-Type (never the OS registry).
_UPLOAD_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

#: ``(status, error type, message)``: how ``/command`` refuses a body.
_Refusal = Tuple[int, str, str]
#: The methods of a path that ``HEAD`` does not serve (its ``405`` names them in ``Allow``).
_ALLOWED = {"/events": "GET", "/command": "POST", "/upload": "POST"}
#: The methods the request log names. Any other is the page's own text, which can carry the
#: token as a query can, so the log records ``_OTHER_METHOD`` in its place.
_LOGGED_METHODS = frozenset({"GET", "HEAD", "POST", "OPTIONS"})
_OTHER_METHOD = "<other>"
#: ``error.type`` of each refusal the standard library answers itself. Fixed here, never
#: derived from ``HTTPStatus.phrase``, which Python versions rename (3.13: 414 is "URI Too
#: Long", 413 "Content Too Large"), so the page gets the same type whatever the host runs.
_STDLIB_ERROR_TYPES = {
    400: "bad_request",
    413: "too_large",
    414: "request_uri_too_long",
    431: "request_header_fields_too_large",
    501: "not_implemented",
    505: "http_version_not_supported",
}
#: ``error.type`` of any other status the standard library answers with.
_STDLIB_ERROR_DEFAULT = "http_error"


def _stdlib_refusal(code: int) -> Tuple[str, str]:
    """``(error type, message)`` for a status the standard library answers itself: the type
    from ``_STDLIB_ERROR_TYPES``, the message the status's phrase (which is only read)."""
    try:
        phrase = HTTPStatus(code).phrase
    except ValueError:
        phrase = "Error"
    return _STDLIB_ERROR_TYPES.get(code, _STDLIB_ERROR_DEFAULT), phrase + "."


class _AccountChangedError(Exception):
    """An upload whose signed-in identity changed under it, or could not be read after it."""


def _refuse_constant(name: str) -> Any:
    """``json.loads``' ``parse_constant``: NaN and the infinities, which ``JSON.parse`` on
    the page refuses too, are refused here."""
    raise ValueError("{} is not JSON".format(name))


def _finite_float(text: str) -> float:
    """``json.loads``' ``parse_float``: a number too large for a float (``1e999``) is valid
    JSON that Python reads as an infinity, which the page's ``JSON.parse`` could never have
    meant; it is refused like ``Infinity`` itself."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("a number out of range")
    return value


def _utf8(text: str) -> bytes:
    """``text`` as UTF-8, each lone surrogate (which UTF-8 cannot carry) as U+FFFD. Engine
    text, a streamed reply or an error message, can hold one that ``wire.py`` never saw,
    and a frame that cannot be encoded would end the stream at the same event every time
    the page resumes it. U+FFFD is 3 bytes, as many as the hub counted for the surrogate."""
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        return _LONE_SURROGATE.sub("\ufffd", text).encode("utf-8")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    #: Never share the port: with SO_REUSEADDR another process could bind it on Windows.
    allow_reuse_address = False

    def server_bind(self) -> None:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)  # Windows only
        if exclusive is not None:
            self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        # TCPServer's bind, not HTTPServer's: that one resolves socket.getfqdn(), a reverse
        # DNS lookup that can take seconds on a misconfigured machine.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # a page closing its socket is normal
        logger.exception("UI server request failed")


class _Handler(BaseHTTPRequestHandler):
    ui: "UIServer"  # set on the per-server subclass
    protocol_version = "HTTP/1.1"
    server_version = "t2v-ui"
    sys_version = ""
    #: A page that stops reading for this long is dropped; it reconnects with Last-Event-ID.
    timeout = 30.0

    # ── Logging: the path only, never the query (``?t=`` is the token) ──

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def log_error(self, format: str, *args: Any) -> None:
        # The arguments can carry the raw request line, token and all: never log them.
        logger.debug("http request rejected")

    def log_request(self, code: Union[int, str] = "-", size: Union[int, str] = "-") -> None:
        status = getattr(code, "value", code)
        method = getattr(self, "command", None)
        if method not in _LOGGED_METHODS:  # the page's own text, like a query: never logged
            method = _OTHER_METHOD
        logger.debug("http %s %s %s", method, request_path(getattr(self, "path", "")), status)

    # ── Responses ──

    def _send(
        self,
        status: int,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        extra: Iterable[Tuple[str, str]] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"))

    def _error(
        self,
        status: int,
        error_type: str,
        message: str,
        *,
        close: bool = False,
        extra: Iterable[Tuple[str, str]] = (),
    ) -> None:
        headers = list(extra)
        if close:
            # The body was not read: never reuse the socket, and tell the page so.
            self.close_connection = True
            headers.append(("Connection", "close"))
        body = json.dumps(http_error(error_type, message)).encode("utf-8")
        self._send(status, body, extra=headers)

    def send_error(
        self, code: int, message: Optional[str] = None, explain: Optional[str] = None
    ) -> None:
        """The standard library's own refusals (a bad request line, 414, 431, 501, 505),
        answered like every other one: the JSON error, the security headers, a closed
        connection, and never the request line echoed back (it can carry the ``?t=``
        token). A genuine HTTP/0.9 request (``GET <path>``: two words) takes no status line
        or headers, so it keeps the standard library's own answer."""
        version = getattr(self, "request_version", None)
        words = getattr(self, "requestline", "").split()
        if version == "HTTP/0.9" and len(words) == 2:
            super().send_error(code, message, explain)
            return
        if version in (None, "HTTP/0.9"):
            # Unset, or still the default: the line failed before its version was read.
            self.request_version = ""
        if getattr(self, "command", None) is None:
            self.command = ""
        self.log_error("code %d", code)
        error_type, message = _stdlib_refusal(code)
        self._error(code, error_type, message, close=True)

    def handle_expect_100(self) -> bool:
        """``Expect: 100-continue`` passes the gate first: a request the gate refuses gets
        its refusal, never a 100 that invites its body. A refusal that comes later, from the
        handler (``/upload`` while attachments are turned off, a ``Content-Length`` it will
        not take), follows the 100; the body is still never read, and the connection closes."""
        return self._gate() and super().handle_expect_100()

    def _gate(self) -> bool:
        """``check_request``, then: a stopped server refuses everything (503), and a ``GET``,
        ``HEAD`` or ``OPTIONS`` that declares a body is refused (400), since the body
        would otherwise be read as the next request. Every refusal closes the connection."""
        status = check_request(
            self.command, self.path, self.headers, self.client_address[0], self.ui.port,
            self.ui._token,
        )
        if status == 401:
            self._error(401, "unauthorized", "Missing or wrong token.", close=True)
        elif status is not None:
            self._error(403, "forbidden", "Not allowed.", close=True)
        elif self.ui._stopped:
            self._error(503, "stopped", "This chat has stopped.", close=True)
        elif self.command in ("GET", "HEAD", "OPTIONS") and self._declares_a_body():
            self._error(400, "bad_request", "This request takes no body.", close=True)
        else:
            return True
        return False

    def _declares_a_body(self) -> bool:
        lengths = self.headers.get_all("Content-Length") or []
        return (
            "Transfer-Encoding" in self.headers
            or len(lengths) > 1
            or any(value.strip() != "0" for value in lengths)
        )

    # ── Methods ──

    def do_OPTIONS(self) -> None:
        # A preflight is never answered and no Access-Control-* header is ever sent.
        if self._gate():
            self._error(403, "forbidden", "Not allowed.", close=True)

    def do_GET(self) -> None:
        if not self._gate():
            return
        path = request_path(self.path)
        if path == HEALTH_PATH:
            self._json(200, {"ok": True})
        elif path == "/events":
            self._events()
        else:
            self._static()

    def do_HEAD(self) -> None:
        # The page's files and /healthz; the API paths have no HEAD.
        if not self._gate():
            return
        path = request_path(self.path)
        if path == HEALTH_PATH:
            self._json(200, {"ok": True})
        elif path in _ALLOWED:
            self._error(405, "method_not_allowed", "Not allowed on this path.", close=True,
                        extra=(("Allow", _ALLOWED[path]),))
        else:
            self._static()

    def do_POST(self) -> None:
        if not self._gate():
            return
        path = request_path(self.path)
        if path == "/command":
            self._command()
        elif path == "/upload":
            self._upload()
        else:
            self._error(404, "not_found", "No such endpoint.", close=True)

    # ── Bodies ──

    def _read_body(self, limit: int) -> Optional[bytes]:
        if "Transfer-Encoding" in self.headers:  # even an empty one: never guess the framing
            self._error(411, "length_required", "Send a Content-Length, not chunks.", close=True)
            return None
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1:
            self._error(400, "bad_request", "Send one Content-Length.", close=True)
            return None
        raw = lengths[0] if lengths else None
        if raw is None or not raw.isascii() or not raw.isdigit():
            self._error(411, "length_required", "Content-Length is required.", close=True)
            return None
        length = int(raw)
        if length > limit:
            self._error(413, "too_large", "The body is larger than {} bytes.".format(limit),
                        close=True)
            return None
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True  # the page went away mid-body
            return None
        return body

    def _command(self) -> None:
        media = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if media != "application/json":
            self._error(415, "unsupported_media_type", "Commands are application/json.",
                        close=True)
            return
        parsed = self._read_command()
        if parsed is None:
            return  # answered already: the body was refused unread, or never came
        if not isinstance(parsed, Command):
            self._error(*parsed)
            return
        refused = self.ui._router.submit(parsed)
        if refused == QUEUE_FULL:
            self._error(503, "queue_full", "Too many commands are waiting; try again.")
            return
        if refused is not None:  # halted by a BaseException from host code, or stop() ran
            self._error(503, "stopped", "This chat has stopped.", close=True)
            return
        self._json(202, {"ok": True, "id": parsed.id})

    def _read_command(self) -> Union[Command, _Refusal, None]:
        """The body's command, or how to refuse it; None once the body itself was refused
        (answered already). Until ``parse_command`` wraps it in a ``Secret`` a password is
        plain text in the raw body and the parsed payload, so both are locals of this frame
        only, never of ``_command``, which a later traceback would show; and nothing here
        logs a traceback, or an exception's message."""
        body = self._read_body(MAX_COMMAND_BYTES)
        if body is None:
            return None
        try:
            payload = json.loads(
                body.decode("utf-8"), parse_float=_finite_float, parse_constant=_refuse_constant
            )
        except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
            return (400, "bad_request", "The body is not JSON.")
        try:
            return parse_command(payload)
        except CommandError as exc:
            return (400, "bad_command", str(exc))
        except Exception as exc:
            logger.error("a command could not be parsed: %s", type(exc).__name__)
            return (500, "internal_error", "Something went wrong.")

    def _upload(self) -> None:
        ui = self.ui
        if not ui._app.options.features.attachments:
            # Before the body is read: a host that turned attachments off takes no file.
            self._error(403, "attachments_disabled", "Attachments are turned off in this app.",
                        close=True)
            return
        body = self._read_body(ui._max_upload_bytes + _MULTIPART_OVERHEAD)
        if body is None:
            return
        try:
            upload = parse_upload(body, self.headers.get("Content-Type", ""))
        except MultipartError as exc:
            self._error(400, "bad_upload", str(exc))
            return
        if len(upload.data) > ui._max_upload_bytes:
            self._error(413, "too_large", "The file is larger than {} bytes.".format(
                ui._max_upload_bytes))
            return
        mime = upload.content_type
        if mime in (None, "application/octet-stream"):
            mime = _UPLOAD_TYPES.get(os.path.splitext(upload.filename)[1].lower(), "")
        if not is_allowed_attachment_type(mime or ""):
            self._error(415, "unsupported_media_type",
                        "Talk2View supports {}.".format(ATTACHMENT_TYPES_LABEL))
            return
        if ui._app.t2v is None:
            self._error(503, "account_unavailable",
                        "This chat is not connected to a Talk2View account.")
            return
        try:
            attachment = ui._run_upload(upload.data, upload.filename, mime or "")
        except _AccountChangedError:
            self._error(409, "account_changed",
                        "The account changed during the upload. Upload the file again.")
            return
        except T2VError as exc:
            self._error(502, exc.type or "upload_failed", str(exc))
            return
        except Exception:
            logger.exception("upload failed")
            self._error(502, "upload_failed", "The upload failed.")
            return
        self._json(200, {
            "id": attachment.id,
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": attachment.size_bytes,
        })

    # ── Static files and the event stream ──

    def _static(self) -> None:
        full = resolve_static(self.ui._static_dir, self.path)
        body: Optional[bytes] = None
        if full is not None:
            try:
                with open(full, "rb") as fh:
                    body = fh.read()
            except OSError:
                body = None
        if full is None or body is None:
            self._send(404, b"Not found", "text/plain; charset=utf-8",
                       (("Content-Security-Policy", CSP),))
            return
        self._send(200, body, content_type_for(full), (("Content-Security-Policy", CSP),))

    def _events(self) -> None:
        ui = self.ui
        raw = self.headers.get("Last-Event-ID")
        last = int(raw) if raw is not None and _LAST_EVENT_ID.match(raw) else None
        try:
            client = ui._hub.attach(last)
        except TooManyClientsError as exc:
            self._error(503, "too_many_streams", str(exc), close=True)
            return
        self.close_connection = True
        ui._track(self.connection, True)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            for name, value in SECURITY_HEADERS:
                self.send_header(name, value)
            self.send_header("Connection", "close")  # no Content-Length: the body ends at close
            self.end_headers()
            self.wfile.write(b": t2v-ui stream open\n\n")
            self.wfile.flush()
            while True:
                event = ui._hub.next_event(client, ui._heartbeat_s)
                if event is None:
                    return
                kind, seq, data = event
                if kind == "hb":
                    frame = b": hb\n\n"
                else:
                    frame = _utf8("event: {}\nid: {}\ndata: {}\n\n".format(kind, seq, data))
                self.wfile.write(frame)
                self.wfile.flush()
        except OSError:
            return  # the page went away, or stop() shut the socket
        finally:
            ui._hub.detach(client)
            ui._track(self.connection, False)


class UIServer:
    """Serve the chat page and ``controller``'s state on ``127.0.0.1``.

    ``app`` defaults to an ``AppState`` over ``controller.t2v`` (None for a controller
    without one: the account slices then read signed out). ``options`` are the
    integrator's ``ChatOptions`` for the page; a host that gives its own ``app`` gives them
    to that ``AppState`` instead, and passing both raises ``ValueError``. So does an ``app``
    over another ``Talk2View`` than ``controller.t2v`` (the page would show one account
    and chat as another; a None on either side is allowed), and ``allow_anonymous=False``
    with any ``Talk2View`` in play built with ``anonymous_auto_start=True``: a send would
    start a guest session behind the page's sign-in gate.
    ``on_skills_list`` runs on the router thread when the page asks for the skills list,
    before the platform catalogue is read: a host rescans its skill folders there (it must
    not block on the host's main thread). Raises ``StaticAssetsMissingError`` when the
    page is not built.
    """

    def __init__(
        self,
        controller: ControllerLike,
        host: HostAdapter,
        *,
        app: Optional[AppState] = None,
        static_dir: Optional[Union[str, "os.PathLike[str]"]] = None,
        port: int = 0,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        heartbeat_s: float = 15.0,
        max_clients: int = MAX_CLIENTS,
        on_skills_list: Optional[Callable[[], None]] = None,
        options: Optional[ChatOptions] = None,
    ) -> None:
        if app is not None and options is not None:
            raise ValueError("give the options to your AppState: AppState(..., options=...)")
        account = getattr(controller, "t2v", None)  # None for a controller without one
        if app is not None:
            # Without an app, the AppState built below checks the controller's Talk2View.
            theirs = app.t2v
            if account is not None and theirs is not None and theirs is not account:
                raise ValueError(
                    "the AppState and the controller must share one Talk2View: build the "
                    "AppState over controller.t2v"
                )
            if not app.options.allow_anonymous and any(
                t2v is not None and t2v.anonymous_auto_start for t2v in (account, theirs)
            ):
                raise ValueError(
                    "ChatOptions(allow_anonymous=False) needs the controller's Talk2View "
                    "built without anonymous_auto_start: a send would start a guest session "
                    "behind the sign-in gate"
                )
        self._static_dir = str(require_static_dir(static_dir))
        self._controller = controller
        self._host = host
        self._app = app if app is not None else AppState(account, host, options=options)
        self._requested_port = port
        self._max_upload_bytes = max_upload_bytes
        self._heartbeat_s = heartbeat_s
        self._token = new_token()
        self._hub = EventHub(max_clients=max_clients)
        self._uploads = UploadRegistry()
        self._router = CommandRouter(
            controller, self._app, host, self._uploads, on_skills_list=on_skills_list
        )
        self._upload_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="t2v-ui-upload")
        self._lock = threading.Lock()  # guards the fields below
        self._started = False
        self._stopped = False
        self._port = 0
        self._httpd: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._streams: Set[socket.socket] = set()

    # ── Public ──

    @property
    def port(self) -> int:
        """The bound port (0 until ``start()``)."""
        return self._port

    @property
    def url(self) -> str:
        """``http://127.0.0.1:<port>/#t=<token>``: the token rides in the fragment, which the
        browser never sends; the page moves it to sessionStorage and strips the hash."""
        if not self._port:
            raise RuntimeError("UIServer.start() first")
        return "http://127.0.0.1:{}/#t={}".format(self._port, self._token)

    def start(self) -> str:
        """Bind, join the controller, serve. Returns ``url``. A server starts once. A start
        that fails part-way undoes all of it before it raises: no worker is left running,
        the controller is unsubscribed, the port is closed, ``url`` raises again, and
        ``stop()`` returns at once. A ``stop()`` on another thread that lands before the
        server is published makes ``start()`` undo it all the same way and raise
        ``RuntimeError``: a stopped server never goes on to serve."""
        with self._lock:
            if self._started or self._stopped:
                raise RuntimeError("a UIServer starts once")
            self._started = True
        handler = type("_BoundHandler", (_Handler,), {"ui": self})
        httpd = _Server(("127.0.0.1", self._requested_port), handler)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, name="t2v-ui-http",
            daemon=True,
        )
        unsubscribe: Optional[Callable[[], None]] = None
        try:
            self._port = int(httpd.server_address[1])
            self._router.start()
            self._hub.set_resync_handler(lambda: self._router.submit_job(self._rebase))
            self._app.attach(self._hub, self._controller)
            unsubscribe = self._hub.join(self._controller)
            thread.start()
        except BaseException:
            self._undo_start(httpd, unsubscribe)
            raise
        with self._lock:
            stopped = self._stopped
            if not stopped:
                # Only a server whose thread runs is one stop() shuts down: shutdown()
                # waits for a serve_forever() that has started.
                self._httpd, self._thread, self._unsubscribe = httpd, thread, unsubscribe
        if stopped:
            # stop() ran before it could see this server: stop it here, then undo the rest.
            httpd.shutdown()
            thread.join()
            self._undo_start(httpd, unsubscribe)
            raise RuntimeError("UIServer.stop() ran before start() finished")
        return self.url

    def _undo_start(self, httpd: _Server, unsubscribe: Optional[Callable[[], None]]) -> None:
        """Undo a start that did not finish. Every step is safe to repeat: a racing
        ``stop()`` may have done some of them already."""
        self._port = 0
        if unsubscribe is not None:
            unsubscribe()
        self._router.stop()
        self._app.detach()
        httpd.server_close()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop serving: end every stream, stop the workers, unsubscribe. Idempotent. Does not
        close the controller, the ``Talk2View`` or the host.

        Called inside the chat controller's emit window (from a ``deliver_on_main=False``
        subscriber, or an auth listener running on the emitting thread) it waits out
        ``timeout``: a command on the router can be waiting for that same window. It does
        not deadlock, and the router exits once the window closes, but ``subscribe()``'s
        callback rules say not to block there: call it from the host's main thread."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            httpd, thread = self._httpd, self._thread
        if httpd is not None:
            httpd.shutdown()
        self._hub.close()
        with self._lock:
            # Read after the hub closed: a stream that attached during shutdown() is here
            # too, and none can attach from now on.
            streams = list(self._streams)
        for connection in streams:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if httpd is not None:
            httpd.server_close()
        if self._unsubscribe is not None:
            self._unsubscribe()
        self._router.stop(timeout)
        self._app.detach(timeout)
        self._upload_pool.shutdown(wait=False)
        if thread is not None:
            thread.join(timeout)

    def show(self) -> None:
        """Ask the host to show the page: ``host.show(url)`` is posted through
        ``host.post``, so it runs on the host's main thread when the host next pumps
        (``HostAdapter``'s rule), whichever thread calls this. Raises ``RuntimeError``
        before ``start()``."""
        host, url = self._host, self.url
        host.post(lambda: host.show(url))

    def set_theme(self, theme: Theme) -> None:
        self._app.set_theme(theme)

    # ── Internals ──

    def _rebase(self) -> None:
        self._hub.rebase(self._controller)

    def _run_upload(self, data: bytes, filename: str, mime_type: str) -> Attachment:
        """Upload on the upload worker (never the HTTP thread); wait for its answer."""
        t2v = self._app.t2v
        if t2v is None:
            raise RuntimeError("no Talk2View behind this server")
        future = self._upload_pool.submit(self._upload_and_file, t2v, data, filename, mime_type)
        return future.result()

    def _upload_and_file(
        self, t2v: "Talk2View", data: bytes, filename: str, mime_type: str
    ) -> Attachment:
        """The upload worker's job: upload, then file the attachment under the identity that
        uploaded it. ``_AccountChangedError`` files nothing: after the upload no signed-in
        user can own it (``router._can_own``: signed out, an unreadable store, or a stored
        token whose user is missing or garbled), or a signed-in identity was replaced by
        another meanwhile (signed out before is fine: the upload may start a guest
        session)."""
        before = _identity(t2v)
        attachment = t2v.upload_attachment(data, filename=filename, mime_type=mime_type)
        after = _identity(t2v)
        if not _can_own(after) or (before != _SIGNED_OUT and before != after):
            raise _AccountChangedError()
        self._uploads.add(attachment, owner=after)
        return attachment

    def _track(self, connection: socket.socket, open_: bool) -> None:
        with self._lock:
            if open_:
                self._streams.add(connection)
            else:
                self._streams.discard(connection)
