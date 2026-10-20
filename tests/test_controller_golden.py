"""Golden op batches: the exact ops the controller emits for five scripted conversations.

The first four were recorded on the controller before the reducer split and asserted
unchanged after it. A subscriber, the LLM history and the final snapshot must be byte-identical
across the split: every ``set`` / ``append-text`` op, its path, its value and its batch
boundary. The fifth (``cancel_mid_stream_then_cancel_an_approval``) was added afterwards, on
the split controller, so that ``cancel()`` is pinned the same way.

Message ids carry a wall-clock suffix and timestamps are wall-clock, so both are normalised
(``msg_<counter>`` and ``<ts>``) before the comparison. Everything else is compared verbatim.

Regenerate ONLY when a behaviour change is intended and reviewed::

    T2V_REGEN_GOLDEN=1 uv run pytest tests/test_controller_golden.py
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from talk2view.cancel import Cancelled, CancelToken
from talk2view.controller import DEMO_LIMIT_REACHED, Capabilities, ChatController, Decision
from talk2view.types import ChatEvent, ToolEvent

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    approval_result_chunk,
    error_chunk,
    interrupt_chunk,
    make_controller,
    status_chunk,
    stop_chunk,
    text_chunk,
    todos_chunk,
    tool_call_chunk,
)

GOLDEN = Path(__file__).parent / "golden" / "controller_ops.json"
_MESSAGE_ID = re.compile(r"^msg_(\d+)_\d+$")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def normalise(value: Any) -> Any:
    """Strip the two wall-clock inputs (id suffix, timestamp); leave everything else alone."""
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key == "id" and isinstance(item, str) and _MESSAGE_ID.match(item):
                out[key] = "msg_" + _MESSAGE_ID.match(item).group(1)  # type: ignore[union-attr]
            elif key == "timestamp" and isinstance(item, str) and _TIMESTAMP.match(item):
                out[key] = "<ts>"
            else:
                out[key] = normalise(item)
        return out
    if isinstance(value, list):
        return [normalise(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return normalise(dataclasses.asdict(value))
    return value


def engine_approval(tool_name: str, call_id: str, args: Dict[str, Any]) -> ChatEvent:
    return ChatEvent(
        type="approval_required",
        tool_name=tool_name,
        tool_call_id=call_id,
        arguments=args,
        description="Send an email to a@b.com",
        kind="engine",
        details=[{"label": "To", "value": "a@b.com"}],
    )


def tool_start(tool_name: str, call_id: str, args: Dict[str, Any]) -> ChatEvent:
    return ChatEvent(
        type="tool_event",
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_event=ToolEvent(
            type="tool_start", tool_name=tool_name, tool_call_id=call_id, arguments=args
        ),
    )


def tool_end(tool_name: str, call_id: str, status: str, display: Any = None) -> ChatEvent:
    return ChatEvent(
        type="tool_event",
        tool_name=tool_name,
        tool_call_id=call_id,
        tool_event=ToolEvent(
            type="tool_end",
            tool_name=tool_name,
            tool_call_id=call_id,
            status=status,
            display=display,
        ),
    )


def _record(t2v: FakeT2V, controller: ChatController, drive: Callable[[], None]) -> Dict[str, Any]:
    start = controller.snapshot()
    log = BatchLog()
    notices: List[str] = []
    controller.subscribe(log)
    controller.on(DEMO_LIMIT_REACHED, lambda: notices.append(DEMO_LIMIT_REACHED))

    drive()

    snapshot = controller.snapshot()
    assert log.replay(start) == snapshot  # the replay net holds on every golden scenario
    return normalise(
        {
            "batches": log.batches,
            "snapshot": snapshot,
            "histories": [call["history"] for call in t2v.chat_calls],
            "approvals": [
                {"tool_call_id": call["approval"].tool_call_id, "decision": call["decision"]}
                for call in t2v.approval_calls
            ],
            "notices": notices,
        }
    )


def scenario_segmented_turn() -> Dict[str, Any]:
    """Status, plan, text, a client tool call, text in a new segment, done; then a second
    turn to show the history the first one left."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [
            status_chunk("thinking", "..."),
            todos_chunk("- [ ] a"),
            text_chunk("one "),
            tool_call_chunk("search", "call_1", {"q": "x", "page": None}),
            text_chunk("two"),
            stop_chunk("thread_z"),
        ],
        [text_chunk("three"), stop_chunk("thread_z")],
    ]
    controller = make_controller(t2v)

    def drive() -> None:
        controller.send("go")
        controller.send("more")

    return _record(t2v, controller, drive)


