"""Engine-enforced approvals and platform tool events — opt-in Capabilities."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

from talk2view.controller import Capabilities, Decision
from talk2view.types import ChatEvent, ToolEvent

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    approval_result_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
)


def engine_approval_chunk(tool_name: str, tool_call_id: str, args: Dict[str, Any]) -> ChatEvent:
    return ChatEvent(
        type="approval_required",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args,
        description="Send an email to a@b.com",
        kind="engine",
        details=[{"label": "To", "value": "a@b.com"}],
    )


def approval_chunk_of_kind(kind: str, tool_name: str, tool_call_id: str) -> ChatEvent:
    """An ``approval_required`` whose ``kind`` this SDK does not know — including the empty
    string, the one malformed value a truthiness test would have read as "client"."""
    return ChatEvent(
        type="approval_required",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments={},
        description="Do it",
        kind=kind,
    )


def tool_start(
    tool_name: str, tool_call_id: str, args: Optional[Dict[str, Any]] = None
) -> ChatEvent:
    return ChatEvent(
        type="tool_event",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_event=ToolEvent(
            type="tool_start", tool_name=tool_name, tool_call_id=tool_call_id, arguments=args
        ),
    )


def tool_end(
    tool_name: str,
    tool_call_id: str,
    status: str = "ok",
    display: Optional[Dict[str, Any]] = None,
) -> ChatEvent:
    return ChatEvent(
        type="tool_event",
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        tool_event=ToolEvent(
            type="tool_end",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            status=status,
            display=display,
        ),
    )


class TestEngineApproval:
    def test_engine_approval_sets_kind_details_and_platform_source(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[engine_approval_chunk("gmail_send", "call_9", {"to": "a@b.com"})]]
        controller = make_controller(t2v, capabilities=Capabilities(approval=True))

        controller.send("email them")

        snap = controller.snapshot()
        assert snap["pendingApproval"] == {
            "toolCallId": "call_9",
            "toolName": "gmail_send",
            "arguments": {"to": "a@b.com"},
            "description": "Send an email to a@b.com",
            "kind": "engine",
            "details": [{"label": "To", "value": "a@b.com"}],
        }
        assert snap["isLoading"] is False
        step = snap["messages"][-1]["steps"][0]
        assert step["source"] == "platform"
        assert step["status"] == "running"

    def test_client_approvals_carry_no_kind_key(self) -> None:
        """Wire parity with TS: a client approval is exactly the four TS keys."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                ChatEvent(
                    type="approval_required",
                    tool_name="set_layout",
                    tool_call_id="call_1",
                    arguments={},
                    description="Layout",
                    kind="client",
                )
            ]
        ]
        controller = make_controller(t2v)
        controller.send("x")
        pending = controller.snapshot()["pendingApproval"]
        assert pending is not None
        assert set(pending) == {"toolCallId", "toolName", "arguments", "description"}

    def test_approving_an_engine_approval_passes_kind_engine(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[engine_approval_chunk("gmail_send", "call_9", {"to": "a@b.com"})]]
        t2v.approval_scripts = [
            [
                approval_result_chunk("gmail_send", "once", "call_9"),
                tool_start("gmail_send", "call_9", {"to": "a@b.com"}),
                tool_end("gmail_send", "call_9", "ok"),
                text_chunk("Sent."),
                stop_chunk(),
            ]
        ]
        controller = make_controller(
            t2v, capabilities=Capabilities(approval=True, tool_events=True)
        )

        controller.send("email them")
        controller.approve(Decision(action="once"))

        request = t2v.approval_calls[0]["approval"]
        assert request.kind == "engine"
        assert request.tool_call_id == "call_9"
        assert request.details == [{"label": "To", "value": "a@b.com"}]
        snap = controller.snapshot()
        assistants = [m for m in snap["messages"] if m["role"] == "assistant"]
        # ONE step for call_9: the tool_start re-used the step the approval recorded.
        assert len(assistants[0]["steps"]) == 1
        assert assistants[0]["steps"][0]["status"] == "used"
        assert assistants[-1]["content"] == "Sent."

    def test_an_engine_approval_for_an_always_allowed_tool_still_pauses(self) -> None:
        """The "always allow" memory holds CLIENT tool names. An engine-gated call of the same
        name is a different call with different consequences, so it is asked about every time,
        details and all."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("gmail_send", "call_1", {}, "Send")],
            [engine_approval_chunk("gmail_send", "call_9", {"to": "a@b.com"})],
        ]
        t2v.approval_scripts = [[text_chunk("Sent 1"), stop_chunk()]]
        controller = make_controller(
            t2v, capabilities=Capabilities(approval=True, tool_events=True)
        )
        controller.send("send the first one")
        controller.approve(Decision(action="always"))
        assert controller.snapshot()["alwaysAllowedTools"] == ["gmail_send"]

        log = BatchLog()
        controller.subscribe(log)
        controller.send("email them")

        snap = controller.snapshot()
        assert snap["pendingApproval"] == {
            "toolCallId": "call_9",
            "toolName": "gmail_send",
            "arguments": {"to": "a@b.com"},
            "description": "Send an email to a@b.com",
            "kind": "engine",
            "details": [{"label": "To", "value": "a@b.com"}],
        }
        assert len(t2v.approval_calls) == 1  # only the first, manually approved call
        assert log.values("agentStatus") == []  # never auto-approved

    @pytest.mark.parametrize("kind", ["", "weird"])
    def test_an_unknown_kind_is_never_auto_approved_from_the_memory(self, kind: str) -> None:
        """Only the literal "client" is a client tool. Anything else — an engine kind, a kind
        from a newer engine, or the empty string — falls on the non-client side and pauses."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("set_layout", "call_1", {}, "Change layout")],
            [approval_chunk_of_kind(kind, "set_layout", "call_2")],
        ]
        t2v.approval_scripts = [[text_chunk("Done 1"), stop_chunk()]]
        controller = make_controller(t2v, capabilities=Capabilities(approval=True))
        controller.send("four-up please")
        controller.approve(Decision(action="always"))
        assert controller.snapshot()["alwaysAllowedTools"] == ["set_layout"]

        log = BatchLog()
        controller.subscribe(log)
        controller.send("now axial")

        snap = controller.snapshot()
        assert snap["pendingApproval"] is not None
        assert snap["pendingApproval"]["kind"] == kind
        assert snap["isLoading"] is False
        assert len(t2v.approval_calls) == 1
        assert log.values("agentStatus") == []
        assert snap["messages"][-1]["steps"][0]["source"] == "platform"

    @pytest.mark.parametrize("kind", ["", "weird"])
    def test_always_on_an_unknown_kind_is_not_remembered(self, kind: str) -> None:
        """A kind this SDK cannot read must not teach it to answer for the next such call."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[approval_chunk_of_kind(kind, "gmail_send", "call_1")]]
        t2v.approval_scripts = [[stop_chunk()]]
        controller = make_controller(t2v, capabilities=Capabilities(approval=True))

        controller.send("email them")
        controller.approve(Decision(action="always"))

        assert controller.snapshot()["alwaysAllowedTools"] == []
        # ...but the decision itself travels unchanged, so this one call is approved.
        assert t2v.approval_calls[0]["decision"] == Decision(action="always")
        assert t2v.approval_calls[0]["approval"].kind == kind


class TestTheEngineSequenceForAGatedTool:
    """What the engine really sends for an approval-gated platform tool, end to end.

    It emits NO ``tool_start`` for one (a gated call may never run, and when it does it
    runs on the resume stream), so the whole lifecycle is: ``approval_required`` on the chat
    stream, then ``approval_result`` plus a single ``tool_end`` on the resume stream. The step
    the approval recorded is the only step there ever is for that call — the user must not
    see the same send twice.
    """

    def _resume(self, status: str, display: Optional[Dict[str, Any]] = None) -> FakeT2V:
        t2v = FakeT2V()
        t2v.chat_scripts = [[engine_approval_chunk("gmail_send", "call_9", {"to": "a@b.com"})]]
        t2v.approval_scripts = [
            [
                approval_result_chunk("gmail_send", "once", "call_9"),
                tool_end("gmail_send", "call_9", status, display),
                text_chunk("Sent."),
                stop_chunk(),
            ]
        ]
        return t2v

    def test_an_approved_gated_tool_ends_as_one_used_step_carrying_its_display(self) -> None:
        sent = {"kind": "email", "to": "a@b.com"}
        t2v = self._resume("ok", sent)
        controller = make_controller(
            t2v, capabilities=Capabilities(approval=True, tool_events=True)
        )
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.send("email them")
        controller.approve(Decision(action="once"))

        snap = controller.snapshot()
        assistants = [m for m in snap["messages"] if m["role"] == "assistant"]
        steps = [s for m in assistants for s in m.get("steps", [])]
        assert len(steps) == 1  # the tool_end reused the approval's step, it did not add one
        assert steps[0]["name"] == "gmail_send"
        assert steps[0]["status"] == "used"
        assert steps[0]["source"] == "platform"
        assert steps[0]["display"] == sent
        assert "result" not in steps[0]
        assert assistants[-1]["content"] == "Sent."
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert log.replay(start) == snap

    def test_a_gated_tool_that_fails_on_the_resume_is_not_shown_as_sent(self) -> None:
        """The dangerous direction: the user approved a send, so a step left at
        ``used`` reads as "it went out". A ``tool_end`` error must undo the ``used`` the
        ``approval_result`` put there and say what happened instead."""
        t2v = self._resume("error")
        controller = make_controller(
            t2v, capabilities=Capabilities(approval=True, tool_events=True)
        )
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.send("email them")
        controller.approve(Decision(action="once"))

        snap = controller.snapshot()
        assistants = [m for m in snap["messages"] if m["role"] == "assistant"]
        steps = [s for m in assistants for s in m.get("steps", [])]
        assert len(steps) == 1
        assert steps[0]["status"] == "denied"
        assert steps[0]["result"] == "Tool failed"
        assert log.replay(start) == snap


class TestToolEvents:
    def test_tool_event_start_end_updates_step_by_toolCallId(self) -> None:  # noqa: N802
        t2v = FakeT2V()
        places = {"kind": "places", "items": [{"name": "Cafe"}]}
        t2v.chat_scripts = [
            [
                tool_start("maps_search", "call_a", {"q": "coffee", "near": None}),
                tool_start("maps_search", "call_b", {"q": "tea"}),
                tool_end("maps_search", "call_a", "ok", places),
                tool_end("maps_search", "call_b", "error"),
                text_chunk("Here you go"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        log = BatchLog()
        controller.subscribe(log)

        controller.send("coffee?")

        messages = controller.snapshot()["messages"]
        steps = messages[1]["steps"]
        assert steps == [
            {
                "name": "maps_search",
                "status": "used",
                "args": {"q": "coffee"},
                "toolCallId": "call_a",
                "source": "platform",
                "display": places,
            },
            {
                # An errored call is NOT "used": the page counts `used` steps as completed,
                # and call_b never completed. `result` is what tells it from a refusal.
                "name": "maps_search",
                "status": "denied",
                "args": {"q": "tea"},
                "toolCallId": "call_b",
                "source": "platform",
                "result": "Tool failed",
            },
        ]
        assert messages[2]["content"] == "Here you go"  # text after tools -> new segment
        # While running, call_a was visible as `running`.
        first_step_lists = [
            op["value"]
            for b in log.batches
            for op in b["ops"]
            if op["path"] == ["messages", 1, "steps"]
        ]
        assert [[s["status"] for s in steps] for steps in first_step_lists] == [["running"]]

    def test_tool_end_after_text_updates_the_earlier_segment(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_start("generate_image", "call_i", {"prompt": "a cat"}),
                text_chunk("Working on it"),
                tool_end(
                    "generate_image", "call_i", "ok", {"kind": "image", "attachment_id": "att_1"}
                ),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        controller.send("draw a cat")
        messages = controller.snapshot()["messages"]
        assert messages[1]["steps"][0]["status"] == "used"
        assert messages[1]["steps"][0]["display"]["attachment_id"] == "att_1"
        assert messages[2]["content"] == "Working on it"

    def test_tool_end_denied_and_tool_end_without_a_start(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_start("gmail_send", "call_d"),
                tool_end("gmail_send", "call_d", "denied"),
                tool_end("web_search", "call_orphan", "ok"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        controller.send("x")
        steps = controller.snapshot()["messages"][1]["steps"]
        assert [(s["name"], s["status"]) for s in steps] == [
            ("gmail_send", "denied"),
            ("web_search", "used"),
        ]

    def test_a_failed_tool_keeps_the_display_payload_it_managed_to_produce(self) -> None:
        """An error is no reason to drop what the tool did return: ``display`` survives every
        status, and ``result`` is what separates a failure from a refusal."""
        t2v = FakeT2V()
        partial = {"kind": "places", "items": [{"name": "Cafe"}]}
        t2v.chat_scripts = [
            [
                tool_start("maps_search", "call_e", {"q": "coffee"}),
                tool_end("maps_search", "call_e", "error", partial),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        controller.send("coffee?")
        step = controller.snapshot()["messages"][1]["steps"][0]
        assert (step["status"], step["result"]) == ("denied", "Tool failed")
        assert step["display"] == partial

    def test_a_tool_end_status_this_sdk_cannot_read_is_not_shown_as_used(self) -> None:
        """Allow-listed like ``Decision.approved``: only "ok" completed. A status from a newer
        engine is never rendered as a call that succeeded."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_start("gmail_send", "call_t"),
                tool_end("gmail_send", "call_t", "timeout"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        controller.send("x")
        step = controller.snapshot()["messages"][1]["steps"][0]
        assert step["status"] == "denied"
        assert "result" not in step  # "Tool failed" is reserved for a reported error

    def test_a_tool_event_with_no_payload_or_an_unknown_type_is_ignored(self) -> None:
        """The wire edge: a chunk whose ``tool_event`` did not parse, and a lifecycle type
        from a newer engine. Neither breaks the turn nor invents a step."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                ChatEvent(type="tool_event", tool_name="x", tool_call_id="call_x"),
                ChatEvent(
                    type="tool_event",
                    tool_name="x",
                    tool_call_id="call_x",
                    tool_event=ToolEvent(
                        type="tool_progress", tool_name="x", tool_call_id="call_x"
                    ),
                ),
                text_chunk("still here"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v, capabilities=Capabilities(tool_events=True))
        controller.send("x")

        snap = controller.snapshot()
        assert snap["error"] is None
        # No tool ran, so the text stays in the segment that was already open.
        assert snap["messages"][1]["content"] == "still here"
        assert "steps" not in snap["messages"][1]

    def test_replaying_batches_with_tool_events_reproduces_the_snapshot(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_start("maps_search", "call_a", {"q": "coffee"}),
                tool_end("maps_search", "call_a", "ok", {"kind": "places"}),
                text_chunk("ok"),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)
        controller.send("x")
        assert log.replay(start) == controller.snapshot()
