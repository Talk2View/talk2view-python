"""get_config (single-flight + cache), list_models, completions, warm_up, attachments."""

from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import talk2view
import talk2view.client as client_module
from talk2view import (
    ALLOWED_ATTACHMENT_MIME,
    ATTACHMENT_ACCEPT,
    ATTACHMENT_TYPES_LABEL,
    AttachmentContent,
    MemoryStorage,
    Model,
    PartnerConfig,
    Talk2View,
    User,
    is_allowed_attachment_type,
)
from talk2view.client import T2VClient
from talk2view.errors import AuthenticationError, NetworkError, PartnerKeyError, T2VError

ATT_ID = "3f2b6c1e-8a4d-4b7e-9c2a-1d5e6f7a8b9c"
MISSING_ID = "0b9d7c1a-2e3f-4a5b-8c6d-7e8f9a0b1c2d"

CONFIG: Dict[str, Any] = {
    "default_llm_model": "gemini-3.8-flash",
    "default_stt_model": "scribe_v2",
    "default_image_model": None,
    "system_prompt": None,
    "allowed_llm_models": ["gemini-3.8-flash", "gpt-5"],
    "allowed_stt_models": None,
    "gated_llm_models": ["gpt-5"],
    "gated_stt_models": [],
    "web_search_enabled": True,
}

ANON_TOKENS: Dict[str, Any] = {
    "access_token": "anon_tok",
    "refresh_token": "anon_ref",
    "user": {"id": "anon_1", "email": None},
    "is_anonymous": True,
}


def _t2v(httpserver: HTTPServer, **kwargs: Any) -> Talk2View:
    t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), **kwargs)
    t2v.storage.set("access_token", "tok")
    t2v.storage.set("refresh_token", "ref")
    t2v.storage.set("user", json.dumps({"id": "u1", "email": "a@b.co"}))
    return t2v


def _hits(httpserver: HTTPServer, path: str) -> int:
    return len([entry for entry in httpserver.log if entry[0].path == path])


def _json(body: Any, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, content_type="application/json")


def _error(error_type: str, message: str = "nope") -> Dict[str, Any]:
    return {"detail": {"error": {"type": error_type, "message": message}}}


def _settle_warm_up() -> None:
    """warm_up() returns at once; wait (bounded) until no warm-up thread is running. A
    loop, not one pass: a warm-up thread can start another (an auth listener's)."""
    deadline = time.monotonic() + 5.0
    while True:
        alive = [t for t in threading.enumerate() if t.name == "t2v-warm-up"]
        if not alive:
            return
        assert time.monotonic() < deadline, "a warm-up thread did not finish"
        alive[0].join(0.05)


def _close_once_answered(
    monkeypatch: pytest.MonkeyPatch, t2v: Talk2View, endpoint: str
) -> None:
    """Close ``t2v`` right after the engine has answered ``endpoint`` (success or
    error) and before the SDK acts on the answer: a close() that lands while the
    answer is in transit. (Closing mid-read cannot be timed from a test: a closed
    httpx client does not wake a blocked read, which then waits out its timeout.)"""
    real_request = t2v._client.request

    def request(path: str, *args: Any, **kwargs: Any) -> Any:
        try:
            return real_request(path, *args, **kwargs)
        finally:
            if path == endpoint:
                t2v.close()

    monkeypatch.setattr(t2v._client, "request", request)


@pytest.fixture
def threaded_httpserver() -> Iterator[HTTPServer]:
    """Answers concurrent requests (the default fixture is serial), for tests that keep
    one request stalled while another has to get through."""
    server = HTTPServer(threaded=True)
    server.start()
    try:
        yield server
    finally:
        server.clear()
        if server.is_running():
            server.stop()


class _Parked:
    """Counts callers that have parked on a shared config request."""

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


def _spy_on_config_waiters(monkeypatch: pytest.MonkeyPatch) -> _Parked:
    """Make every caller that waits on a shared config request report it, so a test
    holds the request until the callers it needs are parked instead of racing a sleep."""
    parked = _Parked()

    class _SpyEvent(threading.Event):
        def wait(self, timeout: Any = None) -> bool:
            parked.mark()
            return super().wait(timeout)

    class _SpyFlight(talk2view._ConfigFlight):  # type: ignore[name-defined]
        def __init__(self) -> None:
            super().__init__()
            self.done = _SpyEvent()

    monkeypatch.setattr(talk2view, "_ConfigFlight", _SpyFlight)
    return parked


# ── get_config ──────────────────────────────────────────────────────────────


