"""ChatController skeleton: initial state, snapshot, subscribe, listeners, the reducer seam,
close."""

from __future__ import annotations

from typing import Any, List

import pytest

from talk2view._reducer import _Cursor
from talk2view.cancel import CancelToken
from talk2view.controller import (
    DEMO_LIMIT_REACHED,
    Capabilities,
    ChatController,
    ControllerClosedError,
    Decision,
)
from talk2view.state import OpBatch, SeqGapError, initial_snapshot
from talk2view.types import ChatEvent

from .controller_fakes import BatchLog, FakeT2V, make_controller, text_chunk


def _poke(controller: ChatController, key: str, value: Any) -> None:
    """White-box mutation: what every public method does — mutate under the lock, flush."""
    with controller._lock:
        controller._rec.set([key], value)
    controller._flush()


class TestInitialState:
    """Ported from chat-state.test.ts › initial state."""

    def test_starts_with_empty_messages(self) -> None:
        assert make_controller().snapshot()["messages"] == []

    def test_starts_with_is_loading_false(self) -> None:
        assert make_controller().snapshot()["isLoading"] is False

    def test_starts_with_no_error(self) -> None:
        assert make_controller().snapshot()["error"] is None

    def test_starts_with_no_pending_approval(self) -> None:
        assert make_controller().snapshot()["pendingApproval"] is None

    def test_starts_with_no_agent_status(self) -> None:
        assert make_controller().snapshot()["agentStatus"] is None

    def test_starts_with_no_thread_id(self) -> None:
        assert make_controller().snapshot()["threadId"] is None

    def test_starts_with_empty_always_allowed_tools(self) -> None:
        assert make_controller().snapshot()["alwaysAllowedTools"] == []


class TestSnapshot:
    def test_initial_snapshot_equals_the_state_factory(self) -> None:
        assert make_controller().snapshot() == initial_snapshot()

    def test_is_anonymous_is_read_from_auth(self) -> None:
        t2v = FakeT2V()
        t2v.auth.anonymous = True
        assert make_controller(t2v).snapshot()["isAnonymous"] is True

    def test_is_anonymous_false_when_auth_has_no_probe(self) -> None:
        t2v = FakeT2V()
        t2v.auth = object()  # type: ignore[assignment]  # an auth with no is_anonymous()
        assert make_controller(t2v).snapshot()["isAnonymous"] is False

    def test_a_raising_probe_does_not_break_construction(self) -> None:
        t2v = FakeT2V()
        t2v.auth.is_anonymous = lambda: 1 / 0  # type: ignore[method-assign, assignment]
        assert make_controller(t2v).snapshot()["isAnonymous"] is False

    def test_snapshot_is_a_copy(self) -> None:
        controller = make_controller()
        controller.snapshot()["alwaysAllowedTools"].append("leak")
        assert controller.snapshot()["alwaysAllowedTools"] == []

    def test_decision_and_capabilities_are_importable_from_the_controller_module(self) -> None:
        assert Decision(action="once").feedback is None
        assert Capabilities().approval is False


class TestPublicExports:
    def test_controller_and_state_names_are_exported_from_the_package(self) -> None:
        import talk2view

        for name in (
            "ChatController",
            "ControllerBusyError",
            "ControllerClosedError",
            "ChatSnapshot",
            "DisplayMessage",
            "Op",
            "OpBatch",
            "PendingApproval",
            "ToolStep",
            "apply_batch",
            # What a host needs to use the batch protocol at all: the errors `apply_batch`
            # raises, and the two constants the controller's own behaviour is stated in.
            "OpError",
            "SeqGapError",
            "CANCELLED_APPROVAL_FEEDBACK",
            "DEMO_LIMIT_REACHED",
        ):
            assert name in talk2view.__all__, name
            assert hasattr(talk2view, name), name
        assert talk2view.ChatController is ChatController
        assert talk2view.SeqGapError is SeqGapError
        assert issubclass(talk2view.SeqGapError, talk2view.OpError)
        assert talk2view.DEMO_LIMIT_REACHED == DEMO_LIMIT_REACHED


class TestSubscribe:
    def test_subscribe_unsubscribe(self) -> None:
        controller = make_controller()
        log = BatchLog()
        unsubscribe = controller.subscribe(log)

        _poke(controller, "error", "boom")
        assert log.seqs() == [1]
        assert log.batches[0]["ops"] == [{"type": "set", "path": ["error"], "value": "boom"}]

        unsubscribe()
        _poke(controller, "error", "again")
        assert log.seqs() == [1]
        assert controller.snapshot()["seq"] == 2  # state still advances without subscribers

    def test_the_same_callback_can_be_registered_twice_and_dropped_once(self) -> None:
        """A registration is identified by object, not by value: the same callback with the
        same flag can be subscribed twice, and each unsubscribe drops only its own."""
        controller = make_controller()
        log = BatchLog()
        controller.subscribe(log)
        second = controller.subscribe(log)

        _poke(controller, "error", "boom")
        assert log.seqs() == [1, 1]  # both registrations were delivered to

        second()  # the LATER one: a value-wise match would find the earlier one instead
        _poke(controller, "error", "again")

        assert log.seqs() == [1, 1, 2]  # ...and the survivor still is

    def test_replaying_batches_onto_the_first_snapshot_gives_the_current_one(self) -> None:
        controller = make_controller()
        start = controller.snapshot()
        log = BatchLog()
        controller.subscribe(log)
        _poke(controller, "error", "boom")
        _poke(controller, "threadId", "thread_9")
        assert log.replay(start) == controller.snapshot()

    def test_a_raising_subscriber_does_not_break_the_others(self) -> None:
        controller = make_controller()
        log = BatchLog()

        def bad(batch: OpBatch) -> None:
            raise RuntimeError("subscriber bug")

        controller.subscribe(bad)
        controller.subscribe(log)
        _poke(controller, "error", "boom")
        assert log.seqs() == [1]

    def test_no_batch_when_nothing_changed(self) -> None:
        controller = make_controller()
        log = BatchLog()
        controller.subscribe(log)
        controller.clear_error()  # error is already None
        assert log.batches == []

    def test_clear_error_emits_one_set_op(self) -> None:
        controller = make_controller()
        _poke(controller, "error", "boom")
        log = BatchLog()
        controller.subscribe(log)
        controller.clear_error()
        assert controller.snapshot()["error"] is None
        assert log.batches == [
            {"seq": 2, "ops": [{"type": "set", "path": ["error"], "value": None}]}
        ]


