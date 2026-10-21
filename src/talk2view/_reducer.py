"""TurnReducer — folds one stream's ``ChatEvent``s into the chat state, one event at a time.

This is the half of the controller that knows what an event *means*: which message segment a
text delta lands in, when a tool call opens a new segment, how an approval or a tool event
finds its step, and how a turn is finalized. It knows nothing about threads, locks, runs or
subscribers — the ``ChatController`` shell holds the state lock while it calls in here, and
flushes afterwards.

Everything the reducer needs is in three places: the ``OpRecorder`` (the live state and the
op stream, read through ``rec.state`` and written only through ``rec.set`` /
``rec.append_text`` / ``rec.append_message`` / ``rec.replace_messages``), the ``_Cursor``
(where this stream writes, plus what it has told the shell so far: ``turn_failed`` and the
``notices`` it raised), and the event. So a call to ``reduce`` is a function of
``(recorder state, cursor, event)`` — the only other input is the message-id counter and the
clock behind ``append_assistant``, which is why the shell's ``_prepare_send`` and
``approve()`` mint their ids through this class too, so every id comes off one counter.

``reduce`` returns ``None`` to keep streaming, or a ``_Consumed`` to pause the stream: with
``auto_approval`` set the shell answers the approval itself, without it the shell ends the run
and leaves ``pendingApproval`` for the host to answer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, cast

from .state import (
    DisplayMessage,
    OpRecorder,
    PendingApproval,
    ToolStep,
    counter_suffix,
    is_safe_counter_digits,
)
from .tools import strip_null_args
from .types import ChatEvent

#: Event name for ``ChatController.on``: the anonymous demo budget ran out. Defined here so
#: the reducer can raise it; ``controller.py`` re-exports it as the public constant.
DEMO_LIMIT_REACHED = "demoLimitReached"


@dataclass
class _Cursor:
    """Where the current stream writes, and what it has told the shell so far.

    ``assistant_id`` is the segment text lands in; ``had_tool`` makes the next text open a new
    segment; ``completed`` records that the terminal ``done`` was read (a turn that reached
    ``done`` is complete, even if a cancel lands in the same instant -- see ``_drain``).
    ``turn_failed`` and ``notices`` are the reducer's two outputs that are not chat state: the
    shell folds ``turn_failed`` into its own flag after every event and moves ``notices`` onto
    its listener queue, so the reducer never touches either of those directly.
    """

    assistant_id: str
    had_tool: bool = False
    completed: bool = False
    turn_failed: bool = False
    notices: List[str] = field(default_factory=list)


@dataclass
class _Consumed:
    paused: bool
    auto_approval: Optional[PendingApproval]
    assistant_id: str
    completed: bool = False


Handler = Callable[[ChatEvent, _Cursor], Optional[_Consumed]]


def now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def set_top(rec: OpRecorder, key: str, value: Any) -> None:
    """Set a top-level key, recording an op only when the value actually changes."""
    if cast(Dict[str, Any], rec.state)[key] != value:
        rec.set([key], value)


class TurnReducer:
    """The event handlers, the step helpers and the message half of finalize. No locks: the
    caller holds the controller's state lock around every method here."""

    def __init__(
        self, rec: OpRecorder, *, allows_always: Optional[Callable[[str], bool]] = None
    ) -> None:
        self._rec = rec
        # Whether a CLIENT tool's "always" memory may auto-consume an approval at all
        # (``save_skill`` says no). Defaults to always-True -- unchanged
        # behaviour -- so a reducer built with no argument (every existing caller) is
        # untouched; ``ChatController`` passes ``T2VTools.allows_always`` (defensively
        # wrapped). Checked even though ``alwaysAllowedTools`` itself is only ever meant
        # to hold a name that allows it (``approve()``'s own guard) -- belt and braces:
        # a name that got in there some other way (a restored conversation, a test) must
        # still never auto-consume a no-always tool's approval.
        self._allows_always: Callable[[str], bool] = allows_always or (lambda _name: True)
        self._message_counter = 0
        # Explicit, so a wire type can only ever reach a handler listed here. Unknown types
        # (``reasoning``, or one from a newer engine) are ignored by ``reduce``.
        self._handlers: Dict[str, Handler] = {
            "text": self._on_text,
            "status": self._on_status,
            "todos": self._on_todos,
            "tool_call": self._on_tool_call,
            "done": self._on_done,
            "error": self._on_error,
            "approval_required": self._on_approval_required,
            "approval_result": self._on_approval_result,
            "tool_event": self._on_tool_event,
        }

    # ── Ids and segments (also used by the shell's send/approve paths) ─────

    def next_message_id(self) -> str:
        self._message_counter += 1
        return "msg_{}_{}".format(self._message_counter, int(time.time() * 1000))

    def bump_message_counter(self, ids: Iterable[str]) -> None:
        """Move the counter past every ``msg_<n>_…`` id in ``ids``, so a message minted
        after a restore can never repeat a restored one's number. An id whose digit run is
        not safe to read (not ASCII, or longer than cpython's own ``int()`` limit) is
        skipped rather than raised on, the same as an id with no digit run at all --
        ``restore_conversation`` validates the ids it accepts before this ever runs, but
        this stays safe on its own regardless of the caller."""
        for message_id in ids:
            digits = counter_suffix(message_id)
            if digits is not None and is_safe_counter_digits(digits):
                self._message_counter = max(self._message_counter, int(digits))

    def append_assistant(self) -> str:
        message: DisplayMessage = {
            "id": self.next_message_id(),
            "role": "assistant",
            "content": "",
            "timestamp": now_iso(),
            "isStreaming": True,
        }
        self._rec.append_message(message)
        return message["id"]

    # ── The reduction ───────────────────────────────────────────────────────

    def reduce(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        """Apply one event. ``None`` keeps streaming; a ``_Consumed`` pauses the stream."""
        handler = self._handlers.get(event.type)
        if handler is None:
            return None
        return handler(event, cursor)

    def finalize(self, assistant_id: str) -> List[str]:
        """Close the turn's segments and return the text of every trailing assistant segment,
        oldest first — the shell turns that into the turn's one history entry (or, when it is
        empty, decides whether to roll the user turn back)."""
        index = self._index_of(assistant_id)
        if index is not None:
            self._rec.set(["messages", index, "isStreaming"], False)
        messages = self._rec.state["messages"]
        # An empty trailing segment (opened right before `done`) is dropped. A tools-only or
        # plan-only message stays.
        if messages:
            last = messages[-1]
            if (
                last["role"] == "assistant"
                and not last["content"]
                and not last.get("steps")
                and not last.get("plan")
            ):
                self._rec.replace_messages(list(messages[:-1]))

        parts: List[str] = []
        for message in reversed(self._rec.state["messages"]):
            if message["role"] != "assistant":
                break
            if message["content"]:
                parts.insert(0, message["content"])

        set_top(self._rec, "agentStatus", None)
        set_top(self._rec, "isLoading", False)
        return parts

    # ── Event handlers (return a _Consumed to pause the stream) ─────────────

    def _on_text(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        if cursor.had_tool:
            # Text after a tool call starts a new segment (message segmentation, as in TS).
            previous = self._index_of(cursor.assistant_id)
            if previous is not None:
                self._rec.set(["messages", previous, "isStreaming"], False)
            cursor.assistant_id = self.append_assistant()
            cursor.had_tool = False
        index = self._index_of(cursor.assistant_id)
        if index is not None:
            self._rec.append_text(["messages", index, "content"], event.content or "")
        return None

    def _on_status(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        set_top(
            self._rec, "agentStatus", {"type": event.status or "", "message": event.message or ""}
        )
        return None

    def _on_todos(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        index = self._index_of(cursor.assistant_id)
        if index is not None:
            self._rec.set(["messages", index, "plan"], event.todos or "")
        return None

    def _on_tool_call(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        step: ToolStep = {
            "name": event.tool_name or "",
            "status": "used",
            "args": strip_null_args(event.arguments or {}),
            "source": "client",
        }
        if event.tool_call_id:
            step["toolCallId"] = event.tool_call_id
        self._append_step(cursor.assistant_id, step)
        cursor.had_tool = True
        return None

    def _on_done(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        cursor.completed = True
        set_top(self._rec, "threadId", event.thread_id)
        return None

    def _on_error(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        cursor.turn_failed = True
        if event.error_type == "budget_exceeded" and self._rec.state["isAnonymous"]:
            # The anonymous demo budget ran out: prompt for signup, do not show a raw error.
            cursor.notices.append(DEMO_LIMIT_REACHED)
        else:
            set_top(self._rec, "error", event.message or "")
        return None

    def _on_approval_required(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        name = event.tool_name or ""
        arguments = strip_null_args(event.arguments or {})
        # Only the literal "client" is a client tool; an ABSENT kind is one (an engine that
        # predates ``kind``), and every other value — "engine", the empty string, a kind
        # from a newer engine — is carried through unchanged so each later check (the
        # auto-approve check below, approve()'s memory) fails closed on it.
        kind = "client" if event.kind is None else event.kind
        approval: PendingApproval = {
            "toolCallId": event.tool_call_id or "",
            "toolName": name,
            "arguments": arguments,
            "description": event.description or "",
        }
        if kind != "client":
            approval["kind"] = kind
            if event.details:
                # The engine's label/value rows for its own approval card. Omitted, never
                # empty, so a kind that sends none keeps the four TS keys plus `kind`.
                approval["details"] = [dict(detail) for detail in event.details]
        step: ToolStep = {
            "name": name,
            "status": "running",
            "args": arguments,
            "toolCallId": approval["toolCallId"],
            "source": "client" if kind == "client" else "platform",
        }
        self._append_step(cursor.assistant_id, step)
        if (
            kind == "client"
            and name in self._rec.state["alwaysAllowedTools"]
            and self._allows_always(name)
        ):
            return _Consumed(True, approval, cursor.assistant_id)
        set_top(self._rec, "pendingApproval", approval)
        return _Consumed(True, None, cursor.assistant_id)

    def _on_approval_result(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        # Allow-listed, like Decision.approved: a decision this SDK cannot read is shown as
        # denied rather than claiming the call ran.
        status = "used" if event.decision in ("once", "always") else "denied"
        name = event.tool_name or ""
        call_id = event.tool_call_id
        found: Optional[Tuple[int, int]] = None
        if call_id:
            # The id names exactly one call, so the tool NAME is never a fallback here: it
            # could re-mark a different call of the same tool.
            found = self._find_step(
                lambda s: s["status"] == "running" and s.get("toolCallId") == call_id
            ) or self._find_step(lambda s: s.get("toolCallId") == call_id)
        else:
            # No id (and TS matched by name only, index.ts:752-757): the last running step
            # of that tool, else its last step.
            found = self._find_step(
                lambda s: s["status"] == "running" and s["name"] == name
            ) or self._find_step(lambda s: s["name"] == name)
        if found is not None:
            changes: Dict[str, Any] = {"status": status}
            # Only a builtin installed with result_on_step (save_skill) carries its result
            # here (sessions.py); a host tool's approval_result has no content.
            if status == "used" and event.content is not None:
                changes["result"] = event.content
            self._update_step(found, changes)
        cursor.had_tool = True
        return None

    def _on_tool_event(self, event: ChatEvent, cursor: _Cursor) -> Optional[_Consumed]:
        """Platform-tool lifecycle (``Capabilities.tool_events``), keyed by ``toolCallId``."""
        tool_event = event.tool_event
        if tool_event is None:
            return None
        call_id = tool_event.tool_call_id
        # The id names exactly one call, so the tool NAME is never a fallback here: it could
        # match a different call of the same tool. Without an id nothing is matched at all.
        found = self._find_step(lambda s: s.get("toolCallId") == call_id) if call_id else None
        if tool_event.type == "tool_start":
            if found is not None:
                # An engine approval already recorded this call (its step has been waiting,
                # then `used`): flip that one step back to running instead of showing two.
                self._update_step(found, {"status": "running"})
            else:
                started: ToolStep = {
                    "name": tool_event.tool_name,
                    "status": "running",
                    "args": strip_null_args(tool_event.arguments or {}),
                    "toolCallId": call_id,
                    "source": "platform",
                }
                self._append_step(cursor.assistant_id, started)
            cursor.had_tool = True
            return None
        if tool_event.type != "tool_end":
            return None
        # Allow-listed like Decision.approved: only "ok" ran to completion. An "error", a
        # "denied", and a status this SDK cannot read all land on "denied" — never `used` —
        # because the reference renderer counts every `used` step as "completed"
        # (sdk/src/ui/components/ToolDisplay.tsx), and telling the user a side-effecting call
        # succeeded when it did not is the dangerous direction. "denied" is the
        # closest of the three values `ToolStep.status` allows; that union is the wire type
        # shared with TS (sdk/src/types.ts), which is why `result` below is what tells a
        # failure from a refusal. Giving a failure its own status is a coordinated change —
        # the TS union, the page's assistant-ui/convert.ts, the renderer, and this line.
        changes: Dict[str, Any] = {"status": "used" if tool_event.status == "ok" else "denied"}
        if tool_event.status == "error":
            changes["result"] = "Tool failed"
        # Kept whatever the status: a call that failed late may still have produced something
        # renderable, and dropping it would lose the only detail the user can see.
        if tool_event.display is not None:
            changes["display"] = tool_event.display
        if found is not None:
            self._update_step(found, changes)
        else:
            # A tool_end whose start we never saw. The engine sends no start for an approval-
            # gated tool — it only ever runs on the resume stream — so record it as finished.
            finished = cast(
                ToolStep,
                {
                    "name": tool_event.tool_name,
                    "toolCallId": call_id,
                    "source": "platform",
                    **changes,
                },
            )
            self._append_step(cursor.assistant_id, finished)
        cursor.had_tool = True
        return None

    # ── Step and segment helpers ────────────────────────────────────────────

    def _index_of(self, message_id: str) -> Optional[int]:
        messages = self._rec.state["messages"]
        for index in range(len(messages) - 1, -1, -1):
            if messages[index]["id"] == message_id:
                return index
        return None

    def _update_step(self, found: Tuple[int, int], changes: Dict[str, Any]) -> None:
        """Merge ``changes`` into the step ``_find_step`` located. Copy-then-set: the
        recorder's state is the live state and is never mutated in place."""
        message_index, step_index = found
        step = self._rec.state["messages"][message_index]["steps"][step_index]
        self._rec.set(
            ["messages", message_index, "steps", step_index], cast(ToolStep, {**step, **changes})
        )

    def _find_step(self, predicate: Callable[[ToolStep], bool]) -> Optional[Tuple[int, int]]:
        """Newest matching step among the trailing assistant segments of this turn."""
        messages = self._rec.state["messages"]
        for message_index in range(len(messages) - 1, -1, -1):
            message = messages[message_index]
            if message["role"] != "assistant":
                break
            steps = message.get("steps", [])
            for step_index in range(len(steps) - 1, -1, -1):
                if predicate(steps[step_index]):
                    return message_index, step_index
        return None

    def _append_step(self, assistant_id: str, step: ToolStep) -> None:
        index = self._index_of(assistant_id)
        if index is None:
            return
        steps = self._rec.state["messages"][index].get("steps")
        if steps is None:
            self._rec.set(["messages", index, "steps"], [step])
        else:
            self._rec.set(["messages", index, "steps", len(steps)], step)
