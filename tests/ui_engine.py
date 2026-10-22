"""``FakeEngine`` plus the account routes the UI server reads: ``GET /v1/config``,
``GET /v1/models`` with display names, ``POST /v1/attachments``, ``GET /v1/audio/models``
and ``POST /v1/audio/transcriptions`` (kept in ``transcriptions``; ``transcription_answer``
set to ``(status, error type, message)`` makes it refuse instead).

``config_gate``, when set to an unset ``Event``, holds every ``/v1/config`` request until
the test sets it (to race a refresh against an auth change).

Every ``/v1/auth/*`` request is kept in ``auth_bodies`` as ``(path, JSON body)``, so a test
can check what reached the engine (a password, a captcha token). ``anonymous_refusal``,
when set to ``(status, error type)``, makes ``POST /v1/auth/anonymous`` refuse with that
error instead.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from .fake_engine import FakeEngine, _error, _json

CONFIG: Dict[str, Any] = {
    "default_llm_model": "m-fast",
    "default_stt_model": None,
    "default_image_model": None,
    "system_prompt": None,
    "allowed_llm_models": ["m-fast", "m-deep"],
    "allowed_stt_models": None,
    "gated_llm_models": None,
    "gated_stt_models": None,
    "web_search_enabled": False,
}
MODELS: List[Dict[str, Any]] = [
    {"id": "m-fast", "object": "model", "created": 1, "owned_by": "t2v", "name": "Fast"},
    {"id": "m-deep", "object": "model", "created": 2, "owned_by": "t2v"},
]


class AccountEngine(FakeEngine):
    def __init__(self, server: Optional[HTTPServer] = None) -> None:
        super().__init__(server)
        self.config: Dict[str, Any] = dict(CONFIG)
        self.models: List[Dict[str, Any]] = [dict(m) for m in MODELS]
        self.audio_models: List[Dict[str, Any]] = [
            {"id": "scribe_v2", "object": "model", "created": 1, "owned_by": "t2v",
             "name": "Scribe v2", "supported_languages": ["en", "vi"], "description": "Fast"},
            {"id": "faster-whisper-base", "object": "model", "created": 2, "owned_by": "t2v"},
        ]
        self.uploads: List[Dict[str, Any]] = []
        self.transcriptions: List[Dict[str, Any]] = []
        self.transcription_answer: Optional[Tuple[int, str, str]] = None
        self.transcription_text = "hello from the mic"
        self.config_gate: Optional[threading.Event] = None
        self.config_requests = 0
        self.auth_bodies: List[Tuple[str, Dict[str, Any]]] = []
        self.anonymous_refusal: Optional[Tuple[int, str]] = None

    def _handle(self, request: Request) -> Response:
        if request.path.startswith("/v1/auth/oauth/desktop/"):
            return super()._handle(request)
        if request.path.startswith("/v1/auth/"):
            with self.lock:
                self.auth_bodies.append((request.path, request.get_json(silent=True) or {}))
                refusal = self.anonymous_refusal
                if request.path != "/v1/auth/anonymous":
                    refusal = None
                if refusal is not None:
                    self.log.append("{} {} -".format(request.method, request.path))
            if refusal is not None:
                return _error(refusal[0], refusal[1], "Refused by the test")
            return super()._handle(request)
        if request.path not in (
            "/v1/config", "/v1/models", "/v1/attachments", "/v1/audio/models",
            "/v1/audio/transcriptions",
        ):
            return super()._handle(request)
        auth = request.headers.get("Authorization", "")
        with self.lock:
            user_id = self.tokens.get(auth[len("Bearer "):], "") if auth else ""
            self.log.append("{} {} {}".format(request.method, request.path, user_id or "-"))
        if not user_id:
            return _error(401, "authentication_error", "Invalid token")
        if request.path == "/v1/config":
            with self.lock:
                self.config_requests += 1
            gate = self.config_gate
            if gate is not None:
                gate.wait(10)
            return _json(self.config)
        if request.path == "/v1/models":
            return _json({"object": "list", "data": self.models})
        if request.path == "/v1/audio/models":
            return _json({"object": "list", "data": self.audio_models})
        if request.path == "/v1/audio/transcriptions":
            upload = request.files["file"]
            data = upload.read()
            record = {
                "model": request.form.get("model"), "filename": upload.filename,
                "content_type": upload.mimetype, "size": len(data), "user": user_id,
            }
            if request.form.get("language"):
                record["language"] = request.form["language"]
            with self.lock:
                self.transcriptions.append(record)
                answer = self.transcription_answer
            if answer is not None:
                return _error(answer[0], answer[1], answer[2])
            return _json({"text": self.transcription_text})
        upload = request.files["file"]
        data = upload.read()
        record = {
            "id": str(uuid.uuid4()),
            "filename": upload.filename,
            "mime_type": upload.mimetype,
            "size_bytes": len(data),
        }
        with self.lock:
            self.uploads.append(dict(record, data=data, user=user_id))
        return _json(record)
