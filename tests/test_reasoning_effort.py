"""reasoning_effort on the core request path: it travels with the model pick.

The pick is the pair (model, effort). Naming a model without an effort means that model's
default effort, so a model change never carries the old model's effort along, and
DEFAULT_MODEL forgets both."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import DEFAULT_MODEL, Talk2View
from talk2view.types import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
)

from .controller_fakes import FakeT2V, make_controller
from .fake_engine import FakeEngine

SESSION = {"session_id": "s1", "thread_id": "th1", "model": "g4h", "reasoning_effort": "high"}


def _t2v(session: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Talk2View:
    t2v = Talk2View("pk_test_123", base_url="http://localhost:9999", **kwargs)
    t2v._client.close()
    client = MagicMock()
    answer = SESSION if session is None else session

    def request(endpoint: str, **kw: Any) -> Dict[str, Any]:
        if endpoint == "/v1/sessions":
            return dict(answer)
        return {}

    def stream_request(endpoint: str, body: Dict[str, Any], **kw: Any) -> Iterator[Any]:
        return iter(
            [
                ChatCompletionChunk(
                    id="c1",
                    choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content="hi"))],
                ),
                ChatCompletionChunk(
                    id="c2", choices=[ChatCompletionChunkChoice(finish_reason="stop")]
                ),
            ]
        )

    client.request.side_effect = request
    client.stream_request.side_effect = stream_request
    t2v._client = client
    t2v.tools._client = client
    return t2v


def bodies(t2v: Talk2View, endpoint: str) -> List[Dict[str, Any]]:
    """The ``body`` of each ``request`` call to ``endpoint``."""
    calls = t2v._client.request.call_args_list  # type: ignore[attr-defined]
    return [c[1].get("body") for c in calls if c[0][0] == endpoint]


def message_bodies(t2v: Talk2View) -> List[Dict[str, Any]]:
    calls = t2v._client.stream_request.call_args_list  # type: ignore[attr-defined]
    return [c[0][1] for c in calls if c[0][0].endswith("/messages")]


def test_the_effort_rides_next_to_the_model_on_create_and_message() -> None:
    t2v = _t2v(model="g4h", reasoning_effort="high")
    session = t2v.create_session()
    list(session.send_message("hello"))
    (create_body,) = bodies(t2v, "/v1/sessions")
    (message_body_,) = message_bodies(t2v)
    assert create_body == {"model": "g4h", "reasoning_effort": "high"}
    assert message_body_["model"] == "g4h" and message_body_["reasoning_effort"] == "high"
    assert session.reasoning_effort == "high" and t2v.reasoning_effort == "high"


def test_bodies_are_unchanged_without_an_effort() -> None:
    t2v = _t2v(model="g4h-thinking")
    list(t2v.create_session().send_message("hello"))
    (create_body,) = bodies(t2v, "/v1/sessions")
    (message_body_,) = message_bodies(t2v)
    assert create_body == {"model": "g4h-thinking"}
    assert "reasoning_effort" not in message_body_
    assert t2v.reasoning_effort is None

    t2v = _t2v()
    list(t2v.create_session().send_message("hello"))
    (create_body,) = bodies(t2v, "/v1/sessions")
    (message_body_,) = message_bodies(t2v)
    assert create_body == {}
    assert "model" not in message_body_ and "reasoning_effort" not in message_body_


def test_the_effort_is_normalised_and_checked_before_anything_changes() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="  High ")
    assert t2v.reasoning_effort == "high"
    with pytest.raises(TypeError, match="must be a string or None"):
        t2v.set_chat_defaults(model="other", reasoning_effort=3)  # type: ignore[arg-type]
    assert (t2v.model, t2v.reasoning_effort) == ("g4h", "high")
    with pytest.raises(TypeError):
        Talk2View("pk", model="g4h", reasoning_effort=["high"])  # type: ignore[arg-type]
    assert _t2v(model="g4h", reasoning_effort="MEDIUM").reasoning_effort == "medium"


def test_a_blank_effort_counts_as_none() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="high")
    t2v.set_chat_defaults(model="other", reasoning_effort="  ")
    assert (t2v.model, t2v.reasoning_effort) == ("other", None)
    assert _t2v(reasoning_effort="").reasoning_effort is None
    assert _t2v(model="g4h", reasoning_effort=" ").reasoning_effort is None
    t2v = _t2v()
    t2v.set_chat_defaults(reasoning_effort="")
    assert (t2v.model, t2v.reasoning_effort) == (None, None)
    t2v.create_session(reasoning_effort="")
    assert bodies(t2v, "/v1/sessions")[-1] == {}


def test_an_effort_needs_a_model() -> None:
    with pytest.raises(ValueError, match="needs a model"):
        Talk2View("pk", reasoning_effort="high")
    with pytest.raises(ValueError, match="needs a model"):
        _t2v().set_chat_defaults(reasoning_effort="high")
    with pytest.raises(ValueError, match="needs a model"):
        _t2v().create_session(reasoning_effort="high")
    t2v = _t2v(model="g4h", reasoning_effort="low")
    with pytest.raises(ValueError):
        t2v.set_chat_defaults(model=DEFAULT_MODEL, reasoning_effort="high")
    assert (t2v.model, t2v.reasoning_effort) == ("g4h", "low")


def test_naming_a_model_without_an_effort_clears_the_effort() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="high")
    t2v.set_chat_defaults(model="gpt")
    assert (t2v.model, t2v.reasoning_effort) == ("gpt", None)
    t2v.set_chat_defaults(model="g4h", reasoning_effort="high")
    before = t2v._defaults_version
    t2v.set_chat_defaults(model="g4h")  # the same model, restated
    assert t2v.reasoning_effort is None
    assert t2v._defaults_version == before + 1


def test_an_effort_only_change_makes_the_next_turn_build_a_new_session_object() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="low")
    t2v.create_session()
    before = t2v._defaults_version
    t2v.set_chat_defaults(reasoning_effort="high")
    assert (t2v.model, t2v.reasoning_effort) == ("g4h", "high")
    assert t2v._defaults_version == before + 1 and t2v._session_version != t2v._defaults_version
    t2v.set_chat_defaults(reasoning_effort="high")  # no change
    assert t2v._defaults_version == before + 1
    t2v.set_chat_defaults()  # neither: nothing changes
    assert t2v._defaults_version == before + 1


def test_an_explicit_create_takes_its_own_effort_not_the_defaults() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="high")
    t2v.create_session(model="other")
    assert bodies(t2v, "/v1/sessions")[-1] == {"model": "other"}
    t2v.create_session(model="other", reasoning_effort="medium")
    assert bodies(t2v, "/v1/sessions")[-1] == {"model": "other", "reasoning_effort": "medium"}
    t2v.create_session(reasoning_effort=" Low")  # the default model, at this effort
    assert bodies(t2v, "/v1/sessions")[-1] == {"model": "g4h", "reasoning_effort": "low"}
    assert (t2v.model, t2v.reasoning_effort) == ("g4h", "high")  # defaults untouched


def test_a_session_sends_the_effort_it_was_created_with() -> None:
    t2v = _t2v()
    t2v.set_chat_defaults(model="g4h", reasoning_effort="high")
    session = t2v.create_session(model="other", reasoning_effort="medium")
    list(session.send_message("hello"))
    (body,) = message_bodies(t2v)
    assert (body["model"], body["reasoning_effort"]) == ("other", "medium")


def test_a_session_from_an_old_engine_has_no_effort() -> None:
    t2v = _t2v({"session_id": "s1", "thread_id": "th1", "model": "g4h"}, model="g4h")
    assert t2v.create_session().reasoning_effort is None
    t2v = _t2v({"session_id": "s1", "thread_id": "th1", "model": "g4h", "reasoning_effort": 5})
    assert t2v.create_session().reasoning_effort is None


def test_completions_sends_the_effort_only_when_given() -> None:
    t2v = _t2v()
    t2v._require_session = MagicMock()  # type: ignore[method-assign]
    messages = [{"role": "user", "content": "hi"}]
    t2v.completions("g4h", messages, reasoning_effort=" Medium")
    assert bodies(t2v, "/v1/completions")[-1] == {
        "model": "g4h",
        "messages": messages,
        "reasoning_effort": "medium",
    }
    t2v.completions("g4h", messages)
    assert "reasoning_effort" not in bodies(t2v, "/v1/completions")[-1]
    t2v.completions("g4h", messages, reasoning_effort="  ")
    assert "reasoning_effort" not in bodies(t2v, "/v1/completions")[-1]


def test_the_controller_forwards_the_effort() -> None:
    t2v = FakeT2V()
    make_controller(t2v, model="g4h", reasoning_effort="high")
    assert t2v.defaults == [{"model": "g4h", "capabilities": None, "reasoning_effort": "high"}]
    t2v = FakeT2V()
    make_controller(t2v, reasoning_effort="low")
    assert t2v.defaults == [{"model": None, "capabilities": None, "reasoning_effort": "low"}]


# ── Against the fake engine (helpers as in test_default_model.py) ──

Call = Tuple[str, str, Optional[Dict[str, Any]]]


class Recorder(FakeEngine):
    """``FakeEngine`` that keeps every request's method, path and JSON body."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.calls: List[Call] = []

    def _handle(self, request: Request) -> Response:
        with self.lock:
            self.calls.append((request.method, request.path, request.get_json(silent=True)))
        return super()._handle(request)

    def take(self) -> List[Call]:
        with self.lock:
            taken, self.calls = self.calls, []
        return taken


