"""set_chat_defaults(model=DEFAULT_MODEL): back to the partner's default model.

TS Settings' "Default" sends no model (``chat/views/settings.tsx:94-101``). The engine keeps
the model a session last used when a message names none, so after a model was in use the
next turn runs on a fresh engine session, which starts on the partner's default.
``POST /v1/sessions`` is get-or-create, so the reset stays pending until the engine confirms
a ``DELETE``."""

from __future__ import annotations

import json
import re
import sys
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view import DEFAULT_MODEL, Capabilities, Talk2View

from .fake_engine import FakeEngine

Call = Tuple[str, str, Optional[Dict[str, Any]]]

PARTNER_DEFAULT = "partner-default"


class Recorder(FakeEngine):
    """``FakeEngine`` that keeps every request's method, path and JSON body, and the model
    each message ran on under the engine's rule (``resolve_session_model``): a new session
    starts on the partner's default, a create or a message naming a model switches the
    session to it, and one naming none keeps the session's model. With ``fail_deletes``
    every ``DELETE`` answers 503 and the session stays. With ``hold_create`` set to an
    event, ``POST /v1/sessions`` sets ``creating`` and waits for that event."""

    def __init__(self, server: HTTPServer) -> None:
        super().__init__(server)
        self.calls: List[Call] = []
        self.models: Dict[str, str] = {}  # session id -> its model
        self.turns: List[str] = []  # the model each message ran on
        self.fail_deletes = False
        self.hold_create: Optional[threading.Event] = None
        self.creating = threading.Event()

    def _handle(self, request: Request) -> Response:
        body = request.get_json(silent=True)
        with self.lock:
            self.calls.append((request.method, request.path, body))
            if request.method == "DELETE" and self.fail_deletes:
                return Response(status=503)
            hold = self.hold_create if request.path == "/v1/sessions" else None
        if hold is not None:
            self.creating.set()
            hold.wait(10)
        response = super()._handle(request)
        is_message = request.path.endswith("/messages")
        if response.status_code != 200 or not (request.path == "/v1/sessions" or is_message):
            return response
        session_id = (
            request.path.split("/")[3]
            if is_message
            else json.loads(response.get_data())["session_id"]
        )
        requested = body.get("model") if isinstance(body, dict) else None
        with self.lock:
            self.models.setdefault(session_id, PARTNER_DEFAULT)
            if requested:
                self.models[session_id] = requested
            if is_message:
                self.turns.append(self.models[session_id])
        return response

    def take(self) -> List[Call]:
        with self.lock:
            taken, self.calls = self.calls, []
        return taken


@pytest.fixture
def recorder(fake_engine_server: HTTPServer) -> Iterator[Recorder]:
    fake = Recorder(fake_engine_server).start()
    yield fake
    fake.stop()


@pytest.fixture
def t2v(recorder: Recorder) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=recorder.url(), model="m-deep")
    client.auth.login("ana@example.com", "pw")
    yield client
    client.close()


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


def test_default_model_is_a_named_sentinel() -> None:
    assert repr(DEFAULT_MODEL) == "DEFAULT_MODEL"


