"""Tests for CancelToken and cross-thread cancellation of an SSE stream."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Dict, Iterator, List

import pytest
from pytest_httpserver import HTTPServer
from werkzeug.wrappers import Request, Response

from talk2view.cancel import Cancelled, CancelToken
from talk2view.client import T2VClient
from talk2view.storage import MemoryStorage


def _text_chunk(content: str) -> Dict[str, Any]:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
        "thread_id": "t1",
    }


@pytest.fixture()
def stall_server() -> Iterator[HTTPServer]:
    """A threaded server, so a stalled handler cannot block the next request."""
    server = HTTPServer(threaded=True)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


def _make_client(server: HTTPServer) -> T2VClient:
    storage = MemoryStorage()
    storage.set("access_token", "tok")
    return T2VClient("pk_test", storage, server.url_for(""))


class TestCancelToken:
    def test_cancel_runs_closers_once(self) -> None:
        token = CancelToken()
        calls: List[str] = []
        token.on_cancel(lambda: calls.append("a"))
        assert token.cancelled is False

        token.cancel()
        token.cancel()

        assert token.cancelled is True
        assert calls == ["a"]

    def test_unregister_removes_closer(self) -> None:
        token = CancelToken()
        calls: List[str] = []
        unregister = token.on_cancel(lambda: calls.append("a"))
        unregister()
        unregister()  # idempotent

        token.cancel()

        assert calls == []

    def test_on_cancel_after_cancel_runs_immediately(self) -> None:
        token = CancelToken()
        token.cancel()
        calls: List[str] = []

        token.on_cancel(lambda: calls.append("late"))

        assert calls == ["late"]

    def test_closer_exception_does_not_stop_other_closers(self) -> None:
        token = CancelToken()
        calls: List[str] = []

        def boom() -> None:
            raise RuntimeError("closer failed")

        token.on_cancel(boom)
        token.on_cancel(lambda: calls.append("b"))

        token.cancel()

        assert calls == ["b"]


class TestCloserLifetime:
    """``unregister()`` is final: it ends the closer's life, it does not just
    drop it from a list ``cancel()`` may already have snapshotted.

    A stream reader unregisters its abort closer and then hands its socket back
    to the connection pool. If a closer could still land after that, it would
    shut down a socket the next request is already using — a real bug, where
    the aborted "next request" was the token refresh.
    """

    def test_unregister_waits_for_an_in_flight_closer_run(self) -> None:
        token = CancelToken()
        closer_running = threading.Event()
        release_closer = threading.Event()
        closer_returned = threading.Event()
        unregister_returned = threading.Event()

        def slow_closer() -> None:
            closer_running.set()
            assert release_closer.wait(5), "test never released the closer"
            closer_returned.set()

        unregister = token.on_cancel(slow_closer)
        canceller = threading.Thread(target=token.cancel, daemon=True)
        canceller.start()
        assert closer_running.wait(5), "cancel() never reached the closer"

        reader = threading.Thread(
            target=lambda: (unregister(), unregister_returned.set()), daemon=True
        )
        reader.start()
        try:
            assert not unregister_returned.wait(0.3), (
                "unregister() returned while its closer was still running"
            )
            release_closer.set()
            assert unregister_returned.wait(5), "unregister() never returned"
            # Once unregister() returns, the closer has finished — the reader
            # can pool its socket knowing nothing will touch it.
            assert closer_returned.is_set()
        finally:
            release_closer.set()
            canceller.join(5)
            reader.join(5)
            assert not canceller.is_alive() and not reader.is_alive()

    def test_a_closer_cannot_land_after_its_unregister_returns(self) -> None:
        token = CancelToken()
        gate_running = threading.Event()
        release_gate = threading.Event()
        unregister_returned = threading.Event()
        order: List[str] = []

        def gate() -> None:
            """Holds cancel() inside its closer run, opening the old window."""
            gate_running.set()
            assert release_gate.wait(5), "test never released the gate"
            order.append("gate")

        # Registered first, so cancel() is still busy with it while the reader
        # below unregisters the stream's closer.
        token.on_cancel(gate)
        unregister = token.on_cancel(lambda: order.append("stream-closer"))

        canceller = threading.Thread(target=token.cancel, daemon=True)
        canceller.start()
        assert gate_running.wait(5), "cancel() never reached the gate"

        def reader() -> None:
            unregister()
            order.append("unregistered")
            unregister_returned.set()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        try:
            assert not unregister_returned.wait(0.3), (
                "unregister() returned during a closer run"
            )
            release_gate.set()
            assert unregister_returned.wait(5), "unregister() never returned"
        finally:
            release_gate.set()
            canceller.join(5)
            reader_thread.join(5)
            assert not canceller.is_alive() and not reader_thread.is_alive()

        assert "unregistered" in order
        if "stream-closer" in order:
            assert order.index("stream-closer") < order.index("unregistered"), order

    def test_a_closer_may_cancel_and_unregister_re_entrantly(self) -> None:
        """A closer runs host code; it must be able to re-enter the token."""
        token = CancelToken()
        calls: List[str] = []
        other_unregister: List[Callable[[], None]] = []

        def reentrant() -> None:
            calls.append("reentrant")
            token.cancel()  # already cancelled — must return, not deadlock
            other_unregister[0]()  # drops a sibling closer from inside the run
            token.on_cancel(lambda: calls.append("late"))  # runs immediately

        token.on_cancel(reentrant)
        other_unregister.append(token.on_cancel(lambda: calls.append("sibling")))

        done = threading.Event()
        worker = threading.Thread(
            target=lambda: (token.cancel(), done.set()), daemon=True
        )
        worker.start()

        assert done.wait(5), "cancel() deadlocked on a re-entrant closer"
        worker.join(5)
        assert calls == ["reentrant", "late", "sibling"]


class TestStreamCancel:
    def test_cancel_unblocks_stalled_read_within_1s(self, stall_server: HTTPServer) -> None:
        release = threading.Event()

        def handler(request: Request) -> Response:
            def body() -> Iterator[bytes]:
                yield ("data: " + json.dumps(_text_chunk("hi")) + "\n\n").encode()
                release.wait(15)  # stall: no more bytes, connection held open

            return Response(body(), mimetype="text/event-stream")

        stall_server.expect_request("/v1/sessions/s1/messages").respond_with_handler(handler)
        client = _make_client(stall_server)
        token = CancelToken()
        first_chunk = threading.Event()
        outcome: Dict[str, Any] = {}

        def consume() -> None:
            try:
                for chunk in client.stream_request(
                    "/v1/sessions/s1/messages", {"messages": []}, cancel=token
                ):
                    outcome["content"] = chunk.choices[0].delta.content
                    first_chunk.set()
                outcome["ended"] = "clean"
            except Cancelled:
                outcome["ended"] = "cancelled"
            except Exception as exc:  # pragma: no cover — failure detail for the spike
                outcome["ended"] = repr(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert first_chunk.wait(5), "never received the first chunk"
            token.cancel()
            worker.join(1.0)

            assert not worker.is_alive(), "reader still blocked 1s after cancel()"
            assert outcome == {"content": "hi", "ended": "cancelled"}
        finally:
            release.set()
            client.close()

    def test_cancel_before_connect_never_sends(self, stall_server: HTTPServer) -> None:
        stall_server.expect_request("/v1/sessions/s1/messages").respond_with_data(
            "data: [DONE]\n\n", content_type="text/event-stream"
        )
        client = _make_client(stall_server)
        token = CancelToken()
        token.cancel()

        with pytest.raises(Cancelled):
            list(client.stream_request("/v1/sessions/s1/messages", {"messages": []}, cancel=token))

        assert stall_server.log == []
        client.close()

    def test_connection_pool_usable_after_cancel(self, stall_server: HTTPServer) -> None:
        release = threading.Event()

        def handler(request: Request) -> Response:
            def body() -> Iterator[bytes]:
                yield ("data: " + json.dumps(_text_chunk("hi")) + "\n\n").encode()
                release.wait(15)

            return Response(body(), mimetype="text/event-stream")

        stall_server.expect_request("/v1/stall").respond_with_handler(handler)
        stall_server.expect_request("/v1/test").respond_with_json({"ok": True})
        stall_server.expect_request("/v1/stream").respond_with_data(
            "data: " + json.dumps(_text_chunk("again")) + "\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )
        client = _make_client(stall_server)
        token = CancelToken()
        first_chunk = threading.Event()
        outcome: Dict[str, Any] = {}

        def consume() -> None:
            try:
                for _ in client.stream_request("/v1/stall", {}, cancel=token):
                    first_chunk.set()
                outcome["ended"] = "clean"
            except Cancelled:
                outcome["ended"] = "cancelled"
            except Exception as exc:  # pragma: no cover — failure detail only
                outcome["ended"] = repr(exc)

        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        try:
            assert first_chunk.wait(5)
            token.cancel()
            worker.join(1.0)
            assert not worker.is_alive()
            assert outcome == {"ended": "cancelled"}

            # The shared pooled client must still serve JSON and SSE requests.
            assert client.request("/v1/test") == {"ok": True}
            chunks = list(client.stream_request("/v1/stream", {}))
            assert [c.choices[0].delta.content for c in chunks] == ["again"]
        finally:
            release.set()
            client.close()

    def test_stream_without_token_is_unchanged(self, stall_server: HTTPServer) -> None:
        stall_server.expect_request("/v1/stream").respond_with_data(
            "data: " + json.dumps(_text_chunk("plain")) + "\n\ndata: [DONE]\n\n",
            content_type="text/event-stream",
        )
        client = _make_client(stall_server)

        chunks = list(client.stream_request("/v1/stream", {}))

        assert [c.choices[0].delta.content for c in chunks] == ["plain"]
        client.close()
