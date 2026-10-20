"""Skills across sessions (create, recovery, recycle) and the controller's skills slice."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock

import pytest

from talk2view import Talk2View
from talk2view.cancel import CancelToken
from talk2view.controller import ChatController
from talk2view.errors import T2VError
from talk2view.skills import STORAGE_KEY, Skill, T2VSkills
from talk2view.state import OpBatch
from talk2view.storage import MemoryStorage
from talk2view.types import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ClientToolSchema,
)

from .controller_fakes import BatchLog, FakeT2V, inline_executor, make_controller
from .test_skills_core import ManualTimer

StreamFactory = Callable[[], Iterator[ChatCompletionChunk]]


def text_chunk(content: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t",
        model="m",
        thread_id="th",
        choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content=content))],
    )


def stop_chunk() -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t", model="m", thread_id="th", choices=[ChatCompletionChunkChoice(finish_reason="stop")]
    )


def yielding_stream(*chunks: ChatCompletionChunk) -> StreamFactory:
    return lambda: iter(chunks)


def throwing_stream(error: BaseException) -> StreamFactory:
    def factory() -> Iterator[ChatCompletionChunk]:
        raise error
        yield  # pragma: no cover

    return factory


class Harness:
    """A real Talk2View + T2VSkills over a mocked HTTP client that logs every call.

    By default register calls are answered as an engine older than the ``replace`` flag
    answers them: merged, no ``replaced`` key. ``e1=True`` answers as the shipped engine
    does, with ``replaced`` echoing the request's ``replace``.
    """

    def __init__(self, *streams: StreamFactory, e1: bool = False) -> None:
        self.t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
        self.t2v._client.close()
        self.client = MagicMock()
        self.t2v._client = self.client
        self.t2v.tools._client = self.client
        self.timer = ManualTimer()
        self.t2v.skills = T2VSkills(self.client, self.t2v.data_storage, start_timer=self.timer)
        self.e1 = e1
        self.calls: List[str] = []
        self.register_bodies: List[Dict[str, Any]] = []
        self.fail_next_register: Optional[T2VError] = None
        self.sessions = 0
        self._streams = list(streams)
        self.client.request.side_effect = self._request
        self.client.stream_request.side_effect = self._stream_request

    def _request(self, endpoint: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
        self.calls.append("{} {}".format(method, endpoint))
        if endpoint == "/v1/sessions":
            self.sessions += 1
            n = self.sessions
            return {"session_id": "s{}".format(n), "thread_id": "th{}".format(n), "model": "m"}
        if endpoint == "/v1/skills/register":
            body = kwargs["body"]
            self.register_bodies.append(body)
            if self.fail_next_register is not None:
                error, self.fail_next_register = self.fail_next_register, None
                raise error
            names = [s["name"] for s in body["skills"]]
            answer: Dict[str, Any] = {"registered": names, "count": len(names)}
            if self.e1:
                answer["replaced"] = bool(body.get("replace", False))
            return answer
        return {}

    def _stream_request(
        self, endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatCompletionChunk]:
        self.calls.append("STREAM {}".format(endpoint))
        return self._streams.pop(0)()


def _session_gone() -> T2VError:
    return T2VError("Session not found", "not_found", 404)


RECYCLE = ["DELETE /v1/sessions/s1", "POST /v1/sessions", "POST /v1/skills/register"]


# ── Talk2View lifecycle ────────────────────────────────────────────────────


class TestTalk2ViewLifecycle:
    def test_create_session_re_registers_tools_then_skills(self) -> None:
        h = Harness()
        h.t2v.tools.register([ClientToolSchema(name="t", description="d")])
        h.calls.clear()
        h.t2v.skills.add(Skill(name="measure", content="how"))
        h.t2v.create_session()
        assert h.calls == [
            "POST /v1/sessions",
            "POST /v1/tools/register",
            "POST /v1/skills/register",
        ]
        assert h.t2v.skills.state()["registered"] == ["measure"]
        assert h.timer.pending is None  # the pending debounce was folded into the create

    def test_create_session_with_no_skills_sends_no_register(self) -> None:
        h = Harness()
        h.t2v.create_session()
        assert h.calls == ["POST /v1/sessions"]

    def test_skills_failure_on_create_is_non_fatal(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        real = h.client.request.side_effect

        def failing(endpoint: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
            if endpoint == "/v1/skills/register":
                raise T2VError("rate limited", "rate_limit_error", 429)
            return real(endpoint, method=method, **kwargs)  # type: ignore[no-any-return]

        h.client.request.side_effect = failing
        session = h.t2v.create_session()
        assert session.id == "s1"
        assert h.t2v.get_session() is session

    def test_session_recovery_re_registers_skills(self) -> None:
        h = Harness(
            throwing_stream(_session_gone()), yielding_stream(text_chunk("hi"), stop_chunk())
        )
        h.t2v.skills.add(Skill(name="a", content="1"))
        events = list(h.t2v.chat("hello"))
        assert [e.type for e in events] == ["text", "done"]
        assert h.calls == [
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s1/messages",
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s2/messages",
        ]

    def test_e1_chat_inside_the_debounce_window_sends_the_replace_not_a_recycle(self) -> None:
        h = Harness(yielding_stream(stop_chunk()), yielding_stream(stop_chunk()), e1=True)
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))
        h.calls.clear()
        h.t2v.skills.remove("b")
        assert h.t2v.skills.needs_session_reset() is False
        list(h.t2v.chat("two"))  # the debounce timer has not fired: chat() flushes first
        assert h.calls == ["POST /v1/skills/register", "STREAM /v1/sessions/s1/messages"]
        assert h.register_bodies[-1] == {
            "skills": [{"name": "a", "description": "", "content": "1"}],
            "replace": True,
        }
        assert h.timer.pending is None
        assert h.t2v.skills.state()["registered"] == ["a"]

    def test_chat_recycles_the_session_when_a_reset_is_pending(self) -> None:
        # The default harness is an engine that merges: a removal needs a new session.
        h = Harness(yielding_stream(stop_chunk()), yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))
        h.calls.clear()
        h.t2v.skills.remove("b")
        assert h.t2v.skills.needs_session_reset() is True
        list(h.t2v.chat("two"))
        assert h.calls == [
            "DELETE /v1/sessions/s1",
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s2/messages",
        ]
        assert h.t2v.skills.needs_session_reset() is False
        assert h.t2v.skills.state()["registered"] == ["a"]

    def test_recycle_session_deletes_then_creates(self) -> None:
        h = Harness()
        h.t2v.create_session()
        h.calls.clear()
        session = h.t2v.recycle_session()
        assert session.id == "s2"
        assert h.calls == ["DELETE /v1/sessions/s1", "POST /v1/sessions"]

    def test_skills_are_saved_to_data_storage_not_the_token_store(self) -> None:
        tokens, data = MemoryStorage(), MemoryStorage()
        t2v = Talk2View(
            "pk_test_123", base_url="http://localhost:9999", storage=tokens, data_storage=data
        )
        try:
            t2v.skills.add(Skill(name="a", content="1"))
            t2v.skills.save()
            assert data.get("t2v_user_skills") is not None
            assert tokens.get("t2v_user_skills") is None  # a keyring never holds skill bodies
        finally:
            t2v.close()

    def test_data_storage_defaults_to_the_token_store(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.save()
        assert h.t2v.data_storage is h.t2v.storage
        assert h.t2v.storage.get("t2v_user_skills") is not None


# ── Possibly reused sessions ────────────────────────────────────────────────


class ReusingEngine:
    """``POST /v1/sessions`` is get-or-create, as on the engine: the user's one session comes
    back until it is deleted, still holding the skills an earlier run of the app registered.
    Replace-honouring register calls; ``fail_registers`` makes the next N fail."""

    def __init__(self, held: List[str]) -> None:
        self.held = list(held)
        self.calls: List[str] = []
        self.register_bodies: List[Dict[str, Any]] = []
        self.fail_registers = 0
        self.session = 1

    def request(self, endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
        self.calls.append("{} {}".format(method, endpoint))
        if endpoint == "/v1/sessions":
            return {"session_id": "s{}".format(self.session), "thread_id": "t", "model": "m"}
        if method == "DELETE":
            self.held, self.session = [], self.session + 1
            return None
        if endpoint == "/v1/skills/register":
            self.register_bodies.append(kwargs["body"])
            if self.fail_registers:
                self.fail_registers -= 1
                raise T2VError("rate limited", "rate_limit_exceeded", 429)
            names = [s["name"] for s in kwargs["body"]["skills"]]
            self.held = names
            return {"registered": names, "count": len(names), "removed": [], "replaced": True}
        return {}

    def stream(
        self, endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatCompletionChunk]:
        self.calls.append("STREAM {} holding {}".format(endpoint, self.held))
        return iter([stop_chunk()])


def _on(engine: ReusingEngine) -> Talk2View:
    t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
    t2v._client.close()
    client = MagicMock()
    client.request.side_effect = engine.request
    client.stream_request.side_effect = engine.stream
    t2v._client = client
    t2v.tools._client = client
    t2v.skills = T2VSkills(client, t2v.data_storage, start_timer=ManualTimer())
    return t2v


class TestPossiblyReusedSession:
    def test_a_failed_register_after_a_restart_recycles_before_the_turn(self) -> None:
        """probe_reregister_failopen: after a restart the engine's session still holds "b"
        (removed while the app was closed), and the first register fails once. The turn
        must not run while "b" may be live."""
        engine = ReusingEngine(held=["a", "b"])
        engine.fail_registers = 1
        t2v = _on(engine)
        t2v.skills.add(Skill(name="a", content="A"))  # the local set after the restart
        list(t2v.chat("hi"))
        assert engine.calls == [
            "POST /v1/sessions",
            "POST /v1/skills/register",  # fails: what s1 holds is unknown
            "DELETE /v1/sessions/s1",
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s2/messages holding ['a']",
        ]
        assert t2v.skills.needs_session_reset() is False

    @pytest.mark.parametrize("saved", ["[]", None], ids=["saved-empty", "nothing-saved"])
    def test_load_sends_the_saved_set_even_when_it_is_empty(self, saved: Optional[str]) -> None:
        """rr_probe_empty_restart: the user removed every skill last run, but the engine's
        kept session still holds "b". The saved set -- nothing -- must reach it as an empty
        replace before the first turn, with no failure needed to get there."""
        engine = ReusingEngine(held=["b"])
        t2v = _on(engine)
        if saved is not None:
            t2v.data_storage.set(STORAGE_KEY, saved)
        assert t2v.skills.load() == []
        list(t2v.chat("hi"))
        assert engine.calls == [
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s1/messages holding []",
        ]
        assert engine.register_bodies == [{"skills": [], "replace": True}]

    def test_a_failed_register_on_a_recycled_session_leaves_the_edit_pending(self) -> None:
        # The default harness is an engine that merges: s1 holds a and b after the create.
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        h.t2v.create_session()
        h.t2v.skills.remove("b")
        assert h.t2v.skills.needs_session_reset() is True
        h.fail_next_register = T2VError("rate limited", "rate_limit_exceeded", 429)
        h.calls.clear()
        h.t2v.recycle_session()  # DELETE confirmed: s2 is known to be new
        assert h.calls == RECYCLE
        assert h.t2v.skills.needs_session_reset() is False  # s2 holds no user skill at all
        assert h.t2v.skills.state()["registered"] == []
        h.calls.clear()
        h.t2v.skills.flush()  # the edit is still pending
        assert h.calls == ["POST /v1/skills/register"]
        assert h.t2v.skills.state()["registered"] == ["a"]

    def test_a_recycle_whose_delete_failed_is_not_known_fresh(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.create_session()
        real = h.client.request.side_effect

        def failing(endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
            answer = real(endpoint, method=method, **kwargs)
            if method == "DELETE" or endpoint == "/v1/skills/register":
                raise T2VError("rate limited", "rate_limit_exceeded", 429)
            return answer

        h.client.request.side_effect = failing
        h.t2v.recycle_session()  # the engine may have kept s1 and handed it back
        assert h.t2v.skills.needs_session_reset() is True

    def test_at_most_one_extra_recycle_per_turn(self) -> None:
        h = Harness(yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        real = h.client.request.side_effect

        def failing(endpoint: str, method: str = "GET", **kwargs: Any) -> Any:
            answer = real(endpoint, method=method, **kwargs)
            if method == "DELETE" or endpoint == "/v1/skills/register":
                raise T2VError("rate limited", "rate_limit_exceeded", 429)
            return answer

        h.client.request.side_effect = failing
        list(h.t2v.chat("hi"))
        assert h.calls == [
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "DELETE /v1/sessions/s1",
            "POST /v1/sessions",
            "POST /v1/skills/register",
            "STREAM /v1/sessions/s2/messages",  # the turn runs; no second extra recycle
        ]
        assert h.t2v.skills.needs_session_reset() is True  # still unknown: next turn retries


# ── Close ───────────────────────────────────────────────────────────────────


class TestClose:
    def test_close_stops_a_pending_edit_before_the_client_closes(self) -> None:
        h = Harness()
        h.t2v.create_session()
        h.t2v.skills.add(Skill(name="a", content="1"))
        assert h.timer.pending is not None
        _delay, tick = h.timer.pending
        # A timer thread that had already fired when close() cancelled it, run at the worst
        # moment -- while the HTTP client is being closed -- and once more afterwards.
        h.client.close.side_effect = tick
        h.calls.clear()
        h.t2v.close()
        assert h.timer.pending is None  # cancelled
        tick()
        assert h.calls == []
        h.client.close.assert_called_once()
        assert [s["name"] for s in h.t2v.skills.state()["local"]] == ["a"]  # reads still work

    def test_flush_after_close_does_not_post(self) -> None:
        h = Harness()
        h.t2v.create_session()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.calls.clear()
        h.t2v.close()
        h.t2v.skills.flush()
        assert h.calls == []

    def test_re_register_after_close_is_a_no_op(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.close()
        assert h.t2v.skills.re_register() is None
        assert h.calls == []
        assert [s.name for s in h.t2v.skills.get_all()] == ["a"]

    def test_a_register_call_cut_off_by_close_logs_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        h = Harness()
        h.t2v.create_session()
        real = h.client.request.side_effect

        def closed_mid_call(endpoint: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
            if endpoint == "/v1/skills/register":
                h.t2v.close()  # shutdown lands while this POST is in flight
                raise RuntimeError("Cannot send a request, as the client has been closed.")
            return real(endpoint, method=method, **kwargs)  # type: ignore[no-any-return]

        h.client.request.side_effect = closed_mid_call
        h.t2v.skills.add(Skill(name="a", content="1"))
        with caplog.at_level(logging.DEBUG, logger="talk2view.skills"):
            h.timer.fire()
        records = [r for r in caplog.records if r.name == "talk2view.skills"]
        assert [r.levelno for r in records] == [logging.DEBUG]
        assert records[0].exc_info is not None

    def test_a_failed_register_call_while_open_still_logs_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        h = Harness()
        h.t2v.create_session()
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.fail_next_register = T2VError("down", "internal_error", 500)
        with caplog.at_level(logging.DEBUG, logger="talk2view.skills"):
            h.timer.fire()
        records = [r for r in caplog.records if r.name == "talk2view.skills"]
        assert [(r.levelno, r.getMessage()) for r in records] == [
            (logging.ERROR, "Skills registration failed")
        ]

    def test_no_timer_is_started_after_close(self) -> None:
        h = Harness()
        h.t2v.skills.close()
        started = h.timer.started
        h.t2v.skills.add(Skill(name="a", content="1"))
        assert h.timer.started == started
        assert h.timer.pending is None
        assert [s["name"] for s in h.t2v.skills.state()["local"]] == ["a"]


# ── ChatController skills slice ─────────────────────────────────────────────


class TestControllerSlice:
    def test_set_skills_state_records_one_set_op_on_the_seq_line(self) -> None:
        controller = make_controller()
        log = BatchLog()
        controller.subscribe(log)
        start = controller.snapshot()
        state = {
            "local": [
                {"name": "a", "description": "", "enabled": True, "origin": "code", "label": "code"}
            ],
            "platform": None,
            "registered": ["a"],
            "available": True,
            "needsSessionReset": False,
        }
        controller.set_skills_state(state)  # type: ignore[arg-type]
        assert len(log.batches) == 1
        batch: OpBatch = log.batches[0]
        assert batch["seq"] == start["seq"] + 1
        assert batch["ops"] == [{"type": "set", "path": ["skills"], "value": state}]
        assert log.replay(start) == controller.snapshot()
        assert controller.snapshot()["skills"] == state
        # The same state again records nothing.
        controller.set_skills_state(state)  # type: ignore[arg-type]
        assert len(log.batches) == 1

    def test_initial_snapshot_carries_the_skills_state_without_an_op(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        controller = ChatController(h.t2v, executor=inline_executor)
        snapshot = controller.snapshot()
        assert snapshot["seq"] == 0
        assert [s["name"] for s in snapshot["skills"]["local"]] == ["a"]
        controller.close()

    def test_a_change_between_seeding_and_subscribing_is_not_lost(self) -> None:
        # The controller seeds the slice first and subscribes last; a change delivered in
        # between (from the debounce timer's thread, say) reaches no listener of its own.
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        subscribe = h.t2v.skills.on_change

        def on_change_after_a_change(callback: Any) -> Callable[[], None]:
            h.t2v.skills.add(Skill(name="b", content="2"))
            return subscribe(callback)

        h.t2v.skills.on_change = on_change_after_a_change
        controller = ChatController(h.t2v, executor=inline_executor)
        live = h.t2v.skills.state()
        assert [s["name"] for s in live["local"]] == ["a", "b"]
        assert controller.snapshot()["skills"] == live
        controller.close()

    def test_skills_changes_flow_through_on_change_into_batches(self) -> None:
        h = Harness()
        controller = ChatController(h.t2v, executor=inline_executor)
        log = BatchLog()
        controller.subscribe(log)
        h.t2v.skills.add(Skill(name="a", content="1"))
        assert log.values("skills")[-1]["local"][0]["name"] == "a"
        h.timer.fire()
        assert log.values("skills")[-1]["registered"] == ["a"]
        controller.close()
        h.t2v.skills.add(Skill(name="b", content="2"))
        assert len(log.values("skills")) == 2  # unsubscribed on close

    def test_e1_removal_is_one_replace_and_no_session_delete(self) -> None:
        h = Harness(
            yielding_stream(text_chunk("hi"), stop_chunk()), yielding_stream(stop_chunk()), e1=True
        )
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        controller = ChatController(h.t2v, executor=inline_executor)
        log = BatchLog()
        controller.subscribe(log)
        controller.send("hello")
        assert controller.wait_idle(1.0)
        h.calls.clear()
        h.t2v.skills.remove("b")
        assert h.calls == []  # idle, and still nothing is recycled
        assert controller.snapshot()["skills"]["needsSessionReset"] is False
        assert log.values("isLoading") == [True, False]  # the turn only: no recycle run
        h.timer.fire()  # the debounced replace
        controller.send("two")
        assert controller.wait_idle(1.0)
        assert h.calls == ["POST /v1/skills/register", "STREAM /v1/sessions/s1/messages"]
        assert controller.snapshot()["skills"]["registered"] == ["a"]
        controller.close()

    def test_a_failed_replace_falls_back_to_a_recycle(self) -> None:
        h = Harness(yielding_stream(text_chunk("hi"), stop_chunk()), e1=True)
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        controller = ChatController(h.t2v, executor=inline_executor)
        controller.send("hello")
        assert controller.wait_idle(1.0)
        h.calls.clear()
        h.t2v.skills.remove("b")
        h.fail_next_register = T2VError("rate limited", "rate_limit_exceeded", 429)
        h.timer.fire()
        # Fail closed: the engine may still hold "b", so the idle controller recycles.
        assert h.calls == ["POST /v1/skills/register", *RECYCLE]
        snapshot = controller.snapshot()
        assert snapshot["skills"]["needsSessionReset"] is False
        assert snapshot["skills"]["registered"] == ["a"]
        controller.close()

    def test_controller_recycles_the_session_when_idle(self) -> None:
        # The default harness is an engine that merges: a removal needs a new session.
        h = Harness(yielding_stream(text_chunk("hi"), stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        controller = ChatController(h.t2v, executor=inline_executor)
        log = BatchLog()
        controller.subscribe(log)
        controller.send("hello")
        assert controller.wait_idle(1.0)
        h.calls.clear()
        h.t2v.skills.remove("b")
        assert h.calls == RECYCLE
        snapshot = controller.snapshot()
        assert snapshot["skills"]["needsSessionReset"] is False
        assert snapshot["skills"]["registered"] == ["a"]
        assert [m["content"] for m in snapshot["messages"]] == ["hello", "hi"]
        assert snapshot["isLoading"] is False
        assert log.values("isLoading") == [True, False, True, False]
        controller.close()

    def test_recycle_is_deferred_while_a_run_is_active(self) -> None:
        h = Harness(yielding_stream(text_chunk("hi"), stop_chunk()), yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        gate = threading.Event()
        removed = threading.Event()
        original = h.client.stream_request.side_effect

        def slow_stream(endpoint: str, body: Dict[str, Any], cancel: Any = None) -> Any:
            stream = original(endpoint, body, cancel)
            if endpoint.endswith("s1/messages"):
                removed.set()
                gate.wait(2.0)
            return stream

        h.client.stream_request.side_effect = slow_stream
        controller = ChatController(h.t2v)  # thread executor
        controller.send("hello")
        assert removed.wait(2.0)
        h.calls.clear()
        h.t2v.skills.remove("b")  # mid-turn: must NOT delete the session now
        assert "DELETE /v1/sessions/s1" not in h.calls
        assert controller.snapshot()["skills"]["needsSessionReset"] is True
        gate.set()
        assert controller.wait_idle(2.0)
        assert "DELETE /v1/sessions/s1" not in h.calls
        h.calls.clear()
        controller.send("two")  # the next turn recycles before sending
        assert controller.wait_idle(2.0)
        assert h.calls[:3] == RECYCLE
        assert controller.snapshot()["skills"]["needsSessionReset"] is False
        controller.close()

    def test_recycle_failure_leaves_the_controller_usable(self) -> None:
        h = Harness(yielding_stream(stop_chunk()), yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        controller = ChatController(h.t2v, executor=inline_executor)
        controller.send("hello")
        real = h.client.request.side_effect

        def failing(endpoint: str, method: str = "GET", **kwargs: Any) -> Dict[str, Any]:
            if endpoint == "/v1/sessions" and method == "POST":
                raise T2VError("down", "internal_error", 500)
            return real(endpoint, method=method, **kwargs)  # type: ignore[no-any-return]

        h.client.request.side_effect = failing
        h.t2v.skills.remove("a")
        assert controller.wait_idle(1.0)
        assert controller.snapshot()["isLoading"] is False
        assert controller.snapshot()["skills"]["needsSessionReset"] is True
        h.client.request.side_effect = real
        controller.send("again")  # chat() recycles before the turn
        assert controller.snapshot()["skills"]["needsSessionReset"] is False
        controller.close()

    def test_a_refused_recycle_launch_is_logged_and_leaves_the_controller_usable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        h = Harness(yielding_stream(stop_chunk()), yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))  # s1 holds a and b; the engine merges
        refusing = [True]

        def executor(fn: Callable[[], None]) -> None:
            if refusing[0]:
                raise RuntimeError("pool is shut down")
            fn()

        controller = ChatController(h.t2v, executor=executor)
        h.calls.clear()
        with caplog.at_level(logging.ERROR, logger="talk2view.skills"):
            h.t2v.skills.remove("b")  # asks for a reset; the recycle job is refused
        raised = [r for r in caplog.records if r.getMessage() == "skills on_change listener raised"]
        assert len(raised) == 1
        assert raised[0].exc_info is not None
        assert str(raised[0].exc_info[1]) == "pool is shut down"
        assert h.calls == []
        assert controller.wait_idle(1.0)
        snapshot = controller.snapshot()
        assert snapshot["isLoading"] is False
        assert snapshot["error"] == "pool is shut down"
        assert snapshot["skills"]["needsSessionReset"] is True  # still pending
        refusing[0] = False
        controller.send("two")  # chat() recycles before the turn
        assert h.calls[:3] == RECYCLE
        assert controller.snapshot()["skills"]["needsSessionReset"] is False
        controller.close()

    def test_a_recycle_abandoned_by_clear_before_it_runs_is_skipped(self) -> None:
        h = Harness(yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))  # s1 holds a and b; the engine merges
        queued: List[Callable[[], None]] = []
        controller = ChatController(h.t2v, executor=queued.append)  # runs nothing by itself
        h.calls.clear()
        h.t2v.skills.remove("b")  # asks for a reset: the recycle job is queued, not run
        assert len(queued) == 1
        assert controller.snapshot()["isLoading"] is True
        controller.clear()  # abandons the recycle and queues its own DELETE
        assert len(queued) == 2
        for job in queued:
            job()
        assert h.calls == ["DELETE /v1/sessions/s1"]  # clear()'s only: the recycle never ran
        assert controller.wait_idle(1.0)
        assert controller.snapshot()["isLoading"] is False
        controller.close()

    def test_a_delivery_that_lands_after_close_records_nothing(self) -> None:
        # on_change copies its listener list before calling it, so a delivery already under
        # way on another thread can still reach a controller that has just closed.
        h = Harness(yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))  # s1 holds a and b; the engine merges
        controller = ChatController(h.t2v, executor=inline_executor)
        before = controller.snapshot()
        controller.close()
        h.t2v.skills.remove("b")  # unsubscribed: the controller hears nothing
        assert h.t2v.skills.needs_session_reset() is True
        h.calls.clear()
        controller.set_skills_state(h.t2v.skills.state())  # the late delivery
        assert h.calls == []  # no recycle
        assert controller.snapshot() == before

    def test_fake_t2v_without_skills_still_works(self) -> None:
        controller = make_controller(FakeT2V())
        assert controller.snapshot()["skills"]["local"] == []
        controller.close()

    # T2VSkills calls on_change outside its locks, so the deliveries of two threads can reach
    # the controller out of order. The controller records the live state, not the one handed
    # in, and decides a recycle on it too.

    def test_an_older_state_delivered_after_a_newer_one_leaves_the_live_state(self) -> None:
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        controller = ChatController(h.t2v, executor=inline_executor)
        log = BatchLog()
        controller.subscribe(log)
        older = h.t2v.skills.state()
        h.t2v.skills.add(Skill(name="b", content="2"))
        controller.set_skills_state(older)  # the late delivery
        live = h.t2v.skills.state()
        assert [s["name"] for s in live["local"]] == ["a", "b"]
        assert controller.snapshot()["skills"] == live
        assert log.values("skills")[-1] == live
        controller.close()

    def test_an_out_of_order_delivery_on_one_thread_leaves_the_live_state(self) -> None:
        # A listener subscribed before the controller changes the skills from inside the first
        # delivery: the controller is handed that newer state first, and the older one after.
        h = Harness()
        h.t2v.skills.add(Skill(name="a", content="1"))
        nested: List[bool] = []

        def earlier_listener(state: Any) -> None:
            if not nested:
                nested.append(True)
                h.t2v.skills.add(Skill(name="b", content="2"))

        h.t2v.skills.on_change(earlier_listener)
        controller = ChatController(h.t2v, executor=inline_executor)
        h.t2v.skills.set_enabled("a", False)
        live = h.t2v.skills.state()
        assert [s["name"] for s in live["local"]] == ["a", "b"]
        assert controller.snapshot()["skills"] == live
        controller.close()

    def test_a_stale_reset_request_does_not_recycle_a_second_time(self) -> None:
        h = Harness(yielding_stream(stop_chunk()))
        h.t2v.skills.add(Skill(name="a", content="1"))
        h.t2v.skills.add(Skill(name="b", content="2"))
        list(h.t2v.chat("one"))  # s1 holds a and b; the engine merges
        recycled: List[str] = []

        def earlier_listener(state: Any) -> None:
            # Another path recycles first (chat() on a worker, say) and its
            # needsSessionReset=False state reaches the controller before this older one.
            if state["needsSessionReset"] and not recycled:
                recycled.append(h.t2v.recycle_session().id)

        h.t2v.skills.on_change(earlier_listener)
        controller = ChatController(h.t2v, executor=inline_executor)
        h.calls.clear()
        h.t2v.skills.remove("b")
        assert recycled == ["s2"]
        assert h.calls == RECYCLE  # the listener's recycle only; the stale True starts none
        snapshot = controller.snapshot()
        assert snapshot["skills"] == h.t2v.skills.state()
        assert snapshot["skills"]["needsSessionReset"] is False
        assert snapshot["isLoading"] is False
        controller.close()
