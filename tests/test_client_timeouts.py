"""Request and upload timeouts: configurable, 30 s and 120 s by default as in the
TypeScript SDK (``types.ts`` ``requestTimeout``, ``client.ts`` ``DEFAULT_UPLOAD_TIMEOUT``).
A chat stream keeps its 300 s read timeout: heartbeats arrive well inside it."""

from __future__ import annotations

import time
from typing import Any, Callable, Dict

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import MemoryStorage, NetworkError, Talk2View
from talk2view.client import (
    DEFAULT_TIMEOUT,
    DEFAULT_UPLOAD_TIMEOUT,
    STREAM_TIMEOUT,
    T2VClient,
)

PNG = ("scan.png", b"\x89PNG\r\n\x1a\n", "image/png")


def _slow(delay: float) -> Callable[[Request], Response]:
    def handler(request: Request) -> Response:
        time.sleep(delay)
        return Response('{"id": "a1"}', content_type="application/json")

    return handler


def _client(server: HTTPServer, **timeouts: float) -> T2VClient:
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    return T2VClient("pk_test", storage, base_url=server.url_for(""), **timeouts)


def test_the_defaults_match_the_typescript_sdk() -> None:
    assert (DEFAULT_TIMEOUT, DEFAULT_UPLOAD_TIMEOUT, STREAM_TIMEOUT) == (30.0, 120.0, 300.0)
    client = T2VClient("pk_test", MemoryStorage())
    try:
        assert (client.request_timeout, client.upload_timeout) == (30.0, 120.0)
        assert client._http.timeout == httpx.Timeout(30.0)
    finally:
        client.close()


def test_talk2view_hands_its_timeouts_to_the_client() -> None:
    t2v = Talk2View("pk_test", request_timeout=5, upload_timeout=600)
    try:
        assert (t2v._client.request_timeout, t2v._client.upload_timeout) == (5.0, 600.0)
        assert t2v._client._http.timeout == httpx.Timeout(5.0)
    finally:
        t2v.close()


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), True, "30", None])
def test_a_timeout_that_is_not_a_positive_number_of_seconds_is_refused(bad: Any) -> None:
    with pytest.raises(ValueError):
        T2VClient("pk_test", MemoryStorage(), request_timeout=bad)
    with pytest.raises(ValueError):
        T2VClient("pk_test", MemoryStorage(), upload_timeout=bad)


def test_a_request_gives_up_after_request_timeout(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/config").respond_with_handler(_slow(0.5))
    client = _client(httpserver, request_timeout=0.1)
    try:
        started = time.monotonic()
        with pytest.raises(NetworkError):
            client.request("/v1/config")
        assert time.monotonic() - started < 0.45
    finally:
        client.close()


def test_an_upload_has_its_own_timeout(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/attachments", method="POST").respond_with_handler(_slow(0.3))
    client = _client(httpserver, request_timeout=0.1, upload_timeout=5.0)
    try:
        # Longer than request_timeout, well inside upload_timeout: it succeeds.
        assert client.upload_request("/v1/attachments", files={"file": PNG}) == {"id": "a1"}
    finally:
        client.close()
    client = _client(httpserver, request_timeout=5.0, upload_timeout=0.1)
    try:
        with pytest.raises(NetworkError):
            client.upload_request("/v1/attachments", files={"file": PNG})
    finally:
        client.close()


def test_a_stream_connects_within_request_timeout_and_reads_for_300_s() -> None:
    client = T2VClient("pk_test", MemoryStorage(), request_timeout=7.0)
    seen: Dict[str, Any] = {}

    def refuse(method: str, url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise httpx.ConnectError("refused")

    client._http.stream = refuse  # type: ignore[method-assign]
    try:
        with pytest.raises(httpx.ConnectError):
            list(client.stream_request("/v1/sessions/s/messages", {}))
        assert seen["timeout"] == httpx.Timeout(7.0, read=STREAM_TIMEOUT)
    finally:
        client.close()


def test_a_token_refresh_uses_request_timeout() -> None:
    storage = MemoryStorage()
    storage.set("refresh_token", "ref")
    client = T2VClient("pk_test", storage, request_timeout=7.0)
    seen: Dict[str, Any] = {}

    def refuse(url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise httpx.ConnectError("refused")

    client._http.post = refuse  # type: ignore[method-assign]
    try:
        assert client._try_refresh_token(None) == "transient"
        assert seen["timeout"] == 7.0
    finally:
        client.close()
