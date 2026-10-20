"""Anonymous demo sessions, convert-on-signup, ensure_session single-flight, demo limit."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import talk2view
from talk2view import Talk2View
from talk2view.auth import T2VAuth
from talk2view.controller import DEMO_LIMIT_REACHED, ChatController
from talk2view.errors import (
    ANONYMOUS_UNAVAILABLE_TYPES,
    AnonymousUnavailableError,
    AuthenticationError,
    NetworkError,
    T2VError,
)
from talk2view.storage import MemoryStorage
from talk2view.types import ChatEvent, SignupOutcome, User

from .controller_fakes import inline_executor

ANON_USER = {"id": "anon_1", "email": None, "user_metadata": {}}
ANON_TOKENS: Dict[str, Any] = {
    "access_token": "anon_tok",
    "refresh_token": "anon_ref",
    "token_type": "bearer",
    "expires_in": 3600,
    "user": ANON_USER,
    "user_api_key": None,
    "is_anonymous": True,
    "key_pending": False,
}


def _error(error_type: str, message: str = "nope") -> Dict[str, Any]:
    return {"detail": {"error": {"type": error_type, "message": message}}}


def _make_auth() -> tuple:  # type: ignore[type-arg]
    storage = MemoryStorage()
    client = MagicMock()
    client.storage = storage
    client._clear_auth = MagicMock(
        side_effect=lambda: [
            storage.delete(k)
            for k in ("access_token", "refresh_token", "user", "user_api_key", "is_anonymous")
        ]
    )
    return T2VAuth(client), client, storage


def _t2v(httpserver: HTTPServer, **kwargs: Any) -> Talk2View:
    return Talk2View("pk_test_123", base_url=httpserver.url_for(""), **kwargs)


def _json(body: Any, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


class _ParkedCallers:
    """Counts the callers that have parked on a shared anonymous start."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._count = 0

    def mark(self) -> None:
        with self._cond:
            self._count += 1
            self._cond.notify_all()

    def wait_for(self, n: int) -> bool:
        with self._cond:
            return self._cond.wait_for(lambda: self._count >= n, 5.0)


def _spy_on_parked_callers(monkeypatch: pytest.MonkeyPatch) -> _ParkedCallers:
    """Make every caller that waits on a shared start report it, so a test can hold
    the request until the callers it needs are parked instead of racing a sleep."""
    parked = _ParkedCallers()

    class _SpyEvent(threading.Event):
        def wait(self, timeout: Any = None) -> bool:
            parked.mark()
            return super().wait(timeout)

    class _SpyFlight(talk2view._Flight):
        def __init__(self) -> None:
            super().__init__()
            self.done = _SpyEvent()

    monkeypatch.setattr(talk2view, "_Flight", _SpyFlight)
    return parked


# ── start_anonymous / is_anonymous ──────────────────────────────────────────


