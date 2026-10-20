"""Tests for the token-refresh race and 401 handling on streams."""

from __future__ import annotations

import json
import threading
from typing import Any, Dict, Iterator, List, Tuple
from unittest.mock import patch

import httpx
import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view.client import T2VClient
from talk2view.errors import AuthenticationError, NetworkError, PartnerKeyError, T2VError
from talk2view.storage import MemoryStorage

NEW_TOKENS = {
    "access_token": "new_tok",
    "refresh_token": "new_ref",
    "token_type": "bearer",
    "expires_in": 3600,
}


@pytest.fixture()
def threaded_server() -> Iterator[HTTPServer]:
    """Both 401s must be in flight at once, so the server needs threads."""
    server = HTTPServer(threaded=True)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


def _json(payload: Dict[str, Any], status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype="application/json")


def _make_client(server: HTTPServer) -> Tuple[T2VClient, MemoryStorage]:
    storage = MemoryStorage()
    storage.set("access_token", "old_tok")
    storage.set("refresh_token", "old_ref")
    return T2VClient("pk_test", storage, server.url_for("")), storage


def _paths(server: HTTPServer) -> List[str]:
    return [entry[0].path for entry in server.log]


class TestRefreshRace:
    def test_two_threads_401_single_refresh_call(self, threaded_server: HTTPServer) -> None:
        both_rejected = threading.Barrier(2, timeout=5)
        refresh_bodies: List[Dict[str, Any]] = []

        def protected(request: Request) -> Response:
            if request.headers.get("Authorization") == "Bearer old_tok":
                both_rejected.wait()  # hold both 401s until both requests arrived
                return _json({"error": {"type": "authentication_error", "message": "Expired"}}, 401)
            return _json({"ok": True})

        def refresh(request: Request) -> Response:
            refresh_bodies.append(request.get_json())
            return _json(NEW_TOKENS)

        threaded_server.expect_request("/v1/test").respond_with_handler(protected)
        threaded_server.expect_request("/v1/auth/refresh").respond_with_handler(refresh)
        client, storage = _make_client(threaded_server)
        results: List[Any] = []
        failures: List[BaseException] = []

        def call() -> None:
            try:
                results.append(client.request("/v1/test"))
            except BaseException as exc:  # pragma: no cover — reported below
                failures.append(exc)

        workers = [threading.Thread(target=call, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)

        assert failures == []
        assert results == [{"ok": True}, {"ok": True}]
        # ONE refresh, made with the ORIGINAL refresh token; the loser of the
        # race saw the new access token in storage and skipped the server.
        assert refresh_bodies == [{"refresh_token": "old_ref"}]
        assert storage.get("access_token") == "new_tok"
        assert storage.get("refresh_token") == "new_ref"
        client.close()

    def test_no_auth_header_401_still_single_refresh(
        self, threaded_server: HTTPServer
    ) -> None:
        """The guard must also hold when the 401'd request carried NO token.

        Storage had no access token, so both requests went out unauthenticated.
        Once the winner has stored a token, the loser's retry should use it —
        spending the freshly rotated refresh token for a second time instead
        gets a 409 that fails a request which would have succeeded.
        """
        both_rejected = threading.Barrier(2, timeout=5)
        refresh_bodies: List[Dict[str, Any]] = []
        recorded = threading.Lock()

        def protected(request: Request) -> Response:
            if "Authorization" not in request.headers:
                both_rejected.wait()  # hold both 401s until both arrived
                return _json(
                    {"error": {"type": "authentication_error", "message": "No token"}}, 401
                )
            return _json({"ok": True})

        def refresh(request: Request) -> Response:
            with recorded:
                refresh_bodies.append(request.get_json())
            return _json(NEW_TOKENS)

        threaded_server.expect_request("/v1/test").respond_with_handler(protected)
        threaded_server.expect_request("/v1/auth/refresh").respond_with_handler(refresh)
        client, storage = _make_client(threaded_server)
        storage.delete("access_token")  # signed in, but no access token cached
        results: List[Any] = []
        failures: List[BaseException] = []

        def call() -> None:
            try:
                results.append(client.request("/v1/test"))
            except BaseException as exc:  # pragma: no cover — reported below
                failures.append(exc)

        workers = [threading.Thread(target=call, daemon=True) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)

        assert failures == []
        assert results == [{"ok": True}, {"ok": True}]
        assert refresh_bodies == [{"refresh_token": "old_ref"}]
        assert storage.get("access_token") == "new_tok"
        client.close()

    def test_same_token_still_refreshes(self, httpserver: HTTPServer) -> None:
        """The guard must not skip the refresh for the thread that gets there first."""
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        httpserver.expect_ordered_request("/v1/test").respond_with_json({"ok": True})
        client, _ = _make_client(httpserver)

        assert client.request("/v1/test") == {"ok": True}
        assert _paths(httpserver) == ["/v1/test", "/v1/auth/refresh", "/v1/test"]

    def test_409_is_transient_keeps_tokens(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {"error": {"type": "refresh_conflict", "message": "already rotated"}}, status=409
        )
        client, storage = _make_client(httpserver)

        with pytest.raises(NetworkError, match="temporarily unavailable"):
            client.request("/v1/test")

        # 409 = lost a rotation race elsewhere, NOT a dead session: stay signed in.
        assert storage.get("access_token") == "old_tok"
        assert storage.get("refresh_token") == "old_ref"
        assert _paths(httpserver) == ["/v1/test", "/v1/auth/refresh"]


class TestUpload401:
    """upload_request() has the same 401 -> refresh -> retry path as request()."""

    FILES = {"file": ("scan.png", b"bytes", "image/png")}
    ATTACHMENT = {
        "id": "att_1",
        "filename": "scan.png",
        "mime_type": "image/png",
        "size_bytes": 5,
    }

    def test_upload_401_refreshes_and_retries_with_the_new_token(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_ordered_request(
            "/v1/attachments", method="POST", headers={"Authorization": "Bearer old_tok"}
        ).respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        httpserver.expect_ordered_request(
            "/v1/attachments", method="POST", headers={"Authorization": "Bearer new_tok"}
        ).respond_with_json(self.ATTACHMENT)
        client, storage = _make_client(httpserver)

        result = client.upload_request("/v1/attachments", files=self.FILES)

        assert result == self.ATTACHMENT
        assert _paths(httpserver) == [
            "/v1/attachments",
            "/v1/auth/refresh",
            "/v1/attachments",
        ]
        assert storage.get("access_token") == "new_tok"

    def test_upload_401_with_a_dead_refresh_token_logs_out(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_ordered_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Invalid refresh token"}},
            status=401,
        )
        client, storage = _make_client(httpserver)

        with pytest.raises(AuthenticationError, match="Session expired"):
            client.upload_request("/v1/attachments", files=self.FILES)

        assert storage.get("access_token") is None
        assert storage.get("refresh_token") is None

    def test_upload_401_with_a_transient_refresh_failure_keeps_the_session(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_ordered_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {"error": {"type": "refresh_conflict", "message": "already rotated"}}, status=409
        )
        client, storage = _make_client(httpserver)

        with pytest.raises(NetworkError, match="temporarily unavailable"):
            client.upload_request("/v1/attachments", files=self.FILES)

        assert storage.get("access_token") == "old_tok"
        assert storage.get("refresh_token") == "old_ref"

    def test_upload_401_from_a_bad_partner_key_does_not_touch_the_tokens(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "partner_key_error", "message": "Unknown partner key"}},
            status=401,
        )
        client, storage = _make_client(httpserver)

        with pytest.raises(PartnerKeyError, match="Unknown partner key"):
            client.upload_request("/v1/attachments", files=self.FILES)

        assert _paths(httpserver) == ["/v1/attachments"]  # no refresh attempted
        assert storage.get("refresh_token") == "old_ref"

    def test_upload_error_after_a_successful_refresh_is_raised(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_ordered_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        httpserver.expect_ordered_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "payload_too_large", "message": "File too large"}}, status=413
        )
        client, _ = _make_client(httpserver)

        with pytest.raises(T2VError, match="File too large") as excinfo:
            client.upload_request("/v1/attachments", files=self.FILES)

        assert excinfo.value.status_code == 413

    def test_upload_network_failure_on_the_retry_is_a_network_error(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_ordered_request("/v1/attachments", method="POST").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        client, _ = _make_client(httpserver)
        original_post = client._http.post
        calls: List[int] = []

        def post(*args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            if len(calls) == 3:  # upload, refresh, retry
                raise httpx.ConnectError("connection reset")
            return original_post(*args, **kwargs)

        with patch.object(client._http, "post", side_effect=post):
            with pytest.raises(NetworkError, match="Network request failed"):
                client.upload_request("/v1/attachments", files=self.FILES)


class TestStream401:
    def test_stream_401_refreshes_once(self, httpserver: HTTPServer) -> None:
        sse = (
            'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"hi"},'
            '"finish_reason":null}]}\n\ndata: [DONE]\n\n'
        )
        httpserver.expect_ordered_request(
            "/v1/sessions/s1/messages", headers={"Authorization": "Bearer old_tok"}
        ).respond_with_json(
            {"error": {"type": "authentication_error", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        httpserver.expect_ordered_request(
            "/v1/sessions/s1/messages", headers={"Authorization": "Bearer new_tok"}
        ).respond_with_data(sse, content_type="text/event-stream")
        client, storage = _make_client(httpserver)

        chunks = list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))

        assert [c.choices[0].delta.content for c in chunks] == ["hi"]
        assert _paths(httpserver).count("/v1/auth/refresh") == 1
        assert storage.get("access_token") == "new_tok"

    def test_stream_401_after_refresh_does_not_loop(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/sessions/s1/messages").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Still expired"}}, status=401
        )
        httpserver.expect_request("/v1/auth/refresh").respond_with_json(NEW_TOKENS)
        client, _ = _make_client(httpserver)

        with pytest.raises(T2VError, match="Still expired") as excinfo:
            list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))

        assert excinfo.value.status_code == 401
        assert _paths(httpserver) == [
            "/v1/sessions/s1/messages",
            "/v1/auth/refresh",
            "/v1/sessions/s1/messages",
        ]
