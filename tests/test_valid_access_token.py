"""T2VAuth.get_valid_access_token: TS ``auth.ts:452-467``. Refresh when the token is
missing, expired or about to expire; share the SDK's one refresh; None only for a dead
session, which is then signed out."""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Iterator, List, Optional

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import AuthenticationError, MemoryStorage, PartnerKeyError, Talk2View, User
from talk2view.auth import TOKEN_EXPIRY_SKEW_SECONDS


def jwt(exp: Any) -> str:
    def part(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return "{}.{}.signature".format(part({"alg": "HS256"}), part({"sub": "u1", "exp": exp}))


FRESH = jwt(time.time() + 3600)
NEW = jwt(time.time() + 7200)


def refreshed(request: Request) -> Response:
    return Response(
        json.dumps({"access_token": NEW, "refresh_token": "ref-2"}),
        content_type="application/json",
    )


@pytest.fixture
def t2v(httpserver: HTTPServer) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=httpserver.url_for(""))
    yield client
    client.close()


def signed_in(t2v: Talk2View, access: Optional[str], refresh: Optional[str] = "ref-1") -> None:
    if access is not None:
        t2v.storage.set("access_token", access)
    if refresh is not None:
        t2v.storage.set("refresh_token", refresh)
    t2v.storage.set("user", json.dumps({"id": "u1", "email": "ana@example.com"}))


def heard(t2v: Talk2View) -> List[Optional[User]]:
    seen: List[Optional[User]] = []
    t2v.auth.on_auth_state_change(seen.append)
    return seen


def refreshes(server: HTTPServer) -> int:
    return sum(1 for request, _ in server.log if request.path == "/v1/auth/refresh")


def test_a_fresh_token_is_returned_without_a_request(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    signed_in(t2v, FRESH)
    assert t2v.auth.get_valid_access_token() == FRESH
    assert httpserver.log == []


@pytest.mark.parametrize(
    "stored",
    [
        jwt(time.time() - 60),  # expired
        jwt(time.time() + TOKEN_EXPIRY_SKEW_SECONDS - 5),  # inside the skew
        None,  # missing
    ],
)
def test_an_expiring_or_missing_token_is_refreshed(
    t2v: Talk2View, httpserver: HTTPServer, stored: Optional[str]
) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_handler(refreshed)
    signed_in(t2v, stored)
    assert t2v.auth.get_valid_access_token() == NEW
    assert (t2v.storage.get("access_token"), t2v.storage.get("refresh_token")) == (NEW, "ref-2")
    assert json.loads(httpserver.log[0][0].get_data()) == {"refresh_token": "ref-1"}


def test_force_refresh_refreshes_a_fresh_token(t2v: Talk2View, httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_handler(refreshed)
    signed_in(t2v, FRESH)
    assert t2v.auth.get_valid_access_token(force_refresh=True) == NEW
    assert refreshes(httpserver) == 1


@pytest.mark.parametrize("opaque", ["tok-1", jwt("tomorrow"), "a.!!!.c", jwt(True)])
def test_a_token_whose_expiry_cannot_be_read_is_returned_as_it_is(
    t2v: Talk2View, httpserver: HTTPServer, opaque: str
) -> None:
    signed_in(t2v, opaque)
    assert t2v.auth.get_valid_access_token() == opaque
    assert httpserver.log == []


def test_a_rejected_refresh_signs_out_and_returns_none(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_json(
        {"error": {"type": "authentication_error", "message": "Invalid refresh token"}},
        status=401,
    )
    signed_in(t2v, jwt(time.time() - 60))
    seen = heard(t2v)
    assert t2v.auth.get_valid_access_token() is None
    assert not t2v.auth.is_authenticated() and t2v.auth.get_user() is None
    assert seen == [None]


def test_a_partner_key_refused_by_the_refresh_route_keeps_the_session(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    # The refresh route rejects a bad partner key with the same 401 status: that says
    # nothing about the refresh token, so the end-user stays signed in.
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_json(
        {"error": {"type": "partner_key_error", "message": "Invalid partner key"}},
        status=401,
    )
    signed_in(t2v, jwt(time.time() - 60))
    seen = heard(t2v)
    with pytest.raises(PartnerKeyError):
        t2v.auth.get_valid_access_token()
    assert t2v.storage.get("refresh_token") == "ref-1" and t2v.auth.get_user() is not None
    assert seen == []


def test_an_expiry_too_large_for_a_float_never_expires(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    far = jwt(10**400)  # TS reads it as Infinity
    signed_in(t2v, far)
    assert t2v.auth.get_valid_access_token() == far
    assert httpserver.log == []


def test_an_expiry_too_far_back_for_a_float_has_expired(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_handler(refreshed)
    signed_in(t2v, jwt(-(10**400)))  # TS reads it as -Infinity
    assert t2v.auth.get_valid_access_token() == NEW


def test_nothing_stored_is_none_and_signs_nobody_out(
    t2v: Talk2View, httpserver: HTTPServer
) -> None:
    seen = heard(t2v)
    assert t2v.auth.get_valid_access_token() is None
    assert httpserver.log == [] and seen == []


@pytest.mark.parametrize("status", [409, 429, 500])
def test_a_transient_failure_returns_the_stored_token_and_keeps_the_session(
    t2v: Talk2View, httpserver: HTTPServer, status: int
) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_json(
        {"error": {"type": "conflict", "message": "Already rotated"}}, status=status
    )
    stale = jwt(time.time() - 60)
    signed_in(t2v, stale)
    seen = heard(t2v)
    assert t2v.auth.get_valid_access_token() == stale
    assert t2v.storage.get("refresh_token") == "ref-1" and seen == []


def test_a_malformed_refresh_answer_raises(t2v: Talk2View, httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_json(
        {"access_token": NEW}
    )
    signed_in(t2v, jwt(time.time() - 60))
    with pytest.raises(AuthenticationError):
        t2v.auth.get_valid_access_token()
    assert t2v.storage.get("refresh_token") == "ref-1"  # no half pair stored


def test_an_unreadable_token_store_raises() -> None:
    class Unreadable(MemoryStorage):
        def get(self, key: str) -> Optional[str]:
            raise OSError("locked by another process")

    t2v = Talk2View("pk_test", storage=Unreadable())
    try:
        with pytest.raises(OSError):
            t2v.auth.get_valid_access_token()
    finally:
        t2v.close()


def test_concurrent_callers_share_one_refresh(t2v: Talk2View, httpserver: HTTPServer) -> None:
    gate = threading.Event()

    def slow_refresh(request: Request) -> Response:
        gate.wait(5)
        return refreshed(request)

    httpserver.expect_request("/v1/auth/refresh", method="POST").respond_with_handler(
        slow_refresh
    )
    signed_in(t2v, jwt(time.time() - 60))
    results: List[Optional[str]] = []
    callers = [
        threading.Thread(target=lambda: results.append(t2v.auth.get_valid_access_token()))
        for _ in range(3)
    ]
    for caller in callers:
        caller.start()
    time.sleep(0.2)  # every caller reads the stale token, one refreshes, the rest wait
    gate.set()
    for caller in callers:
        caller.join(5)
    assert results == [NEW, NEW, NEW]
    assert refreshes(httpserver) == 1