class TestGetConfig:
    def test_parses_the_nine_field_engine_shape(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/config").respond_with_json(CONFIG)
        t2v = _t2v(httpserver)
        try:
            config = t2v.get_config()
            assert config == PartnerConfig(
                default_llm_model="gemini-3.8-flash",
                default_stt_model="scribe_v2",
                default_image_model=None,
                system_prompt=None,
                allowed_llm_models=["gemini-3.8-flash", "gpt-5"],
                allowed_stt_models=None,
                gated_llm_models=["gpt-5"],
                gated_stt_models=[],
                web_search_enabled=True,
            )
            assert len(vars(config)) == 9
        finally:
            t2v.close()

    def test_cached_until_invalidated(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/config").respond_with_json(CONFIG)
        t2v = _t2v(httpserver)
        try:
            assert t2v.get_config() is t2v.get_config()
            assert _hits(httpserver, "/v1/config") == 1
            t2v.invalidate_config_cache()
            t2v.get_config()
            assert _hits(httpserver, "/v1/config") == 2
        finally:
            t2v.close()

    def test_single_flight_for_concurrent_callers(self, httpserver: HTTPServer) -> None:
        def slow(request: Request) -> Response:
            time.sleep(0.2)
            return Response(json.dumps(CONFIG), status=200, content_type="application/json")

        httpserver.expect_request("/v1/config").respond_with_handler(slow)
        t2v = _t2v(httpserver)
        try:
            results: List[PartnerConfig] = []
            gate = threading.Barrier(5)

            def call() -> None:
                gate.wait()
                results.append(t2v.get_config())

            threads = [threading.Thread(target=call) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            assert len(results) == 5 and all(r == results[0] for r in results)
            assert _hits(httpserver, "/v1/config") == 1
        finally:
            t2v.close()

    def test_invalidated_on_auth_state_change(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/config").respond_with_json(CONFIG)
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u2", "email": "x@y.z"}}
        )
        httpserver.expect_request("/v1/auth/logout", method="POST").respond_with_data(status=204)
        t2v = _t2v(httpserver)
        try:
            t2v.get_config()
            t2v.auth.login("x@y.z", "pw")
            t2v.get_config()
            assert _hits(httpserver, "/v1/config") == 2
            t2v.auth.logout()
            assert t2v._config_cache is None
        finally:
            t2v.close()

    def test_errors_are_not_cached(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request("/v1/config").respond_with_json(
            {"detail": {"error": {"type": "internal_error", "message": "boom"}}}, status=500
        )
        httpserver.expect_ordered_request("/v1/config").respond_with_json(CONFIG)
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError, match="boom"):
                t2v.get_config()
            assert t2v.get_config().web_search_enabled is True
        finally:
            t2v.close()

    def test_concurrent_callers_share_one_failure(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parked = _spy_on_config_waiters(monkeypatch)
        release = threading.Event()
        answers = iter(
            [
                _json(_error("internal_error", "boom"), status=500),
                _json(CONFIG),
            ]
        )

        def handler(request: Request) -> Response:
            release.wait(5.0)
            return next(answers)

        httpserver.expect_request("/v1/config").respond_with_handler(handler)
        t2v = _t2v(httpserver)
        errors: List[BaseException] = []

        def call() -> None:
            try:
                t2v.get_config()
            except T2VError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=call) for _ in range(5)]
        try:
            for t in threads:
                t.start()
            assert parked.wait_for(4), "the other callers never parked on the shared request"
            release.set()
            for t in threads:
                t.join(5.0)
            assert len(errors) == 5 and all(e is errors[0] for e in errors)
            assert _hits(httpserver, "/v1/config") == 1
            assert t2v._config_cache is None
            assert t2v.get_config().web_search_enabled is True  # the next call retries
            assert _hits(httpserver, "/v1/config") == 2
        finally:
            release.set()
            for t in threads:
                t.join(5.0)
            t2v.close()

    def test_an_answer_that_arrives_after_an_auth_change_is_not_cached(
        self, httpserver: HTTPServer
    ) -> None:
        arrived = threading.Event()
        release = threading.Event()
        answers = iter(
            [dict(CONFIG, web_search_enabled=True), dict(CONFIG, web_search_enabled=False)]
        )

        def handler(request: Request) -> Response:
            arrived.set()
            release.wait(5.0)
            return _json(next(answers))

        httpserver.expect_request("/v1/config").respond_with_handler(handler)
        t2v = _t2v(httpserver)
        before: List[PartnerConfig] = []
        worker = threading.Thread(target=lambda: before.append(t2v.get_config()))
        try:
            worker.start()
            assert arrived.wait(5.0)
            t2v.invalidate_config_cache()  # what every auth change does
            release.set()
            worker.join(5.0)
            assert before[0].web_search_enabled is True  # its caller still gets its answer
            assert t2v._config_cache is None  # but it is not kept for the new login
            assert t2v.get_config().web_search_enabled is False
            assert _hits(httpserver, "/v1/config") == 2
        finally:
            release.set()
            worker.join(5.0)
            t2v.close()

    def test_a_caller_after_an_auth_change_does_not_wait_for_the_old_request(
        self, threaded_httpserver: HTTPServer
    ) -> None:
        server = threaded_httpserver
        arrived = threading.Event()
        release = threading.Event()
        seen: List[int] = []
        seen_lock = threading.Lock()

        def handler(request: Request) -> Response:
            with seen_lock:
                seen.append(1)
                first = len(seen) == 1
            if first:
                arrived.set()
                release.wait(5.0)
                return _json(dict(CONFIG, web_search_enabled=True))
            return _json(dict(CONFIG, web_search_enabled=False))

        server.expect_request("/v1/config").respond_with_handler(handler)
        t2v = _t2v(server)
        old: List[PartnerConfig] = []
        fresh: List[PartnerConfig] = []
        first = threading.Thread(target=lambda: old.append(t2v.get_config()))
        second = threading.Thread(target=lambda: fresh.append(t2v.get_config()))
        try:
            first.start()
            assert arrived.wait(5.0)
            t2v.invalidate_config_cache()
            second.start()
            second.join(2.0)
            # No lock is held across the stalled request: the new caller got through.
            assert fresh and fresh[0].web_search_enabled is False
        finally:
            release.set()
            first.join(5.0)
            second.join(5.0)
            t2v.close()
        assert old[0].web_search_enabled is True

    def test_a_non_object_answer_raises_and_is_not_cached(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request("/v1/config").respond_with_json(["web_search_enabled"])
        httpserver.expect_ordered_request("/v1/config").respond_with_json(CONFIG)
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError):
                t2v.get_config()
            assert t2v._config_cache is None
            assert t2v.get_config().web_search_enabled is True
        finally:
            t2v.close()

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"web_search_enabled": "true"},
            {"web_search_enabled": 1},
            {"web_search_enabled": None},
            {"allowed_llm_models": "gpt-5", "default_llm_model": 7},
        ],
    )
    def test_a_partial_or_garbled_answer_enables_nothing(
        self, httpserver: HTTPServer, body: Dict[str, Any]
    ) -> None:
        httpserver.expect_request("/v1/config").respond_with_json(body)
        t2v = _t2v(httpserver)
        try:
            config = t2v.get_config()
            assert config.web_search_enabled is False
            assert config.allowed_llm_models is None
            assert config.default_llm_model is None
        finally:
            t2v.close()


