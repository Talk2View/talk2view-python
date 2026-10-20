"""Tests for T2VVoice."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from talk2view.client import T2VClient
from talk2view.storage import MemoryStorage
from talk2view.voice import T2VVoice


def _multipart_field_value(body: str, field_name: str) -> Optional[str]:
    """Extract a single multipart/form-data field's value from a raw request body.

    Anchored to the field's own ``Content-Disposition`` part so the value is
    checked in its own slot, not as a bare substring that could coincidentally
    match unrelated bytes elsewhere in the body (another field's value, the
    filename, the boundary).
    """
    match = re.search(
        rf'name="{re.escape(field_name)}"\r\n\r\n(.*?)\r\n--', body, re.DOTALL
    )
    return match.group(1) if match else None


class TestT2VVoice:
    def _make_voice(self, base_url: str) -> T2VVoice:
        storage = MemoryStorage()
        storage.set("access_token", "tok_abc")
        client = T2VClient(
            partner_key="pk_test_123",
            storage=storage,
            base_url=base_url,
        )
        return T2VVoice(client)

    def test_transcribe_bytes(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "hello world"})

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(b"fake audio data", model="test-stt-model")
        assert result.text == "hello world"

    def test_transcribe_file_path(self, httpserver, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "from file"})

        audio_file = tmp_path / "test.webm"
        audio_file.write_bytes(b"fake audio")

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(str(audio_file), model="test-stt-model")
        assert result.text == "from file"

    def test_transcribe_with_language(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "hola mundo"})

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(b"audio", model="test-stt-model", language="es")
        assert result.text == "hola mundo"

    def test_list_models(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/audio/models").respond_with_json({
            "object": "list",
            "data": [
                {"id": "whisper-1", "object": "model", "created": 0, "owned_by": "openai"},
                {"id": "deepgram-nova-2", "object": "model", "created": 0, "owned_by": "deepgram"},
            ],
        })

        voice = self._make_voice(httpserver.url_for(""))
        models = voice.list_models()
        assert len(models) == 2
        assert models[0].id == "whisper-1"
        assert models[1].id == "deepgram-nova-2"

    def test_list_models_empty(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request("/v1/audio/models").respond_with_json({
            "object": "list",
            "data": [],
        })

        voice = self._make_voice(httpserver.url_for(""))
        models = voice.list_models()
        assert models == []

    def test_transcribe_file_like(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "from file-like"})

        import io
        audio_io = io.BytesIO(b"fake audio")
        audio_io.name = "recording.mp3"

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(audio_io, model="test-stt-model", filename="recording.mp3")
        assert result.text == "from file-like"

    def test_transcribe_sends_translate_to_and_guidance(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "hola"})

        voice = self._make_voice(httpserver.url_for(""))
        voice.transcribe(
            b"fake audio data",
            model="test-stt-model",
            translate_to="es",
            translation_guidance="Prefer formal register.",
        )

        # pytest-httpserver records every handled request as (request, response).
        request, _ = httpserver.log[0]
        body = request.get_data(as_text=True)
        assert "translate_to" in body
        assert "translation_guidance" in body
        # Anchored to each field's own part: "es" is a substring of
        # "test-stt-model" (t-**es**-t), so a bare `"es" in body` check would
        # pass even if the wrong value were sent under the translate_to key.
        assert _multipart_field_value(body, "translate_to") == "es"
        assert (
            _multipart_field_value(body, "translation_guidance")
            == "Prefer formal register."
        )

    def test_transcribe_omits_translation_fields_when_unset(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "hello"})

        voice = self._make_voice(httpserver.url_for(""))
        voice.transcribe(b"fake audio data", model="test-stt-model")

        request, _ = httpserver.log[0]
        body = request.get_data(as_text=True)
        assert "translate_to" not in body
        assert "translation_guidance" not in body

    def test_transcribe_surfaces_degraded_translation(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """A failed translation must be visible to the caller, not silently
        swallowed as the untranslated source text.

        The engine returns HTTP 200 with ``translated: False`` and
        ``source_text`` set when translation degrades (e.g. upstream error).
        Before ``source_text``/``translated`` existed on this dataclass, a
        Python caller had no way to tell this apart from a real translation.
        """
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json(
            {
                "text": "Bạn đau ở đâu?",
                "source_text": "Bạn đau ở đâu?",
                "translated": False,
            }
        )

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(
            b"fake audio data", model="test-stt-model", translate_to="en"
        )
        assert result.text == "Bạn đau ở đâu?"
        assert result.translated is False
        assert result.source_text == "Bạn đau ở đâu?"

    def test_transcribe_leaves_translation_fields_none_when_absent(self, httpserver) -> None:  # type: ignore[no-untyped-def]
        """A plain (non-translating) response has no source_text/translated

        keys at all — the engine omits them entirely. Both fields must default
        to None rather than raise or misreport.
        """
        httpserver.expect_request(
            "/v1/audio/transcriptions", method="POST"
        ).respond_with_json({"text": "hello world"})

        voice = self._make_voice(httpserver.url_for(""))
        result = voice.transcribe(b"fake audio data", model="test-stt-model")
        assert result.text == "hello world"
        assert result.translated is None
        assert result.source_text is None