def test_default_model_forgets_the_model_and_starts_a_fresh_engine_session(
    t2v: Talk2View, recorder: Recorder
) -> None:
    first_turn = turn(t2v, recorder, "one")
    assert [b for m, p, b in first_turn if (m, p) == ("POST", "/v1/sessions")] == [
        {"model": "m-deep"}
    ]
    assert message_body(first_turn)["model"] == "m-deep"
    first = t2v.get_session()
    assert first is not None

    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    assert t2v._model is None
    assert t2v.get_session() is first  # kept: the next chat() deletes it by id
    assert recorder.take() == []  # nothing is sent until then

    calls = turn(t2v, recorder, "two")
    assert session_calls(calls)[:2] == [
        ("DELETE", "/v1/sessions/" + first.id),
        ("POST", "/v1/sessions"),
    ]
    assert [b for m, p, b in calls if (m, p) == ("POST", "/v1/sessions")] == [{}]
    assert "model" not in message_body(calls)
    session = t2v.get_session()
    assert session is not None and session.id != first.id
    assert recorder.turns == ["m-deep", PARTNER_DEFAULT]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"model": "m-fast"}, id="named-pick"),
        pytest.param({"capabilities": Capabilities(reasoning=True)}, id="capabilities"),
    ],
)
def test_a_reset_after_a_change_deletes_the_held_session_first(
    t2v: Talk2View, recorder: Recorder, change: Dict[str, Any]
) -> None:
    turn(t2v, recorder, "one")
    first = t2v.get_session()
    assert first is not None
    t2v.set_chat_defaults(**change)  # the session object stays: not reused, still deletable
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    calls = turn(t2v, recorder, "two")
    # Built on the old settings, but still the session the engine holds: deleted by its id.
    assert session_calls(calls)[:2] == [
        ("DELETE", "/v1/sessions/" + first.id),
        ("POST", "/v1/sessions"),
    ]
    assert [m for m, _ in session_calls(calls)] == ["DELETE", "POST", "POST"]
    assert recorder.turns == ["m-deep", PARTNER_DEFAULT]


def test_a_named_pick_keeps_the_session_object_and_the_next_turn_switches(
    t2v: Talk2View, recorder: Recorder
) -> None:
    turn(t2v, recorder, "one")
    first = t2v.get_session()
    t2v.set_chat_defaults(model="m-fast")
    assert t2v.get_session() is first  # an approval pending on it can still be answered
    assert recorder.take() == []
    calls = turn(t2v, recorder, "two")
    assert [(m, p, b) for m, p, b in calls if p == "/v1/sessions"] == [
        ("POST", "/v1/sessions", {"model": "m-fast"})
    ]
    assert [m for m, _ in session_calls(calls)] == ["POST", "POST"]
    assert t2v.get_session() is not first
    assert recorder.turns == ["m-deep", "m-fast"]


def test_a_reset_whose_delete_fails_stays_pending_until_one_is_confirmed(
    t2v: Talk2View, recorder: Recorder
) -> None:
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    recorder.fail_deletes = True
    calls = turn(t2v, recorder, "two")
    # The delete and the one recycle both fail: the turn runs on the session it has.
    assert [m for m, _ in session_calls(calls)] == ["DELETE", "POST", "DELETE", "POST", "POST"]
    recorder.fail_deletes = False
    calls = turn(t2v, recorder, "three")
    assert [m for m, _ in session_calls(calls)] == ["DELETE", "POST", "POST"]
    assert recorder.turns == ["m-deep", "m-deep", PARTNER_DEFAULT]
    calls = turn(t2v, recorder, "four")
    assert [m for m, _ in session_calls(calls)] == ["POST"]  # consumed once confirmed


def test_the_fresh_session_is_started_once(t2v: Talk2View, recorder: Recorder) -> None:
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    turn(t2v, recorder, "two")
    calls = turn(t2v, recorder, "three")
    assert [m for m, _ in session_calls(calls)] == ["POST"]  # the messages call only
    assert "model" not in message_body(calls)


def test_a_recycle_in_between_is_the_fresh_session(t2v: Talk2View, recorder: Recorder) -> None:
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    t2v.recycle_session()  # e.g. ChatController's idle recycle for a skills reset
    calls = turn(t2v, recorder, "two")
    assert [m for m, _ in session_calls(calls)] == ["POST"]  # no second delete


def test_default_model_with_no_model_set_changes_nothing(recorder: Recorder) -> None:
    t2v = Talk2View("pk_test", base_url=recorder.url())
    try:
        t2v.auth.login("ana@example.com", "pw")
        turn(t2v, recorder, "one")
        session = t2v.get_session()
        t2v.set_chat_defaults(model=DEFAULT_MODEL)
        assert t2v.get_session() is session
        calls = turn(t2v, recorder, "two")
        assert ("DELETE", "/v1/sessions/" + session.id) not in session_calls(calls)  # type: ignore[union-attr]
    finally:
        t2v.close()