# ── list_models / completions ───────────────────────────────────────────────


class TestModelsAndCompletions:
    def test_list_models_with_display_names(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/models").respond_with_json(
            {
                "object": "list",
                "data": [
                    {"id": "gemini-3.8-flash", "object": "model", "created": 1, "owned_by": "t2v",
                     "name": "Gemini 3.8 Flash"},
                    {"id": "gpt-5", "object": "model", "created": 2, "owned_by": "t2v"},
                    {"object": "model"},
                ],
            }
        )
        t2v = _t2v(httpserver)
        try:
            models = t2v.list_models()
            assert models.object == "list"
            assert models.data == [
                Model(id="gemini-3.8-flash", created=1, owned_by="t2v", name="Gemini 3.8 Flash"),
                Model(id="gpt-5", created=2, owned_by="t2v", name=None),
            ]
            assert [m.name or m.id for m in models.data] == ["Gemini 3.8 Flash", "gpt-5"]
        finally:
            t2v.close()

    def test_list_models_tolerates_a_garbled_created(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/models").respond_with_json(
            {"data": [{"id": "m", "created": "yesterday", "name": 5}]}
        )
        t2v = _t2v(httpserver)
        try:
            assert t2v.list_models().data == [Model(id="m", created=0, name=None)]
        finally:
            t2v.close()

    def test_completions_passthrough(self, httpserver: HTTPServer) -> None:
        completion = {"id": "cmpl", "choices": [{"message": {"role": "assistant", "content": "4"}}]}
        httpserver.expect_request(
            "/v1/completions",
            method="POST",
            json={
                "model": "medgemma-27b",
                "messages": [{"role": "user", "content": "2+2?"}],
                "max_tokens": 16,
                "temperature": 0.0,
            },
        ).respond_with_json(completion)
        t2v = _t2v(httpserver)
        try:
            result = t2v.completions(
                "medgemma-27b",
                [{"role": "user", "content": "2+2?"}],
                max_tokens=16,
                temperature=0.0,
                timeout=120.0,
            )
            assert result == completion
        finally:
            t2v.close()

    def test_completions_omits_unset_fields(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            "/v1/completions", method="POST", json={"model": "m", "messages": []}
        ).respond_with_json({"ok": True})
        t2v = _t2v(httpserver)
        try:
            assert t2v.completions("m", []) == {"ok": True}
        finally:
            t2v.close()


def _call_models(t2v: Talk2View) -> Any:
    return t2v.list_models()


def _call_completions(t2v: Talk2View) -> Any:
    return t2v.completions("m", [])


AUTO_STARTED_CALLS = pytest.mark.parametrize(
    "call, path",
    [(_call_models, "/v1/models"), (_call_completions, "/v1/completions")],
    ids=["list_models", "completions"],
)


