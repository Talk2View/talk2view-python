"""The request gate of the localhost UI server: who may talk to it, and with what token.

Order; the first failure wins:

1. the peer is a loopback address;
2. the request target is origin-form (starts with ``/``) and the header *block* is
   well-formed (see ``_has_header_block_defect``: this is narrower than "no defects at
   all", because a multipart ``Content-Type`` alone makes ``email``'s parser record a
   defect on every request, not just malformed ones);
3. ``Host`` is our own name and port (``127.0.0.1:<port>`` or ``localhost:<port>``), which
   defeats DNS rebinding;
4. ``Origin``, when present, equals ``http://`` + that same ``Host`` value, so no other
   site's page can post here;
5. ``OPTIONS`` is refused: a preflight is never answered and no ``Access-Control-*``
   header is ever sent, so a cross-origin script cannot send the custom header;
6. the bearer token, on every path except ``/healthz`` and a ``GET``/``HEAD`` of a path
   outside ``PROTECTED_PATHS`` (``hmac.compare_digest``).

Static files and ``/healthz`` need no token. The token lives in the URL fragment, which
the browser never sends, so the page's own HTML and scripts have to load without it.
``?t=<token>`` is honoured on ``GET /events`` only, and only when no ``Authorization``
header was sent at all (a sent header decides the outcome by itself); that is why the
server never logs a query string. A header sent twice (``Host``, ``Origin``,
``Authorization``) fails its check rather than letting one copy vouch for the other, and
so does a repeated ``t`` query parameter.

An absolute-form or otherwise unparseable request target (``urlsplit`` raises on some of
these, such as an unmatched ``[`` in an authority) is refused with 403 rather than
propagating an exception: a local process that can make such a request has no token
either, and the server must answer, not crash.
"""

from __future__ import annotations

import hmac
import secrets
from email.errors import (
    FirstHeaderLineIsContinuationDefect,
    HeaderDefect,
    MissingHeaderBodySeparatorDefect,
)
from email.message import Message
from typing import FrozenSet, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "CSP",
    "HEALTH_PATH",
    "LOOPBACK_PEERS",
    "PROTECTED_PATHS",
    "SECURITY_HEADERS",
    "bearer_ok",
    "check_request",
    "new_token",
    "request_path",
]

LOOPBACK_PEERS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
#: Paths that need the token. Everything else a GET/HEAD reaches is a static file or /healthz.
PROTECTED_PATHS: FrozenSet[str] = frozenset({"/events", "/command", "/upload"})
HEALTH_PATH = "/healthz"
#: Measured in Slicer's Chromium 83: the chat page runs with zero CSP violations under exactly this.
CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; connect-src 'self'"
)
#: Sent on every response.
SECURITY_HEADERS: Tuple[Tuple[str, str], ...] = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


def new_token() -> str:
    """A per-launch secret: 32 random bytes, URL-safe."""
    return secrets.token_urlsafe(32)


def request_path(raw_path: str) -> str:
    """The path of a request target, without the query string or fragment.

    A target ``urlsplit`` cannot parse yields ``""`` rather than raising, so logging a
    malformed request never crashes the server.
    """
    try:
        return urlsplit(raw_path).path
    except ValueError:
        return ""


def bearer_ok(header_value: Optional[str], token: str) -> bool:
    """An empty server token never authenticates anything."""
    if not token:
        return False
    if not header_value or not header_value.startswith("Bearer "):
        return False
    return hmac.compare_digest(header_value[7:].encode("utf-8"), token.encode("utf-8"))


def _one(headers: Message, name: str) -> Tuple[bool, Optional[str]]:
    """(well-formed, value): a header sent more than once is never well-formed."""
    values = headers.get_all(name) or []
    if len(values) > 1:
        return False, None
    return True, (str(values[0]) if values else None)


def _has_header_block_defect(headers: Message) -> bool:
    """A defect in the header *block* itself, never a MIME multipart-*body* defect.

    ``http.client.parse_headers`` hands ``email``'s ``Parser`` only the header block (the
    body is read separately); for a multipart ``Content-Type`` the parser still tries to
    read that (necessarily absent, since no body was given to it) payload as MIME parts,
    and always records ``StartBoundaryNotFoundDefect``/``NoBoundaryInMultipartDefect``/
    ``MultipartInvariantViolationDefect`` as a result — on every real ``POST /upload``,
    not just a malformed one. Refusing on any defect at all would refuse every upload.

    What must still be caught is a header line the parser could not attach anywhere (the
    way a smuggled second ``Host`` hides): ``MissingHeaderBodySeparatorDefect`` (a
    non-header, non-continuation line found before the blank line),
    ``FirstHeaderLineIsContinuationDefect`` (the block starts with a continuation line),
    and the ``HeaderDefect`` family (a header whose own name or value is malformed, e.g.
    ``InvalidHeaderDefect`` for a line with no name before ``:``). These three are
    unrelated to the declared ``Content-Type`` and so never fire on a legitimate upload.
    """
    return any(
        isinstance(
            defect,
            (MissingHeaderBodySeparatorDefect, FirstHeaderLineIsContinuationDefect, HeaderDefect),
        )
        for defect in headers.defects
    )


def check_request(
    method: str, raw_path: str, headers: Message, peer_ip: str, port: int, token: str
) -> Optional[int]:
    """``None`` when the request may proceed, else the HTTP status to answer with."""
    if peer_ip not in LOOPBACK_PEERS:
        return 403
    if not raw_path.startswith("/"):
        # Chromium only ever sends origin-form targets; anything else (an absolute-form
        # target, say) is refused before it is parsed at all.
        return 403
    if _has_header_block_defect(headers):
        # A malformed header block can hide a second header (e.g. a smuggled ``Host``)
        # behind a line the parser could not attach to anything.
        return 403
    own_hosts = ("127.0.0.1:%d" % port, "localhost:%d" % port)
    ok, host = _one(headers, "Host")
    if not ok or host not in own_hosts:
        return 403
    ok, origin = _one(headers, "Origin")
    if not ok or (origin is not None and origin != "http://" + host):
        return 403
    if method == "OPTIONS":
        return 403
    try:
        parts = urlsplit(raw_path)
    except ValueError:
        return 403
    if parts.path == HEALTH_PATH:
        return None
    if parts.path not in PROTECTED_PATHS and method in ("GET", "HEAD"):
        return None
    ok, authorization = _one(headers, "Authorization")
    if not ok:
        return 401
    if authorization is not None:
        # A sent header decides the outcome by itself: a wrong one is never rescued by
        # a correct ``?t=``, and a correct one never needs it.
        return None if bearer_ok(authorization, token) else 401
    if parts.path == "/events" and method == "GET":
        supplied = parse_qs(parts.query).get("t", [])
        if len(supplied) == 1 and hmac.compare_digest(
            supplied[0].encode("utf-8"), token.encode("utf-8")
        ):
            return None
    return 401
