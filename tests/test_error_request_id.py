"""A failed request says what to quote to support (``request_id``) and, when the
engine says, how long to wait (``retry_after``). Every request names this SDK
and its version in ``X-T2V-Client``."""

from __future__ import annotations

import httpx
import pytest

from talk2view.client import CLIENT_HEADER, SDK_VERSION, T2VClient
from talk2view.errors import PartnerKeyError, T2VError, parse_retry_after
from talk2view.storage import MemoryStorage
from talk2view.streaming import parse_sse_stream


def _client(httpserver) -> T2VClient:  # type: ignore[no-untyped-def]
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    return T2VClient("pk_test", storage, httpserver.url_for(""))


def _raised(httpserver) -> T2VError:  # type: ignore[no-untyped-def]
    with pytest.raises(T2VError) as excinfo:
        _client(httpserver).request("/v1/thing")
    return excinfo.value


def test_request_id_comes_from_the_error_body(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"error": {"type": "not_found", "message": "Not Found", "request_id": "req-body"}},
        status=404,
    )
    assert _raised(httpserver).request_id == "req-body"


def test_request_id_falls_back_to_the_header(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/thing").respond_with_data(
        "<html>bad gateway</html>", status=502, headers={"X-Request-Id": "req-hdr"}
    )
    err = _raised(httpserver)
    assert err.request_id == "req-hdr"
    assert "html" not in str(err)


def test_request_id_is_none_when_the_engine_sent_none(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"error": {"type": "internal_error", "message": "x"}}, status=500
    )
    err = _raised(httpserver)
    assert err.request_id is None
    assert err.retry_after is None


def test_retry_after_is_read_from_the_header(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"error": {"type": "rate_limit_exceeded", "message": "Slow down", "request_id": "r"}},
        status=429,
        headers={"Retry-After": "60"},
    )
    err = _raised(httpserver)
    assert err.retry_after == 60
    assert err.status_code == 429


def test_a_partner_key_error_keeps_its_class_and_request_id(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request("/v1/thing").respond_with_json(
        {"error": {"type": "partner_key_error", "message": "No", "request_id": "req-pk"}},
        status=401,
    )
    err = _raised(httpserver)
    assert isinstance(err, PartnerKeyError)
    assert err.request_id == "req-pk"


def test_parse_retry_after_reads_seconds_and_http_dates() -> None:
    now = 1_792_490_400.0  # Tue, 20 Oct 2026 10:00:00 GMT
    assert parse_retry_after("120", now) == 120
    assert parse_retry_after("Tue, 20 Oct 2026 10:00:30 GMT", now) == 30
    assert parse_retry_after("Tue, 20 Oct 2026 09:00:00 GMT", now) == 0
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after(None, now) is None


def test_a_stream_error_carries_the_request_id() -> None:
    body = (
        'data: {"error": {"type": "internal_error", "message": "Oops.", '
        '"request_id": "req-sse"}}\n\ndata: [DONE]\n\n'
    )
    response = httpx.Response(200, text=body)
    (chunk,) = list(parse_sse_stream(response))
    assert chunk.error is not None
    assert chunk.error.request_id == "req-sse"


def test_the_client_header_names_this_sdk() -> None:
    assert CLIENT_HEADER == "talk2view-python/" + SDK_VERSION


def test_every_request_sends_the_client_header(httpserver) -> None:  # type: ignore[no-untyped-def]
    httpserver.expect_request(
        "/v1/thing", headers={"X-T2V-Client": CLIENT_HEADER}
    ).respond_with_json({"ok": True})
    assert _client(httpserver).request("/v1/thing") == {"ok": True}


def test_the_token_refresh_sends_the_client_header(httpserver) -> None:  # type: ignore[no-untyped-def]
    storage = MemoryStorage()
    storage.set("access_token", "old")
    storage.set("refresh_token", "old_r")
    httpserver.expect_ordered_request("/v1/thing").respond_with_json(
        {"error": {"type": "authentication_error", "message": "expired"}}, status=401
    )
    httpserver.expect_ordered_request(
        "/v1/auth/refresh", method="POST", headers={"X-T2V-Client": CLIENT_HEADER}
    ).respond_with_json({"access_token": "new", "refresh_token": "new_r", "expires_in": 3600})
    httpserver.expect_ordered_request(
        "/v1/thing", headers={"X-T2V-Client": CLIENT_HEADER}
    ).respond_with_json({"ok": True})
    client = T2VClient("pk_test", storage, httpserver.url_for(""))
    assert client.request("/v1/thing") == {"ok": True}
    httpserver.check_assertions()
