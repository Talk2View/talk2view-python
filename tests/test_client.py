"""Tests for T2VClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

import talk2view.client as client_module
from talk2view.client import T2VClient
from talk2view.errors import AuthenticationError, NetworkError, T2VError
from talk2view.storage import MemoryStorage


class TestT2VClient:
    def _make_client(self, base_url: str = "http://localhost:9999") -> tuple:  # type: ignore[type-arg]
        storage = MemoryStorage()
        client = T2VClient(
            partner_key="pk_test_123",
            storage=storage,
            base_url=base_url,
        )
        return client, storage

    def test_headers_with_auth(self) -> None:
        client, storage = self._make_client()
        storage.set("access_token", "tok_abc")
        headers = client._headers(requires_auth=True)
        assert headers["X-T2V-Partner-Key"] == "pk_test_123"
        assert headers["Authorization"] == "Bearer tok_abc"
        assert headers["Content-Type"] == "application/json"

    def test_headers_without_auth(self) -> None:
        client, _ = self._make_client()
        headers = client._headers(requires_auth=False)
        assert "Authorization" not in headers
        assert headers["X-T2V-Partner-Key"] == "pk_test_123"

    def test_headers_no_token(self) -> None:
        client, _ = self._make_client()
        headers = client._headers(requires_auth=True)
        assert "Authorization" not in headers

    def test_request_network_error(self) -> None:
        client, _ = self._make_client("http://localhost:1")
        with pytest.raises(NetworkError, match="Network request failed"):
            client.request(
                "/v1/auth/login",
                method="POST",
                body={"email": "a", "password": "b"},
                requires_auth=False,
            )

    def test_clear_auth(self) -> None:
        client, storage = self._make_client()
        storage.set("access_token", "tok")
        storage.set("refresh_token", "ref")
        storage.set("user", '{"id":"1","email":"a@b.com"}')
        storage.set("user_api_key", "uak_1")
        storage.set("is_anonymous", "true")

        client._clear_auth()

        assert storage.get("access_token") is None
        assert storage.get("refresh_token") is None
        assert storage.get("user") is None
        assert storage.get("user_api_key") is None
        assert storage.get("is_anonymous") is None

    def test_parse_error_json(self) -> None:
        """Test _parse_error with a valid JSON error body."""
        from unittest.mock import MagicMock

        response = MagicMock()
        response.json.return_value = {
            "error": {"type": "auth_error", "message": "Invalid credentials"}
        }
        result = T2VClient._parse_error(response)
        assert result["message"] == "Invalid credentials"
        assert result["type"] == "auth_error"

    def test_parse_error_non_json(self) -> None:
        from unittest.mock import MagicMock

        response = MagicMock()
        response.json.side_effect = Exception("not json")
        response.status_code = 500
        response.text = "Internal Server Error"
        result = T2VClient._parse_error(response)
        assert "Request failed (HTTP 500)" in result["message"]


class TestT2VClientWithServer:
    """Integration tests using pytest-httpserver."""

    def test_request_success(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/test").respond_with_json({"ok": True})

        storage = MemoryStorage()
        storage.set("access_token", "tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        result = c.request("/v1/test")
        assert result == {"ok": True}

    def test_request_401_no_refresh(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Unauthorized"}}, status=401
        )

        storage = MemoryStorage()
        storage.set("access_token", "expired_tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with pytest.raises(AuthenticationError, match="Session expired"):
            c.request("/v1/test")

    def test_request_error_status(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/test").respond_with_json(
            {"error": {"type": "bad_request", "message": "Invalid input"}}, status=400
        )

        storage = MemoryStorage()
        storage.set("access_token", "tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with pytest.raises(T2VError, match="Invalid input"):
            c.request("/v1/test")

    def test_request_204_no_content(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/logout").respond_with_data("", status=204)

        storage = MemoryStorage()
        storage.set("access_token", "tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        result = c.request("/v1/logout", method="POST")
        assert result is None

    def test_request_401_with_refresh(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """Test that 401 triggers token refresh and retries."""
        # First request returns 401
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Expired"}}, status=401
        )
        # Refresh succeeds
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {
                "access_token": "new_tok",
                "refresh_token": "new_ref",
                "token_type": "bearer",
                "expires_in": 3600,
            }
        )
        # Retry succeeds
        httpserver.expect_ordered_request("/v1/test").respond_with_json({"ok": True})

        storage = MemoryStorage()
        storage.set("access_token", "old_tok")
        storage.set("refresh_token", "old_ref")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        result = c.request("/v1/test")
        assert result == {"ok": True}
        assert storage.get("access_token") == "new_tok"
        assert storage.get("refresh_token") == "new_ref"

    @pytest.mark.parametrize("refresh_status", [409, 429, 503])
    def test_request_transient_refresh_keeps_session(
        self, httpserver, refresh_status: int
    ) -> None:  # type: ignore[no-untyped-def]
        """A non-401 refresh failure (409 already-rotated, 429 rate-limited, 5xx)
        is transient: raise a retryable NetworkError WITHOUT clearing tokens."""
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {"error": {"type": "refresh_conflict", "message": "rotated"}},
            status=refresh_status,
        )

        storage = MemoryStorage()
        storage.set("access_token", "old_tok")
        storage.set("refresh_token", "old_ref")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with pytest.raises(NetworkError, match="temporarily unavailable"):
            c.request("/v1/test")
        # Session preserved — the next attempt picks up where this left off.
        assert storage.get("access_token") == "old_tok"
        assert storage.get("refresh_token") == "old_ref"

    def test_request_refresh_missing_refresh_token_does_not_mutate(
        self, httpserver
    ) -> None:  # type: ignore[no-untyped-def]
        """A 2xx refresh body missing refresh_token must NOT persist a mismatched
        new-access + old-refresh pair, and must surface the error clearly."""
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Expired"}}, status=401
        )
        # Partial success body: access_token present, refresh_token missing.
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {
                "access_token": "new_tok",
                "token_type": "bearer",
                "expires_in": 3600,
            }
        )

        storage = MemoryStorage()
        storage.set("access_token", "old_tok")
        storage.set("refresh_token", "old_ref")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with pytest.raises(AuthenticationError, match="incomplete response"):
            c.request("/v1/test")
        # Neither token mutated — no half-written, mismatched pair persisted.
        assert storage.get("access_token") == "old_tok"
        assert storage.get("refresh_token") == "old_ref"

    def test_request_refresh_complete_body_writes_both(
        self, httpserver
    ) -> None:  # type: ignore[no-untyped-def]
        """A complete refresh body writes BOTH tokens atomically."""
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {
                "access_token": "new_tok",
                "refresh_token": "new_ref",
                "token_type": "bearer",
                "expires_in": 3600,
            }
        )
        httpserver.expect_ordered_request("/v1/test").respond_with_json({"ok": True})

        storage = MemoryStorage()
        storage.set("access_token", "old_tok")
        storage.set("refresh_token", "old_ref")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        result = c.request("/v1/test")
        assert result == {"ok": True}
        assert storage.get("access_token") == "new_tok"
        assert storage.get("refresh_token") == "new_ref"

    def test_request_refresh_401_logs_out(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """A 401 from /refresh means the token is genuinely dead -> clear + raise."""
        httpserver.expect_ordered_request("/v1/test").respond_with_json(
            {"error": {"type": "auth", "message": "Expired"}}, status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh").respond_with_json(
            {"error": {"type": "auth", "message": "Invalid refresh token"}}, status=401
        )

        storage = MemoryStorage()
        storage.set("access_token", "old_tok")
        storage.set("refresh_token", "dead_ref")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with pytest.raises(AuthenticationError, match="Session expired"):
            c.request("/v1/test")
        assert storage.get("access_token") is None
        assert storage.get("refresh_token") is None


class TestAbortResponse:
    """The cancel closer's two paths: shut the socket down, or close()."""

    def test_socketless_response_falls_back_to_close(self) -> None:
        """A mock transport exposes no network_stream, so there is no socket to
        shut down and nothing can be blocked in recv(): close() is enough."""
        transport = httpx.MockTransport(lambda request: httpx.Response(200, text="body"))
        with httpx.Client(transport=transport) as client:
            with client.stream("GET", "http://example.invalid/x") as streamed:
                assert streamed.extensions.get("network_stream") is None

                client_module._abort_response(streamed)

                assert streamed.is_closed is True

    def test_a_shutdown_failure_is_swallowed(self) -> None:
        """A socket already torn down by the peer raises OSError on shutdown;
        the closer must not propagate it into cancel()."""
        sock = MagicMock()
        sock.shutdown.side_effect = OSError("already closed")
        network_stream = MagicMock()
        network_stream.get_extra_info.return_value = sock
        response = MagicMock()
        response.extensions = {"network_stream": network_stream}

        client_module._abort_response(response)  # must not raise

        sock.shutdown.assert_called_once()
        response.close.assert_not_called()


