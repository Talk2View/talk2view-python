"""The names the TypeScript-parity code (timeouts, models, voice, notices, UI) is written against.

If one of these fails, the core moved: reconcile the parity code against it.
"""

from __future__ import annotations

import dataclasses
import inspect

from talk2view import DEMO_LIMIT_REACHED, ChatController, Model, Talk2View
from talk2view.auth import T2VAuth
from talk2view.client import DEFAULT_TIMEOUT, STREAM_TIMEOUT, T2VClient
from talk2view.types import AudioModelsResponse
from talk2view.ui.app_state import APP_KEYS, AppState, UnknownModelError, _identity
from talk2view.ui.router import CommandRouter, _openable
from talk2view.ui.server import UIServer
from talk2view.ui.testing import FakeController
from talk2view.ui.wire import COMMANDS, ERROR_TYPES
from talk2view.voice import T2VVoice


def _params(fn: object) -> list:  # type: ignore[type-arg]
    return list(inspect.signature(fn).parameters)  # type: ignore[arg-type]


def test_client_timeouts_and_refresh_hooks() -> None:
    assert DEFAULT_TIMEOUT == 30.0 and STREAM_TIMEOUT == 300.0
    assert _params(T2VClient.__init__)[:4] == ["self", "partner_key", "storage", "base_url"]
    assert _params(T2VClient._try_refresh_token) == ["self", "failed_access_token"]
    for name in ("_sign_out_rejected", "upload_request", "request", "stream_request"):
        assert callable(getattr(T2VClient, name, None)), name


def test_talk2view_surface_the_parity_code_extends() -> None:
    assert "anonymous_auto_start" in _params(Talk2View.__init__)
    defaults = inspect.signature(Talk2View.set_chat_defaults).parameters
    assert defaults["model"].kind is inspect.Parameter.KEYWORD_ONLY
    for name in ("_require_session", "_delete_session", "on_anonymous_unavailable", "chat"):
        assert callable(getattr(Talk2View, name, None)), name
    assert callable(getattr(T2VAuth, "_notify_listeners", None))


def test_voice_and_models() -> None:
    assert _params(T2VVoice.__init__)[:2] == ["self", "client"]
    assert [f.name for f in dataclasses.fields(Model)][:5] == [
        "id", "object", "created", "owned_by", "name",
    ]
    assert [f.name for f in dataclasses.fields(AudioModelsResponse)] == ["object", "data"]


def test_controller_notices() -> None:
    assert DEMO_LIMIT_REACHED == "demoLimitReached"
    assert _params(ChatController.on) == ["self", "event", "callback"]
    for name in ("_stream_turn", "_flush", "snapshot", "subscribe"):
        assert callable(getattr(FakeController, name, None)), name


def test_ui_surface_the_parity_code_extends() -> None:
    assert set(APP_KEYS) >= {
        "auth", "models", "config", "capabilities", "theme", "preferences", "results",
    }
    assert _params(AppState.__init__)[:3] == ["self", "t2v", "host"]
    assert _params(AppState.attach)[:2] == ["self", "hub"]
    for name in ("select_model", "_on_auth", "_record_auth_locked", "_refresh_once"):
        assert callable(getattr(AppState, name, None)), name
    assert issubclass(UnknownModelError, LookupError)
    assert _identity(None) == (False, None)
    assert {"app", "static_dir", "on_skills_list"} <= set(_params(UIServer.__init__))
    for name in ("execute", "_send", "_start_anonymous"):
        assert callable(getattr(CommandRouter, name, None)), name
    assert _openable("https://example.com/reset") and not _openable("javascript:alert(1)")
    assert set(COMMANDS["selectModel"]) == {"modelId"}
    assert {"unknown_model", "account_unavailable", "internal_error"} <= ERROR_TYPES
