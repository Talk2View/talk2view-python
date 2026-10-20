"""talk2view.ui.security: the request gate."""

from __future__ import annotations

import http.client
import io
from email.message import Message
from typing import Optional, Sequence, Tuple

import pytest

from talk2view.ui.security import (
    CSP,
    PROTECTED_PATHS,
    SECURITY_HEADERS,
    bearer_ok,
    check_request,
    new_token,
    request_path,
)

TOKEN = "tok-123"  # noqa: S105 — a test token, not a credential
PORT = 5555


def headers(*pairs: Tuple[str, str]) -> Message:
    message = Message()
    for name, value in pairs:
        message[name] = value  # Message keeps repeats, as http.client does
    return message


GOOD: Sequence[Tuple[str, str]] = (("Host", "127.0.0.1:5555"),)
AUTH = ("Authorization", "Bearer " + TOKEN)


def check(
    method: str = "GET",
    path: str = "/events",
    extra: Sequence[Tuple[str, str]] = (),
    peer: str = "127.0.0.1",
    host: Optional[Sequence[Tuple[str, str]]] = None,
) -> Optional[int]:
    base = tuple(GOOD if host is None else host)
    return check_request(method, path, headers(*(base + tuple(extra))), peer, PORT, TOKEN)


def test_bearer_accepted_on_every_protected_path() -> None:
    assert PROTECTED_PATHS == {"/events", "/command", "/upload"}
    assert check("GET", "/events", [AUTH]) is None
    assert check("POST", "/command", [AUTH]) is None
    assert check("POST", "/upload", [AUTH]) is None


def test_missing_or_wrong_bearer_is_401() -> None:
    assert check("GET", "/events") == 401
    assert check("POST", "/command", [("Authorization", "Bearer nope")]) == 401
    assert check("POST", "/command", [("Authorization", TOKEN)]) == 401
    assert check("POST", "/upload", [("Authorization", "bearer " + TOKEN)]) == 401


def test_query_token_only_on_get_events() -> None:
    assert check("GET", "/events?t=" + TOKEN) is None
    assert check("GET", "/events?t=wrong") == 401
    assert check("POST", "/command?t=" + TOKEN) == 401
    assert check("POST", "/upload?t=" + TOKEN) == 401
    assert check("POST", "/events?t=" + TOKEN) == 401


def test_static_and_healthz_need_no_token() -> None:
    assert check("GET", "/") is None
    assert check("GET", "/assets/index.js") is None
    assert check("GET", "/healthz") is None


def test_host_must_be_our_own_loopback_name_and_port() -> None:
    assert check("GET", "/", host=[("Host", "localhost:5555")]) is None
    for bad in ("evil.example:5555", "127.0.0.1:1", "127.0.0.1", "", "[::1]:5555"):
        assert check("GET", "/", host=[("Host", bad)]) == 403, bad
    assert check("GET", "/", host=[]) == 403


def test_origin_if_present_must_be_our_own_origin() -> None:
    ok = [("Origin", "http://127.0.0.1:5555"), AUTH]
    assert check("POST", "/command", ok) is None
    # Own origin is "http://" + *this request's* Host value, not any of our accepted
    # names: Host 127.0.0.1 with Origin localhost is refused even though both are ours.
    assert check(
        "POST", "/command", [("Origin", "http://localhost:5555"), AUTH],
        host=[("Host", "localhost:5555")],
    ) is None
    for bad in (
        "http://evil.example", "null", "https://127.0.0.1:5555", "http://127.0.0.1:1",
        "http://localhost:5555",
    ):
        assert check("POST", "/command", [("Origin", bad), AUTH]) == 403, bad


def test_non_loopback_peer_and_options_are_403() -> None:
    assert check("GET", "/events", [AUTH], peer="192.168.1.9") == 403
    assert check("GET", "/", peer="10.0.0.2") == 403
    assert check("OPTIONS", "/command", [AUTH]) == 403
    assert check("GET", "/events", [AUTH], peer="::1") is None


