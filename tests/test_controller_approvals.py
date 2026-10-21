"""Client-tool permission approvals (ported from chat-state.test.ts › approval flow)."""

from __future__ import annotations

import threading

import pytest

from talk2view.controller import ChatController, ControllerBusyError, Decision
from talk2view.types import ChatEvent

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    approval_result_chunk,
    engine_interrupt_chunk,
    error_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
    tool_call_chunk,
)


class TestApprovalFlow:
    def test_pauses_on_approval_required_and_sets_pending_approval(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                text_chunk("Let me "),
                interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send an email"),
            ]
        ]
        controller = make_controller(t2v)

        controller.send("send email")

        snap = controller.snapshot()
        assert snap["pendingApproval"] == {
            "toolCallId": "call_1",
            "toolName": "send_email",
            "arguments": {"to": "a@b.com"},
            "description": "Send an email",
        }
        # Not loading while waiting — the page derives isRunning from pendingApproval.
        assert snap["isLoading"] is False
        # ...but the message keeps streaming so the approval card renders on it.
        assert snap["messages"][-1]["isStreaming"] is True
        assert snap["messages"][-1]["steps"] == [
            {
                "name": "send_email",
                "status": "running",
                "args": {"to": "a@b.com"},
                "toolCallId": "call_1",
                "source": "client",
            }
        ]

    def test_approve_tool_call_resumes_stream_after_approval(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                text_chunk("Let me "),
                interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send an email"),
            ]
        ]
        t2v.approval_scripts = [
            [approval_result_chunk("send_email", "once"), text_chunk("Email sent!"), stop_chunk()]
        ]
        controller = make_controller(t2v)

        controller.send("send email")
        assert controller.snapshot()["pendingApproval"] is not None
        controller.approve(Decision(action="once"))

        snap = controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assistants = [m for m in snap["messages"] if m["role"] == "assistant"]
        # After approval, text arrives in a new segment (message segmentation).
        assert len(assistants) == 2
        assert "Email sent!" in assistants[-1]["content"]
        assert assistants[0]["steps"][0]["status"] == "used"
        assert all(m["isStreaming"] is False for m in assistants)
        call = t2v.approval_calls[0]
        assert call["approval"] == ChatEvent(
            type="approval_required",
            kind="client",
            tool_name="send_email",
            tool_call_id="call_1",
            arguments={"to": "a@b.com"},
            description="Send an email",
        )
        assert call["decision"] == Decision(action="once")
        assert call["cancel"] is not None

    def test_approve_tool_call_with_deny_marks_step_as_denied(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_call_chunk("delete_file", "call_2", {"path": "/data/x"}),
                interrupt_chunk("delete_file", "call_2", {"path": "/data/x"}, "Delete file"),
            ]
        ]
        t2v.approval_scripts = [
            [
                approval_result_chunk("delete_file", "deny"),
                text_chunk("Ok, skipping."),
                stop_chunk(),
            ]
        ]
        controller = make_controller(t2v)

        controller.send("delete file")
        controller.approve(Decision(action="deny"))

        assistant = next(m for m in controller.snapshot()["messages"] if m["role"] == "assistant")
        # TS re-marks EVERY step named delete_file (index.ts:752-757). Python marks only the
        # step that was waiting: the completed `used` step before it is left alone.
        assert [s["status"] for s in assistant["steps"]] == ["used", "denied"]

    def test_approve_tool_call_does_nothing_if_no_pending_approval(self) -> None:
        t2v = FakeT2V()
        controller = make_controller(t2v)
        controller.approve(Decision(action="once"))
        assert controller.snapshot()["messages"] == []
        assert t2v.approval_calls == []