def test_a_reset_with_no_session_held_recycles_the_one_the_create_hands_back(
    t2v: Talk2View, recorder: Recorder
) -> None:
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    calls = turn(t2v, recorder, "one")
    # The engine may keep a session this client never held, on another model.
    assert [m for m, _ in session_calls(calls)] == ["POST", "DELETE", "POST", "POST"]
    assert "model" not in message_body(calls)
    assert recorder.turns == [PARTNER_DEFAULT]


def test_a_reset_with_new_capabilities_deletes_the_session_once(
    t2v: Talk2View, recorder: Recorder
) -> None:
    turn(t2v, recorder, "one")
    first = t2v.get_session()
    assert first is not None
    t2v.set_chat_defaults(model=DEFAULT_MODEL, capabilities=Capabilities(reasoning=True))
    calls = turn(t2v, recorder, "two")
    assert [m for m, _ in session_calls(calls)] == ["DELETE", "POST", "POST"]
    assert message_body(calls)["supports_reasoning"] is True


def test_none_still_leaves_the_model_as_it_is(t2v: Talk2View) -> None:
    t2v.set_chat_defaults(model=None)
    assert t2v._model == "m-deep"


def test_a_named_pick_after_a_reset_cancels_it(t2v: Talk2View, recorder: Recorder) -> None:
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    t2v.set_chat_defaults(model="m-fast")
    calls = turn(t2v, recorder, "two")
    # As a pick alone: the create switches the session, which keeps its checkpoint.
    assert [m for m, _ in session_calls(calls)] == ["POST", "POST"]
    assert recorder.turns == ["m-deep", "m-fast"]


def test_an_explicit_create_after_a_reset_cancels_it(t2v: Talk2View, recorder: Recorder) -> None:
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    t2v.create_session(model="m-x")
    calls = turn(t2v, recorder, "two")
    assert [m for m, _ in session_calls(calls)] == ["POST"]  # the message only
    assert recorder.turns == ["m-deep", "m-x"]


@pytest.mark.parametrize("host_model", [None, "m-deep"], ids=["no-host-model", "host-model"])
def test_a_reset_landing_during_an_explicit_create_is_not_lost(
    recorder: Recorder, host_model: Optional[str]
) -> None:
    """The host's ``create_session(model=...)`` is the earlier choice here: a Default that
    lands while its request is in flight must still apply to the next turn."""
    t2v = Talk2View("pk_test", base_url=recorder.url(), model=host_model)
    release = threading.Event()
    creator = threading.Thread(target=lambda: t2v.create_session(model="m-x"))
    try:
        t2v.auth.login("ana@example.com", "pw")
        turn(t2v, recorder, "one")
        recorder.hold_create = release
        creator.start()
        assert recorder.creating.wait(5)
        t2v.set_chat_defaults(model=DEFAULT_MODEL)  # the page's Default, meanwhile
        release.set()
        creator.join(5)
        recorder.hold_create = None
        turn(t2v, recorder, "two")
        assert recorder.turns == [host_model or PARTNER_DEFAULT, PARTNER_DEFAULT]
    finally:
        release.set()
        t2v.close()


def test_an_explicit_create_that_fails_leaves_the_reset_pending(
    t2v: Talk2View, recorder: Recorder
) -> None:
    """Nothing superseded the Default: the engine session never switched to ``m-x``."""
    turn(t2v, recorder, "one")
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    real = t2v._client.request

    def refused(endpoint: str, *args: Any, **kwargs: Any) -> Any:
        if (endpoint, kwargs.get("method")) == ("/v1/sessions", "POST"):
            raise OSError("offline")
        return real(endpoint, *args, **kwargs)

    t2v._client.request = refused  # type: ignore[method-assign]
    with pytest.raises(OSError):
        t2v.create_session(model="m-x")
    t2v._client.request = real  # type: ignore[method-assign]
    turn(t2v, recorder, "two")
    assert recorder.turns == ["m-deep", PARTNER_DEFAULT]