def test_a_header_sent_twice_fails_its_check() -> None:
    two_hosts = [("Host", "127.0.0.1:5555"), ("Host", "evil.example:5555")]
    assert check("GET", "/", host=two_hosts) == 403
    assert check("POST", "/command", [("Origin", "http://127.0.0.1:5555"),
                                      ("Origin", "http://evil.example"), AUTH]) == 403
    assert check("POST", "/command", [AUTH, AUTH]) == 401


def test_order_is_peer_then_host_then_origin_then_options_then_token() -> None:
    assert check("GET", "/events", peer="10.0.0.1", host=[("Host", "evil:1")]) == 403
    assert check("OPTIONS", "/command", host=[("Host", "evil:1")]) == 403
    assert check("GET", "/events", [("Origin", "http://evil")]) == 403  # not 401


def test_bearer_ok_and_helpers() -> None:
    assert bearer_ok("Bearer " + TOKEN, TOKEN)
    assert not bearer_ok(None, TOKEN)
    assert not bearer_ok("Bearer " + TOKEN + "x", TOKEN)
    assert not bearer_ok("Bearer café", TOKEN)
    token = new_token()
    assert len(token) >= 43 and token != new_token()
    assert request_path("/events?t=secret#frag") == "/events"


def test_bearer_ok_refuses_an_empty_server_token() -> None:
    assert not bearer_ok("Bearer ", "")
    assert not bearer_ok("Bearer anything", "")
    assert not bearer_ok(None, "")


def test_a_malformed_request_target_is_refused_not_raised() -> None:
    # urlsplit raises ValueError on these; an absolute-form or unparseable target is
    # refused with 403 rather than propagating an exception up through the gate.
    for path in (
        "http://[evil/events",
        "//[x/events",
        "http://ex＃ample/events",
        "http://a℀b/",
    ):
        assert check("GET", path) == 403, path
    assert request_path("http://[evil/events") == ""
    assert request_path("/events?t=x") == "/events"


def test_unknown_paths_need_the_bearer_unless_get_or_head() -> None:
    # The spec's "everything except static + healthz" means every method, not just the
    # ones that happen to collide with a protected literal: only a GET/HEAD of a path
    # outside PROTECTED_PATHS, or /healthz on any method, may skip the token.
    for path in ("/foo", "/", "/index.html", "/command/", "//command", "/%63ommand"):
        assert check("POST", path) == 401, path
    assert check("GET", "/healthz") is None
    assert check("POST", "/healthz") is None
    assert check("GET", "/index.html") is None
    assert check("HEAD", "/index.html") is None
    assert check("HEAD", "/command") == 401


def test_events_query_fallback_is_ignored_once_a_bearer_header_is_sent() -> None:
    good_query = "/events?t=" + TOKEN
    assert check("GET", good_query, [("Authorization", "Bearer wrong")]) == 401
    assert check("GET", good_query, [AUTH, AUTH]) == 401  # duplicate Authorization
    assert check("GET", "/events?t=" + TOKEN + "&t=" + TOKEN) == 401  # repeated t
    assert check("GET", "/events?t=wrong&t=" + TOKEN) == 401


def test_a_header_defect_is_refused() -> None:
    # A malformed header line (no colon, not a continuation) can hide a second Host from
    # get_all while still being visible on Message.defects.
    raw = b"Host: 127.0.0.1:5555\r\nBad Line\r\nHost: evil:1\r\n\r\n"
    parsed = http.client.parse_headers(io.BytesIO(raw))
    assert parsed.defects  # sanity: this raw block really does produce a defect
    assert check_request("GET", "/", parsed, "127.0.0.1", PORT, TOKEN) == 403


