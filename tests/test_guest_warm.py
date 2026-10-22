"""``warm: true`` on a guest start the end-user's own action asked for.

An engine with the field starts the guest's key mint during the sign-in itself, one round
trip before ``POST /v1/account/warm`` would. Keys are minted lazily and only on intent, so
the field goes only on a guest start made for a message (or an AI call) already on its
way, never on one made at startup or for a model list.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import Talk2View

from .test_auth_anonymous import ANON_TOKENS
from .ui_helpers import wait_for


class _Engine:
    """Answers the guest start, records its bodies, and refuses the rest harmlessly."""

    def __init__(self, httpserver: HTTPServer) -> None:
        self.bodies: List[Dict[str, Any]] = []
        self.paths: List[str] = []
        httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_handler(
            self._anonymous
        )
        httpserver.expect_request("/v1/models").respond_with_json({"data": []})
        httpserver.expect_request("/v1/audio/models").respond_with_json({"data": []})
        httpserver.expect_request("/v1/completions", method="POST").respond_with_json(
            {"choices": []}
        )
        httpserver.expect_request("/v1/account/warm", method="POST").respond_with_handler(
            self._warm
        )
        httpserver.expect_request("/v1/attachments", method="POST").respond_with_json(
            {"id": "att_1", "filename": "a.png", "mime_type": "image/png", "size_bytes": 3}
        )
        # Every other route (the chat session's): a refusal, which ends the turn.
        httpserver.expect_request("/v1/sessions", method="POST").respond_with_json(
            {"error": {"type": "server_error", "message": "stop here"}}, status=500
        )

    def _anonymous(self, request: Request) -> Response:
        self.bodies.append(json.loads(request.get_data() or b"{}"))
        return Response(json.dumps(ANON_TOKENS), content_type="application/json")

    def _warm(self, request: Request) -> Response:
        self.paths.append(request.path)
        return Response(status=204)


@pytest.fixture()
def engine(httpserver: HTTPServer) -> _Engine:
    return _Engine(httpserver)


@pytest.fixture()
def t2v(httpserver: HTTPServer) -> Iterator[Talk2View]:
    client = Talk2View(
        "pk_test_123", base_url=httpserver.url_for(""), anonymous_auto_start=True
    )
    try:
        yield client
    finally:
        client.close()


class TestAMessageOnItsWay:
    def test_chat_starts_a_warm_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        with pytest.raises(Exception):  # noqa: B017 — the session route refuses on purpose
            list(t2v.chat("hi"))
        assert engine.bodies == [{"warm": True}]

    def test_completions_start_a_warm_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        t2v.completions("m", [{"role": "user", "content": "hi"}])
        assert engine.bodies == [{"warm": True}]

    def test_warm_up_starts_a_warm_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        """A first keystroke: the SDK's own warm-up follows it, as before."""
        t2v.warm_up()
        assert wait_for(lambda: engine.paths == ["/v1/account/warm"])
        assert engine.bodies == [{"warm": True}]

    def test_start_anonymous_warms_only_when_asked(
        self, engine: _Engine, t2v: Talk2View
    ) -> None:
        t2v.auth.start_anonymous(captcha_token="turnstile-xyz", warm=True)
        assert engine.bodies == [{"captcha_token": "turnstile-xyz", "warm": True}]


class TestNoMessageYet:
    def test_list_models_starts_a_cold_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        t2v.list_models()
        assert engine.bodies == [{}]

    def test_ensure_session_starts_a_cold_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        assert t2v.ensure_session() is True
        assert engine.bodies == [{}]

    def test_start_anonymous_starts_a_cold_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        t2v.auth.start_anonymous()
        assert engine.bodies == [{}]

    def test_an_upload_starts_a_cold_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        """An upload alone is no message: it may never be sent."""
        t2v.upload_attachment(b"png", filename="a.png")
        assert engine.bodies == [{}]

    def test_the_voice_models_start_a_cold_guest(self, engine: _Engine, t2v: Talk2View) -> None:
        t2v.voice.list_models()
        assert engine.bodies == [{}]
