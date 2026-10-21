"""HTTP/SSE API client with two-tier auth (partner key + user JWT)."""

from __future__ import annotations

import codecs
import contextlib
import json
import logging
import math
import socket
import threading
import urllib.request
from functools import partial
from types import TracebackType
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Literal,
    Optional,
    Tuple,
    Type,
    TypedDict,
    cast,
)

import httpx

from .cancel import Cancelled, CancelToken
from .errors import (
    AuthenticationError,
    NetworkError,
    T2VError,
    error_for_type,
    parse_retry_after,
)
from .metrics import _TurnRecorder, current_turn
from .storage import TokenStorage
from .streaming import parse_sse_text
from .types import ChatCompletionChunk

# Silence httpx/httpcore request logging (noisy at INFO/DEBUG level)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("talk2view.client")

#: The stored session: what ``_clear_auth`` deletes.
_AUTH_KEYS = ("access_token", "refresh_token", "user", "user_api_key", "is_anonymous")
#: No refresh on record for ``_sign_out_rejected`` on this thread.
_NO_REFRESH = object()

DEFAULT_BASE_URL = "https://engine.talk2view.com"
#: Keep in step with ``[project].version`` in pyproject.toml (test_release_surface checks).
SDK_VERSION = "0.4.0"
# Sent as X-T2V-Client so the engine can tell which SDK release a request came from.
CLIENT_HEADER = "talk2view-python/" + SDK_VERSION
#: Seconds for a request, and to connect a stream (TS ``requestTimeout``, 30 s).
DEFAULT_TIMEOUT = 30.0
#: Seconds for an upload, attachments and audio alike (TS ``DEFAULT_UPLOAD_TIMEOUT``, 120 s).
DEFAULT_UPLOAD_TIMEOUT = 120.0
#: Seconds a chat stream may go without a byte once open. Not configurable: the engine sends
#: heartbeats well inside it. TS sets none once connected.
STREAM_TIMEOUT = 300.0

#: The most ``request_bytes`` downloads: above the engine's 20 MB attachment cap
#: (``max_attachment_size_mb``), so any real attachment fits.
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
#: Seconds an idle pooled connection is kept for the next request. httpx's default, 5 s,
#: is shorter than a user's think time between messages, so the first request of nearly
#: every turn paid a new TCP + TLS handshake. The engine's ALB would keep one for 900 s
#: (``idle_timeout`` in packages/server/deploy/aws/alb.tf), but a NAT or firewall on the
#: way may forget an idle connection sooner without telling either end, and a request sent
#: on it then waits out its whole timeout (300 s for a stream). So: 300 s, under most such
#: timers, with TCP keepalives (``_TCP_KEEPALIVE``) refreshing them and exposing a dead
#: peer, which makes the pool discard the connection before reusing it.
KEEPALIVE_EXPIRY = 300.0
#: TCP keepalive on every connection: the first probe after 60 s idle, then every 10 s,
#: and the peer is dead after 3 unanswered ones.
_TCP_KEEPALIVE = (60, 10, 3)
#: How long, and how many bytes, the rest of a stream the reader stopped early is read for
#: (see ``_drain``). What follows the event a reader stops at is normally just the end of
#: the body, already on its way; anything longer is not worth waiting for, since a new
#: connection costs well under a second.
_DRAIN_SECONDS = 1.0
_DRAIN_MAX_BYTES = 64 * 1024

# Set by this client alone: a caller's ``headers=`` may not replace them (any case).
_PROTECTED_HEADERS = frozenset({"authorization", "x-t2v-partner-key"})

# They describe the wire encoding of a body that has since been decoded.
_WIRE_ENCODING_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


class _ParsedError(TypedDict):
    """Result of :meth:`T2VClient._parse_error`.

    ``message``/``type`` are always plain ``str`` (user-safe), so they flow into
    ``T2VError`` without widening to ``Optional``; ``detail`` carries a separate,
    structured detail (JSON-serialized) for debugging or ``None``.
    """

    message: str
    type: str
    code: Optional[str]
    detail: Optional[str]
    request_id: Optional[str]
    retry_after: Optional[int]


