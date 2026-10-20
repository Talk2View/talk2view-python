"""Tests for the error hierarchy, error_for_type and is_session_gone."""

from __future__ import annotations

import pytest
from pytest_httpserver import HTTPServer

from talk2view.client import T2VClient
from talk2view.errors import (
    SESSION_LOST_AFTER_DENY_MESSAGE,
    SESSION_LOST_MESSAGE,
    AuthenticationError,
    NetworkError,
    PartnerKeyError,
    SessionError,
    T2VError,
    error_for_type,
    is_session_gone,
)
from talk2view.storage import MemoryStorage


def _make_client(base_url: str) -> tuple:  # type: ignore[type-arg]
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    storage.set("refresh_token", "ref")
    return T2VClient("pk_test", storage, base_url), storage


class TestT2VError:
    def test_single_arg_form_still_works(self) -> None:
        err = T2VError("boom")
        assert str(err) == "boom"
        assert err.type == "sdk_error"
        assert err.status_code is None
        assert err.code is None
        assert err.detail is None

    def test_subclass_single_arg_defaults(self) -> None:
        assert AuthenticationError("x").type == "authentication_error"
        assert AuthenticationError("x").status_code == 401
        assert PartnerKeyError("x").type == "partner_key_error"
        assert PartnerKeyError("x").status_code == 401
        assert SessionError("x").type == "session_error"
        assert SessionError("x").status_code is None
        assert NetworkError("x").type == "network_error"


class TestErrorForType:
    def test_error_for_type_maps_three_types(self) -> None:
        auth = error_for_type("a", "authentication_error", 401)
        key = error_for_type("b", "partner_key_error", 401)
        gone = error_for_type("c", "session_not_found", 404)

        assert type(auth) is AuthenticationError
        assert type(key) is PartnerKeyError
        assert type(gone) is SessionError
        assert gone.type == "session_not_found"
        assert gone.status_code == 404

    def test_not_found_stays_base_T2VError(self) -> None:  # noqa: N802
        err = error_for_type("Session not found", "not_found", 404)

        assert type(err) is T2VError
        assert err.type == "not_found"
        assert is_session_gone(err) is True

    def test_missing_type_falls_back_to_sdk_error(self) -> None:
        err = error_for_type("x", None, 500)
        assert type(err) is T2VError
        assert err.type == "sdk_error"


class TestIsSessionGone:
    def test_matches_both_engine_types(self) -> None:
        assert is_session_gone(T2VError("x", "not_found", 404)) is True
        assert is_session_gone(SessionError("x", "session_not_found", 404)) is True

    def test_rejects_other_errors(self) -> None:
        assert is_session_gone(T2VError("x", "not_found", 400)) is False
        assert is_session_gone(T2VError("x", "internal_error", 404)) is False
        assert is_session_gone(ValueError("x")) is False


class TestSessionLostMessages:
    def test_copied_verbatim_from_ts_sdk(self) -> None:
        assert SESSION_LOST_MESSAGE == (
            "The chat session ended before that step could be confirmed, so the "
            "assistant never got the result. It may already have been applied — "
            "check before sending your message again."
        )
        assert SESSION_LOST_AFTER_DENY_MESSAGE == (
            "The chat session ended before that step could be confirmed, so the "
            "assistant never got your answer. Send your message again."
        )


class TestClientErrorMapping:
    def test_code_and_detail_preserved_not_in_message(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/thing").respond_with_json(
            {
                "error": {
                    "type": "budget_exceeded",
                    "message": "You have used your demo allowance.",
                    "code": "anonymous_budget",
                    "detail": "Traceback: /srv/app/billing.py line 9",
                }
            },
            status=402,
        )
        client, _ = _make_client(httpserver.url_for(""))

        with pytest.raises(T2VError) as excinfo:
            client.request("/v1/thing")

        err = excinfo.value
        assert str(err) == "You have used your demo allowance."
        assert err.type == "budget_exceeded"
        assert err.status_code == 402
        assert err.code == "anonymous_budget"
        assert err.detail == "Traceback: /srv/app/billing.py line 9"
        assert "anonymous_budget" not in str(err)
        assert "billing.py" not in str(err)

    def test_401_with_partner_key_error_type(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/test").respond_with_json(
            {"error": {"type": "partner_key_error", "message": "Invalid partner key"}},
            status=401,
        )
        client, storage = _make_client(httpserver.url_for(""))

        with pytest.raises(PartnerKeyError, match="Invalid partner key"):
            client.request("/v1/test")

        # A bad partner key is not an expired user session: no refresh attempt,
        # and the end-user stays signed in.
        assert [entry[0].path for entry in httpserver.log] == ["/v1/test"]
        assert storage.get("access_token") == "tok"
        assert storage.get("refresh_token") == "ref"

    def test_public_401_authentication_error_is_not_a_partner_key_error(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/auth/login").respond_with_json(
            {"error": {"type": "authentication_error", "message": "Invalid credentials"}},
            status=401,
        )
        client, _ = _make_client(httpserver.url_for(""))

        with pytest.raises(AuthenticationError, match="Invalid credentials") as excinfo:
            client.request(
                "/v1/auth/login",
                method="POST",
                body={"email": "a@b.com", "password": "pw"},
                requires_auth=False,
            )

        assert not isinstance(excinfo.value, PartnerKeyError)

    def test_404_not_found_is_base_error_and_session_gone(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/sessions/s1/messages").respond_with_json(
            {"error": {"type": "not_found", "message": "Session not found"}}, status=404
        )
        client, _ = _make_client(httpserver.url_for(""))

        with pytest.raises(T2VError) as excinfo:
            list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}))

        assert type(excinfo.value) is T2VError
        assert is_session_gone(excinfo.value) is True

    def test_detail_wrapped_error_envelope_is_unwrapped(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/thing").respond_with_json(
            {"detail": {"error": {"type": "tool_call_id_mismatch", "message": "Moved on"}}},
            status=409,
        )
        client, _ = _make_client(httpserver.url_for(""))

        with pytest.raises(T2VError) as excinfo:
            client.request("/v1/thing")

        assert excinfo.value.type == "tool_call_id_mismatch"
        assert str(excinfo.value) == "Moved on"