@pytest.mark.parametrize("dropped", [False, True], ids=["session-held", "session-dropped"])
def test_a_reset_after_an_explicit_create_reaches_the_default(
    recorder: Recorder, dropped: bool
) -> None:
    t2v = Talk2View("pk_test", base_url=recorder.url())  # no model of the host's
    try:
        t2v.auth.login("ana@example.com", "pw")
        t2v.create_session(model="m-x")
        turn(t2v, recorder, "one")
        if dropped:
            t2v.set_chat_defaults(capabilities=Capabilities(reasoning=True))
        t2v.set_chat_defaults(model=DEFAULT_MODEL)
        calls = turn(t2v, recorder, "two")
        assert "DELETE" in [m for m, _ in session_calls(calls)]
        assert recorder.turns == ["m-x", PARTNER_DEFAULT]
    finally:
        t2v.close()


def test_a_reset_after_a_confirmed_clear_has_nothing_to_do(recorder: Recorder) -> None:
    t2v = Talk2View("pk_test", base_url=recorder.url())  # no model of the host's
    try:
        t2v.auth.login("ana@example.com", "pw")
        t2v.create_session(model="m-x")
        turn(t2v, recorder, "one")
        t2v.clear_session()  # the engine confirms: it holds no session for us now
        t2v.set_chat_defaults(model=DEFAULT_MODEL)
        calls = turn(t2v, recorder, "two")
        assert [m for m, _ in session_calls(calls)] == ["POST", "POST"]  # no recycle
        assert recorder.turns == ["m-x", PARTNER_DEFAULT]
    finally:
        t2v.close()


class Preempted(Talk2View):
    """Holds the thread that clears ``_model`` just before that write, until ``resume``: a
    thread switch in the middle of ``set_chat_defaults``, made deterministic."""

    paused: Optional[threading.Event] = None
    resume: Optional[threading.Event] = None

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_model" and value is None and self.paused and self.resume:
            self.paused.set()
            self.resume.wait(5)
        super().__setattr__(name, value)


def test_a_reset_preempted_while_a_delete_is_in_flight_is_not_lost(recorder: Recorder) -> None:
    """``_delete_session`` clears the flag with no lock: that is safe only because
    ``set_chat_defaults`` writes ``_model`` before the flag. Here the reset is held at its
    ``_model`` write while a recycle deletes and re-creates: the create reads the old
    model, so the reset must still be pending afterwards."""
    t2v = Preempted("pk_test", base_url=recorder.url(), model="m-deep")
    paused, resume = threading.Event(), threading.Event()
    flag_while_held: List[bool] = []
    try:
        t2v.auth.login("ana@example.com", "pw")
        turn(t2v, recorder, "one")
        t2v.paused, t2v.resume = paused, resume
        resetter = threading.Thread(target=lambda: t2v.set_chat_defaults(model=DEFAULT_MODEL))
        real = t2v._client.request

        def request(endpoint: str, *args: Any, **kwargs: Any) -> Any:
            method = kwargs.get("method", "GET")
            if method == "DELETE":
                resetter.start()
                if paused.wait(5):  # the reset is held at its _model write
                    flag_while_held.append(t2v._fresh_session)
            result = real(endpoint, *args, **kwargs)
            if (endpoint, method) == ("/v1/sessions", "POST"):
                resume.set()  # the create has read _model: let the reset finish
                resetter.join(5)
            return result

        t2v._client.request = request  # type: ignore[method-assign]
        t2v.recycle_session()  # e.g. ChatController's idle recycle for a skills reset
        t2v._client.request = real  # type: ignore[method-assign]
        assert flag_while_held == [False]  # the flag comes after _model, never before it
        turn(t2v, recorder, "two")
        assert recorder.turns == ["m-deep", PARTNER_DEFAULT]
    finally:
        resume.set()
        t2v.close()


class PickedBetweenReads(Talk2View):
    """Lands ``pick`` right after ``_create_session`` reads the first of ``_defaults_version``
    and ``_model``, before it reads the other: a thread switch between the two reads (the
    router's pick while the chat worker creates the session), made deterministic."""

    pick: Optional[str] = None

    def __getattribute__(self, name: str) -> Any:
        value = super().__getattribute__(name)
        pick = super().__getattribute__("pick")
        if pick and name in ("_defaults_version", "_model"):
            if sys._getframe(1).f_code.co_name == "_create_session":  # the reader
                self.pick = None
                self.set_chat_defaults(model=pick)
        return value