def _stringify_detail(raw: Any) -> Optional[str]:
    """Normalize a structured error ``detail`` into a debug string.

    Strings pass through unchanged; dicts/lists (e.g. a FastAPI 422 validation
    list) are JSON-serialized so they stay inspectable on ``T2VError.detail``.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    try:
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return str(raw)


def _bearer_token(headers: Dict[str, str]) -> Optional[str]:
    """The access token a request was sent with, read back from its headers."""
    value = headers.get("Authorization", "")
    return value[len("Bearer "):] if value.startswith("Bearer ") else None


def _merge_headers(base: Dict[str, str], extra: Dict[str, str]) -> None:
    """Add a caller's ``extra`` headers to ``base``, replacing a header of the same name
    in any case (so no name is sent twice). ``Authorization`` and ``X-T2V-Partner-Key``
    raise ``ValueError``: the refresh logic reads back the token this client sent."""
    for name in extra:
        if name.lower() in _PROTECTED_HEADERS:
            raise ValueError(
                "{} is set by the client and cannot be passed in headers=".format(name)
            )
    for name, value in extra.items():
        for existing in [key for key in base if key.lower() == name.lower()]:
            del base[existing]
        base[name] = value


def _seconds(name: str, value: Any) -> float:
    """A timeout: a positive, finite number of seconds. Anything else raises ``ValueError``
    (``True`` included: it is an ``int`` to Python, never a timeout)."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("{} must be a positive number of seconds, not {!r}".format(name, value))
    return float(value)


def _too_large(max_bytes: int) -> T2VError:
    return T2VError(
        "The response is larger than the {} byte download limit.".format(max_bytes),
        "response_too_large",
    )


def _abort_response(response: httpx.Response) -> None:
    """Unblock a read of ``response`` that is stalled on another thread.

    ``response.close()`` (and ``httpx.Client.close()``) from a second thread do
    not wake a thread blocked in ``recv()``. Shutting the socket down does: the
    blocked read returns at once and the reader thread closes the response
    itself. On that path — the only one a real network stream takes — no httpx
    object is mutated across threads.

    Without a reachable socket (a mock transport, or a response whose stream
    has already been released) there is nothing to shut down, so this falls
    back to ``response.close()``, which DOES touch httpx state from this
    thread. That path only runs where no read can be blocked on a socket.
    """
    network_stream = response.extensions.get("network_stream")
    sock = (
        network_stream.get_extra_info("socket") if network_stream is not None else None
    )
    if sock is None:
        # No reachable socket (e.g. a mock transport): best effort.
        response.close()
        return
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _drain(response: httpx.Response, raw: Iterator[bytes]) -> None:
    """Read what is left of a stream the reader has stopped taking events from, so that
    closing it returns the connection to the pool.

    The engine's streams are chunked: after ``[DONE]`` comes the zero-length chunk that
    ends the body, and a paused stream ends just after the interrupt the session stops at
    to run the tool. httpcore only pools a connection whose response was read to its end;
    one closed earlier is dropped, and the next request of the turn opened a new one.

    ``raw`` iterates ``response.stream``, which, unlike httpx's ``iter_*``, does not close
    the response when the body runs out. So nothing here returns the connection to the
    pool: that happens only when ``_do_stream`` leaves its ``with`` block and closes the
    response, after this function has unregistered its deadline and ``_do_stream`` the
    stream's cancel closer. ``unregister()`` is final, so neither can shut down a socket
    the pool has since handed to another request.

    Bounded by ``_DRAIN_SECONDS`` (a timer shuts the socket down, as a cancel does) and
    ``_DRAIN_MAX_BYTES``. A drain that stops short or fails leaves the response unfinished,
    so closing it drops the connection instead of pooling it half-read.
    """
    deadline = CancelToken()
    unregister = deadline.on_cancel(partial(_abort_response, response))
    timer = threading.Timer(_DRAIN_SECONDS, deadline.cancel)
    timer.daemon = True
    try:
        timer.start()
        left = _DRAIN_MAX_BYTES
        for data in raw:
            left -= len(data)
            if left <= 0:
                break
    except Exception:  # noqa: S110 — aborted or broken: closing drops the connection
        pass
    finally:
        timer.cancel()
        unregister()


