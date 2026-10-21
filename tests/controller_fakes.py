"""Fakes shared by the ChatController tests.

``FakeT2V`` stands in for ``Talk2View``: ``chat()`` and ``respond_to_approval()`` play
scripted event lists, the way the TS suite's ``setupMockClient`` spies do. A script item is a
``ChatEvent`` (yielded), an ``Exception`` (raised), or a callable taking the ``CancelToken``
(called — use it to block until a test releases it or the token is cancelled).
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Union

from talk2view.cancel import Cancelled, CancelToken
from talk2view.controller import ChatController
from talk2view.dispatch import Dispatcher, InlineDispatcher
from talk2view.state import ChatSnapshot, OpBatch, apply_batch
from talk2view.types import ChatEvent

ScriptItem = Union[ChatEvent, Exception, Callable[[Optional[CancelToken]], None]]
Script = List[ScriptItem]


def inline_executor(fn: Callable[[], None]) -> None:
    """Run the job on the calling thread: send()/approve() return when the run is over."""
    fn()


class Gate:
    """Script item that parks the stream until ``release`` is set or the token is cancelled
    (then it raises ``Cancelled``, as the client's ``stream_request`` does)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, cancel: Optional[CancelToken]) -> None:
        self.entered.set()
        while not self.release.wait(0.01):
            if cancel is not None and cancel.cancelled:
                raise Cancelled()


class RaisingDispatcher:
    """A host dispatcher whose ``post`` raises -- a queue shut down under the SDK's feet, a
    Qt timer whose window has already been destroyed. It is the only host code the controller
    calls while delivering a batch, so it is the one that can break a flush in half."""

    def __init__(self) -> None:
        self.failing = True
        self.posts = 0

    def post(self, fn: Callable[[], None]) -> None:
        self.posts += 1
        if self.failing:
            raise RuntimeError("dispatcher is shut down")
        fn()

    def is_main_thread(self) -> bool:
        return False


class FakeAuth:
    def __init__(self) -> None:
        self.anonymous = False

    def is_anonymous(self) -> bool:
        return self.anonymous


class FakeTools:
    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher
        # Mirrors T2VTools.allows_always(): a test names a tool here to simulate a
        # builtin installed with add_builtin(..., allow_always=False) (save_skill)
        # -- every other tool keeps today's "always" behaviour.
        self.no_always: Set[str] = set()

    def allows_always(self, tool_name: str) -> bool:
        return tool_name not in self.no_always


class FakeT2V:
    """Same call signatures as ``Talk2View`` for everything the controller uses."""

    def __init__(self, dispatcher: Optional[Dispatcher] = None) -> None:
        self.tools = FakeTools(dispatcher or InlineDispatcher())
        self.auth = FakeAuth()
        self.chat_scripts: List[Script] = []
        self.approval_scripts: List[Script] = []
        self.chat_calls: List[Dict[str, Any]] = []
        self.approval_calls: List[Dict[str, Any]] = []
        self.defaults: List[Dict[str, Any]] = []
        self.cleared = 0
        self.detached = 0

    def set_chat_defaults(self, *, model: Optional[str] = None, capabilities: Any = None) -> None:
        self.defaults.append({"model": model, "capabilities": capabilities})

    def chat(
        self,
        message: str,
        system_prompt: Optional[str] = None,
        history: Any = None,
        attachments: Any = None,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        self.chat_calls.append(
            {
                "message": message,
                "system_prompt": system_prompt,
                "history": copy.deepcopy(history),
                "attachments": attachments,
                "cancel": cancel,
            }
        )
        script = self.chat_scripts.pop(0) if self.chat_scripts else []
        return _play(script, cancel)

    def respond_to_approval(
        self, approval: ChatEvent, decision: Any, cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatEvent]:
        self.approval_calls.append({"approval": approval, "decision": decision, "cancel": cancel})
        script = self.approval_scripts.pop(0) if self.approval_scripts else []
        return _play(script, cancel)

    def clear_session(self) -> None:
        self.cleared += 1

    def detach_session(self) -> None:
        self.detached += 1


def _play(script: Script, cancel: Optional[CancelToken]) -> Iterator[ChatEvent]:
    try:
        for item in script:
            if isinstance(item, Exception):
                raise item
            if callable(item):
                item(cancel)
                continue
            yield item
    except Cancelled:
        return  # Talk2View.chat() ends quietly when its token is cancelled


def make_controller(t2v: Optional[FakeT2V] = None, **kwargs: Any) -> ChatController:
    kwargs.setdefault("executor", inline_executor)
    return ChatController(t2v or FakeT2V(), **kwargs)  # type: ignore[arg-type]


class BatchLog:
    """A subscriber that records batches and can replay them onto a starting snapshot."""

    def __init__(self) -> None:
        self.batches: List[OpBatch] = []
        self.threads: List[int] = []

    def __call__(self, batch: OpBatch) -> None:
        self.batches.append(batch)
        self.threads.append(threading.get_ident())

    def seqs(self) -> List[int]:
        return [b["seq"] for b in self.batches]

    def values(self, key: str) -> List[Any]:
        """Every value a top-level ``set`` op gave ``key``, in order."""
        return [
            op["value"]
            for batch in self.batches
            for op in batch["ops"]
            if op["type"] == "set" and op["path"] == [key]
        ]

    def replay(self, start: ChatSnapshot) -> ChatSnapshot:
        snapshot = start
        for batch in self.batches:
            snapshot = apply_batch(snapshot, batch)
        return snapshot


# ── Event builders (names mirror chat-state.test.ts) ──


def text_chunk(content: str) -> ChatEvent:
    return ChatEvent(type="text", content=content)


def stop_chunk(thread_id: str = "thread_1") -> ChatEvent:
    return ChatEvent(type="done", thread_id=thread_id)


def interrupt_chunk(
    tool_name: str, tool_call_id: str, args: Dict[str, Any], description: str = ""
) -> ChatEvent:
    return ChatEvent(
        type="approval_required",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args,
        description=description,
        kind="client",
    )


def engine_interrupt_chunk(
    tool_name: str, tool_call_id: str, args: Dict[str, Any], description: str = ""
) -> ChatEvent:
    """An engine-gated platform tool. Its twin above is a client tool; the two must never be
    confused, because the "always allow" memory holds client tool names only."""
    return ChatEvent(
        type="approval_required",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args,
        description=description,
        kind="engine",
    )


def status_chunk(status: str, message: str) -> ChatEvent:
    return ChatEvent(type="status", status=status, message=message)


def todos_chunk(content: str) -> ChatEvent:
    return ChatEvent(type="todos", todos=content)


def tool_call_chunk(tool_name: str, tool_call_id: str, args: Dict[str, Any]) -> ChatEvent:
    return ChatEvent(
        type="tool_call", tool_name=tool_name, tool_call_id=tool_call_id, arguments=args
    )


def approval_result_chunk(
    tool_name: str, decision: str, tool_call_id: Optional[str] = None
) -> ChatEvent:
    # Talk2View sends no tool_call_id on approval_result; pass one to exercise the id match.
    return ChatEvent(
        type="approval_result", tool_name=tool_name, tool_call_id=tool_call_id, decision=decision
    )


def error_chunk(message: str, error_type: Optional[str] = None) -> ChatEvent:
    return ChatEvent(type="error", message=message, error_type=error_type)
