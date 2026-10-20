"""ChatController on a REAL Talk2View whose HTTP client is mocked.

Ports the two cases of the TS ``session-recovery.test.ts`` that need the chat
state machine, plus the fail-closed case that needs the real
permission layer: a malformed decision must deny and never reach the handler.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List, Optional
from unittest.mock import MagicMock

from talk2view import Talk2View
from talk2view.cancel import CancelToken
from talk2view.controller import ChatController, Decision
from talk2view.errors import T2VError
from talk2view.types import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ToolCallInterrupt,
)

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


def interrupt_chunk(tool_name: str, tool_call_id: str) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="t",
        model="m",
        thread_id="th",
        choices=[ChatCompletionChunkChoice()],
        interrupt=ToolCallInterrupt(
            type="tool_call",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments={"text": "x"},
        ),
    )


def yielding_stream(*chunks: ChatCompletionChunk) -> StreamFactory:
    return lambda: iter(chunks)


def throwing_stream(error: Exception) -> StreamFactory:
    def factory() -> Iterator[ChatCompletionChunk]:
        raise error
        yield  # pragma: no cover — makes this a generator

    return factory


def session_gone() -> T2VError:
    return T2VError("Session not found", "not_found", 404)


class Harness:
    """A real Talk2View; only its T2VClient is a mock (the TS test's vi.mock of ./client)."""

    def __init__(self, *streams: StreamFactory) -> None:
        self.t2v = Talk2View("pk_test_123", base_url="http://localhost:9999")
        self.t2v._client.close()
        self.client = MagicMock()
        self.t2v._client = self.client
        self.t2v.tools._client = self.client
        self.stream_bodies: List[Dict[str, Any]] = []
        self._streams = list(streams)
        self.client.request.side_effect = self._request
        self.client.stream_request.side_effect = self._stream_request
        self.controller = ChatController(self.t2v, executor=lambda fn: fn())

    def _request(self, endpoint: str, **kwargs: Any) -> Dict[str, Any]:
        if endpoint == "/v1/sessions":
            n = len([c for c in self.client.request.call_args_list if c[0][0] == "/v1/sessions"])
            return {"session_id": "s{}".format(n), "thread_id": "th{}".format(n), "model": "m"}
        return {}

    def _stream_request(
        self, endpoint: str, body: Dict[str, Any], cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatCompletionChunk]:
        self.stream_bodies.append(body)
        return self._streams.pop(0)()


class TestSendMessageRecoversTransparently:
    def test_lands_the_recovered_reply_in_messages_leaves_error_null_and_resends_the_same_history(
        self,
    ) -> None:
        h = Harness(
            throwing_stream(session_gone()), yielding_stream(text_chunk("hello"), stop_chunk())
        )

        h.controller.send("hi")

        snap = h.controller.snapshot()
        assert any(m["role"] == "assistant" and m["content"] == "hello" for m in snap["messages"])
        assert snap["error"] is None
        assert snap["isLoading"] is False
        assert len(h.stream_bodies) == 2
        assert h.stream_bodies[1]["messages"] == h.stream_bodies[0]["messages"]


class TestRespondToApprovalSessionDied:
    def test_reports_the_loss_instead_of_throwing_and_clears_the_pending_approval(self) -> None:
        h = Harness(
            yielding_stream(interrupt_chunk("insert_text", "tc1")),
            throwing_stream(session_gone()),
        )
        h.t2v.tools.handle("insert_text", lambda text: "inserted", permission=True)
        h.controller.send("insert x")
        assert h.controller.snapshot()["pendingApproval"] is not None

        h.controller.approve(Decision(action="deny"))

        snap = h.controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert snap["error"] is not None
        assert re.search(r"send your message again", snap["error"], re.IGNORECASE)
        assert snap["messages"][-1]["steps"][0]["status"] == "denied"
        assert h.t2v.get_session() is None  # dropped: the next send opens a new session


class TestAMalformedDecisionFailsClosed:
    """``Decision.approved`` allow-lists the action, so an un-normalised UI string denies.
    Needs the real tools layer: the point is that the handler never runs."""

    def test_denies_the_call_never_runs_the_handler_and_remembers_nothing(self) -> None:
        h = Harness(
            yielding_stream(interrupt_chunk("insert_text", "tc1")),
            yielding_stream(text_chunk("ok"), stop_chunk()),
        )
        ran: List[str] = []

        def insert_text(text: str) -> str:
            ran.append(text)
            return "inserted"

        h.t2v.tools.handle("insert_text", insert_text, permission=True)
        h.controller.send("insert x")

        h.controller.approve(Decision(action="Always"))  # type: ignore[arg-type]

        assert ran == []  # the handler never ran
        snap = h.controller.snapshot()
        assert snap["alwaysAllowedTools"] == []  # and nothing was remembered
        assert snap["error"] is None
        steps = [
            step
            for message in snap["messages"]
            if message["role"] == "assistant"
            for step in message.get("steps", [])
        ]
        assert [s["status"] for s in steps] == ["denied"]
        # The engine is told it was denied, with no tool result.
        resume = h.stream_bodies[1]
        assert resume["is_error"] is True
        assert "User denied this action" in resume["result"]
