"""Error events, orphan-turn rollback and the anonymous demo limit."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest

from talk2view.controller import DEMO_LIMIT_REACHED
from talk2view.types import ChatMessage

from .controller_fakes import (
    BatchLog,
    FakeT2V,
    error_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
    tool_call_chunk,
)


class TestSendMessageErrors:
    def test_sets_error_on_stream_error_event(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("partial"), error_chunk("Something went wrong")]]
        controller = make_controller(t2v)
        controller.send("test")
        snap = controller.snapshot()
        assert snap["error"] == "Something went wrong"
        assert snap["isLoading"] is False
        # The engine mirrors the message into delta.content; it must never become chat text.
        assert snap["messages"][1]["content"] == "partial"


class TestConversationHistoryIntegrity:
    def test_rolls_back_orphan_user_turn_when_stream_errors_before_any_text(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("Network failure")], [text_chunk("ok"), stop_chunk()]]
        controller = make_controller(t2v)

        controller.send("failed message")
        assert controller.snapshot()["error"] == "Network failure"
        controller.send("next message")

        assert t2v.chat_calls[1]["message"] == "next message"
        assert t2v.chat_calls[1]["history"] == []

    def test_preserves_successful_turn_when_a_later_turn_errors(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [text_chunk("good reply"), stop_chunk()],
            [error_chunk("boom")],
            [text_chunk("fine"), stop_chunk()],
        ]
        controller = make_controller(t2v)

        controller.send("good message")
        controller.send("bad message")
        assert controller.snapshot()["error"] == "boom"
        controller.send("third")

        assert t2v.chat_calls[2]["history"] == [
            ChatMessage(role="user", content="good message"),
            ChatMessage(role="assistant", content="good reply"),
        ]

    def test_orphan_user_turn_rolled_back(self) -> None:
        """Same rule when the stream RAISES (a transport failure) instead of sending an error
        chunk: no assistant text -> the user turn leaves the LLM history."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[RuntimeError("connection reset")], [stop_chunk()]]
        controller = make_controller(t2v)

        controller.send("lost")
        snap = controller.snapshot()
        assert snap["error"] == "connection reset"
        # The display keeps the user's bubble; the empty assistant segment is dropped.
        assert [m["role"] for m in snap["messages"]] == ["user"]
        controller.send("again")

        assert t2v.chat_calls[1]["history"] == []

    def test_partial_text_before_an_error_stays_in_history(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("half"), error_chunk("boom")], [stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("q")
        controller.send("q2")
        assert t2v.chat_calls[1]["history"] == [
            ChatMessage(role="user", content="q"),
            ChatMessage(role="assistant", content="half"),
        ]

    def test_executor_refusal_rolls_back_orphan_turn(self) -> None:
        """The executor raising on submit (e.g. a shut-down pool) must not leave the refused
        turn's user message stuck in ``_history`` for the next send to replay."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()]]
        attempt = {"n": 0}

        def flaky_executor(fn: object) -> None:
            attempt["n"] += 1
            if attempt["n"] == 1:
                raise RuntimeError("executor refused")
            fn()  # type: ignore[operator]

        controller = make_controller(t2v, executor=flaky_executor)

        with pytest.raises(RuntimeError, match="executor refused"):
            controller.send("first")
        assert controller.snapshot()["error"] == "executor refused"
        # The refused turn never reached FakeT2V.chat() at all.
        assert t2v.chat_calls == []

        controller.send("second")
        assert t2v.chat_calls[0]["history"] == []

    def test_failure_reaching_runs_catch_all_rolls_back_and_sets_turn_failed(self) -> None:
        """Force an exception out of finalize-adjacent code in `_drain` so it escapes to
        `_run`'s own `except Exception` -- the third error path, distinct from a stream
        raising (caught inside `_consume`) and the executor refusing the job (caught inside
        `_launch`). Least invasive seam: monkeypatch the bound `_finalize` method itself,
        since that is the only thing `_drain` calls between a clean `_consume` and a clean
        `_end_run`."""
        t2v = FakeT2V()
        t2v.chat_scripts = [[stop_chunk()], [stop_chunk()]]
        controller = make_controller(t2v)

        def broken_finalize(assistant_id: str, *, cancelled: bool) -> None:
            raise RuntimeError("finalize exploded")

        controller._finalize = broken_finalize  # type: ignore[method-assign]

        controller.send("boom")

        assert controller._turn_failed is True
        snap = controller.snapshot()
        assert snap["error"] == "finalize exploded"
        assert snap["isLoading"] is False

        del controller._finalize  # restore the real bound method for the next turn
        controller.send("again")
        assert t2v.chat_calls[1]["history"] == []


class TestAFailureBeforeLaunch:
    """An exception after the run started and before it was launched (the host handed
    ``send`` something that is not an ``Attachment``, or anything else in the turn's
    preparation) used to leave ``active_run`` set for good: ``wait_idle`` never returned
    True and every later ``send`` was refused as busy."""

    def test_a_malformed_attachment_ends_the_run_and_is_raised(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[text_chunk("ok"), stop_chunk()]]
        controller = make_controller(t2v)
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)
        not_an_attachment = SimpleNamespace(id="a1")  # no filename, mime_type, size_bytes
        with pytest.raises(AttributeError, match="filename"):
            controller.send("look", [not_an_attachment])  # type: ignore[list-item]
        assert controller.wait_idle(1.0)
        snap = controller.snapshot()
        assert snap["isLoading"] is False and "filename" in (snap["error"] or "")
        assert log.replay(start) == snap  # the run's end was delivered, as any run end is
        assert t2v.chat_calls == []
        controller.send("again")
        assert [m["content"] for m in controller.snapshot()["messages"]] == ["again", "ok"]
        assert t2v.chat_calls[0]["history"] == []

    @pytest.mark.parametrize("start", ["send", "retry", "edit"])
    def test_a_failure_after_the_turn_was_recorded_ends_the_run(
        self, monkeypatch: pytest.MonkeyPatch, start: str
    ) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [
            [tool_call_chunk("measure", "c1", {}), stop_chunk()],  # tools only: no text
            [text_chunk("fine"), stop_chunk()],
        ]
        controller = make_controller(t2v)
        controller.send("tools")  # leaves its user entry in the history, with no reply
        user_id = controller.snapshot()["messages"][0]["id"]
        log = BatchLog()
        controller.subscribe(log)
        before = controller.snapshot()

        def exploded(text: str, files: Any) -> Any:
            raise RuntimeError("content exploded")

        monkeypatch.setattr("talk2view.controller.build_user_content", exploded)
        with pytest.raises(RuntimeError, match="content exploded"):
            if start == "send":
                controller.send("more")
            elif start == "retry":
                controller.retry()
            else:
                controller.edit(user_id, "edited")
        assert controller.wait_idle(1.0)
        snap = controller.snapshot()
        assert snap["isLoading"] is False and snap["error"] == "content exploded"
        assert snap["messages"][-1]["role"] == "user"  # the empty reply segment is gone
        assert log.replay(before) == snap
        monkeypatch.undo()
        controller.send("third")
        history = [(m.role, m.content) for m in t2v.chat_calls[-1]["history"]]
        # Only what reached the engine: the failed turn left nothing, and took nothing.
        assert history == ([] if start in ("retry", "edit") else [("user", "tools")])


class TestClearError:
    def test_clears_the_error(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("boom")]]
        controller = make_controller(t2v)
        controller.send("test")
        assert controller.snapshot()["error"] == "boom"

        controller.clear_error()
        assert controller.snapshot()["error"] is None

    def test_the_next_send_clears_the_error(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("boom")], [text_chunk("ok"), stop_chunk()]]
        controller = make_controller(t2v)
        controller.send("a")
        controller.send("b")
        assert controller.snapshot()["error"] is None


class TestDemoLimit:
    def test_budget_exceeded_anonymous_emits_demo_limit(self) -> None:
        t2v = FakeT2V()
        t2v.auth.anonymous = True
        t2v.chat_scripts = [[error_chunk("Budget exceeded", "budget_exceeded")], [stop_chunk()]]
        controller = make_controller(t2v)
        hits: List[str] = []
        controller.on(DEMO_LIMIT_REACHED, lambda: hits.append("limit"))

        controller.send("one more")

        assert hits == ["limit"]
        snap = controller.snapshot()
        assert snap["error"] is None  # a signup prompt, not a raw error
        assert snap["isLoading"] is False
        controller.send("after signup")
        assert t2v.chat_calls[1]["history"] == []  # the refused turn was rolled back too

    def test_budget_exceeded_for_a_signed_in_user_is_an_ordinary_error(self) -> None:
        t2v = FakeT2V()
        t2v.chat_scripts = [[error_chunk("Budget exceeded", "budget_exceeded")]]
        controller = make_controller(t2v)
        hits: List[str] = []
        controller.on(DEMO_LIMIT_REACHED, lambda: hits.append("limit"))

        controller.send("one more")

        assert hits == []
        assert controller.snapshot()["error"] == "Budget exceeded"