def test_a_pick_between_the_creates_two_reads_applies_from_the_next_turn(
    recorder: Recorder,
) -> None:
    """``_create_session`` reads the version count before the model. Read after it, a pick
    landing between the two would leave a session built on the old model looking current,
    and the next turn would reuse it."""
    t2v = PickedBetweenReads("pk_test", base_url=recorder.url(), model="m-deep")
    try:
        t2v.auth.login("ana@example.com", "pw")
        t2v.pick = "m-fast"
        turn(t2v, recorder, "one")
        assert t2v.pick is None  # it landed
        turn(t2v, recorder, "two")
        assert recorder.turns[-1] == "m-fast"
    finally:
        t2v.close()


def test_a_session_created_for_the_previous_end_user_is_not_reused(recorder: Recorder) -> None:
    """The end-user switches accounts while a create (the host's, or the controller's idle
    recycle) is on the wire with the old token: the session it stores is the old account's,
    which the engine would 404 for the new one."""
    t2v = Talk2View("pk_test", base_url=recorder.url())
    release = threading.Event()
    recovered: List[str] = []
    t2v.on_session_recovered(recovered.append)
    creator = threading.Thread(target=t2v.create_session)
    try:
        t2v.auth.login("ana@example.com", "pw")
        recorder.hold_create = release
        creator.start()
        assert recorder.creating.wait(5)
        t2v.auth.login("bob@example.com", "pw")
        release.set()
        creator.join(5)
        recorder.hold_create = None
        calls = turn(t2v, recorder, "hello")
        assert [m for m, _ in session_calls(calls)] == ["POST", "POST"]  # no 404, no resend
        assert recovered == []
    finally:
        release.set()
        t2v.close()


@pytest.mark.parametrize(
    "entry", ["Talk2View(model=...)", "create_session", "completions", "voice.transcribe"]
)
def test_the_sentinel_is_refused_where_it_cannot_work(recorder: Recorder, entry: str) -> None:
    """Only ``set_chat_defaults`` takes ``DEFAULT_MODEL``. Anywhere else it is refused at
    once, before any request (a guest start included)."""
    t2v = Talk2View("pk_test", base_url=recorder.url(), anonymous_auto_start=True)
    attempts: Dict[str, Callable[[], Any]] = {
        "Talk2View(model=...)": lambda: Talk2View(
            "pk_test", base_url=recorder.url(), model=DEFAULT_MODEL  # type: ignore[arg-type]
        ),
        "create_session": lambda: t2v.create_session(model=DEFAULT_MODEL),  # type: ignore[arg-type]
        "completions": lambda: t2v.completions(
            DEFAULT_MODEL, [{"role": "user", "content": "hi"}]  # type: ignore[arg-type]
        ),
        "voice.transcribe": lambda: t2v.voice.transcribe(
            b"RIFF", model=DEFAULT_MODEL, filename="a.wav"  # type: ignore[arg-type]
        ),
    }
    try:
        recorder.take()
        with pytest.raises(TypeError, match=re.escape("set_chat_defaults(model=DEFAULT_MODEL)")):
            attempts[entry]()
        assert recorder.take() == []
    finally:
        t2v.close()


def test_preferences_refuse_the_sentinel_too(t2v: Talk2View) -> None:
    with pytest.raises(TypeError):
        t2v.preferences.update(model=DEFAULT_MODEL)  # type: ignore[arg-type]
    assert t2v.preferences.get().model is None  # nothing stored; unset with None


def test_model_reads_the_chosen_model_and_is_read_only(t2v: Talk2View) -> None:
    assert t2v.model == "m-deep"
    t2v.set_chat_defaults(model="m-fast")
    assert t2v.model == "m-fast"
    t2v.set_chat_defaults(model=DEFAULT_MODEL)
    assert t2v.model is None  # the partner's default
    with pytest.raises(AttributeError):
        t2v.model = "m-x"  # type: ignore[misc]