@pytest.fixture
def recorder(fake_engine_server: HTTPServer) -> Iterator[Recorder]:
    fake = Recorder(fake_engine_server).start()
    yield fake
    fake.stop()


def turn(t2v: Talk2View, recorder: Recorder, text: str) -> List[Call]:
    recorder.take()
    assert [e.type for e in t2v.chat(text)] == ["text", "done"]
    return recorder.take()


def session_calls(calls: List[Call]) -> List[Tuple[str, str]]:
    return [(method, path) for method, path, _ in calls if path.startswith("/v1/sessions")]


def message_body(calls: List[Call]) -> Dict[str, Any]:
    (body,) = [b for _, path, b in calls if path.endswith("/messages")]
    assert body is not None
    return body


def test_default_model_drops_the_effort_too(recorder: Recorder) -> None:
    t2v = Talk2View("pk_test", base_url=recorder.url(), model="m-deep", reasoning_effort="high")
    try:
        t2v.auth.login("ana@example.com", "pw")
        first = turn(t2v, recorder, "one")
        assert [b for m, p, b in first if (m, p) == ("POST", "/v1/sessions")] == [
            {"model": "m-deep", "reasoning_effort": "high"}
        ]
        assert message_body(first)["reasoning_effort"] == "high"
        held = t2v.get_session()
        assert held is not None

        t2v.set_chat_defaults(model=DEFAULT_MODEL)
        assert t2v.reasoning_effort is None
        calls = turn(t2v, recorder, "again")
        assert ("DELETE", "/v1/sessions/" + held.id) in session_calls(calls)  # a fresh one
        assert [b for m, p, b in calls if (m, p) == ("POST", "/v1/sessions")] == [{}]
        body = message_body(calls)
        assert "model" not in body and "reasoning_effort" not in body
    finally:
        t2v.close()


def test_an_effort_change_switches_the_next_turn(recorder: Recorder) -> None:
    t2v = Talk2View("pk_test", base_url=recorder.url(), model="m-deep", reasoning_effort="low")
    try:
        t2v.auth.login("ana@example.com", "pw")
        turn(t2v, recorder, "one")
        t2v.set_chat_defaults(reasoning_effort="high")
        calls = turn(t2v, recorder, "two")
        assert [b for m, p, b in calls if (m, p) == ("POST", "/v1/sessions")] == [
            {"model": "m-deep", "reasoning_effort": "high"}
        ]
        body = message_body(calls)
        assert (body["model"], body["reasoning_effort"]) == ("m-deep", "high")
    finally:
        t2v.close()
