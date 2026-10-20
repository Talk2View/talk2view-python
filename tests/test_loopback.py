"""The one-shot loopback listener behind provider sign-in (RFC 8252 §7.3)."""

from __future__ import annotations

import http.client
import http.server
import select
import socket
import struct
import threading
import time
from typing import Callable, Iterator, List, Optional

import httpx
import pytest

from talk2view import _loopback
from talk2view._loopback import CALLBACK_PATH, LoopbackListener, LoopbackOutcome

STATE = "S" * 43
CODE = "C" * 43
DONE = "https://engine.example/v1/auth/oauth/desktop/done"


@pytest.fixture
def listener() -> Iterator[LoopbackListener]:
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    yield lst
    lst.close()


def _get(lst: LoopbackListener, path: str, host: Optional[str] = None) -> httpx.Response:
    headers = {"Host": host} if host is not None else {}
    # httpx does not follow redirects unless asked: the 303 itself is what is checked.
    return httpx.get("http://127.0.0.1:{}{}".format(lst.port, path), headers=headers, timeout=5)


def _callback(query: str) -> str:
    return CALLBACK_PATH + "?" + query


def _eventually(check: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def _handler_threads() -> List[threading.Thread]:
    return [t for t in threading.enumerate() if "process_request_thread" in t.name]


def test_it_binds_the_loopback_address_only(listener: LoopbackListener) -> None:
    host, port = listener._server.server_address[:2]
    assert host == "127.0.0.1" and port == listener.port and port > 0


def test_the_callback_with_a_code_ends_the_wait_and_leaves_no_code_behind(
    listener: LoopbackListener,
) -> None:
    r = _get(listener, _callback("code={}&state={}".format(CODE, STATE)))
    assert r.status_code == 303
    assert r.headers["location"] == DONE  # the browser moves on to a code-free page
    assert CODE not in r.text and CODE not in str(r.headers)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert listener.wait(1) == LoopbackOutcome(code=CODE)


def test_an_error_ends_the_wait(listener: LoopbackListener) -> None:
    r = _get(listener, _callback("error=access_denied&state=" + STATE))
    assert r.status_code == 303
    assert r.headers["location"] == DONE + "?failed=1"
    assert listener.wait(1) == LoopbackOutcome(error="access_denied")


def test_favicon_then_callback_still_signs_in(listener: LoopbackListener) -> None:
    assert _get(listener, "/favicon.ico").status_code == 400
    assert listener.wait(0.2) is None
    _get(listener, _callback("code={}&state={}".format(CODE, STATE)))
    assert listener.wait(1) == LoopbackOutcome(code=CODE)


def test_wrong_state_is_refused_and_the_wait_goes_on(listener: LoopbackListener) -> None:
    r = _get(listener, _callback("code={}&state={}".format(CODE, "X" * 43)))
    assert r.status_code == 400
    assert "location" not in r.headers
    assert listener.wait(0.2) is None
    _get(listener, _callback("code={}&state={}".format(CODE, STATE)))
    assert listener.wait(1) == LoopbackOutcome(code=CODE)


@pytest.mark.parametrize("host", ["localhost:{port}", "evil.example", "127.0.0.1"])
def test_a_request_for_another_host_is_refused(listener: LoopbackListener, host: str) -> None:
    # DNS rebinding: a page on another name that resolves here must not end the sign-in.
    r = _get(listener, _callback("code={}&state={}".format(CODE, STATE)),
             host=host.format(port=listener.port))
    assert r.status_code == 400
    assert listener.wait(0.2) is None


@pytest.mark.parametrize("query", [
    "code={c}&state={s}&extra=1",
    "code={c}&code={c}&state={s}",
    "code={c}&state={s}&state={s}",
    "code=short&state={s}",
    "code={c}&error=access_denied&state={s}",
    "code={c}&error=&state={s}",
    "state={s}",
    "code={c}",
    "error=Access-Denied&state={s}",
    "",
])
def test_a_malformed_callback_is_refused(listener: LoopbackListener, query: str) -> None:
    r = _get(listener, _callback(query.format(c=CODE, s=STATE)))
    assert r.status_code == 400
    assert listener.wait(0.2) is None


def test_other_paths_and_methods_are_refused(listener: LoopbackListener) -> None:
    good = "?code={}&state={}".format(CODE, STATE)
    assert _get(listener, CALLBACK_PATH + "/x" + good).status_code == 400
    assert httpx.post("http://127.0.0.1:{}{}{}".format(listener.port, CALLBACK_PATH, good),
                      timeout=5).status_code in (405, 501)
    assert listener.wait(0.2) is None


def test_after_the_answer_nothing_more_is_served(listener: LoopbackListener) -> None:
    _get(listener, _callback("code={}&state={}".format(CODE, STATE)))
    assert listener._thread is not None
    listener._thread.join(2)
    with pytest.raises(httpx.TransportError):
        _get(listener, _callback("code={}&state={}".format("D" * 43, STATE)))
    assert listener.wait(0) == LoopbackOutcome(code=CODE)


def test_abort_unblocks_the_wait(listener: LoopbackListener) -> None:
    threading.Timer(0.1, listener.abort).start()
    started = time.monotonic()
    assert listener.wait(5) is None
    assert time.monotonic() - started < 2


def test_close_frees_the_port() -> None:
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    port = lst.port
    lst.close()
    lst.close()  # idempotent
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_close_without_start_frees_the_port() -> None:
    lst = LoopbackListener(STATE, DONE)
    port = lst.port
    lst.close()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_nothing_is_printed_or_logged(listener: LoopbackListener, capfd: pytest.CaptureFixture[str],
                                      caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("DEBUG")
    # A client that resets mid-request: socketserver would print a traceback to stderr.
    with socket.create_connection(("127.0.0.1", listener.port)) as raw:
        raw.sendall(b"GET /t2v/call")
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close = RST
    _get(listener, "/favicon.ico")
    _get(listener, _callback("code={}&state={}".format(CODE, STATE)))
    assert listener.wait(1) == LoopbackOutcome(code=CODE)
    # Every connection, the reset one included, has been handled and let go.
    assert _eventually(lambda: not listener._server._connections)
    out, err = capfd.readouterr()
    for secret in (CODE, STATE):
        assert secret not in out and secret not in err and secret not in caplog.text
    assert err == ""


def test_the_outcome_never_shows_its_code() -> None:
    assert CODE not in repr(LoopbackOutcome(code=CODE))
    assert CODE not in str(LoopbackOutcome(code=CODE))


def test_a_stalled_connection_does_not_delay_the_callback() -> None:
    # Chrome opens speculative preconnect sockets that send nothing. Each connection has
    # its own thread, so the real callback is answered at once, not after a timeout.
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    try:
        with socket.create_connection(("127.0.0.1", lst.port)):  # connects, sends nothing
            started = time.monotonic()
            r = _get(lst, _callback("code={}&state={}".format(CODE, STATE)))
            assert r.status_code == 303
            assert time.monotonic() - started < 1
    finally:
        lst.close()


def test_a_stalled_connection_is_dropped_after_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_loopback, "REQUEST_TIMEOUT_S", 0.3)
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    try:
        with socket.create_connection(("127.0.0.1", lst.port)) as raw:
            raw.settimeout(3)
            assert raw.recv(1) == b""  # the listener hung up on it
    finally:
        lst.close()


def test_after_abort_a_valid_callback_on_an_open_connection_is_refused(
    listener: LoopbackListener,
) -> None:
    with socket.create_connection(("127.0.0.1", listener.port)) as raw:
        assert _eventually(lambda: bool(listener._server._connections))  # accepted
        listener.abort()
        raw.settimeout(3)
        raw.sendall("GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n".format(
            _callback("code={}&state={}".format(CODE, STATE)), listener.port).encode())
        assert raw.recv(64).startswith(b"HTTP/1.0 400 ")
    assert listener.wait(0) is None


def test_the_port_is_not_shareable(listener: LoopbackListener) -> None:
    sock = listener._server.socket
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0


@pytest.mark.skipif(not hasattr(socket, "SO_EXCLUSIVEADDRUSE"), reason="Windows only")
def test_the_port_is_exclusive_on_windows(listener: LoopbackListener) -> None:
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE")
    assert listener._server.socket.getsockopt(socket.SOL_SOCKET, exclusive) != 0


def test_close_racing_start_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    real_start = threading.Thread.start

    def slow_start(self: threading.Thread) -> None:
        if self.name == "t2v-oauth-loopback":
            time.sleep(0.3)
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", slow_start)
    lst = LoopbackListener(STATE, DONE)
    starter = threading.Thread(target=lst.start)
    starter.start()
    time.sleep(0.05)
    lst.close()  # during start(): must neither raise nor leave the port bound
    starter.join(2)
    assert lst._thread is not None
    lst._thread.join(2)
    with socket.socket() as s:
        s.bind(("127.0.0.1", lst.port))


def _trickle(raw: socket.socket, stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            raw.send(b"X")
            time.sleep(0.2)
    except OSError:
        pass


def test_a_trickling_client_is_dropped_at_the_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The timeout bounds the whole request, not each read: a byte every 0.2 s must not
    # keep a connection alive.
    monkeypatch.setattr(_loopback, "REQUEST_TIMEOUT_S", 0.5)
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    stop = threading.Event()
    try:
        with socket.create_connection(("127.0.0.1", lst.port)) as raw:
            threading.Thread(target=_trickle, args=(raw, stop), daemon=True).start()
            started = time.monotonic()
            readable, _, _ = select.select([raw], [], [], 3)
            assert readable, "still connected after 3 s"
            try:
                assert raw.recv(1) == b""
            except ConnectionResetError:
                pass
            assert time.monotonic() - started < 1.5
            stop.set()
    finally:
        stop.set()
        lst.close()


def test_no_handler_outlives_close() -> None:
    before = set(_handler_threads())
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    stop = threading.Event()
    with socket.create_connection(("127.0.0.1", lst.port)) as raw:
        threading.Thread(target=_trickle, args=(raw, stop), daemon=True).start()
        assert _eventually(lambda: bool(lst._server._connections))
        mine = [t for t in _handler_threads() if t not in before]
        assert mine
        lst.close()
        time.sleep(0.5)
        stop.set()
        assert not [t for t in mine if t.is_alive()]


def _close_as_the_sdk_does(lst: LoopbackListener) -> threading.Thread:
    """``sign_in_with_provider``: close the listener the moment ``wait`` returns."""

    def sdk() -> None:
        lst.wait(10)
        lst.close()

    thread = threading.Thread(target=sdk, daemon=True)
    thread.start()
    return thread


def _raw_callback(lst: LoopbackListener, query: str) -> object:
    """The browser's GET: the status, or the exception's name when no answer came."""
    connection = http.client.HTTPConnection("127.0.0.1", lst.port, timeout=5)
    try:
        connection.request("GET", _callback(query))
        return connection.getresponse().status
    except (OSError, http.client.HTTPException) as exc:
        return type(exc).__name__
    finally:
        connection.close()


@pytest.mark.parametrize("query", ["code={}&state={}".format(CODE, STATE),
                                   "error=access_denied&state={}".format(STATE)])
def test_close_never_cuts_the_303_it_is_waiting_for(monkeypatch: pytest.MonkeyPatch,
                                                    query: str) -> None:
    # The 303 is slow to go out (a busy host process): the SDK, woken by the outcome,
    # must not close the listener, and cut the connection, before it has gone.
    real = http.server.BaseHTTPRequestHandler.send_response

    def slow(handler: http.server.BaseHTTPRequestHandler, code: int,
             message: Optional[str] = None) -> None:
        if isinstance(handler.server, _loopback._Server) and code == 303:
            time.sleep(0.3)
        real(handler, code, message)

    monkeypatch.setattr(http.server.BaseHTTPRequestHandler, "send_response", slow)
    lst = LoopbackListener(STATE, DONE)
    lst.start()
    sdk = _close_as_the_sdk_does(lst)
    try:
        assert _raw_callback(lst, query) == 303
    finally:
        sdk.join(5)
        lst.close()
    assert not sdk.is_alive()


def test_the_303_always_gets_out_under_load() -> None:
    # The same race, unforced, many times over, with the CPU busy as a host's Python
    # usually is: every run must answer 303. (With `_done` set before the 303, about one
    # run in ten here lost it.)
    stop = threading.Event()

    def spin() -> None:
        x = 0
        while not stop.is_set():
            x += 1

    spinners = [threading.Thread(target=spin, daemon=True)]
    for spinner in spinners:
        spinner.start()
    outcomes: dict = {}
    deadline = time.monotonic() + 30
    try:
        for _ in range(60):
            if time.monotonic() > deadline:
                break
            state = "T" * 43
            lst = LoopbackListener(state, DONE)
            lst.start()
            sdk = _close_as_the_sdk_does(lst)
            status = _raw_callback(lst, "code={}&state={}".format(CODE, state))
            sdk.join(5)
            lst.close()
            outcomes[status] = outcomes.get(status, 0) + 1
    finally:
        stop.set()
    assert set(outcomes) == {303}, outcomes
    assert outcomes[303] >= 30  # a slow machine may run fewer inside the budget
