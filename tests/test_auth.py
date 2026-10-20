"""Tests for T2VAuth."""

from __future__ import annotations

from typing import List
from unittest.mock import MagicMock

import pytest

from talk2view.auth import T2VAuth
from talk2view.errors import AuthenticationError
from talk2view.storage import MemoryStorage


def _make_auth() -> tuple:  # type: ignore[type-arg]
    storage = MemoryStorage()
    client = MagicMock()
    client.storage = storage
    client._clear_auth = MagicMock(side_effect=lambda: (
        storage.delete("access_token"),
        storage.delete("refresh_token"),
        storage.delete("user"),
    ))
    auth = T2VAuth(client)
    return auth, client, storage


class TestT2VAuth:
    def test_login_success(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = {
            "access_token": "tok_abc",
            "refresh_token": "ref_xyz",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": "u1", "email": "test@example.com", "user_metadata": {}},
            "user_api_key": "uak_123",
        }

        user = auth.login("test@example.com", "password123")

        assert user.id == "u1"
        assert user.email == "test@example.com"
        assert storage.get("access_token") == "tok_abc"
        assert storage.get("refresh_token") == "ref_xyz"
        assert storage.get("user_api_key") == "uak_123"

        # Check request was made correctly
        client.request.assert_called_once_with(
            "/v1/auth/login",
            method="POST",
            body={"email": "test@example.com", "password": "password123"},
            requires_auth=False,
        )

    def test_signup_success(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = {
            "access_token": "tok",
            "refresh_token": "ref",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": "u2", "email": "new@example.com"},
        }

        outcome = auth.signup("new@example.com", "strongpassword")
        assert outcome.confirmation_required is False
        assert outcome.user is not None and outcome.user.email == "new@example.com"
        assert storage.get("access_token") == "tok"
        assert storage.get("is_anonymous") == "false"

    def test_login_no_user_raises(self) -> None:
        auth, client, _ = _make_auth()
        client.request.return_value = {
            "access_token": "tok",
            "refresh_token": "ref",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": None,
        }

        with pytest.raises(AuthenticationError, match="no user returned"):
            auth.login("test@example.com", "password")

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"access_token": "tok", "refresh_token": "ref", "user": None},
            {"access_token": "", "refresh_token": "ref", "user": {"id": "u1", "email": "a@b.co"}},
            {"refresh_token": "ref", "user": {"id": "u1", "email": "a@b.co"}},
            {"access_token": "tok", "user": {"id": "u1", "email": "a@b.co"}},
            {"access_token": 123, "refresh_token": "ref", "user": {"id": "u1", "email": "a@b.co"}},
        ],
    )
    def test_login_garbled_answer_stores_nothing(self, body: object) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = body
        events: list = []  # type: ignore[type-arg]
        auth.on_auth_state_change(events.append)

        with pytest.raises(AuthenticationError, match="Login succeeded but no"):
            auth.login("a@b.co", "pw")

        for key in ("access_token", "refresh_token", "user", "user_api_key", "is_anonymous"):
            assert storage.get(key) is None, key
        assert auth.is_authenticated() is False
        assert events == []

    def test_empty_access_token_is_not_authenticated(self) -> None:
        auth, _, storage = _make_auth()
        storage.set("access_token", "")
        assert auth.is_authenticated() is False

    def test_logout(self) -> None:
        auth, client, storage = _make_auth()
        storage.set("access_token", "tok")
        storage.set("refresh_token", "ref")
        storage.set("user", '{"id":"u1","email":"a@b.com"}')

        auth.logout()

        assert storage.get("access_token") is None
        client._clear_auth.assert_called_once()

    def test_logout_ignores_server_error(self) -> None:
        auth, client, storage = _make_auth()
        storage.set("access_token", "tok")
        client.request.side_effect = Exception("server error")

        auth.logout()  # Should not raise
        client._clear_auth.assert_called_once()

    def test_logout_without_token_skips_server_call(self) -> None:
        auth, client, _ = _make_auth()

        auth.logout()

        client.request.assert_not_called()
        client._clear_auth.assert_called_once()

    def test_signup_confirmation_pending_does_not_crash_or_store_tokens(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = {
            "access_token": "",
            "refresh_token": "",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": "u3", "email": "pending@example.com"},
        }
        events: list = []  # type: ignore[type-arg]
        auth.on_auth_state_change(lambda u: events.append(u))

        outcome = auth.signup("pending@example.com", "strongpassword")

        assert outcome.confirmation_required is True
        assert outcome.user is not None and outcome.user.id == "u3"
        assert storage.get("access_token") is None
        assert storage.get("refresh_token") is None
        assert auth.is_authenticated() is False
        assert events == []

    def test_signup_without_token_keys_does_not_key_error(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = {"user": {"id": "u4", "email": "p@example.com"}}

        outcome = auth.signup("p@example.com", "strongpassword")

        assert outcome.user is not None and outcome.user.email == "p@example.com"
        assert outcome.confirmation_required is True
        assert storage.get("access_token") is None

    @pytest.mark.parametrize(
        "tokens",
        [
            {"access_token": "tok"},
            {"access_token": "tok", "refresh_token": ""},
            {"refresh_token": "ref"},
            {"access_token": "", "refresh_token": "ref"},
        ],
        ids=["access-only", "empty-refresh", "refresh-only", "empty-access"],
    )
    def test_signup_with_half_a_session_stores_nothing(self, tokens: dict) -> None:  # type: ignore[type-arg]
        # Checked before storing, as login() does: half a pair is not a session.
        auth, client, storage = _make_auth()
        client.request.return_value = {"user": {"id": "u5", "email": "h@example.com"}, **tokens}
        events: list = []  # type: ignore[type-arg]
        auth.on_auth_state_change(events.append)

        with pytest.raises(AuthenticationError, match="account .*created.*[Ss]ign in"):
            auth.signup("h@example.com", "strongpassword")

        for key in ("access_token", "refresh_token", "user", "user_api_key", "is_anonymous"):
            assert storage.get(key) is None, key
        assert auth.is_authenticated() is False
        assert events == []

    def test_get_user(self) -> None:
        auth, _, storage = _make_auth()
        user_json = '{"id":"u1","email":"test@example.com","user_metadata":{"name":"Test"}}'
        storage.set("user", user_json)

        user = auth.get_user()
        assert user is not None
        assert user.id == "u1"
        assert user.email == "test@example.com"
        assert user.user_metadata == {"name": "Test"}

    def test_get_user_none(self) -> None:
        auth, _, _ = _make_auth()
        assert auth.get_user() is None

    def test_get_user_bad_json(self) -> None:
        auth, _, storage = _make_auth()
        storage.set("user", "not json")
        assert auth.get_user() is None

    def test_is_authenticated(self) -> None:
        auth, _, storage = _make_auth()
        assert auth.is_authenticated() is False

        storage.set("access_token", "tok")
        assert auth.is_authenticated() is True

    def test_auth_state_callbacks(self) -> None:
        auth, client, _ = _make_auth()
        client.request.return_value = {
            "access_token": "tok",
            "refresh_token": "ref",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": "u1", "email": "a@b.com"},
        }

        events = []
        unsub = auth.on_auth_state_change(lambda u: events.append(u))

        auth.login("a@b.com", "pw")
        assert len(events) == 1
        assert events[0] is not None
        assert events[0].email == "a@b.com"

        auth.logout()
        assert len(events) == 2
        assert events[1] is None

        # Unsubscribe
        unsub()
        client.request.return_value = {
            "access_token": "tok2",
            "refresh_token": "ref2",
            "token_type": "bearer",
            "expires_in": 3600,
            "user": {"id": "u2", "email": "b@c.com"},
        }
        auth.login("b@c.com", "pw")
        assert len(events) == 2  # No new events

    def test_early_listeners_run_first_in_the_order_they_were_added(self) -> None:
        # The SDK's own state holders (Talk2View's session hook, a ChatController) must hear
        # a change before any host listener can act on it or change the auth state again.
        auth, client, _ = _make_auth()
        client.request.return_value = {
            "access_token": "tok",
            "refresh_token": "ref",
            "user": {"id": "u1", "email": "a@b.com"},
        }
        order: List[str] = []
        auth.on_auth_state_change(lambda _u: order.append("host-1"))
        auth._add_listener(lambda _u: order.append("sdk-1"), early=True)
        auth.on_auth_state_change(lambda _u: 1 / 0)  # still isolated
        off = auth._add_listener(lambda _u: order.append("sdk-2"), early=True)
        auth.on_auth_state_change(lambda _u: order.append("host-2"))

        auth.login("a@b.com", "pw")
        assert order == ["sdk-1", "sdk-2", "host-1", "host-2"]

        off()
        order.clear()
        auth.logout()
        assert order == ["sdk-1", "host-1", "host-2"]
