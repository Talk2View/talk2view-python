"""Session recovery + stop() — ported from the TypeScript SDK's
tests/core/session-recovery.test.ts (same cases, same names).

Not ported here: the "Talk2View.sendMessage — recovers transparently through
the public state-management API" case and the ``pendingApproval`` assertion.
Both exercise chat STATE, which the Python SDK keeps in ChatController.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

import talk2view
from talk2view import Talk2View
from talk2view.cancel import Cancelled, CancelToken
from talk2view.dispatch import QueueDispatcher
from talk2view.errors import SessionError, T2VError
from talk2view.tools import T2VTools
from talk2view.types import (
    Capabilities,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatEvent,
    ChatMessage,
    Decision,
    PermissionCheck,
    ToolCallInterrupt,
)

StreamFactory = Callable[[], Iterator[ChatCompletionChunk]]


def _session_gone() -> T2VError:
    return T2VError("Session not found", "not_found", 404)


def _text_chunk(content: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t",
        model="m",
        choices=[ChatCompletionChunkChoice(delta=ChatCompletionChunkDelta(content=content))],
        thread_id="th",
    )


def _stop_chunk() -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t",
        model="m",
        choices=[ChatCompletionChunkChoice(finish_reason="stop")],
        thread_id="th",
    )


def _interrupt_chunk(tool_name: str, tool_call_id: str) -> ChatCompletionChunk:
    """A chunk carrying a client-tool interrupt — the engine pausing for a tool call."""
    return ChatCompletionChunk(
        id="t",
        model="m",
        choices=[ChatCompletionChunkChoice()],
        thread_id="th",
        interrupt=ToolCallInterrupt(
            type="tool_call", tool_name=tool_name, tool_call_id=tool_call_id, arguments={}
        ),
    )


def _throwing_stream(error: BaseException) -> StreamFactory:
    """A stream that raises before yielding anything — the 404 a dead session gives."""

    def factory() -> Iterator[ChatCompletionChunk]:
        raise error
        yield  # pragma: no cover — makes this a generator, like the real client

    return factory


def _yielding_stream(*chunks: ChatCompletionChunk) -> StreamFactory:
    def factory() -> Iterator[ChatCompletionChunk]:
        yield from chunks

    return factory


def _partial_then_gone() -> Iterator[ChatCompletionChunk]:
    yield _text_chunk("partial")
    raise _session_gone()


class _Harness:
    """A Talk2View whose HTTP client and tools are mocks (the TS test's vi.mock)."""

    def __init__(self, *streams: StreamFactory) -> None:
        self.t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
        self.t2v._client.close()
        self.client = MagicMock()
        self.tools = MagicMock(spec=T2VTools)
        self.t2v._client = self.client
        self.t2v.tools = self.tools

        self.client.request.side_effect = self._request
        self._streams = list(streams)
        self.client.stream_request.side_effect = self._stream_request

        self.tools.re_register.return_value = None
        self.tools.check_permission.return_value = PermissionCheck(action="allow")
        self.tools.has_handler.return_value = False
        self.tools.execute_tool_call.return_value = ("ok", False)
        self.tools.get_description.return_value = ""

    def _request(self, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        if endpoint == "/v1/sessions":
            n = self.sessions_created()
            return {"session_id": "s{}".format(n), "thread_id": "th{}".format(n), "model": "m"}
        return {}

    def _stream_request(
        self, endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatCompletionChunk]:
        return self._streams.pop(0)()

    def sessions_created(self) -> int:
        return len([c for c in self.client.request.call_args_list if c[0][0] == "/v1/sessions"])


APPROVAL = ChatEvent(
    type="approval_required",
    kind="client",
    tool_name="insert_text",
    tool_call_id="tc1",
    arguments={"text": "x"},
)


class TestChatSessionLost:
    """Talk2View.chat — a chat session lost to a deploy or an eviction."""

    def test_creates_a_new_session_and_resends_the_turn_keeping_the_conversation(self) -> None:
        h = _Harness(
            _throwing_stream(_session_gone()),
            _yielding_stream(_text_chunk("hello"), _stop_chunk()),
        )
        recovered: List[str] = []
        h.t2v.on_session_recovered(recovered.append)

        events = list(h.t2v.chat("hi", history=[ChatMessage(role="user", content="earlier")]))

        assert [e.type for e in events] == ["text", "done"]
        assert h.sessions_created() == 2
        assert recovered == ["s2"]
        endpoint, body = h.client.stream_request.call_args_list[1][0]
        assert endpoint == "/v1/sessions/s2/messages"
        assert len(body["messages"]) == 2

    def test_recovery_reregisters_tools(self) -> None:
        """TS: 're-registers the partner tools on the new session'."""
        h = _Harness(_throwing_stream(_session_gone()), _yielding_stream(_stop_chunk()))

        list(h.t2v.chat("hi"))

        assert h.tools.re_register.call_count == 2

    def test_does_not_resend_once_the_reply_has_started_reports_the_loss(self) -> None:
        h = _Harness(_partial_then_gone)

        events = list(h.t2v.chat("hi"))

        assert [e.type for e in events] == ["text", "error"]
        assert events[1].error_type == "session_lost"
        assert events[1].message == talk2view.SESSION_LOST_MESSAGE
        assert "session not found" not in (events[1].message or "").lower()
        assert h.sessions_created() == 1
        assert h.t2v.get_session() is None

    def test_reports_the_loss_when_a_tool_resume_404s(self) -> None:
        h = _Harness(
            _yielding_stream(_interrupt_chunk("insert_text", "tc1")),
            _throwing_stream(_session_gone()),
        )
        h.tools.has_handler.return_value = True

        events = list(h.t2v.chat("hi"))

        assert [e.type for e in events] == ["tool_call", "error"]
        assert events[1].error_type == "session_lost"
        assert h.sessions_created() == 1
        assert h.t2v.get_session() is None

    def test_gives_up_after_one_retry_rather_than_looping(self) -> None:
        h = _Harness(_throwing_stream(_session_gone()), _throwing_stream(_session_gone()))

        with pytest.raises(T2VError, match="Session not found"):
            list(h.t2v.chat("hi"))

        assert h.sessions_created() == 2

    def test_recovers_when_the_engine_raises_its_dedicated_session_not_found_type_too(
        self,
    ) -> None:
        h = _Harness(
            _throwing_stream(T2VError("Session not found", "session_not_found", 404)),
            _yielding_stream(_stop_chunk()),
        )

        events = list(h.t2v.chat("hi"))

        assert [e.type for e in events] == ["done"]
        assert h.sessions_created() == 2

    def test_does_not_recover_once_the_caller_has_cancelled(self) -> None:
        token = CancelToken()

        def cancelled_while_the_404_was_in_flight() -> Iterator[ChatCompletionChunk]:
            token.cancel()  # a stop() landing between the 404 and the recovery
            raise _session_gone()
            yield  # pragma: no cover — makes this a generator, like the client

        h = _Harness(cancelled_while_the_404_was_in_flight)
        recovered: List[str] = []
        h.t2v.on_session_recovered(recovered.append)

        with pytest.raises(T2VError, match="Session not found"):
            list(h.t2v.chat("hi", cancel=token))

        assert h.sessions_created() == 1  # the turn's own session, no recovery
        assert recovered == []

    def test_an_already_cancelled_token_does_not_even_create_a_session(self) -> None:
        """A stop() before the first next() must not POST /v1/sessions."""
        h = _Harness()
        token = CancelToken()
        token.cancel()

        events = list(h.t2v.chat("hi", cancel=token))

        assert events == []
        assert h.sessions_created() == 0
        assert h.client.stream_request.call_count == 0

    def test_a_raising_recovered_listener_does_not_kill_the_turn(self) -> None:
        h = _Harness(
            _throwing_stream(_session_gone()),
            _yielding_stream(_text_chunk("hello"), _stop_chunk()),
        )
        seen: List[str] = []

        def broken(session_id: str) -> None:
            raise RuntimeError("partner listener blew up")

        h.t2v.on_session_recovered(broken)
        h.t2v.on_session_recovered(seen.append)

        events = list(h.t2v.chat("hi"))

        assert [e.type for e in events] == ["text", "done"]
        assert seen == ["s2"]  # the listener after the broken one still ran

    def test_does_not_resend_the_turn_if_clear_session_fires_while_recovery_reregisters_tools(
        self,
    ) -> None:
        h = _Harness(_throwing_stream(_session_gone()), _yielding_stream(_stop_chunk()))

        def re_register() -> None:
            # The 2nd call is recovery's create_session(): a concurrent
            # clear_session() lands while the tools are being re-registered.
            if h.tools.re_register.call_count == 2:
                h.t2v.clear_session()

        h.tools.re_register.side_effect = re_register

        events = list(h.t2v.chat("hi"))

        assert events == []
        assert h.tools.re_register.call_count == 2
        assert h.client.stream_request.call_count == 1

    def test_passes_other_failures_through_untouched(self) -> None:
        h = _Harness(_throwing_stream(T2VError("Boom", "internal_error", 500)))

        with pytest.raises(T2VError, match="Boom"):
            list(h.t2v.chat("hi"))

        assert h.sessions_created() == 1

    def test_a_failed_tool_reregistration_does_not_break_the_session(self) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()))
        h.tools.re_register.side_effect = T2VError("rate limited", "rate_limit_error", 429)

        events = list(h.t2v.chat("hi"))

        assert [e.type for e in events] == ["done"]


