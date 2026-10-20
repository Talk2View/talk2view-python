"""Voice parity with the TypeScript SDK: a guest session is started first
(``index.ts:409-416, 452-455``), and audio models carry their languages and blurb
(``types.ts`` ``AudioModel``)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, List

import pytest
from pytest_httpserver import HTTPServer

from talk2view import AudioModel, AuthenticationError, Model, Talk2View
from talk2view.client import T2VClient
from talk2view.storage import MemoryStorage
from talk2view.voice import T2VVoice

GUEST = {
    "access_token": "tok-guest",
    "refresh_token": "ref-guest",
    "user": {"id": "anon-1", "email": ""},
    "is_anonymous": True,
}


@pytest.fixture
def guest_t2v(httpserver: HTTPServer) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=httpserver.url_for(""), anonymous_auto_start=True)
    yield client
    client.close()


def paths(server: HTTPServer) -> List[str]:
    return [request.path for request, _ in server.log]


def test_audio_models_carry_languages_and_description(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/audio/models").respond_with_json(
        {
            "object": "list",
            "data": [
                {
                    "id": "scribe",
                    "object": "model",
                    "created": 3,
                    "owned_by": "elevenlabs",
                    "name": "Scribe",
                    "supported_languages": ["en", "fr", 7],
                    "description": "Fast voice typing",
                },
                {"id": "whisper-1", "supported_languages": "en", "description": 5},
                {"object": "model"},
                "junk",
            ],
        }
    )
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    voice = T2VVoice(T2VClient("pk_test", storage, base_url=httpserver.url_for("")))
    models = voice.list_models()
    assert models == [
        AudioModel(
            id="scribe",
            object="model",
            created=3,
            owned_by="elevenlabs",
            name="Scribe",
            supported_languages=["en", "fr"],
            description="Fast voice typing",
        ),
        AudioModel(id="whisper-1"),
    ]
    assert isinstance(models[0], Model)  # an AudioModel is a Model, as in TS


def test_listing_audio_models_starts_a_guest_session_first(
    httpserver: HTTPServer, guest_t2v: Talk2View
) -> None:
    httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(GUEST)
    httpserver.expect_request("/v1/audio/models").respond_with_json(
        {"object": "list", "data": [{"id": "scribe"}]}
    )
    assert [m.id for m in guest_t2v.voice.list_models()] == ["scribe"]
    assert paths(httpserver) == ["/v1/auth/anonymous", "/v1/audio/models"]
    assert httpserver.log[1][0].headers["Authorization"] == "Bearer tok-guest"
    assert guest_t2v.auth.is_anonymous()


def test_transcribing_starts_a_guest_session_first(
    httpserver: HTTPServer, guest_t2v: Talk2View
) -> None:
    httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(GUEST)
    httpserver.expect_request("/v1/audio/transcriptions", method="POST").respond_with_json(
        {"text": "hello"}
    )
    assert guest_t2v.voice.transcribe(b"audio", model="scribe").text == "hello"
    assert paths(httpserver) == ["/v1/auth/anonymous", "/v1/audio/transcriptions"]
    assert httpserver.log[1][0].headers["Authorization"] == "Bearer tok-guest"


def test_a_refused_guest_start_stops_voice_before_any_audio_request(
    httpserver: HTTPServer, guest_t2v: Talk2View
) -> None:
    httpserver.expect_request("/v1/auth/anonymous", method="POST").respond_with_json(
        {"error": {"type": "anonymous_access_disabled", "message": "Guests are off"}}, status=403
    )
    with pytest.raises(AuthenticationError) as caught:
        guest_t2v.voice.transcribe(b"audio", model="scribe")
    assert caught.value.type == "sign_in_required"
    with pytest.raises(AuthenticationError):
        guest_t2v.voice.list_models()
    # Each call asks again (a refusal is not cached), and neither reaches the audio routes.
    assert paths(httpserver) == ["/v1/auth/anonymous", "/v1/auth/anonymous"]


def test_a_missing_audio_file_fails_before_any_guest_start(
    httpserver: HTTPServer, guest_t2v: Talk2View, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        guest_t2v.voice.transcribe(tmp_path / "missing.webm", model="scribe")
    assert paths(httpserver) == []


def test_without_auto_start_voice_starts_nothing(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v1/audio/models").respond_with_json(
        {"object": "list", "data": []}
    )
    t2v = Talk2View("pk_test", base_url=httpserver.url_for(""))
    try:
        assert t2v.voice.list_models() == []
        assert paths(httpserver) == ["/v1/audio/models"]
    finally:
        t2v.close()