class TestApproveNamesTheCall:
    """``approve(decision, tool_call_id=...)``: the page names the call the card it drew was
    for, so a click on a card the conversation has already moved past answers nothing."""

    def _paused(self, t2v: FakeT2V) -> ChatController:
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send")]]
        t2v.approval_scripts = [[text_chunk("Sent!"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("send email")
        assert controller.snapshot()["pendingApproval"] is not None
        return controller

    def test_the_matching_tool_call_id_approves(self) -> None:
        t2v = FakeT2V()
        controller = self._paused(t2v)

        controller.approve(Decision(action="once"), tool_call_id="call_1")

        assert controller.snapshot()["pendingApproval"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "Sent!"
        assert t2v.approval_calls[0]["approval"].tool_call_id == "call_1"

    def test_a_stale_tool_call_id_answers_nothing(self) -> None:
        t2v = FakeT2V()
        controller = self._paused(t2v)
        before = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)

        controller.approve(Decision(action="deny"), tool_call_id="call_0")

        after = controller.snapshot()
        assert after["pendingApproval"] == before["pendingApproval"]
        assert t2v.approval_calls == []  # no run was started
        assert after["isLoading"] is False
        assert log.batches == []  # and nothing changed at all
        # ...and the call it really is waiting on is still answerable.
        controller.approve(Decision(action="once"), tool_call_id="call_1")
        assert controller.snapshot()["pendingApproval"] is None

    def test_omitting_the_tool_call_id_answers_whatever_is_pending(self) -> None:
        t2v = FakeT2V()
        controller = self._paused(t2v)

        controller.approve(Decision(action="once"))

        assert controller.snapshot()["pendingApproval"] is None
        assert controller.snapshot()["messages"][-1]["content"] == "Sent!"

    def test_a_tool_call_id_with_nothing_pending_is_still_a_no_op(self) -> None:
        t2v = FakeT2V()
        controller = make_controller(t2v)
        controller.approve(Decision(action="once"), tool_call_id="call_1")
        assert controller.snapshot()["messages"] == []
        assert t2v.approval_calls == []


class TestAlwaysApprove:
    def test_remembers_tools_with_always_decision(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send")]]
        t2v.approval_scripts = [[text_chunk("Sent!"), stop_chunk()]]
        controller = make_controller(t2v)

        controller.send("send email")
        controller.approve(Decision(action="always"))

        assert controller.snapshot()["alwaysAllowedTools"] == ["send_email"]

    def test_auto_approves_on_subsequent_calls_for_always_allowed_tools(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("send_email", "call_1", {}, "Send")],
            [interrupt_chunk("send_email", "call_2", {}, "Send")],
        ]
        t2v.approval_scripts = [
            [text_chunk("Sent 1"), stop_chunk()],
            [text_chunk("Sent 2"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("send first email")
        controller.approve(Decision(action="always"))

        log = BatchLog()
        controller.subscribe(log)
        controller.send("send second email")

        assert {"type": "auto-approved", "message": "Auto-approved send_email"} in log.values(
            "agentStatus"
        )
        assert controller.snapshot()["pendingApproval"] is None
        assert log.values("pendingApproval") == []  # never even set

    def test_always_allow_auto_approves_next(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("set_layout", "call_1", {"layout": "four-up"}, "Change layout")],
            [interrupt_chunk("set_layout", "call_2", {"layout": "axial"}, "Change layout")],
        ]
        t2v.approval_scripts = [
            [
                approval_result_chunk("set_layout", "always", "call_1"),
                text_chunk("Done 1"),
                stop_chunk(),
            ],
            [
                approval_result_chunk("set_layout", "once", "call_2"),
                text_chunk("Done 2"),
                stop_chunk(),
            ],
        ]
        controller = make_controller(t2v)
        controller.send("four-up please")
        controller.approve(Decision(action="always"))

        controller.send("now axial")  # no approve() call: the controller answers by itself

        snap = controller.snapshot()
        assert snap["pendingApproval"] is None
        assert snap["isLoading"] is False
        assert len(t2v.approval_calls) == 2
        auto = t2v.approval_calls[1]
        assert auto["approval"].tool_call_id == "call_2"
        assert auto["approval"].arguments == {"layout": "axial"}
        assert auto["decision"] == Decision(action="once")
        assert snap["messages"][-1]["content"] == "Done 2"
        assert snap["messages"][-2]["steps"][0]["status"] == "used"

    def test_a_malformed_always_action_is_not_remembered(self) -> None:
        """Only the literal "always" is remembered: a typo is a DENIAL, so it can
        never teach the controller to auto-approve anything. The scripted ``deny`` result is
        what the real session reports for an unapproved decision — see
        ``test_controller_recovery.py`` for that end-to-end on a real Talk2View."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        t2v.approval_scripts = [
            [approval_result_chunk("send_email", "deny", "call_1"), stop_chunk()]
        ]
        controller = make_controller(t2v)

        controller.send("send email")
        controller.approve(Decision(action="Always"))  # type: ignore[arg-type]

        snap = controller.snapshot()
        assert snap["alwaysAllowedTools"] == []
        assert snap["messages"][-1]["steps"][0]["status"] == "denied"

    def test_an_engine_approval_is_never_auto_approved_from_the_client_memory(self) -> None:
        """The memory is per CLIENT tool. A platform tool the engine gates is a different
        call with different consequences, so an "always" on the client tool of the same name
        must not answer for it."""
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("send_email", "call_1", {}, "Send")],
            [engine_interrupt_chunk("send_email", "call_2", {"to": "x"}, "Send it")],
        ]
        t2v.approval_scripts = [[text_chunk("Sent!"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("send first email")
        controller.approve(Decision(action="always"))
        assert controller.snapshot()["alwaysAllowedTools"] == ["send_email"]

        log = BatchLog()
        controller.subscribe(log)
        controller.send("send second email")

        snap = controller.snapshot()
        assert snap["pendingApproval"] == {
            "toolCallId": "call_2",
            "toolName": "send_email",
            "arguments": {"to": "x"},
            "description": "Send it",
            "kind": "engine",
        }
        assert snap["isLoading"] is False
        assert len(t2v.approval_calls) == 1  # only the first, manually approved call
        assert log.values("agentStatus") == []  # never auto-approved
        assert snap["messages"][-1]["steps"][0]["source"] == "platform"

    def test_always_on_an_engine_approval_is_not_remembered(self) -> None:
        """Answering "always" to a non-client approval approves that one call and nothing
        more: remembering it would auto-approve later engine-gated calls."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[engine_interrupt_chunk("web_search", "call_1", {"q": "x"}, "Search")]]
        t2v.approval_scripts = [[stop_chunk()]]
        controller = make_controller(t2v)

        controller.send("search the web")
        controller.approve(Decision(action="always"))

        assert controller.snapshot()["alwaysAllowedTools"] == []
        # ...but the decision itself travels unchanged, so this one call is approved.
        assert t2v.approval_calls[0]["decision"] == Decision(action="always")
        assert t2v.approval_calls[0]["approval"].kind == "engine"


# ── A tool that disallows "always" at all (save_skill) ──────────────────────────────────


class TestNoAlwaysTools:
    def test_a_no_always_tool_runs_once_but_is_never_remembered(self) -> None:
        """An "always" decision on a tool T2VTools.allows_always() refuses runs exactly
        like "once": approved this one time, never written to alwaysAllowedTools."""
        t2v = FakeT2V()
        t2v.tools.no_always.add("save_skill")
        t2v.chat_scripts = [[interrupt_chunk("save_skill", "call_1", {"name": "a"}, "Save")]]
        t2v.approval_scripts = [
            [approval_result_chunk("save_skill", "always"), text_chunk("Saved!"), stop_chunk()]
        ]
        controller = make_controller(t2v)

        controller.send("save this")
        controller.approve(Decision(action="always"))

        snap = controller.snapshot()
        assert snap["alwaysAllowedTools"] == []
        # ...but the decision itself travels unchanged, so this one call is approved --
        # exactly the same shape as test_always_on_an_engine_approval_is_not_remembered.
        assert t2v.approval_calls[0]["decision"] == Decision(action="always")
        assistants = [m for m in snap["messages"] if m["role"] == "assistant"]
        assert assistants[0]["steps"][0]["status"] == "used"

    def test_a_no_always_tool_is_never_auto_approved_on_the_next_call(self) -> None:
        """Even granting the "always" memory should never help here: a fresh card must
        show up for the very next call, with no auto-approved status posted."""
        t2v = FakeT2V()
        t2v.tools.no_always.add("save_skill")
        t2v.chat_scripts = [
            [interrupt_chunk("save_skill", "call_1", {"name": "a"}, "Save")],
            [interrupt_chunk("save_skill", "call_2", {"name": "b"}, "Save")],
        ]
        t2v.approval_scripts = [[text_chunk("Saved 1"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("save the first one")
        controller.approve(Decision(action="always"))

        log = BatchLog()
        controller.subscribe(log)
        controller.send("save the second one")

        snap = controller.snapshot()
        assert snap["pendingApproval"] == {
            "toolCallId": "call_2",
            "toolName": "save_skill",
            "arguments": {"name": "b"},
            "description": "Save",
        }
        assert {"type": "auto-approved", "message": "Auto-approved save_skill"} not in log.values(
            "agentStatus"
        )
        assert len(t2v.approval_calls) == 1  # only the first, manually approved call

    def test_a_normal_tool_still_auto_approves_after_always_regression(self) -> None:
        """Regression: a tool T2VTools.allows_always() does not name (the default for
        every host tool, ``FakeTools.no_always`` empty) keeps today's behaviour --
        unaffected by the no-always machinery above."""
        t2v = FakeT2V()
        assert t2v.tools.no_always == set()
        t2v.chat_scripts = [
            [interrupt_chunk("send_email", "call_1", {}, "Send")],
            [interrupt_chunk("send_email", "call_2", {}, "Send")],
        ]
        t2v.approval_scripts = [
            [text_chunk("Sent 1"), stop_chunk()],
            [text_chunk("Sent 2"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("send first email")
        controller.approve(Decision(action="always"))

        log = BatchLog()
        controller.subscribe(log)
        controller.send("send second email")

        assert controller.snapshot()["alwaysAllowedTools"] == ["send_email"]
        assert {"type": "auto-approved", "message": "Auto-approved send_email"} in log.values(
            "agentStatus"
        )
        assert controller.snapshot()["pendingApproval"] is None


class TestResultOnStep:
    """A builtin installed with result_on_step (save_skill) carries its handler's
    sentence on the approval_result event's `content` (sessions.py); the reducer records
    it as the step's `result`, the field the page's settled line reads. A host tool's
    approval_result has no content, so its step keeps no result, as before."""

    def _run(self, tool: str, result: "ChatEvent") -> dict:  # type: ignore[type-arg]
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk(tool, "call_1", {"name": "a"}, "Save")]]
        t2v.approval_scripts = [[result, text_chunk("Done."), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("go")
        controller.approve(Decision(action="once"))
        assistants = [m for m in controller.snapshot()["messages"] if m["role"] == "assistant"]
        return assistants[0]["steps"][0]  # type: ignore[no-any-return]

    def test_a_saved_skill_sentence_lands_on_the_step(self) -> None:
        sentence = "Saved skill 'a'. It applies from the user's next message."
        step = self._run(
            "save_skill",
            ChatEvent(type="approval_result", tool_name="save_skill", tool_call_id="call_1",
                      decision="once", content=sentence),
        )
        assert step["status"] == "used"
        assert step["result"] == sentence

    def test_a_not_saved_sentence_lands_on_the_step(self) -> None:
        sentence = "Not saved. The user already has a skill named 'a'; pass replaces='a'."
        step = self._run(
            "save_skill",
            ChatEvent(type="approval_result", tool_name="save_skill", tool_call_id="call_1",
                      decision="once", content=sentence),
        )
        assert step["result"] == sentence

    def test_a_host_tool_step_keeps_no_result(self) -> None:
        step = self._run("send_email", approval_result_chunk("send_email", "once", "call_1"))
        assert step["status"] == "used"
        assert "result" not in step


class TestConversationHistoryIntegrity:
    def test_rolls_back_orphan_user_turn_when_deny_resume_stream_errors(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [interrupt_chunk("delete_file", "call_1", {"path": "/x"}, "Delete")],
            [text_chunk("ok"), stop_chunk()],
        ]
        t2v.approval_scripts = [[error_chunk("Server down")]]
        controller = make_controller(t2v)

        controller.send("delete x")
        controller.approve(Decision(action="deny"))
        assert controller.snapshot()["error"] == "Server down"
        controller.send("follow up")

        assert t2v.chat_calls[1]["message"] == "follow up"
        assert t2v.chat_calls[1]["history"] == []


class TestApprovalDetails:
    def test_an_approval_never_parks_a_thread(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        workers = []

        def executor(fn):  # type: ignore[no-untyped-def]
            worker = threading.Thread(target=fn, daemon=True)
            workers.append(worker)
            worker.start()

        controller = make_controller(t2v, executor=executor)
        controller.send("send email")

        assert controller.wait_idle(2.0) is True  # idle although an approval is pending
        workers[0].join(2.0)
        assert workers[0].is_alive() is False
        assert controller.snapshot()["pendingApproval"] is not None

    def test_send_while_an_approval_is_pending_raises(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {}, "Send")]]
        controller = make_controller(t2v)
        controller.send("send email")
        with pytest.raises(ControllerBusyError):
            controller.send("something else")

    def test_feedback_and_updated_input_travel_with_the_decision(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("send_email", "call_1", {"to": "a@b.com"}, "Send")]]
        t2v.approval_scripts = [[stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("send email")
        decision = Decision(action="once", updated_input={"to": "c@d.com"})
        controller.approve(decision)
        assert t2v.approval_calls[0]["decision"] is decision

    def test_approval_result_prefers_the_tool_call_id(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [
                tool_call_chunk("write", "call_1", {"n": 1}),
                interrupt_chunk("write", "call_2", {"n": 2}, "Write"),
            ]
        ]
        t2v.approval_scripts = [[approval_result_chunk("write", "deny", "call_2"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("write twice")
        controller.approve(Decision(action="deny", feedback="not that one"))
        steps = controller.snapshot()["messages"][1]["steps"]
        assert [(s["toolCallId"], s["status"]) for s in steps] == [
            ("call_1", "used"),
            ("call_2", "denied"),
        ]

    def test_approval_result_with_an_unknown_tool_call_id_marks_nothing(self) -> None:
        """An id names exactly one call, so with an id present the tool NAME is never a
        fallback: a result for a call this turn never saw must not re-mark another one."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[interrupt_chunk("write", "call_1", {"n": 1}, "Write")]]
        t2v.approval_scripts = [[approval_result_chunk("write", "deny", "call_9"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("write once")
        controller.approve(Decision(action="deny"))
        steps = controller.snapshot()["messages"][-1]["steps"]
        assert [(s["toolCallId"], s["status"]) for s in steps] == [("call_1", "running")]

    def test_replaying_batches_across_an_approval_reproduces_the_snapshot(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("Let me "), interrupt_chunk("t", "call_1", {}, "T")]]
        t2v.approval_scripts = [
            [approval_result_chunk("t", "once"), text_chunk("ok"), stop_chunk()]
        ]
        controller = make_controller(t2v)
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)
        controller.send("go")
        controller.approve(Decision(action="once"))
        assert log.replay(start) == controller.snapshot()