class TestRespondToApprovalSessionLost:
    """Talk2View.respond_to_approval — the chat session died before the resume."""

    def test_reports_the_loss_instead_of_throwing(self) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))

        events = list(h.t2v.respond_to_approval(APPROVAL, Decision("deny")))

        assert [e.type for e in events] == ["approval_result", "error"]
        assert events[1].error_type == "session_lost"
        assert events[1].message == talk2view.SESSION_LOST_AFTER_DENY_MESSAGE
        assert "send your message again" in (events[1].message or "").lower()
        assert h.t2v.get_session() is None

    def test_does_not_resend_the_tool_result_on_a_new_session(self) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))
        sessions_before = h.sessions_created()

        list(h.t2v.respond_to_approval(APPROVAL, Decision("deny")))

        assert h.sessions_created() == sessions_before
        assert h.client.stream_request.call_count == 2

    def test_warns_the_handler_may_already_have_run_when_the_tool_was_actually_executed(
        self,
    ) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))

        events = list(h.t2v.respond_to_approval(APPROVAL, Decision("once")))

        assert [e.type for e in events] == ["approval_result", "error"]
        assert events[1].error_type == "session_lost"
        assert "may already have been applied" in (events[1].message or "")
        assert "reconnect" not in (events[1].message or "").lower()

    def test_runs_the_tool_handler_exactly_once_even_though_the_resume_never_reaches_the_engine(
        self,
    ) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))

        events = list(h.t2v.respond_to_approval(APPROVAL, Decision("once")))

        assert h.tools.execute_tool_call.call_count == 1
        assert events[1].error_type == "session_lost"

    def test_without_a_session_raises_session_error(self) -> None:
        h = _Harness()

        with pytest.raises(SessionError, match="No active session"):
            list(h.t2v.respond_to_approval(APPROVAL, Decision("once")))

    def test_a_malformed_decision_gets_the_denial_wording(self) -> None:
        """One rule decides "approved" everywhere (Decision.approved): the
        resume denied the call, so the wording must not tell the user that
        something may already have been applied."""
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))

        events = list(
            h.t2v.respond_to_approval(APPROVAL, Decision(action="Once"))  # type: ignore[arg-type]
        )

        assert events[1].error_type == "session_lost"
        assert events[1].message == talk2view.SESSION_LOST_AFTER_DENY_MESSAGE

    def test_an_approved_engine_tool_never_ran_so_nothing_was_applied(self) -> None:
        """A platform tool runs on the ENGINE. A 404 on the resume means the
        approval never arrived, so nothing was applied — even though the
        end-user approved it."""
        h = _Harness(_yielding_stream(_stop_chunk()), _throwing_stream(_session_gone()))
        list(h.t2v.chat("hi"))
        engine_approval = ChatEvent(
            type="approval_required",
            kind="engine",
            tool_name="send_email",
            tool_call_id="tc9",
            arguments={"to": "a@b.com"},
        )

        events = list(h.t2v.respond_to_approval(engine_approval, Decision("once")))

        assert [e.type for e in events] == ["approval_result", "error"]
        assert events[1].message == talk2view.SESSION_LOST_AFTER_DENY_MESSAGE
        assert "may already have been applied" not in (events[1].message or "")
        h.tools.execute_tool_call.assert_not_called()

    def test_other_failures_pass_through(self) -> None:
        h = _Harness(
            _yielding_stream(_stop_chunk()),
            _throwing_stream(T2VError("Moved on", "tool_call_id_mismatch", 409)),
        )
        list(h.t2v.chat("hi"))

        with pytest.raises(T2VError, match="Moved on"):
            list(h.t2v.respond_to_approval(APPROVAL, Decision("once")))