class TestListeners:
    def test_on_returns_an_unsubscribe(self) -> None:
        controller = make_controller()
        hits: List[int] = []
        off = controller.on(DEMO_LIMIT_REACHED, lambda: hits.append(1))

        with controller._lock:
            controller._notices.append(DEMO_LIMIT_REACHED)
        controller._flush()
        assert hits == [1]

        off()
        with controller._lock:
            controller._notices.append(DEMO_LIMIT_REACHED)
        controller._flush()
        assert hits == [1]

    def test_a_raising_listener_still_lets_the_batch_through(self) -> None:
        controller = make_controller()
        log = BatchLog()
        controller.subscribe(log)

        def bad() -> None:
            raise RuntimeError("listener bug")

        controller.on(DEMO_LIMIT_REACHED, bad)

        with controller._lock:
            controller._rec.set(["error"], "boom")
            controller._notices.append(DEMO_LIMIT_REACHED)
        controller._flush()
        assert log.seqs() == [1]


class TestTheReducerSeam:
    """The shell's side of the reducer contract: `_reduce_locked` is the only call site of
    `TurnReducer.reduce`, and it absorbs the cursor in a `finally`, so the two outputs the
    reducer hands back out of band -- a failed turn and a notice -- reach the shell even when
    a handler raises after recording one."""

    def test_absorb_moves_the_cursor_outputs_exactly_once(self) -> None:
        controller = make_controller()
        cursor = _Cursor(assistant_id="msg_1", turn_failed=True, notices=[DEMO_LIMIT_REACHED])

        with controller._lock:
            controller._absorb(cursor)
            controller._absorb(cursor)  # the cursor was drained: nothing to move a second time

        assert controller._turn_failed is True
        assert controller._notices == [DEMO_LIMIT_REACHED]
        assert cursor.notices == []

    def test_reduce_locked_absorbs_even_when_a_handler_raises(self) -> None:
        controller = make_controller()
        cursor = _Cursor(assistant_id="msg_1")

        def raise_after_recording_a_notice(event: ChatEvent, cur: _Cursor) -> None:
            cur.turn_failed = True
            cur.notices.append(DEMO_LIMIT_REACHED)
            raise RuntimeError("handler bug")

        controller._reducer.reduce = raise_after_recording_a_notice  # type: ignore[method-assign]

        with controller._lock:
            with pytest.raises(RuntimeError):
                controller._reduce_locked(text_chunk("x"), cursor)

        assert controller._turn_failed is True
        assert controller._notices == [DEMO_LIMIT_REACHED]


class TestLifecycle:
    def test_wait_idle_is_true_when_nothing_runs(self) -> None:
        assert make_controller().wait_idle(0.01) is True

    def test_close_drops_subscribers_and_refuses_new_ones(self) -> None:
        controller = make_controller()
        log = BatchLog()
        controller.subscribe(log)
        controller.close()
        _poke(controller, "error", "late")
        assert log.batches == []
        with pytest.raises(ControllerClosedError):
            controller.subscribe(log)

    def test_close_cancels_the_active_runs_token(self) -> None:
        """A host that stops pumping must be able to unblock a parked worker by closing:
        ``run_on_main`` waits untimed, and only the token can wake it."""
        controller = make_controller()
        token = CancelToken()
        with controller._lock:
            controller._active_run = 1
            controller._cancel_token = token

        controller.close()

        assert token.cancelled is True
        assert controller._active_run is None
        assert controller._cancel_token is None
        assert controller.wait_idle(0.01) is True

    def test_close_is_idempotent_without_a_run(self) -> None:
        controller = make_controller()
        controller.close()
        controller.close()

    def test_model_and_capabilities_become_talk2view_chat_defaults(self) -> None:
        t2v = FakeT2V()
        capabilities = Capabilities(approval=True)
        make_controller(t2v, model="gpt-4o", capabilities=capabilities)
        assert t2v.defaults == [{"model": "gpt-4o", "capabilities": capabilities}]

    def test_chat_defaults_are_left_alone_when_neither_is_given(self) -> None:
        t2v = FakeT2V()
        make_controller(t2v, system_prompt="Be brief")
        assert t2v.defaults == []

    def test_dispatcher_defaults_to_the_tools_dispatcher(self) -> None:
        t2v = FakeT2V()
        assert make_controller(t2v)._dispatcher is t2v.tools.dispatcher