class TestStartAnonymous:
    def test_stores_tokens_and_flags_the_session_anonymous(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        seen: List[Any] = []
        auth.on_auth_state_change(seen.append)

        user = auth.start_anonymous()

        assert user == User(id="anon_1", email="", user_metadata={})
        assert storage.get("access_token") == "anon_tok"
        assert storage.get("is_anonymous") == "true"
        assert auth.is_anonymous() is True
        assert auth.is_authenticated() is True
        assert seen == [user]
        client.request.assert_called_once_with(
            "/v1/auth/anonymous", method="POST", body={}, requires_auth=False
        )

    @staticmethod
    def _flag_when_token_written(storage: MemoryStorage) -> List[Any]:
        """``is_anonymous`` as it reads the moment each access token is written."""
        seen: List[Any] = []
        write = storage.set

        def spy(key: str, value: str) -> None:
            if key == "access_token":
                seen.append(storage.get("is_anonymous"))
            write(key, value)

        storage.set = spy  # type: ignore[method-assign]
        return seen

    def test_a_guest_token_never_shows_without_its_anonymous_flag(self) -> None:
        """Another thread (a host polling is_authenticated, the page's auth slice) reads the
        storage without the session lock: a guest's token seen before its flag reads as a
        signed-in account."""
        auth, client, storage = _make_auth()
        seen = self._flag_when_token_written(storage)
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        assert seen == ["true"]

    def test_an_account_token_never_shows_with_the_guests_flag(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        seen = self._flag_when_token_written(storage)
        client.request.return_value = {
            "access_token": "t",
            "refresh_token": "r",
            "user": {"id": "u1", "email": "a@b.co"},
        }
        auth.login("a@b.co", "pw")
        assert seen == ["false"]

    def test_captcha_token_is_sent_when_given(self) -> None:
        auth, client, _storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous(captcha_token="cf-turnstile-token")
        assert client.request.call_args.kwargs["body"] == {"captcha_token": "cf-turnstile-token"}

    def test_login_clears_the_anonymous_flag(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        client.request.return_value = {
            "access_token": "t",
            "refresh_token": "r",
            "user": {"id": "u1", "email": "a@b.co"},
        }
        auth.login("a@b.co", "pw")
        assert storage.get("is_anonymous") == "false"
        assert auth.is_anonymous() is False

    def test_logout_clears_the_anonymous_flag(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        auth.logout()
        assert storage.get("is_anonymous") is None
        assert auth.is_anonymous() is False

    def test_no_user_in_the_answer_raises(self) -> None:
        auth, client, _storage = _make_auth()
        client.request.return_value = {**ANON_TOKENS, "user": None}
        with pytest.raises(AuthenticationError, match="returned no user"):
            auth.start_anonymous()

    @pytest.mark.parametrize(
        "error_type, status",
        [
            ("captcha_required", 403),
            ("captcha_invalid", 403),
            ("captcha_unavailable", 503),
            ("anonymous_access_disabled", 403),
            ("anonymous_daily_cap_reached", 429),
        ],
    )
    def test_sticky_refusals_are_typed_errors(
        self, httpserver: HTTPServer, error_type: str, status: int
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error(error_type, "Captcha required."), status=status
        )
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(AnonymousUnavailableError) as info:
                t2v.auth.start_anonymous()
            assert info.value.type == error_type
            assert info.value.status_code == status
            assert str(info.value) == "Captcha required."
            assert isinstance(info.value, T2VError)
            assert error_type in ANONYMOUS_UNAVAILABLE_TYPES
            assert t2v.auth.is_authenticated() is False
        finally:
            t2v.close()

    def test_transient_503_is_a_plain_t2v_error(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_unavailable", "Try again shortly."), status=503
        )
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.auth.start_anonymous()
            assert not isinstance(info.value, AnonymousUnavailableError)
            assert info.value.type == "anonymous_unavailable"
        finally:
            t2v.close()


# ── Fail closed: a garbled answer is never a session ────────────────────────


class TestFailClosed:
    @pytest.mark.parametrize(
        "stored", ["True", "TRUE", "1", "yes", "", " true", "true ", "false", "null"]
    )
    def test_the_anonymous_flag_reads_true_only_for_exactly_true(self, stored: str) -> None:
        auth, _client, storage = _make_auth()
        storage.set("is_anonymous", stored)
        assert auth.is_anonymous() is False

    @pytest.mark.parametrize(
        "body",
        [
            None,
            [],
            {},
            {"user": ANON_USER},  # a user but no session
            {**ANON_TOKENS, "access_token": ""},
            {**ANON_TOKENS, "refresh_token": None},
            {**ANON_TOKENS, "user": None},
            {**ANON_TOKENS, "user": {"email": None}},  # a user with no id
        ],
    )
    def test_a_garbled_answer_stores_nothing(self, body: Any) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = body
        seen: List[Any] = []
        auth.on_auth_state_change(seen.append)

        with pytest.raises(AuthenticationError, match="Anonymous sign-in returned no"):
            auth.start_anonymous()

        for key in ("access_token", "refresh_token", "user", "user_api_key", "is_anonymous"):
            assert storage.get(key) is None, key
        assert auth.is_authenticated() is False
        assert auth.is_anonymous() is False
        assert seen == []

    def test_ensure_session_does_not_read_a_garbled_answer_as_signed_in(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json({})
        t2v = _t2v(httpserver)
        try:
            assert t2v.ensure_session() is False
            assert t2v.auth.is_authenticated() is False
            assert t2v.ensure_session() is False  # asks again: nothing was remembered
            assert len(httpserver.log) == 2
        finally:
            t2v.close()


# ── signup: convert from anonymous ──────────────────────────────────────────


class TestSignupConvert:
    def test_anonymous_signup_converts_and_awaits_confirmation(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        client.request.reset_mock()
        client.request.return_value = {
            "id": "anon_1",
            "email": "me@example.com",
            "user_metadata": {},
            "confirmation_pending": True,
        }

        outcome = auth.signup("me@example.com", "strongpassword")

        assert outcome == SignupOutcome(user=None, confirmation_required=True)
        client.request.assert_called_once_with(
            "/v1/auth/convert",
            method="POST",
            body={"email": "me@example.com", "password": "strongpassword", "client": "python-sdk"},
        )
        # The anonymous session stays live until the link is clicked.
        assert storage.get("access_token") == "anon_tok"
        assert auth.is_anonymous() is True

    def test_convert_409_falls_back_to_login(self) -> None:
        auth, client, storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        login_body = {
            "access_token": "real_tok",
            "refresh_token": "real_ref",
            "user": {"id": "u9", "email": "me@example.com"},
        }
        client.request.side_effect = [
            T2VError("Email already registered", "convert_error", 409),
            login_body,
        ]

        outcome = auth.signup("me@example.com", "strongpassword")

        assert outcome.confirmation_required is False
        assert outcome.user == User(id="u9", email="me@example.com")
        assert storage.get("access_token") == "real_tok"
        assert auth.is_anonymous() is False
        assert client.request.call_args_list[-1].args[0] == "/v1/auth/login"

    def test_convert_400_propagates(self) -> None:
        auth, client, _storage = _make_auth()
        client.request.return_value = ANON_TOKENS
        auth.start_anonymous()
        client.request.side_effect = T2VError("Password too short", "convert_error", 400)
        with pytest.raises(T2VError, match="Password too short"):
            auth.signup("me@example.com", "short")


# ── Talk2View.ensure_session ────────────────────────────────────────────────


class TestEnsureSession:
    def test_already_signed_in_makes_no_request(self, httpserver: HTTPServer) -> None:
        t2v = _t2v(httpserver)
        try:
            t2v.storage.set("access_token", "tok")
            assert t2v.ensure_session() is True
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_starts_an_anonymous_session(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        t2v = _t2v(httpserver)
        try:
            assert t2v.ensure_session() is True
            assert t2v.auth.is_anonymous() is True
            assert len(httpserver.log) == 1
        finally:
            t2v.close()

    def test_concurrent_callers_one_anonymous_request(self, httpserver: HTTPServer) -> None:
        hits: List[float] = []

        def slow(request: Request) -> Response:
            hits.append(time.monotonic())
            time.sleep(0.3)
            return Response(json.dumps(ANON_TOKENS), status=200, content_type="application/json")

        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_handler(slow)
        t2v = _t2v(httpserver)
        try:
            results: List[bool] = []
            start = threading.Barrier(6)

            def call() -> None:
                start.wait(5.0)
                results.append(t2v.ensure_session())

            threads = [threading.Thread(target=call) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            assert results == [True] * 6
            assert len(hits) == 1
            assert t2v.auth.is_anonymous() is True
        finally:
            t2v.close()

    def test_sticky_refusal_returns_false_and_notifies(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_access_disabled"), status=403
        )
        t2v = _t2v(httpserver)
        try:
            seen: List[str] = []
            off = t2v.on_anonymous_unavailable(seen.append)
            assert t2v.ensure_session() is False
            assert seen == ["anonymous_access_disabled"]
            off()
            assert t2v.ensure_session() is False
            assert seen == ["anonymous_access_disabled"]
        finally:
            t2v.close()

    def test_transient_503_is_not_sticky(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_unavailable"), status=503
        )
        httpserver.expect_ordered_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        t2v = _t2v(httpserver)
        try:
            seen: List[str] = []
            t2v.on_anonymous_unavailable(seen.append)
            assert t2v.ensure_session() is False
            assert seen == []  # not a "sign in to continue" refusal
            assert t2v.ensure_session() is True  # the next call simply tries again
        finally:
            t2v.close()

    def test_a_raising_listener_does_not_change_the_answer(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("captcha_required"), status=403
        )
        t2v = _t2v(httpserver)
        try:
            seen: List[str] = []
            t2v.on_anonymous_unavailable(lambda _type: 1 / 0)
            t2v.on_anonymous_unavailable(seen.append)
            assert t2v.ensure_session() is False
            assert seen == ["captcha_required"]  # the listener behind it still ran
        finally:
            t2v.close()

    def test_a_raising_auth_listener_keeps_the_started_session(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        t2v = _t2v(httpserver)
        try:
            seen: List[Any] = []
            t2v.auth.on_auth_state_change(lambda _user: 1 / 0)
            t2v.auth.on_auth_state_change(seen.append)
            assert t2v.ensure_session() is True  # the session exists; the listener is logged
            assert t2v.auth.is_anonymous() is True
            assert [user.id for user in seen] == ["anon_1"]  # the listener behind it still ran
            assert len(httpserver.log) == 1
        finally:
            t2v.close()

    def test_a_refusal_listener_may_call_ensure_session(self, httpserver: HTTPServer) -> None:
        """The listener runs on the thread that made the request, inside its
        ensure_session() call. Calling ensure_session() again from there must not
        wait on that same (unfinished) call."""
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_access_disabled"), status=403
        )
        t2v = _t2v(httpserver)
        try:
            inner: List[bool] = []
            t2v.on_anonymous_unavailable(lambda _type: inner.append(t2v.ensure_session()))
            outer: List[bool] = []
            caller = threading.Thread(
                target=lambda: outer.append(t2v.ensure_session()), daemon=True
            )
            caller.start()
            caller.join(5.0)
            assert not caller.is_alive(), "ensure_session() waited on its own request"
            assert outer == [False]
            assert inner == [False]
            assert len(httpserver.log) == 1  # the listener's call shared the refused one
        finally:
            t2v.close()

    def test_waiting_callers_are_released_before_the_listeners_run(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The run_on_main shape: the controller's worker makes the request while
        the host's main thread waits in ensure_session(), and an auth-state
        listener on the worker hands work to the main thread and waits for it.
        That only finishes if the main thread was released before the listener
        ran."""
        parked = _spy_on_parked_callers(monkeypatch)
        requested = threading.Event()

        def held(request: Request) -> Response:
            requested.set()
            parked.wait_for(1)  # answer only once the main thread is waiting on it
            return _json(ANON_TOKENS)

        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_handler(held)
        t2v = _t2v(httpserver)
        try:
            main_returned = threading.Event()
            listener_saw_main: List[bool] = []
            t2v.auth.on_auth_state_change(
                lambda _user: listener_saw_main.append(main_returned.wait(5.0))
            )
            worker_result: List[bool] = []
            worker = threading.Thread(
                target=lambda: worker_result.append(t2v.ensure_session()), daemon=True
            )
            worker.start()
            assert requested.wait(5.0)
            # The worker's request is in flight: this call shares it.
            assert t2v.ensure_session() is True
            main_returned.set()
            worker.join(5.0)
            assert not worker.is_alive()
            assert worker_result == [True]
            assert listener_saw_main == [True]
            assert len(httpserver.log) == 1
        finally:
            t2v.close()

    def test_a_logout_in_a_success_listener_starts_afresh(self, httpserver: HTTPServer) -> None:
        """Once the start has succeeded, a listener that signs out and asks again
        must get a new start, not the settled True of the one that just ended."""
        httpserver.expect_oneshot_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_oneshot_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_daily_cap_reached"), status=429
        )
        httpserver.expect_request("/v1/auth/logout", method="POST").respond_with_data(
            "", status=204
        )
        t2v = _t2v(httpserver)
        try:
            inner: List[bool] = []

            def sign_out_and_ask_again(user: Any) -> None:
                if user is not None and not inner:
                    t2v.auth.logout()
                    inner.append(t2v.ensure_session())

            t2v.auth.on_auth_state_change(sign_out_and_ask_again)
            assert t2v.ensure_session() is True
            assert inner == [False]  # the second start's refusal, not a stale True
            assert [entry[0].path for entry in httpserver.log] == [
                "/v1/auth/anonymous",
                "/v1/auth/logout",
                "/v1/auth/anonymous",
            ]
        finally:
            t2v.close()

    def test_a_start_that_finished_before_the_lock_is_not_repeated(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The interleaving: this caller reads "not signed in", then another
        caller's start completes and its flight ends, and only then does this
        caller take the lock. It must not start a second guest account."""
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        t2v = _t2v(httpserver)
        try:
            answers = iter([False, True])  # before the lock, then under it
            monkeypatch.setattr(t2v.auth, "is_authenticated", lambda: next(answers))
            assert t2v.ensure_session() is True
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_chat_auto_starts_only_when_asked(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("captcha_required"), status=403
        )
        default = _t2v(httpserver)
        auto = _t2v(httpserver, anonymous_auto_start=True)
        try:
            with pytest.raises(AuthenticationError) as info:
                next(auto.chat("hi"))
            assert info.value.type == "sign_in_required"
            assert len(httpserver.log) == 1
            # Off by default: chat() goes straight to the session route (which 401s here).
            with pytest.raises(T2VError):
                next(default.chat("hi"))
            assert httpserver.log[-1][0].path == "/v1/sessions"
        finally:
            default.close()
            auto.close()

    def test_upload_starts_the_session_before_uploading(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_request("/v1/attachments", method="POST").respond_with_json(
            {"id": "att_1", "filename": "a.png", "mime_type": "image/png", "size_bytes": 3}
        )
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            attachment = t2v.upload_attachment(b"png", filename="a.png")
            assert attachment.id == "att_1"
            assert [entry[0].path for entry in httpserver.log] == [
                "/v1/auth/anonymous",
                "/v1/attachments",
            ]
            assert httpserver.log[1][0].headers["Authorization"] == "Bearer anon_tok"
        finally:
            t2v.close()


# ── What chat() / upload_attachment() raise when the auto-start fails ───────


class TestRequireSession:
    @pytest.mark.parametrize(
        "status, body, error_type",
        [
            (503, _error("anonymous_unavailable", "Try again shortly."), "anonymous_unavailable"),
            (429, {"error": "Rate limit exceeded: 20 per 1 hour"}, "unknown_error"),
            (500, {"detail": "Internal Server Error"}, "unknown_error"),
        ],
    )
    def test_a_failed_start_surfaces_as_itself(
        self, httpserver: HTTPServer, status: int, body: Dict[str, Any], error_type: str
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            body, status=status
        )
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            seen: List[str] = []
            t2v.on_anonymous_unavailable(seen.append)
            with pytest.raises(T2VError) as info:
                next(t2v.chat("hi"))
            assert info.value.type == error_type  # not "sign_in_required"
            assert info.value.status_code == status
            assert not isinstance(info.value, AnonymousUnavailableError)
            assert seen == []
            assert t2v.auth.is_authenticated() is False
            assert [entry[0].path for entry in httpserver.log] == ["/v1/auth/anonymous"]
        finally:
            t2v.close()

    def test_a_garbled_answer_surfaces_as_an_error_and_no_session(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            {"user": ANON_USER}
        )
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            with pytest.raises(T2VError, match="Anonymous sign-in returned no session") as info:
                next(t2v.chat("hi"))
            assert info.value.type != "sign_in_required"
            assert t2v.auth.is_authenticated() is False
            assert [entry[0].path for entry in httpserver.log] == ["/v1/auth/anonymous"]
        finally:
            t2v.close()

    def test_a_network_error_surfaces_as_itself(self) -> None:
        t2v = Talk2View("pk_test_123", base_url="http://localhost:1", anonymous_auto_start=True)
        try:
            with pytest.raises(NetworkError):
                next(t2v.chat("hi"))
            with pytest.raises(NetworkError):
                t2v.upload_attachment(b"png", filename="a.png")
            assert t2v.ensure_session() is False  # the bool contract is unchanged
        finally:
            t2v.close()

    def test_a_sticky_refusal_is_sign_in_required(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_access_disabled"), status=403
        )
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            seen: List[str] = []
            t2v.on_anonymous_unavailable(seen.append)
            with pytest.raises(AuthenticationError) as info:
                next(t2v.chat("hi"))
            assert info.value.type == "sign_in_required"
            cause = info.value.__cause__
            assert isinstance(cause, AnonymousUnavailableError)
            assert cause.type == "anonymous_access_disabled"
            with pytest.raises(AuthenticationError, match="Sign in to continue"):
                t2v.upload_attachment(b"png", filename="a.png")
            assert seen == ["anonymous_access_disabled"] * 2
        finally:
            t2v.close()

    def test_every_caller_of_a_failed_start_gets_the_same_error(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parked = _spy_on_parked_callers(monkeypatch)
        requested = threading.Event()

        def held(request: Request) -> Response:
            requested.set()
            parked.wait_for(3)  # answer only once the other three callers share this start
            return _json(_error("anonymous_unavailable", "Try again shortly."), status=503)

        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_handler(held)
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            errors: List[BaseException] = []

            def send() -> None:
                try:
                    next(t2v.chat("hi"))
                except BaseException as exc:
                    errors.append(exc)

            first = threading.Thread(target=send, daemon=True)
            first.start()
            assert requested.wait(5.0)
            others = [threading.Thread(target=send, daemon=True) for _ in range(3)]
            for thread in others:
                thread.start()
            for thread in [first, *others]:
                thread.join(5.0)
            assert len(errors) == 4
            assert all(err is errors[0] for err in errors)
            assert isinstance(errors[0], T2VError)
            assert errors[0].type == "anonymous_unavailable"
            assert len(httpserver.log) == 1
        finally:
            t2v.close()

    def test_stop_during_the_anonymous_start_ends_the_turn(self, httpserver: HTTPServer) -> None:
        """stop() while the anonymous POST is out: the turn ends there, before a
        chat session is created (and tools and skills are re-sent to it)."""
        requested = threading.Event()
        release = threading.Event()

        def held(request: Request) -> Response:
            requested.set()
            release.wait(5.0)
            return _json(ANON_TOKENS)

        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_handler(held)
        t2v = _t2v(httpserver, anonymous_auto_start=True)
        try:
            events: List[ChatEvent] = []
            errors: List[BaseException] = []

            def run() -> None:
                try:
                    events.extend(t2v.chat("hi"))
                except BaseException as exc:
                    errors.append(exc)

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            assert requested.wait(5.0)
            t2v.stop()
            release.set()
            worker.join(5.0)
            assert not worker.is_alive()
            assert events == [] and errors == []
            assert [entry[0].path for entry in httpserver.log] == ["/v1/auth/anonymous"]
            assert t2v.get_session() is None
            assert t2v.auth.is_anonymous() is True  # the start itself completed
        finally:
            t2v.close()


# ── The controller's demo-limit branch is now reachable ─────────────────────


class TestDemoLimit:
    def test_budget_exceeded_on_an_anonymous_session_raises_the_notice(self) -> None:
        t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
        t2v._client.close()
        t2v.storage.set("access_token", "anon_tok")
        t2v.storage.set("is_anonymous", "true")
        t2v.chat = MagicMock(  # type: ignore[method-assign]
            return_value=iter(
                [ChatEvent(type="error", message="Budget exceeded", error_type="budget_exceeded")]
            )
        )
        controller = ChatController(t2v, executor=inline_executor)
        notices: List[str] = []
        controller.on(DEMO_LIMIT_REACHED, lambda: notices.append("demo"))
        controller.send("hello")
        snapshot = controller.snapshot()
        assert snapshot["isAnonymous"] is True
        assert snapshot["error"] is None
        assert notices == ["demo"]
        controller.close()