def _real_headers(raw: bytes) -> Message:
    """Headers as ``http.client.parse_headers`` (via ``BaseHTTPRequestHandler``) builds
    them: run through the real ``email`` parser, not hand-assembled via ``Message()``."""
    return http.client.parse_headers(io.BytesIO(raw + b"\r\n"))


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "QtWebEngine/5.15.2 Chrome/83.0.4103.122 Safari/537.36"
)
_HOST_LINE = b"Host: 127.0.0.1:5555\r\n"
_ORIGIN_LINE = b"Origin: http://127.0.0.1:5555\r\n"
_COMMON = (
    b"User-Agent: " + _UA.encode() + b"\r\n"
    b"Accept-Encoding: gzip, deflate\r\n"
    b"Accept-Language: en-US,en;q=0.9\r\n"
)


def test_a_chromium_shaped_upload_passes_the_gate() -> None:
    # Regression: Message.defects on a parsed multipart Content-Type always includes
    # StartBoundaryNotFoundDefect/MultipartInvariantViolationDefect, because parse_headers
    # hands email's Parser only the header block (an empty "body"), and the multipart
    # parser complains it cannot find a boundary in that empty payload -- on every real
    # multipart upload, not just malformed ones. A blanket "any defect -> 403" refuses
    # every POST /upload. Only header-BLOCK defects may cause a refusal.
    raw = (
        _HOST_LINE
        + b"Content-Length: 200\r\n"
        + b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        + b"Content-Type: multipart/form-data; boundary=----WebKitFormBoundaryAbc123\r\n"
        + b"Accept: */*\r\n" + _ORIGIN_LINE
        + b"Sec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n"
        + b"Referer: http://127.0.0.1:5555/\r\n" + _COMMON
    )
    parsed = _real_headers(raw)
    assert any(type(d).__name__ == "StartBoundaryNotFoundDefect" for d in parsed.defects)
    assert check_request("POST", "/upload", parsed, "127.0.0.1", PORT, TOKEN) is None


def test_a_chromium_shaped_navigation_and_command_pass_the_gate() -> None:
    nav = _real_headers(
        _HOST_LINE
        + b"Upgrade-Insecure-Requests: 1\r\n"
        + b"Accept: text/html,application/xhtml+xml\r\n"
        + b"Sec-Fetch-Site: none\r\nSec-Fetch-Mode: navigate\r\n"
        + b"Sec-Fetch-User: ?1\r\nSec-Fetch-Dest: document\r\n" + _COMMON
    )
    assert check_request("GET", "/", nav, "127.0.0.1", PORT, TOKEN) is None

    cmd = _real_headers(
        _HOST_LINE
        + b"Content-Length: 18\r\n"
        + b"Authorization: Bearer " + TOKEN.encode() + b"\r\n"
        + b"Content-Type: application/json\r\n"
        + b"Accept: */*\r\n" + _ORIGIN_LINE
        + b"Sec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n"
        + b"Referer: http://127.0.0.1:5555/\r\n" + _COMMON
    )
    assert check_request("POST", "/command", cmd, "127.0.0.1", PORT, TOKEN) is None


def test_a_smuggled_host_is_still_refused_with_or_without_multipart() -> None:
    plain = _real_headers(b"Host: 127.0.0.1:5555\r\nBad Line\r\nHost: evil:1\r\n")
    assert check_request("GET", "/", plain, "127.0.0.1", PORT, TOKEN) == 403

    with_multipart = _real_headers(
        b"Host: 127.0.0.1:5555\r\n"
        b"Content-Type: multipart/form-data; boundary=b\r\n"
        b"Bad Line\r\nHost: evil:1\r\n"
    )
    # The smuggle defect (MissingHeaderBodySeparatorDefect) must still be present and
    # still cause a refusal even alongside the harmless multipart-body defects.
    assert any(
        type(d).__name__ == "MissingHeaderBodySeparatorDefect" for d in with_multipart.defects
    )
    assert check_request(
        "POST", "/upload", with_multipart, "127.0.0.1", PORT, TOKEN
    ) == 403


@pytest.mark.parametrize("name", ["Cache-Control", "X-Content-Type-Options", "Referrer-Policy"])
def test_security_headers_and_csp_are_the_plan0_set(name: str) -> None:
    assert name in dict(SECURITY_HEADERS)
    assert CSP == (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; connect-src 'self'"
    )