class TestT2VClientConnectionPooling:
    """The client must hold ONE pooled httpx.Client and reuse it across calls.

    Without pooling, every hop of a chat turn (create session -> register tools
    -> message -> N resume round-trips) opened a brand-new TCP + TLS connection,
    paying a fresh handshake per hop. These tests pin the pooling behavior so it
    cannot silently regress back to one-shot per-call connections.
    """

    def test_constructs_a_single_pooled_client(self) -> None:
        """A new httpx.Client is created once at construction, not per request."""
        with patch.object(
            client_module.httpx, "Client", wraps=httpx.Client
        ) as client_ctor:
            T2VClient(
                "pk_test",
                MemoryStorage(),
                base_url="http://localhost:9999",
            )
        client_ctor.assert_called_once()

    def test_all_requests_reuse_the_same_pooled_client(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """Multiple request()/stream calls dispatch through one httpx.Client.

        Pre-fix this failed for the right reason: each call went through a
        module-level httpx.request/post/stream, so the pooled client was never
        used (zero dispatches through it) and a new connection was opened per
        call.
        """
        httpserver.expect_request("/v1/test").respond_with_json({"ok": True})

        storage = MemoryStorage()
        storage.set("access_token", "tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        # Spy on the pooled client's request method without breaking dispatch.
        with patch.object(
            c._http, "request", wraps=c._http.request
        ) as spy:
            assert c.request("/v1/test") == {"ok": True}
            assert c.request("/v1/test") == {"ok": True}
            assert c.request("/v1/test") == {"ok": True}

        # All three round-trips went through the ONE pooled client.
        assert spy.call_count == 3

    def test_module_level_httpx_request_is_not_used(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """The one-shot module-level httpx.request must not be called at all.

        This is the explicit fail-before guard: the old code called
        httpx.request() per call; the pooled client never touches it.
        """
        httpserver.expect_request("/v1/test").respond_with_json({"ok": True})

        storage = MemoryStorage()
        storage.set("access_token", "tok")
        c = T2VClient("pk_test", storage, httpserver.url_for(""))

        with patch.object(client_module.httpx, "request") as one_shot:
            c.request("/v1/test")
        one_shot.assert_not_called()

    def test_close_closes_the_pooled_client(self) -> None:
        c = T2VClient("pk_test", MemoryStorage(), base_url="http://localhost:9999")
        c._http = MagicMock()
        c.close()
        c._http.close.assert_called_once()

    def test_close_is_idempotent(self) -> None:
        """Closing twice must not raise (real httpx.Client tolerates this)."""
        c = T2VClient("pk_test", MemoryStorage(), base_url="http://localhost:9999")
        c.close()
        c.close()  # must not raise

    def test_context_manager_closes_on_exit(self) -> None:
        sentinel = MagicMock()
        with T2VClient(
            "pk_test", MemoryStorage(), base_url="http://localhost:9999"
        ) as c:
            c._http = sentinel
        sentinel.close.assert_called_once()


class TestTalk2ViewLifecycle:
    """The high-level wrapper must close its underlying pooled client too."""

    def test_close_closes_underlying_client(self) -> None:
        from talk2view import Talk2View

        t2v = Talk2View("pk_test")
        t2v._client = MagicMock()
        t2v.close()
        t2v._client.close.assert_called_once()

    def test_context_manager_closes_underlying_client(self) -> None:
        from talk2view import Talk2View

        sentinel = MagicMock()
        with Talk2View("pk_test") as t2v:
            t2v._client = sentinel
        sentinel.close.assert_called_once()