def _decoded(raw: Iterator[bytes]) -> Iterator[str]:
    """``raw`` as text. SSE is always UTF-8; a character split across two reads is
    reassembled rather than corrupted."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    for data in raw:
        text = decoder.decode(data)
        if text:
            yield text
    tail = decoder.decode(b"", final=True)
    if tail:
        yield tail


def _httpx_would_proxy() -> bool:
    """Whether httpx would set up a proxy for a client left to read the proxy settings.

    The same reading as httpx's ``get_environment_proxies``: ``urllib.request.getproxies()``
    (the ``*_proxy`` environment variables, or, when none is set, the macOS or Windows
    system settings), of which only an ``http``, ``https`` or ``all`` proxy counts, and
    none at all under ``NO_PROXY=*``. ``NO_PROXY`` alone names no proxy."""
    proxies = urllib.request.getproxies()
    if any(host.strip() == "*" for host in proxies.get("no", "").split(",")):
        return False
    return any(proxies.get(scheme) for scheme in ("http", "https", "all"))


def _tcp_keepalive_options() -> List[Tuple[int, int, int]]:
    """The TCP keepalive socket options this platform accepts (``_TCP_KEEPALIVE``).

    The names differ (``TCP_KEEPALIVE`` is macOS's idle time) and some are missing, or
    refused, on older systems; httpcore applies every option to every new socket and a
    refused one would fail the connection, so each is tried on a scratch socket first."""
    idle_s, interval_s, count = _TCP_KEEPALIVE
    wanted = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    idle = getattr(socket, "TCP_KEEPIDLE", None)
    if idle is None:
        idle = getattr(socket, "TCP_KEEPALIVE", None)
    if idle is not None:
        wanted.append((socket.IPPROTO_TCP, idle, idle_s))
    if hasattr(socket, "TCP_KEEPINTVL"):
        wanted.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval_s))
    if hasattr(socket, "TCP_KEEPCNT"):
        wanted.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count))
    usable: List[Tuple[int, int, int]] = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return usable
    with probe:
        for option in wanted:
            try:
                probe.setsockopt(*option)
            except OSError:
                continue
            usable.append(option)
    return usable


class T2VClient:
    """Low-level HTTP client handling auth headers, token refresh, and SSE streaming."""

    def __init__(
        self,
        partner_key: str,
        storage: TokenStorage,
        base_url: str = DEFAULT_BASE_URL,
        *,
        request_timeout: float = DEFAULT_TIMEOUT,
        upload_timeout: float = DEFAULT_UPLOAD_TIMEOUT,
    ) -> None:
        self._partner_key = partner_key
        self._storage = storage
        self._base_url = base_url.rstrip("/")
        self._request_timeout = _seconds("request_timeout", request_timeout)
        self._upload_timeout = _seconds("upload_timeout", upload_timeout)
        self._refresh_lock = threading.Lock()
        # Held around every write of the stored session, and around the reads a write
        # depends on: T2VAuth's store and logout, a refresh's new tokens, a rejected
        # refresh's clear. A leaf: nothing else is taken, and no host code runs, while it
        # is held (T2VAuth takes it inside its own locks, the refresh inside
        # `_refresh_lock`). Re-entrant, for `_clear_auth` inside logout's hold.
        self._tokens_lock = threading.RLock()
        # A single pooled client so the connection (and its TLS session) is
        # reused across every hop of a chat turn. A desktop turn from 3D Slicer
        # is create-session -> register-tools -> message -> N resume round-trips
        # per tool call; with one-shot module-level httpx calls each of those
        # hops paid a fresh TCP + TLS handshake (~100-300ms on a WAN link).
        # httpx.Client keeps connections alive between requests and is safe to
        # share across threads for request dispatch.
        # A stream is drained to its end once read (`_drain`), or its connection could
        # not be pooled; an idle connection is kept for `KEEPALIVE_EXPIRY`, with TCP
        # keepalives. Those need a transport of our own, but httpx ignores the proxy
        # settings for a client given one: behind a proxy the client keeps httpx's own
        # transports, which honour them, and only the expiry applies.
        limits = httpx.Limits(
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=KEEPALIVE_EXPIRY,
        )
        if _httpx_would_proxy():
            self._http = httpx.Client(
                base_url=self._base_url, timeout=self._request_timeout, limits=limits
            )
        else:
            self._http = httpx.Client(
                base_url=self._base_url,
                timeout=self._request_timeout,
                transport=httpx.HTTPTransport(
                    limits=limits, socket_options=_tcp_keepalive_options()
                ),
            )
        # Called when a rejected refresh has just cleared the stored session (T2VAuth
        # installs it, to tell its listeners). Per thread: a depth of held blocks, and
        # whether one of them has a sign-out waiting for it to end.
        self._sign_out_hook: Optional[Callable[[], None]] = None
        self._held = threading.local()
        # Per thread: the refresh token this thread's last refresh had rejected, for the
        # `_sign_out_rejected` that follows it (it clears only that session).
        self._rejected = threading.local()

    @property
    def storage(self) -> TokenStorage:
        return self._storage

    @property
    def base_url(self) -> str:
        """The engine's base URL, without a trailing slash."""
        return self._base_url

    @property
    def request_timeout(self) -> float:
        """Seconds for a request, a token refresh, and a stream to connect."""
        return self._request_timeout

    @property
    def upload_timeout(self) -> float:
        """Seconds for an upload (``upload_request``: attachments and audio)."""
        return self._upload_timeout

    def set_sign_out_hook(self, hook: Optional[Callable[[], None]]) -> None:
        """Install the callback for "a rejected refresh token has signed the end-user out".

        It runs after the stored session has been cleared, on the thread whose request got
        the rejection, with no lock of this client held — and not at all when nothing was
        stored (a request made while signed out). ``T2VAuth`` installs one that notifies
        its ``on_auth_state_change`` listeners with ``None``.
        """
        self._sign_out_hook = hook

    @contextlib.contextmanager
    def holding_sign_out(self) -> Iterator[None]:
        """Hold back a sign-out announced on THIS thread until the outermost such block
        ends. For a caller that keeps its own lock across requests (``T2VSkills``' post
        lock): entered outside that lock, it makes the auth listeners run once the lock is
        released instead of under it. The session is still cleared at once."""
        depth = getattr(self._held, "depth", 0)
        self._held.depth = depth + 1
        try:
            yield
        finally:
            self._held.depth = depth
            if depth == 0 and getattr(self._held, "pending", False):
                self._held.pending = False
                self._announce_sign_out()

    def close(self) -> None:
        """Close the pooled connection (idempotent).

        Callers holding a long-lived client should call this (or use the
        context-manager form) so pooled connections are released cleanly.
        """
        self._http.close()

    def __enter__(self) -> "T2VClient":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def _headers(
        self, requires_auth: bool = True, accept: Optional[str] = None
    ) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "Talk2View-SDK/" + SDK_VERSION,
            "X-T2V-Client": CLIENT_HEADER,
            "X-T2V-Partner-Key": self._partner_key,
        }
        if accept:
            headers["Accept"] = accept
        if requires_auth:
            token = self._storage.get("access_token")
            if token:
                headers["Authorization"] = "Bearer " + token
        return headers

    def request(
        self,
        endpoint: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        requires_auth: bool = True,
        *,
        timeout: Optional[float] = None,
        headers: Optional[Dict[str, str]] = None,
        raw: bool = False,
        max_bytes: Optional[int] = None,
    ) -> Any:
        """Make an authenticated JSON request. Returns the parsed JSON response.

        ``timeout`` overrides the default (a long completion); ``headers`` are added to
        the standard ones (an ``Origin`` for the OAuth exchange), replacing a standard
        one of the same name in any case — but ``Authorization`` and
        ``X-T2V-Partner-Key`` raise ``ValueError``: only this client sets those.
        ``raw=True`` returns the ``httpx.Response`` itself instead of parsing it
        (attachment bytes). ``max_bytes`` streams the body and raises ``T2VError``
        (``response_too_large``) once it passes that many decoded bytes.
        """
        request_headers = self._headers(requires_auth)
        if headers:
            _merge_headers(request_headers, headers)
        headers = request_headers

        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": timeout if timeout is not None else self._request_timeout,
        }
        if body is not None:
            kwargs["content"] = json.dumps(body)

        try:
            response = self._send(method, endpoint, kwargs, max_bytes)
        except httpx.HTTPError as exc:
            raise NetworkError("Network request failed: " + str(exc)) from exc

        # Handle 401 — try to refresh token
        if response.status_code == 401 and requires_auth:
            self._raise_if_partner_key_error(response)
            refresh_result = self._try_refresh_token(_bearer_token(headers))
            if refresh_result == "refreshed":
                tok = self._storage.get("access_token") or ""
                headers["Authorization"] = "Bearer " + tok
                kwargs["headers"] = headers
                try:
                    retry = self._send(method, endpoint, kwargs, max_bytes)
                except httpx.HTTPError as exc:
                    raise NetworkError(
                        "Network request failed: " + str(exc)
                    ) from exc
                if not retry.is_success:
                    self._raise_for_status(retry)
                if raw:
                    return retry
                if retry.status_code == 204:
                    return None
                return retry.json()
            elif refresh_result == "invalid":
                self._sign_out_rejected()
                raise AuthenticationError(
                    "Session expired. Please log in again."
                )
            else:
                raise NetworkError(
                    "Token refresh temporarily unavailable; please retry."
                )

        if not response.is_success:
            self._raise_for_status(response)

        if raw:
            return response
        if response.status_code == 204:
            return None
        return response.json()

    def _send(
        self,
        method: str,
        endpoint: str,
        kwargs: Dict[str, Any],
        max_bytes: Optional[int],
    ) -> httpx.Response:
        """One request on the pooled client. With ``max_bytes`` the body is streamed and
        refused as soon as it passes that size, decoded: no more than one read chunk
        beyond the cap is ever held. The response returned then carries the decoded
        body, so it no longer describes a wire encoding."""
        if max_bytes is None:
            return self._http.request(method, endpoint, **kwargs)
        with self._http.stream(method, endpoint, **kwargs) as streamed:
            encoding = streamed.headers.get("content-encoding", "identity").strip().lower()
            declared = streamed.headers.get("content-length", "")
            if encoding == "identity" and declared.isdigit() and int(declared) > max_bytes:
                raise _too_large(max_bytes)
            body = bytearray()
            for chunk in streamed.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise _too_large(max_bytes)
        return httpx.Response(
            streamed.status_code,
            headers=[
                (name, value)
                for name, value in streamed.headers.multi_items()
                if name.lower() not in _WIRE_ENCODING_HEADERS
            ],
            content=bytes(body),
            request=streamed.request,
        )

    def request_bytes(self, endpoint: str) -> Tuple[bytes, str]:
        """Authenticated GET of a binary body: ``(bytes, content-type)``. Same 401 refresh
        and error mapping as ``request``.

        At most ``MAX_DOWNLOAD_BYTES``: past that, ``T2VError`` (``response_too_large``).
        The body is asked for unencoded — attachments are already-compressed formats,
        and a gzip body can expand far beyond what was sent — and a server that
        compresses anyway is capped on the decoded size."""
        response: httpx.Response = self.request(
            endpoint,
            raw=True,
            headers={"Accept-Encoding": "identity"},
            max_bytes=MAX_DOWNLOAD_BYTES,
        )
        content_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, content_type.split(";")[0].strip()

    def upload_request(
        self,
        endpoint: str,
        files: Any,
        data: Optional[Dict[str, Any]] = None,
        requires_auth: bool = True,
    ) -> Any:
        """Make an authenticated multipart upload request. Returns parsed JSON response.

        Does NOT set Content-Type — httpx sets it with the correct multipart boundary.
        """
        headers: Dict[str, str] = {
            "X-T2V-Client": CLIENT_HEADER,
            "X-T2V-Partner-Key": self._partner_key,
        }
        if requires_auth:
            token = self._storage.get("access_token")
            if token:
                headers["Authorization"] = "Bearer " + token

        try:
            response = self._http.post(
                endpoint, headers=headers, files=files, data=data or {},
                timeout=self._upload_timeout,
            )
        except httpx.HTTPError as exc:
            raise NetworkError("Network request failed: " + str(exc)) from exc

        # Handle 401 — try to refresh token
        if response.status_code == 401 and requires_auth:
            self._raise_if_partner_key_error(response)
            refresh_result = self._try_refresh_token(_bearer_token(headers))
            if refresh_result == "refreshed":
                tok = self._storage.get("access_token") or ""
                headers["Authorization"] = "Bearer " + tok
                try:
                    retry = self._http.post(
                        endpoint, headers=headers, files=files, data=data or {},
                        timeout=self._upload_timeout,
                    )
                except httpx.HTTPError as exc:
                    raise NetworkError(
                        "Network request failed: " + str(exc)
                    ) from exc
                if not retry.is_success:
                    self._raise_for_status(retry)
                return retry.json()
            elif refresh_result == "invalid":
                self._sign_out_rejected()
                raise AuthenticationError(
                    "Session expired. Please log in again."
                )
            else:
                raise NetworkError(
                    "Token refresh temporarily unavailable; please retry."
                )

        if not response.is_success:
            self._raise_for_status(response)

        return response.json()

    def stream_request(
        self,
        endpoint: str,
        body: Dict[str, Any],
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatCompletionChunk]:
        """Make an authenticated SSE streaming POST request.

        Returns a GENERATOR: nothing is sent until the first ``next()``, and
        every failure — a cancellation, an auth error, a bad status — surfaces
        there rather than from this call.

        Streams over the pooled client so the connection is reused across the
        interrupt/resume round-trips of a chat turn. The caller SHOULD exhaust
        or close the returned iterator to release the underlying connection
        back to the pool.

        Args:
            cancel: When given, ``cancel.cancel()`` from ANY thread aborts the
                stream — before the request is sent, or while a read is
                blocked — and the iterator raises :class:`Cancelled`.
        """
        headers = self._headers(
            requires_auth=True, accept="text/event-stream"
        )
        # Uncompressed: `_read_stream` decodes the wire bytes itself, which is what lets
        # the connection be pooled only after the stream's closers are gone.
        headers["Accept-Encoding"] = "identity"
        content = json.dumps(body)
        timeout = httpx.Timeout(self._request_timeout, read=STREAM_TIMEOUT)

        return self._do_stream(endpoint, headers, content, timeout, cancel=cancel)

    def _do_stream(
        self,
        endpoint: str,
        headers: Dict[str, str],
        content: str,
        timeout: httpx.Timeout,
        is_retry: bool = False,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatCompletionChunk]:
        """Stream ``endpoint``, refreshing the access token once on a 401.

        The cancel closer covers the body read and nothing else: it is
        unregistered, and the response closed, BEFORE a refresh is attempted.
        A refresh therefore runs with no closer attached to it, so a
        ``cancel()`` in that window can no longer shut down the pooled socket
        the refresh reuses — which used to leave the client holding a refresh
        token the server had already rotated (a permanent transient loop, or a
        sign-out on the next refresh).
        """
        if cancel is not None and cancel.cancelled:
            raise Cancelled()
        turn = current_turn()  # the controller turn this stream belongs to, if any

        while True:
            unauthorized: Optional[httpx.Response] = None
            if turn is not None:
                turn.request_sent()
            try:
                with self._http.stream(
                    "POST", endpoint, headers=headers, content=content, timeout=timeout,
                ) as response:
                    if turn is not None:
                        turn.response_headers(response.headers)
                    # Bind THIS response into the closer: the retry rebinds
                    # `response`, and a closer must only ever abort the
                    # response it was registered for.
                    unregister = (
                        cancel.on_cancel(partial(_abort_response, response))
                        if cancel is not None
                        else None
                    )
                    try:
                        if response.status_code == 401 and not is_retry:
                            # Buffer the error body so the connection is pooled
                            # cleanly; the 401 is handled outside this block.
                            response.read()
                            unauthorized = response
                        else:
                            yield from self._read_stream(response, turn, cancel)
                    finally:
                        if unregister is not None:
                            unregister()
            except Cancelled:
                raise
            except Exception as exc:
                # The abort surfaces as an httpx read/protocol error on this thread.
                if cancel is not None and cancel.cancelled:
                    raise Cancelled() from exc
                raise

            if unauthorized is None:
                break

            # The response is closed and no closer of ours is registered, so
            # the refresh below is out of this stream's cancel() reach.
            self._raise_if_partner_key_error(unauthorized)
            refresh_result = self._try_refresh_token(_bearer_token(headers))
            if refresh_result == "invalid":
                self._sign_out_rejected()
                raise AuthenticationError(
                    "Session expired. Please log in again."
                )
            if refresh_result != "refreshed":
                # The refresh token really is unusable right now, but if the
                # caller already cancelled this stream, that's the true
                # reason it's ending — a cancelled turn should end quietly
                # (Cancelled) rather than surface a spurious "please retry".
                if cancel is not None and cancel.cancelled:
                    raise Cancelled()
                raise NetworkError(
                    "Token refresh temporarily unavailable; please retry."
                )
            headers["Authorization"] = "Bearer " + (
                self._storage.get("access_token") or ""
            )
            is_retry = True
            # The rotated pair is safely stored: only now honour a cancel that
            # landed during the refresh, instead of opening the retry stream.
            if cancel is not None and cancel.cancelled:
                raise Cancelled()

        if cancel is not None and cancel.cancelled:
            raise Cancelled()

    def _read_stream(
        self,
        response: httpx.Response,
        turn: Optional[_TurnRecorder] = None,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatCompletionChunk]:
        """Yield the chunks of a streamed response, or raise for its status. Each chunk is
        noted on ``turn`` (its timing) first.

        Once the events end (``[DONE]``, or the caller closing this generator), the rest of
        the body is drained so the connection goes back to the pool. Not after a cancel or
        a read error: that connection must be dropped, never pooled half-read."""
        if not response.is_success:
            response.read()
            error = self._parse_error(response)
            raise error_for_type(
                error["message"],
                error["type"],
                response.status_code,
                error["code"],
                error["detail"],
                request_id=error["request_id"],
                retry_after=error["retry_after"],
            )

        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            # A guard only: `stream_request` asks for `identity`, and the engine never
            # compresses a stream. A server that did anyway can only be read through
            # httpx's decoder: not drained, so the connection is dropped when the reader
            # stops early, and on a body read to its end `iter_text()` pools it before
            # this stream's cancel closer is unregistered (the race `_drain` avoids).
            for chunk in parse_sse_text(response.iter_text()):
                if turn is not None:
                    turn.chunk(chunk)
                yield chunk
            return

        # See `_drain`: unlike `iter_text()`, this never closes the response itself.
        raw = iter(cast(httpx.SyncByteStream, response.stream))
        try:
            for chunk in parse_sse_text(_decoded(raw)):
                if turn is not None:
                    turn.chunk(chunk)
                yield chunk
        except GeneratorExit:
            if cancel is None or not cancel.cancelled:
                _drain(response, raw)
            raise
        if cancel is None or not cancel.cancelled:
            _drain(response, raw)

    def _try_refresh_token(
        self, failed_access_token: Optional[str]
    ) -> Literal["refreshed", "invalid", "transient"]:
        """Attempt a token refresh.

        ``failed_access_token`` is the access token the 401'd request was sent
        with, and is REQUIRED — read it off the request's own headers with
        ``_bearer_token()``. It has no default on purpose: a caller that
        omitted it would report every 401 as "refreshed" without refreshing.

        If, once inside the lock, storage holds a DIFFERENT access token,
        another thread refreshed while this one waited: report "refreshed"
        without calling the server. A second call would spend the freshly
        rotated refresh token for nothing, and on a lost race gets a 409 that
        needlessly fails this request as "transient". ``None`` counts as
        different: a request that went out with no token at all should still
        retry with the one storage has gained since.

        Returns "refreshed" (new tokens stored), "invalid" (the refresh token was
        genuinely rejected -> real logout), or "transient" (the refresh failed for
        a reason that does NOT mean the session is dead -- already rotated, rate
        limited, or a network blip -- so keep the session and let the caller retry).

        A "transient" result surfaces as a ``NetworkError`` to the caller; it is
        safe to retry, ideally with a short backoff so a 429 isn't made worse.
        """
        with self._refresh_lock:
            current_access_token = self._storage.get("access_token")
            if current_access_token and current_access_token != failed_access_token:
                return "refreshed"

            refresh_token = self._storage.get("refresh_token")
            self._rejected.token = refresh_token
            if not refresh_token:
                return "invalid"

            try:
                response = self._http.post(
                    "/v1/auth/refresh",
                    headers={
                        "Content-Type": "application/json",
                        "X-T2V-Client": CLIENT_HEADER,
                        "X-T2V-Partner-Key": self._partner_key,
                    },
                    content=json.dumps({"refresh_token": refresh_token}),
                    timeout=self._request_timeout,
                )
            except Exception:
                return "transient"  # network error — don't log the user out

            if response.is_success:
                try:
                    data = response.json()
                except Exception:
                    return "transient"
                # Read BOTH tokens out of the body before touching storage so a
                # partial 2xx body (e.g. missing refresh_token) can never persist
                # a mismatched new-access + old-refresh pair.
                access_token = (
                    data.get("access_token") if isinstance(data, dict) else None
                )
                refresh_token_new = (
                    data.get("refresh_token") if isinstance(data, dict) else None
                )
                if not access_token or not refresh_token_new:
                    # Malformed success body — surface clearly instead of masking
                    # it as transient while leaving a half-written token pair.
                    raise AuthenticationError(
                        "Token refresh returned an incomplete response "
                        "(missing access_token or refresh_token)."
                    )
                with self._tokens_lock:
                    if self._storage.get("refresh_token") != refresh_token:
                        # The session changed hands while this refresh was out (a sign-in
                        # or a sign-out): these tokens are the old session's, never
                        # written over the new one.
                        return "refreshed" if self._storage.get("access_token") else "invalid"
                    self._storage.set("access_token", access_token)
                    self._storage.set("refresh_token", refresh_token_new)
                return "refreshed"
            # 401 = the refresh token was genuinely rejected -> real logout, unless the
            # route refused the PARTNER key: that says nothing about the refresh token,
            # so PartnerKeyError is raised and the session kept.
            # 409 (already rotated) / 429 (rate limited) are recoverable.
            if response.status_code == 401:
                self._raise_if_partner_key_error(response)
                return "invalid"
            return "transient"

    def _clear_auth(self) -> bool:
        """Delete the stored session. True when there was one: a token or a user."""
        with self._tokens_lock:
            had_session = any(
                self._storage.get(key) for key in ("access_token", "refresh_token", "user")
            )
            for key in _AUTH_KEYS:
                self._storage.delete(key)
        return had_session

    def _sign_out_rejected(self) -> None:
        """The refresh token was rejected: clear the stored session, then announce the
        sign-out if there was one — or, inside ``holding_sign_out()``, once that block
        ends. Every caller runs after ``_try_refresh_token`` has released the refresh
        lock, so no lock of this client is held here.

        Only the session whose refresh token was rejected is cleared: when a sign-in (or
        a sign-out) has replaced it since, it is left as it is and nothing is announced.
        The rejected token is the one this thread's last refresh spent (with none on
        record, whatever is stored is cleared)."""
        with self._tokens_lock:
            rejected = getattr(self._rejected, "token", _NO_REFRESH)
            self._rejected.token = _NO_REFRESH
            if rejected is not _NO_REFRESH and self._storage.get("refresh_token") != rejected:
                return
            if not self._clear_auth():
                return
        if getattr(self._held, "depth", 0):
            self._held.pending = True
            return
        self._announce_sign_out()

    def _announce_sign_out(self) -> None:
        hook = self._sign_out_hook
        if hook is None:
            return
        try:
            hook()
        except Exception:
            # The caller is about to raise its AuthenticationError: that one is its answer.
            logger.exception("sign-out hook raised")

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Raise the most specific error class for a failed response.

        The class is chosen from the engine's ``error.type`` alone (same map as
        the TypeScript SDK): a 401 is an ``AuthenticationError`` only when the
        engine says so, never just because of the status code.
        """
        error = self._parse_error(response)
        raise error_for_type(
            error["message"],
            error["type"],
            response.status_code,
            error["code"],
            error["detail"],
            request_id=error["request_id"],
            retry_after=error["retry_after"],
        )

    def _raise_if_partner_key_error(self, response: httpx.Response) -> None:
        """A 401 caused by the PARTNER key is not an expired user session.

        Refreshing cannot fix it, and treating the failed refresh as "invalid"
        would sign the end-user out. Raise ``PartnerKeyError`` and leave the
        stored tokens alone.
        """
        error = self._parse_error(response)
        if error["type"] == "partner_key_error":
            raise error_for_type(  # a PartnerKeyError
                error["message"],
                error["type"],
                response.status_code,
                error["code"],
                error["detail"],
                request_id=error["request_id"],
                retry_after=error["retry_after"],
            )

    @staticmethod
    def _parse_error(response: httpx.Response) -> _ParsedError:
        """:meth:`_parse_body_error`, plus the request id and ``Retry-After``.

        The request id comes from the body's ``error.request_id``, else the
        ``X-Request-Id`` header, so a non-JSON error page still has one.
        """
        error, body_request_id = T2VClient._parse_body_error(response)
        return {
            "message": error["message"],
            "type": error["type"],
            "code": error["code"],
            "detail": error["detail"],
            "request_id": body_request_id or response.headers.get("X-Request-Id"),
            "retry_after": parse_retry_after(response.headers.get("Retry-After")),
        }

    @staticmethod
    def _parse_body_error(
        response: httpx.Response,
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse a failed response into a user-safe ``message`` + ``type`` plus a
        separate structured ``detail`` (for debugging only).

        Only the server's structured, user-safe ``error.message``/``error.type``
        reach ``message``. The server's raw ``detail`` (stack traces, DB errors,
        file paths) and any opaque non-JSON body (e.g. an HTML 502 from the ALB)
        are kept OUT of ``message`` so they cannot leak to end users; a
        *structured* detail (e.g. a FastAPI 422 validation list) is JSON-
        serialized onto ``detail`` instead. FastAPI 422s and ``{"detail": "..."}``
        responses stay debuggable rather than collapsing to a bare status.
        """
        generic = f"Request failed (HTTP {response.status_code})"
        try:
            data = response.json()
        except Exception:
            # Opaque/non-JSON body (e.g. an HTML error page): never echo it back
            # to the caller — fall back to a generic message + status.
            return {"message": generic, "type": "unknown_error", "code": None, "detail": None}, None

        if not isinstance(data, dict):
            return {"message": generic, "type": "unknown_error", "code": None, "detail": None}, None

        err = data.get("error")
        if isinstance(err, str):
            return {
                "message": err or generic,
                "type": "unknown_error",
                "code": None,
                "detail": None,
            }, None
        if not isinstance(err, dict):
            # Some routes wrap the envelope as {"detail": {"error": {...}}}.
            wrapped = data.get("detail")
            nested = wrapped.get("error") if isinstance(wrapped, dict) else None
            err = nested if isinstance(nested, dict) else {}

        msg = err["message"] if isinstance(err.get("message"), str) else ""
        error_type = err["type"] if isinstance(err.get("type"), str) else "unknown_error"
        code = err["code"] if isinstance(err.get("code"), str) else None
        request_id = err["request_id"] if isinstance(err.get("request_id"), str) else None

        # Structured detail (debug only): the raw detail nested under ``error``
        # (e.g. an engine stack trace) or FastAPI's top-level ``detail`` (a 422
        # validation list, or a string HTTPException message). Never the raw
        # non-JSON body — that path returned above.
        raw_detail = err.get("detail")
        top_detail = data.get("detail")
        detail = _stringify_detail(raw_detail if raw_detail is not None else top_detail)

        if not msg:
            if isinstance(top_detail, str) and top_detail:
                # A FastAPI HTTPException string detail (e.g. a 404 "Not found")
                # is a safe, human message — surface it instead of a bare status.
                msg = top_detail
            elif detail is not None:
                # A structured detail (e.g. a 422 validation list) is not a
                # sentence, but keep it visible so the failure stays debuggable.
                msg = f"{generic}: {detail}"
            else:
                msg = generic

        return {"message": msg, "type": error_type, "code": code, "detail": detail}, request_id