def scenario_client_approval_then_auto() -> Dict[str, Any]:
    """A client approval answered "always", then the same tool auto-approved next turn."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("Let me "), interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send")],
        [interrupt_chunk("send_email", "call_2", {"to": "c@d.com"}, "Send")],
    ]
    t2v.approval_scripts = [
        [
            approval_result_chunk("send_email", "always", "call_1"),
            text_chunk("Sent!"),
            stop_chunk(),
        ],
        [
            approval_result_chunk("send_email", "once", "call_2"),
            text_chunk("Sent 2"),
            stop_chunk(),
        ],
    ]
    controller = make_controller(t2v)

    def drive() -> None:
        controller.send("send email")
        controller.approve(Decision(action="always"), tool_call_id="call_1")
        controller.send("send another")

    return _record(t2v, controller, drive)


def scenario_errors_and_demo_limit() -> Dict[str, Any]:
    """The anonymous budget notice, a retry that errors, clear_error, then a good turn."""
    t2v = FakeT2V()
    t2v.auth.anonymous = True
    t2v.chat_scripts = [
        [error_chunk("Budget exceeded", "budget_exceeded")],
        [text_chunk("half"), error_chunk("boom")],
        [text_chunk("ok"), stop_chunk()],
    ]
    controller = make_controller(t2v)

    def drive() -> None:
        controller.send("one more")
        t2v.auth.anonymous = False
        controller.retry()
        controller.clear_error()
        controller.send("after signup")

    return _record(t2v, controller, drive)


def scenario_engine_gated_tool_then_edit_and_clear() -> Dict[str, Any]:
    """An engine approval whose resume fails, then edit() and clear()."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [engine_approval("gmail_send", "call_9", {"to": "a@b.com"})],
        [
            tool_start("web_search", "call_2", {"q": "x"}),
            tool_end("web_search", "call_2", "ok", {"kind": "results"}),
            text_chunk("Found."),
            stop_chunk(),
        ],
    ]
    t2v.approval_scripts = [
        [
            approval_result_chunk("gmail_send", "once", "call_9"),
            tool_end("gmail_send", "call_9", "error"),
            text_chunk("Sorry."),
            stop_chunk(),
        ]
    ]
    controller = make_controller(
        t2v, capabilities=Capabilities(approval=True, tool_events=True)
    )

    def drive() -> None:
        controller.send("email them")
        controller.approve(Decision(action="once"))
        user_id = controller.snapshot()["messages"][0]["id"]
        controller.edit(user_id, "search instead")
        controller.clear()

    return _record(t2v, controller, drive)


def scenario_cancel_mid_stream_then_cancel_an_approval() -> Dict[str, Any]:
    """Stop after partial text (kept, and replayed as history), Stop before any text (the
    user turn is rolled out of history), then Stop with an approval pending (denied with
    ``CANCELLED_APPROVAL_FEEDBACK``); a last turn shows the history all three left."""
    t2v = FakeT2V()
    controller = make_controller(t2v)

    def stop_pressed(cancel: Optional[CancelToken]) -> None:
        controller.cancel()  # the Stop button, pressed mid-reply
        assert cancel is not None and cancel.cancelled
        raise Cancelled()  # what the transport's next read does once its token is cancelled

    t2v.chat_scripts = [
        [text_chunk("Partial "), text_chunk("answer"), stop_pressed, text_chunk(" never")],
        [stop_pressed],
        [interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")],
        [text_chunk("Welcome"), stop_chunk()],
    ]
    t2v.approval_scripts = [
        [
            approval_result_chunk("delete_file", "deny", "call_1"),
            text_chunk("Not deleted."),
            stop_chunk(),
        ]
    ]

    def drive() -> None:
        controller.send("first")
        controller.send("second")
        controller.send("delete x")
        controller.cancel()
        controller.send("thanks")

    return _record(t2v, controller, drive)


SCENARIOS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "segmented_turn": scenario_segmented_turn,
    "client_approval_then_auto": scenario_client_approval_then_auto,
    "errors_and_demo_limit": scenario_errors_and_demo_limit,
    "engine_gated_tool_then_edit_and_clear": scenario_engine_gated_tool_then_edit_and_clear,
    "cancel_mid_stream_then_cancel_an_approval": (
        scenario_cancel_mid_stream_then_cancel_an_approval
    ),
}


def test_regenerate_golden_when_asked() -> None:
    if os.environ.get("T2V_REGEN_GOLDEN") != "1":
        pytest.skip("set T2V_REGEN_GOLDEN=1 to rewrite tests/golden/controller_ops.json")
    recorded = {name: scenario() for name, scenario in SCENARIOS.items()}
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(recorded, indent=1, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_ops_match_the_golden_recording(name: str) -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))[name]
    actual = json.loads(json.dumps(SCENARIOS[name]()))  # the same round trip the file took
    assert actual["batches"] == expected["batches"]
    assert actual["snapshot"] == expected["snapshot"]
    assert actual["histories"] == expected["histories"]
    assert actual["approvals"] == expected["approvals"]
    assert actual["notices"] == expected["notices"]