class TestStop:
    def test_stop_is_a_noop_when_nothing_is_streaming(self) -> None:
        h = _Harness()
        h.t2v.stop()  # must not raise

    def test_stop_ends_an_inflight_chat_quietly_and_keeps_what_arrived(self) -> None:
        h = _Harness()
        first_event = threading.Event()

        def stalled(
            endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
        ) -> Iterator[ChatCompletionChunk]:
            assert cancel is not None
            yield _text_chunk("partial")
            woke = threading.Event()
            cancel.on_cancel(woke.set)
            assert woke.wait(5), "stop() never reached the stream's cancel token"
            raise Cancelled()

        h.client.stream_request.side_effect = stalled
        events: List[ChatEvent] = []
        failures: List[BaseException] = []

        def consume() -> None:
            try:
                for event in h.t2v.chat("hi"):
                    events.append(event)
                    first_event.set()
            except BaseException as exc:  # pragma: no cover — reported below
                failures.append(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        assert first_event.wait(5)

        h.t2v.stop()
        worker.join(2.0)

        assert not worker.is_alive()
        assert failures == []  # Cancelled never escapes chat()
        assert [e.type for e in events] == ["text"]
        assert h.t2v._active_cancel is None
        assert h.t2v.get_session() is not None  # a stop does not drop the session

    def test_stop_unblocks_a_real_stalled_stream_within_1s(self) -> None:
        """End to end over a real socket: Talk2View.stop() → CancelToken → httpx."""
        release = threading.Event()

        def stall(request: Request) -> Response:
            def body() -> Iterator[bytes]:
                yield (
                    b'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"partial"},'
                    b'"finish_reason":null}]}\n\n'
                )
                release.wait(15)

            return Response(body(), mimetype="text/event-stream")

        server = HTTPServer(threaded=True)
        server.start()
        server.expect_request("/v1/sessions", method="POST").respond_with_json(
            {"session_id": "s1", "thread_id": "th1", "model": "m"}
        )
        server.expect_request("/v1/sessions/s1/messages").respond_with_handler(stall)
        t2v = Talk2View("pk_test", base_url=server.url_for(""))
        t2v._client.storage.set("access_token", "tok")
        events: List[ChatEvent] = []
        first_event = threading.Event()

        def consume() -> None:
            for event in t2v.chat("hi"):
                events.append(event)
                first_event.set()

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert first_event.wait(5)
            t2v.stop()
            worker.join(1.0)

            assert not worker.is_alive(), "chat() still blocked 1s after stop()"
            assert [e.type for e in events] == ["text"]
        finally:
            release.set()
            t2v.close()
            server.clear()
            server.stop()

    def test_stop_after_a_finished_chat_does_not_cancel_the_next_one(self) -> None:
        h = _Harness(_yielding_stream(_stop_chunk()), _yielding_stream(_stop_chunk()))
        list(h.t2v.chat("one"))
        h.t2v.stop()

        events = list(h.t2v.chat("two"))

        assert [e.type for e in events] == ["done"]


class TestTalk2ViewWiring:
    def test_dispatcher_and_capabilities_reach_tools_and_sessions(self) -> None:
        dispatcher = QueueDispatcher()
        caps = Capabilities(tool_events=True)
        t2v = Talk2View("pk_test", dispatcher=dispatcher, capabilities=caps)
        t2v._client.close()
        t2v._client = MagicMock()
        t2v._client.request.return_value = {"session_id": "s1", "thread_id": "th1", "model": "m"}

        session = t2v.create_session()

        assert t2v.tools.dispatcher is dispatcher
        assert session._capabilities is caps

    def test_public_exports(self) -> None:
        for name in (
            "CancelToken",
            "Cancelled",
            "Dispatcher",
            "InlineDispatcher",
            "QueueDispatcher",
            "TimerPumpDispatcher",
            "qt_dispatcher",
            "bpy_dispatcher",
            "run_on_main",
            "Capabilities",
            "Decision",
            "PermissionCheck",
            "PermissionResult",
            "ToolEvent",
            "strip_null_args",
            "error_for_type",
            "is_session_gone",
            "SESSION_LOST_MESSAGE",
            "SESSION_LOST_AFTER_DENY_MESSAGE",
        ):
            assert name in talk2view.__all__, name
            assert hasattr(talk2view, name), name