class TestModelsAndCompletionsAutoStart:
    @AUTO_STARTED_CALLS
    def test_starts_an_anonymous_session_first(
        self, httpserver: HTTPServer, call: Any, path: str
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_request(
            path, headers={"Authorization": "Bearer anon_tok"}
        ).respond_with_json({"object": "list", "data": []})
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            call(t2v)
            assert [entry[0].path for entry in httpserver.log] == ["/v1/auth/anonymous", path]
        finally:
            t2v.close()

    @AUTO_STARTED_CALLS
    def test_a_refusal_is_sign_in_required(
        self, httpserver: HTTPServer, call: Any, path: str
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_access_disabled"), status=403
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            with pytest.raises(AuthenticationError) as info:
                call(t2v)
            assert info.value.type == "sign_in_required"
            assert _hits(httpserver, path) == 0
        finally:
            t2v.close()

    @AUTO_STARTED_CALLS
    def test_a_transient_start_failure_surfaces_as_itself(
        self, httpserver: HTTPServer, call: Any, path: str
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("anonymous_unavailable", "try again"), status=503
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            with pytest.raises(T2VError) as info:
                call(t2v)
            assert info.value.type == "anonymous_unavailable"
            assert _hits(httpserver, path) == 0
        finally:
            t2v.close()


# ── warm_up ─────────────────────────────────────────────────────────────────


class TestWarmUp:
    def test_posts_once_per_identity(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"status": "ok"}
        )
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u2", "email": "x@y.z"}}
        )
        t2v = _t2v(httpserver)
        try:
            t2v.warm_up()
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 1
            t2v.auth.login("x@y.z", "pw")  # a different end-user has a different key
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 2
        finally:
            t2v.close()

    def test_swallows_a_404_and_every_other_failure(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        crashed: List[BaseException] = []
        monkeypatch.setattr(
            threading, "excepthook", lambda args: crashed.append(args.exc_value)
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"detail": "Not Found"}, status=404
        )
        t2v = _t2v(httpserver)
        try:
            t2v.warm_up()  # no raise
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()
        offline = Talk2View("pk_test_123", base_url="http://127.0.0.1:1")
        try:
            offline.storage.set("access_token", "tok")
            offline.warm_up()  # connection refused: still no raise
            _settle_warm_up()
        finally:
            offline.close()
        assert crashed == []  # nor on the background thread

    def test_no_op_while_signed_out(self, httpserver: HTTPServer) -> None:
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""))
        try:
            t2v.warm_up()
            _settle_warm_up()
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_concurrent_calls_make_one_request(self, httpserver: HTTPServer) -> None:
        def slow(request: Request) -> Response:
            time.sleep(0.2)
            return Response('{"status":"ok"}', status=200, content_type="application/json")

        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_handler(slow)
        t2v = _t2v(httpserver)
        try:
            threads = [threading.Thread(target=t2v.warm_up) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()

    def test_returns_without_waiting_for_the_engine(self, httpserver: HTTPServer) -> None:
        release = threading.Event()

        def stalled(request: Request) -> Response:
            release.wait(5.0)
            return _json({"ready": True})

        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_handler(stalled)
        t2v = _t2v(httpserver)
        try:
            started = time.monotonic()
            t2v.warm_up()  # a first keystroke on a UI thread: a key mint takes seconds
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            _settle_warm_up()
            t2v.close()
        assert _hits(httpserver, "/v1/account/warm") == 1

    def test_an_auth_listener_can_warm_up_without_blocking_the_sign_in(
        self, httpserver: HTTPServer
    ) -> None:
        release = threading.Event()

        def stalled(request: Request) -> Response:
            release.wait(5.0)
            return _json({"ready": True})

        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u2", "email": "x@y.z"}}
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_handler(stalled)
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""))
        t2v.auth.on_auth_state_change(lambda user: t2v.warm_up())
        try:
            started = time.monotonic()
            t2v.auth.login("x@y.z", "pw")
            assert time.monotonic() - started < 1.0
        finally:
            release.set()
            _settle_warm_up()
            t2v.close()
        warm = [entry[0] for entry in httpserver.log if entry[0].path == "/v1/account/warm"]
        assert len(warm) == 1 and warm[0].headers["Authorization"] == "Bearer t2"

    def test_a_warm_up_for_the_new_identity_survives_the_identity_change(
        self, httpserver: HTTPServer
    ) -> None:
        """A shared anonymous start releases its waiters BEFORE the auth listeners run,
        so a warm_up() for the new identity can land before this SDK's own listener."""
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""))
        try:
            user = User(id="anon_1", email="")
            t2v.storage.set("access_token", "anon_tok")
            t2v.storage.set("user", json.dumps({"id": "anon_1", "email": ""}))
            t2v.warm_up()  # the tokens are stored, the listeners have not run yet
            _settle_warm_up()
            t2v._notify_signed_in(user)
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()

    def test_auto_start_signs_in_then_warms_once(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_request(
            "/v1/account/warm", method="POST", headers={"Authorization": "Bearer anon_tok"}
        ).respond_with_json({"ready": True})
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            threads = [threading.Thread(target=t2v.warm_up) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5.0)
            _settle_warm_up()
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/auth/anonymous") == 1
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()

    def test_a_listener_warm_up_during_the_auto_start_posts_once(
        self, httpserver: HTTPServer
    ) -> None:
        """The warm-up thread's own anonymous start runs the auth listeners on that
        thread; one of them warming up must neither block it nor double the request."""
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        t2v.auth.on_auth_state_change(lambda user: t2v.warm_up())
        try:
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/auth/anonymous") == 1
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()

    @pytest.mark.parametrize("refused", [False, True], ids=["started", "refused"])
    def test_close_during_the_warm_up_start_stores_nothing_and_fires_no_listener(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch, refused: bool
    ) -> None:
        if refused:
            httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
                _error("captcha_required"), status=403
            )
        else:
            httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
                ANON_TOKENS
            )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        fired: List[Any] = []
        t2v.auth.on_auth_state_change(fired.append)
        t2v.on_anonymous_unavailable(fired.append)
        _close_once_answered(monkeypatch, t2v, "/v1/auth/anonymous")
        t2v.warm_up()
        _settle_warm_up()
        assert t2v.storage.get("access_token") is None
        assert t2v.storage.get("refresh_token") is None
        assert t2v.storage.get("user") is None
        assert fired == []
        assert _hits(httpserver, "/v1/account/warm") == 0

    def test_close_during_a_chat_start_stores_nothing_either(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fence is on the shared start, not only the warm-up's use of it."""
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        fired: List[Any] = []
        t2v.auth.on_auth_state_change(fired.append)
        _close_once_answered(monkeypatch, t2v, "/v1/auth/anonymous")
        assert t2v.ensure_session() is False
        assert t2v.storage.get("access_token") is None
        assert fired == []

    def test_close_after_the_session_is_stored_fires_no_listener(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            ANON_TOKENS
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        fired: List[Any] = []
        t2v.auth.on_auth_state_change(fired.append)
        real_end_flight = t2v._end_flight

        def end_flight_then_close(flight: Any) -> None:
            real_end_flight(flight)
            t2v.close()  # stored already; the listeners have not run yet

        monkeypatch.setattr(t2v, "_end_flight", end_flight_then_close)
        t2v.warm_up()
        _settle_warm_up()
        assert t2v.storage.get("access_token") == "anon_tok"  # stored before close: kept
        assert fired == []
        assert _hits(httpserver, "/v1/account/warm") == 0

    def test_warm_up_after_close_does_nothing(self, httpserver: HTTPServer) -> None:
        t2v = _t2v(httpserver)
        t2v.close()
        t2v.warm_up()
        _settle_warm_up()
        assert httpserver.log == []

    def test_auto_start_refusal_is_silent_and_not_retried_until_an_auth_change(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
            _error("captcha_required"), status=403
        )
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u2", "email": "x@y.z"}}
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            for _ in range(3):  # three keystrokes
                t2v.warm_up()
                _settle_warm_up()
            assert _hits(httpserver, "/v1/auth/anonymous") == 1
            assert _hits(httpserver, "/v1/account/warm") == 0
            t2v.auth.login("x@y.z", "pw")
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 1
        finally:
            t2v.close()


# ── The auth-change hook ────────────────────────────────────────────────────


def _signed_in_storage(user_id: str = "u1") -> MemoryStorage:
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    storage.set("refresh_token", "ref")
    storage.set("user", json.dumps({"id": user_id, "email": "a@b.co"}))
    return storage


class TestAuthChangeHook:
    def test_a_new_identity_forgets_the_chat_session(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u2", "email": "x@y.z"}}
        )
        t2v = Talk2View(
            "pk_test_123", base_url=httpserver.url_for(""), storage=_signed_in_storage()
        )
        try:
            t2v._current_session = object()  # type: ignore[assignment]
            t2v.auth.login("x@y.z", "pw")
            assert t2v.get_session() is None
            # No DELETE (the old identity's token is gone), and no other request either:
            # the hook runs inside the sign-in and must not block it on the network.
            assert [entry[0].path for entry in httpserver.log] == ["/v1/auth/login"]
        finally:
            t2v.close()

    def test_the_same_identity_keeps_the_chat_session(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u1", "email": "a@b.co"}}
        )
        t2v = Talk2View(
            "pk_test_123", base_url=httpserver.url_for(""), storage=_signed_in_storage()
        )
        try:
            session = object()
            t2v._current_session = session  # type: ignore[assignment]
            t2v.auth.login("a@b.co", "pw")
            assert t2v.get_session() is session
        finally:
            t2v.close()

    def test_sign_out_then_in_again_warms_up_again(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/auth/logout", method="POST").respond_with_data(status=204)
        httpserver.expect_request("/v1/auth/login", method="POST").respond_with_json(
            {"access_token": "t2", "refresh_token": "r2", "user": {"id": "u1", "email": "a@b.co"}}
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_json(
            {"ready": True}
        )
        t2v = Talk2View(
            "pk_test_123", base_url=httpserver.url_for(""), storage=_signed_in_storage()
        )
        try:
            t2v.warm_up()
            _settle_warm_up()
            t2v.auth.logout()
            t2v.auth.login("a@b.co", "pw")
            t2v.warm_up()
            _settle_warm_up()
            assert _hits(httpserver, "/v1/account/warm") == 2
        finally:
            t2v.close()


# ── Attachments: client-side MIME guard + content download ──────────────────


class TestAttachments:
    def test_constants_mirror_the_engine_allow_list(self) -> None:
        assert ATTACHMENT_ACCEPT == "image/png,image/jpeg,image/webp,image/gif,application/pdf"
        assert ALLOWED_ATTACHMENT_MIME == frozenset(
            {"image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf"}
        )
        assert ATTACHMENT_TYPES_LABEL == "PNG, JPEG, WebP, GIF, or PDF"
        assert is_allowed_attachment_type("image/png") is True
        assert is_allowed_attachment_type("image/svg+xml") is False

    @pytest.mark.parametrize("mime", ["", " ", "IMAGE/PNG", "image/png; charset=binary", "image/*"])
    def test_only_an_exact_allowed_type_passes(self, mime: str) -> None:
        assert is_allowed_attachment_type(mime) is False

    def test_unsupported_type_is_rejected_before_any_request(self, httpserver: HTTPServer) -> None:
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.upload_attachment(b"<svg/>", filename="a.svg")
            assert info.value.type == "unsupported_attachment_type"
            assert "PNG, JPEG, WebP, GIF, or PDF" in str(info.value)
            with pytest.raises(T2VError):
                t2v.upload_attachment(b"x", filename="blob", mime_type="application/octet-stream")
            assert httpserver.log == []
        finally:
            t2v.close()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"filename": "a.png", "mime_type": ""},  # an explicit empty type is not "guess"
            {"filename": "scan"},  # nothing to guess from
            {},  # bytes with no name at all
        ],
        ids=["empty", "unguessable", "unnamed"],
    )
    def test_an_empty_or_missing_type_is_rejected(
        self, httpserver: HTTPServer, kwargs: Dict[str, Any]
    ) -> None:
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.upload_attachment(b"\x89PNG", **kwargs)
            assert info.value.type == "unsupported_attachment_type"
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_the_guard_runs_before_an_anonymous_auto_start(self, httpserver: HTTPServer) -> None:
        t2v = Talk2View("pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True)
        try:
            with pytest.raises(T2VError) as info:
                t2v.upload_attachment(b"<svg/>", filename="a.svg")
            assert info.value.type == "unsupported_attachment_type"
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_allowed_type_uploads(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/attachments", method="POST").respond_with_json(
            {"id": "att_1", "filename": "a.png", "mime_type": "image/png", "size_bytes": 4}
        )
        t2v = _t2v(httpserver)
        try:
            attachment = t2v.upload_attachment(b"\x89PNG", filename="a.png")
            assert attachment.id == "att_1"
        finally:
            t2v.close()

    def test_the_guard_runs_before_the_file_is_read(
        self, httpserver: HTTPServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        drawing = tmp_path / "huge.svg"
        drawing.write_bytes(b"<svg/>")
        read: List[Path] = []
        real_read_bytes = Path.read_bytes

        def spy(self: Path) -> bytes:
            read.append(self)
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", spy)
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.upload_attachment(drawing)
            assert info.value.type == "unsupported_attachment_type"
            with pytest.raises(T2VError):
                t2v.upload_attachment(str(drawing), mime_type="")
            assert read == []  # rejected on its type alone: never opened
            assert httpserver.log == []
        finally:
            t2v.close()

    def test_get_attachment_content(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/attachments/{}/content".format(ATT_ID)).respond_with_data(
            b"\x89PNG\r\n", content_type="image/png", headers={"ETag": '"{}"'.format(ATT_ID)}
        )
        t2v = _t2v(httpserver)
        try:
            content = t2v.get_attachment_content(ATT_ID)
            assert content == AttachmentContent(data=b"\x89PNG\r\n", mime_type="image/png")
            sent = httpserver.log[0][0]
            assert sent.headers["Authorization"] == "Bearer tok"
            assert sent.headers["X-T2V-Partner-Key"] == "pk_test_123"
        finally:
            t2v.close()

    def test_get_attachment_content_404_is_typed(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/attachments/{}/content".format(MISSING_ID)).respond_with_json(
            {"detail": {"error": {"type": "attachment_not_found", "message": "Not found"}}},
            status=404,
        )
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.get_attachment_content(MISSING_ID)
            assert info.value.type == "attachment_not_found"
            assert info.value.status_code == 404
        finally:
            t2v.close()

    @pytest.mark.parametrize(
        "attachment_id",
        ["", ".", "..", "a/b", "../../v1/account/usage?x=", "att_1", ATT_ID + "/..", " " + ATT_ID],
    )
    def test_a_malformed_id_is_rejected_before_any_request(
        self, httpserver: HTTPServer, attachment_id: str
    ) -> None:
        """The engine types the id as a UUID; anything else could only address another
        route (``..`` collapses to ``GET /v1/content``, ``.`` to ``/v1/attachments/content``)."""
        t2v = _t2v(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                t2v.get_attachment_content(attachment_id)
            assert info.value.type == "invalid_attachment_id"
            assert httpserver.log == []
        finally:
            t2v.close()

    @pytest.mark.parametrize(
        "spelling", [ATT_ID.upper(), "{" + ATT_ID + "}", ATT_ID.replace("-", "")]
    )
    def test_a_valid_id_is_sent_in_its_canonical_form(
        self, httpserver: HTTPServer, spelling: str
    ) -> None:
        httpserver.expect_request("/v1/attachments/{}/content".format(ATT_ID)).respond_with_data(
            b"%PDF", content_type="application/pdf"
        )
        t2v = _t2v(httpserver)
        try:
            assert t2v.get_attachment_content(spelling).data == b"%PDF"
        finally:
            t2v.close()


# ── T2VClient: request(timeout=, headers=, raw=), request_bytes ─────────────


def _client(httpserver: HTTPServer) -> T2VClient:
    storage = MemoryStorage()
    storage.set("access_token", "old_tok")
    storage.set("refresh_token", "old_ref")
    return T2VClient("pk_test_123", storage, base_url=httpserver.url_for(""))


class TestClientRequestOptions:
    def test_request_bytes_refreshes_once_on_a_401(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request(
            "/v1/attachments/a1/content", headers={"Authorization": "Bearer old_tok"}
        ).respond_with_json(_error("authentication_error", "expired"), status=401)
        httpserver.expect_ordered_request(
            "/v1/auth/refresh", method="POST", json={"refresh_token": "old_ref"}
        ).respond_with_json({"access_token": "new_tok", "refresh_token": "new_ref"})
        httpserver.expect_ordered_request(
            "/v1/attachments/a1/content", headers={"Authorization": "Bearer new_tok"}
        ).respond_with_data(b"%PDF-1.7", content_type="application/pdf; charset=binary")
        client = _client(httpserver)
        try:
            assert client.request_bytes("/v1/attachments/a1/content") == (
                b"%PDF-1.7",
                "application/pdf",
            )
            assert client.storage.get("refresh_token") == "new_ref"
            httpserver.check_assertions()
        finally:
            client.close()

    def test_raw_with_a_rejected_refresh_signs_out(self, httpserver: HTTPServer) -> None:
        httpserver.expect_ordered_request("/v1/attachments/a1/content").respond_with_json(
            _error("authentication_error", "expired"), status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh", method="POST").respond_with_json(
            _error("authentication_error", "revoked"), status=401
        )
        client = _client(httpserver)
        try:
            with pytest.raises(AuthenticationError):
                client.request("/v1/attachments/a1/content", raw=True)
            assert client.storage.get("access_token") is None
            assert _hits(httpserver, "/v1/attachments/a1/content") == 1  # no second retry
        finally:
            client.close()

    def test_raw_returns_the_response_unparsed(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/blob").respond_with_data(b"not json", status=200)
        client = _client(httpserver)
        try:
            response = client.request("/v1/blob", raw=True)
            assert response.content == b"not json"
        finally:
            client.close()

    def test_a_raw_error_is_mapped_like_a_json_one(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request("/v1/blob").respond_with_json(
            _error("attachment_not_found", "Not found"), status=404
        )
        client = _client(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                client.request_bytes("/v1/blob")
            assert info.value.type == "attachment_not_found"
            assert info.value.status_code == 404
        finally:
            client.close()

    def test_timeout_overrides_the_default(self, httpserver: HTTPServer) -> None:
        release = threading.Event()
        answered = threading.Event()

        def slow(request: Request) -> Response:
            release.wait(5.0)
            answered.set()
            return _json({"ok": True})

        httpserver.expect_request("/v1/completions", method="POST").respond_with_handler(slow)
        client = _client(httpserver)
        try:
            with pytest.raises(NetworkError):
                client.request("/v1/completions", method="POST", body={}, timeout=0.05)
        finally:
            client.close()
            release.set()
            # Let the handler finish inside this test, or its log entry lands in the next.
            assert answered.wait(5.0)

    def test_extra_headers_are_added_to_the_standard_ones(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            "/v1/auth/oauth/exchange",
            method="POST",
            headers={"Origin": "http://127.0.0.1:5000", "X-T2V-Partner-Key": "pk_test_123"},
        ).respond_with_json({"status": "pending"}, status=202)
        client = _client(httpserver)
        try:
            body = client.request(
                "/v1/auth/oauth/exchange",
                method="POST",
                body={"nonce": "n"},
                requires_auth=False,
                headers={"Origin": "http://127.0.0.1:5000"},
            )
            assert body == {"status": "pending"}
            (sent,) = [e[0] for e in httpserver.log if e[0].path == "/v1/auth/oauth/exchange"]
            assert "Authorization" not in sent.headers
        finally:
            client.close()

    # ── Caller headers never replace the two auth headers ──

    @pytest.mark.parametrize(
        "name",
        ["Authorization", "authorization", "AUTHORIZATION",
         "X-T2V-Partner-Key", "x-t2v-partner-key", "X-t2v-PARTNER-key"],
    )
    def test_caller_headers_cannot_replace_the_auth_headers(
        self, httpserver: HTTPServer, name: str
    ) -> None:
        client = _client(httpserver)
        try:
            with pytest.raises(ValueError, match="(?i){}".format(name)):
                client.request("/v1/config", headers={"Origin": "o", name: "forged"})
            assert httpserver.log == []
        finally:
            client.close()

    def test_a_caller_header_replaces_a_standard_one_whatever_its_case(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/echo", method="POST").respond_with_json({"ok": True})
        client = _client(httpserver)
        # What goes on the wire, before the WSGI server folds repeated headers into one.
        wire: List[List[Any]] = []
        client._http.event_hooks = {
            "request": [lambda request: wire.append(list(request.headers.raw))],
            "response": [],
        }
        try:
            client.request(
                "/v1/echo", method="POST", body={}, headers={"content-type": "text/plain"}
            )
            sent = [(name.lower(), value) for name, value in wire[0]]
            assert [value for name, value in sent if name == b"content-type"] == [b"text/plain"]
            assert [value for name, value in sent if name == b"authorization"] == [
                b"Bearer old_tok"
            ]
        finally:
            client.close()

    # ── request_bytes: the same refresh path, and a size cap ──

    @pytest.mark.parametrize("refresh_status", [409, 429, 503])
    def test_request_bytes_transient_refresh_keeps_the_session(
        self, httpserver: HTTPServer, refresh_status: int
    ) -> None:
        httpserver.expect_ordered_request("/v1/attachments/a1/content").respond_with_json(
            _error("authentication_error", "expired"), status=401
        )
        httpserver.expect_ordered_request("/v1/auth/refresh", method="POST").respond_with_json(
            _error("refresh_conflict", "rotated"), status=refresh_status
        )
        client = _client(httpserver)
        try:
            with pytest.raises(NetworkError, match="temporarily unavailable"):
                client.request_bytes("/v1/attachments/a1/content")
            assert client.storage.get("access_token") == "old_tok"
            assert client.storage.get("refresh_token") == "old_ref"
            assert _hits(httpserver, "/v1/attachments/a1/content") == 1
        finally:
            client.close()

    def test_request_bytes_401_from_a_bad_partner_key_does_not_refresh(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/attachments/a1/content").respond_with_json(
            {"error": {"type": "partner_key_error", "message": "Unknown partner key"}},
            status=401,
        )
        client = _client(httpserver)
        try:
            with pytest.raises(PartnerKeyError, match="Unknown partner key"):
                client.request_bytes("/v1/attachments/a1/content")
            assert [e[0].path for e in httpserver.log] == ["/v1/attachments/a1/content"]
            assert client.storage.get("access_token") == "old_tok"
            assert client.storage.get("refresh_token") == "old_ref"
        finally:
            client.close()

    def test_request_bytes_asks_for_an_unencoded_body(self, httpserver: HTTPServer) -> None:
        httpserver.expect_request(
            "/v1/blob", headers={"Accept-Encoding": "identity"}
        ).respond_with_data(b"%PDF", content_type="application/pdf")
        client = _client(httpserver)
        try:
            assert client.request_bytes("/v1/blob") == (b"%PDF", "application/pdf")
        finally:
            client.close()

    def test_request_bytes_accepts_a_body_at_the_cap(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 16)
        httpserver.expect_request("/v1/blob").respond_with_data(b"x" * 16)
        client = _client(httpserver)
        try:
            assert client.request_bytes("/v1/blob")[0] == b"x" * 16
        finally:
            client.close()

    def test_request_bytes_refuses_a_declared_length_over_the_cap(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 16)
        httpserver.expect_request("/v1/blob").respond_with_data(b"x" * 17)
        client = _client(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                client.request_bytes("/v1/blob")
            assert info.value.type == "response_too_large"
        finally:
            client.close()

    def test_request_bytes_stops_reading_an_undeclared_body_at_the_cap(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 16)

        def endless(request: Request) -> Response:
            # No Content-Length: the body just keeps coming (HTTP/1.0 close framing).
            return Response((b"x" * 8 for _ in range(100_000)), content_type="image/png")

        httpserver.expect_request("/v1/blob").respond_with_handler(endless)
        client = _client(httpserver)
        try:
            started = time.monotonic()
            with pytest.raises(T2VError) as info:
                client.request_bytes("/v1/blob")
            assert info.value.type == "response_too_large"
            assert time.monotonic() - started < 5.0
        finally:
            client.close()

    def test_request_bytes_caps_the_decoded_size_of_a_compressed_body(
        self, httpserver: HTTPServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A server that compresses anyway: 4 KiB of zeros is ~30 bytes on the wire."""
        monkeypatch.setattr(client_module, "MAX_DOWNLOAD_BYTES", 1024)
        packed = gzip.compress(b"\0" * 4096)
        assert len(packed) < 1024
        httpserver.expect_request("/v1/blob").respond_with_data(
            packed, content_type="image/png", headers={"Content-Encoding": "gzip"}
        )
        client = _client(httpserver)
        try:
            with pytest.raises(T2VError) as info:
                client.request_bytes("/v1/blob")
            assert info.value.type == "response_too_large"
        finally:
            client.close()

    def test_request_bytes_decodes_a_compressed_body_under_the_cap(
        self, httpserver: HTTPServer
    ) -> None:
        httpserver.expect_request("/v1/blob").respond_with_data(
            gzip.compress(b"%PDF-1.7"), content_type="application/pdf",
            headers={"Content-Encoding": "gzip"},
        )
        client = _client(httpserver)
        try:
            assert client.request_bytes("/v1/blob") == (b"%PDF-1.7", "application/pdf")
        finally:
            client.close()

    def test_the_download_cap_sits_above_the_engine_attachment_cap(self) -> None:
        assert client_module.MAX_DOWNLOAD_BYTES == 25 * 1024 * 1024  # engine: 20 MB
