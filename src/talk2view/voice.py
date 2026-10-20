"""Voice / audio transcription module."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Union

from .client import T2VClient
from .types import AudioModel, TranscriptionResponse, _refuse_default_model


def _languages(value: Any) -> Optional[List[str]]:
    """The codes a model lists: strings only; anything but a list reads as unrestricted."""
    return [code for code in value if isinstance(code, str)] if isinstance(value, list) else None


class T2VVoice:
    """Speech-to-text via the Talk2View engine.

    Usage::

        t2v = Talk2View(partner_key="pk_live_abc123")
        t2v.auth.login("user@example.com", "password")

        models = t2v.voice.list_models()
        result = t2v.voice.transcribe("recording.webm", model=models[0].id)
        print(result.text)

    ``require_session`` runs before every request, as for every other authenticated call:
    ``Talk2View`` passes the one that starts a guest session when ``anonymous_auto_start``
    is on (TS ``transcribe()`` and ``listAudioModels()`` do the same). A ``T2VVoice`` built
    on a bare ``T2VClient`` starts nothing.
    """

    def __init__(
        self, client: T2VClient, *, require_session: Optional[Callable[[], None]] = None
    ) -> None:
        self._client = client
        self._require_session = require_session

    def transcribe(
        self,
        audio: Union[str, Path, bytes, BinaryIO],
        *,
        model: str,
        language: Optional[str] = None,
        translate_to: Optional[str] = None,
        translation_guidance: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> TranscriptionResponse:
        """Transcribe audio to text.

        The audio is read first, so a missing file raises before any request (a guest
        session included). With ``anonymous_auto_start`` a refused guest session raises
        ``AuthenticationError`` (``type="sign_in_required"``) and nothing is uploaded.

        Args:
            audio: File path (str/Path), raw bytes, or a file-like object.
            model: STT model ID (e.g. from ``list_models()``). Required.
            language: Optional ISO-639-1 language code of the SPOKEN audio.
            translate_to: Optional ISO-639-1 code to translate the transcript
                into. Omit for no translation.
            translation_guidance: Optional per-user instructions added to the
                translation prompt (max 500 characters, enforced server-side).
                Ignored unless ``translate_to`` is set.
            filename: Override filename sent to the server (used for MIME detection).
        """
        _refuse_default_model(model, "voice.transcribe()")
        file_tuple = self._prepare_file(audio, filename)
        data: Dict[str, Any] = {"model": model}
        if language:
            data["language"] = language
        if translate_to:
            data["translate_to"] = translate_to
        if translation_guidance:
            data["translation_guidance"] = translation_guidance

        if self._require_session is not None:
            self._require_session()
        result = self._client.upload_request(
            "/v1/audio/transcriptions",
            files={"file": file_tuple},
            data=data,
        )

        text = result.get("text")
        if text is None:
            raise ValueError("Transcription response missing 'text' field")
        return TranscriptionResponse(
            text=text,
            source_text=result.get("source_text"),
            translated=result.get("translated"),
        )

    def list_models(self) -> List[AudioModel]:
        """List available speech-to-text models, with the languages each one transcribes
        (``supported_languages``; None = unrestricted) and a short ``description``. An
        entry that is not an object, or has no id, is left out."""
        if self._require_session is not None:
            self._require_session()
        result = self._client.request("/v1/audio/models")
        items = result.get("data", []) if isinstance(result, dict) else []
        return [
            AudioModel(
                id=str(m["id"]),
                object=str(m.get("object", "model")),
                created=m["created"] if isinstance(m.get("created"), int) else 0,
                owned_by=str(m.get("owned_by", "talk2view")),
                name=m["name"] if isinstance(m.get("name"), str) else None,
                supported_languages=_languages(m.get("supported_languages")),
                description=m["description"] if isinstance(m.get("description"), str) else None,
            )
            for m in (items if isinstance(items, list) else [])
            if isinstance(m, dict) and m.get("id")
        ]

    @staticmethod
    def _prepare_file(
        audio: Union[str, Path, bytes, BinaryIO],
        filename: Optional[str],
    ) -> tuple[str, bytes, str]:
        """Normalise audio input into an httpx-compatible file tuple.

        Returns ``(filename, content_bytes, mime_type)``.
        All inputs are materialised to bytes so retries work correctly.
        """
        if isinstance(audio, (str, Path)):
            path = Path(audio)
            fname = filename or path.name
            mime = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            return (fname, path.read_bytes(), mime)

        if isinstance(audio, bytes):
            fname = filename or "audio.webm"
            mime = mimetypes.guess_type(fname)[0] or "audio/webm"
            return (fname, audio, mime)

        # File-like object — read into bytes so 401 retries work
        content = audio.read()
        fname = filename or str(getattr(audio, "name", "audio.webm"))
        if isinstance(fname, Path):
            fname = fname.name
        mime = mimetypes.guess_type(fname)[0] or "audio/webm"
        return (fname, content, mime)
