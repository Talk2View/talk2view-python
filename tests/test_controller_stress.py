"""Opt-in stress probe: seq order and replay == snapshot under random concurrent commands.

Three actor threads fire ``send``/``approve``/``cancel``/``clear``/``retry``/``clear_error``
and ``set_skills_state`` (a skills change asking for a session reset, which recycles the
session when the controller is idle) at one controller while a random stream (text, client
tools run through ``run_on_main``, approvals, errors, early ends) plays on the default worker
executor and the test thread pumps a ``QueueDispatcher`` as a host main thread would. Two
subscribers check every batch: one on the emitting thread, one delivered on main. Any seq gap,
any replay mismatch, a run that never goes idle, or an actor that raises fails the round --
except ``ControllerBusyError`` from ``send``/``retry``/``edit``, the calls that refuse while
busy. ``approve()``, ``cancel()`` and ``set_skills_state()`` promise never to raise it, so
from them it is a violation like any other.

It is not part of the default run (timing-dependent by design; ~0.7 s a round)::

    T2V_STRESS=1 uv run pytest tests/test_controller_stress.py -v
"""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Any, Iterator, List, Optional

import pytest

from talk2view.cancel import Cancelled, CancelToken
from talk2view.controller import ChatController, ControllerBusyError, ControllerClosedError
from talk2view.dispatch import QueueDispatcher, run_on_main
from talk2view.state import OpBatch, SeqGapError, SkillsState
from talk2view.types import ChatEvent, Decision

from .controller_fakes import BatchLog, FakeT2V

pytestmark = pytest.mark.stress

ROUNDS = 10
ROUND_SECONDS = 0.6
#: The only calls allowed to refuse with ``ControllerBusyError`` (``edit`` is not fired here).
BUSY_ALLOWED = ("send", "retry", "edit")


def reset_state(n: int) -> SkillsState:
    """A skills change asking for a session reset. ``RandomT2V`` has no ``skills``, so the
    controller records this state as given and recycles when it is idle."""
    return {
        "local": [],
        "platform": None,
        "registered": ["s{}".format(n)],  # varies, so most calls record an op
        "available": True,
        "needsSessionReset": True,
    }


class RandomT2V(FakeT2V):
    def __init__(self, dispatcher: QueueDispatcher, rng: random.Random) -> None:
        super().__init__(dispatcher)
        self.rng = rng
        self.recycled = 0

    def _gen(self, cancel: Optional[CancelToken], resume: bool) -> Iterator[ChatEvent]:
        rng = self.rng
        try:
            if resume:
                yield ChatEvent(
                    type="approval_result", tool_name="t", tool_call_id="c", decision="once"
                )
            for i in range(rng.randint(0, 6)):
                if cancel is not None and cancel.cancelled:
                    raise Cancelled()
                r = rng.random()
                if r < 0.55:
                    yield ChatEvent(type="text", content="w{} ".format(i))
                elif r < 0.7:
                    yield ChatEvent(
                        type="tool_call",
                        tool_name="set_layout",
                        tool_call_id="k{}".format(i),
                        arguments={"a": 1},
                    )
                    # As Talk2View does: the handler runs on the main thread, from the worker.
                    run_on_main(self.tools.dispatcher, lambda: None, cancel)
                elif r < 0.8:
                    yield ChatEvent(type="status", status="thinking", message="...")
                elif r < 0.9:
                    yield ChatEvent(
                        type="approval_required",
                        tool_name=rng.choice(["t", "u"]),
                        tool_call_id="c",
                        arguments={"x": 1},
                        description="d",
                        kind="client",
                    )
                    return
                else:
                    yield ChatEvent(type="error", message="boom")
                    return
                time.sleep(rng.random() * 0.002)
            if rng.random() < 0.9:
                yield ChatEvent(type="done", thread_id="th")
        except Cancelled:
            return

    def chat(self, message: str, *args: Any, **kwargs: Any) -> Iterator[ChatEvent]:  # type: ignore[override]
        self.chat_calls.append({"cancel": kwargs.get("cancel")})
        return self._gen(kwargs.get("cancel"), False)

    def respond_to_approval(  # type: ignore[override]
        self, approval: ChatEvent, decision: Any, cancel: Optional[CancelToken] = None
    ) -> Iterator[ChatEvent]:
        return self._gen(cancel, True)

    def clear_session(self) -> None:
        time.sleep(self.rng.random() * 0.003)
        self.cleared += 1

    def recycle_session(self) -> None:
        time.sleep(self.rng.random() * 0.003)
        self.recycled += 1


class Checker(BatchLog):
    def __init__(self) -> None:
        super().__init__()
        self.violations: List[str] = []
        self.last = 0

    def __call__(self, batch: OpBatch) -> None:
        super().__call__(batch)
        if batch["seq"] != self.last + 1:
            self.violations.append("seq {} after {}".format(batch["seq"], self.last))
        self.last = batch["seq"]


def one_round(seed: int) -> List[str]:
    rng = random.Random(seed)  # noqa: S311 — deterministic per seed, not cryptography
    dispatcher = QueueDispatcher()  # this thread is "main"
    t2v = RandomT2V(dispatcher, rng)
    controller = ChatController(t2v)  # type: ignore[arg-type]  # the DEFAULT executor
    start = controller.snapshot()
    worker_sub = Checker()
    main_sub = Checker()
    controller.subscribe(worker_sub)
    controller.subscribe(main_sub, deliver_on_main=True)

    stop = threading.Event()
    errors: List[str] = []

    def actor(tid: int) -> None:
        arng = random.Random(seed * 100 + tid)  # noqa: S311
        while not stop.is_set():
            r = arng.random()
            command = "snapshot"
            try:
                if r < 0.45:
                    command = "send"
                    controller.send("hi {}".format(tid))
                elif r < 0.6:
                    if controller.snapshot()["pendingApproval"] is not None:
                        command = "approve"
                        controller.approve(
                            Decision(action=arng.choice(["once", "always", "deny"]))
                        )
                elif r < 0.75:
                    command = "cancel"
                    controller.cancel()
                elif r < 0.85:
                    command = "clear"
                    controller.clear()
                elif r < 0.92:
                    command = "retry"
                    controller.retry()
                elif r < 0.97:
                    command = "set_skills_state"
                    controller.set_skills_state(reset_state(arng.randint(0, 3)))
                else:
                    command = "clear_error"
                    controller.clear_error()
            except ControllerBusyError:
                if command not in BUSY_ALLOWED:
                    errors.append("{}() raised ControllerBusyError".format(command))
            except ControllerClosedError:
                errors.append("closed?!")
            except Exception as exc:
                errors.append("actor raised: {!r}".format(exc))
            time.sleep(arng.random() * 0.003)

    actors = [threading.Thread(target=actor, args=(i,), daemon=True) for i in range(3)]
    for a in actors:
        a.start()
    deadline = time.monotonic() + ROUND_SECONDS
    while time.monotonic() < deadline:
        dispatcher.pump()
        time.sleep(0.001)
    stop.set()
    for a in actors:
        a.join(5.0)
        if a.is_alive():
            return ["DEADLOCK: actor still alive"]
    # Settle: drop any pending approval or run, wait idle, drain the main queue.
    controller.clear()
    t_end = time.monotonic() + 5.0
    while not controller.wait_idle(0.01):
        dispatcher.pump()
        if time.monotonic() > t_end:
            return ["DEADLOCK: never idle"]
    while dispatcher.pump():
        pass
    snap = controller.snapshot()
    problems: List[str] = []
    if errors:
        problems.append("errors={}".format(errors[:3]))
    if worker_sub.violations:
        problems.append("worker seq violations {}".format(worker_sub.violations[:3]))
    if main_sub.violations:
        problems.append("main seq violations {}".format(main_sub.violations[:3]))
    try:
        if worker_sub.replay(start) != snap:
            problems.append("worker replay != snapshot")
        if main_sub.replay(start) != snap:
            problems.append("main replay != snapshot")
    except SeqGapError as exc:
        problems.append("replay gap {}".format(exc))
    if snap["isLoading"]:
        problems.append("isLoading stuck")
    controller.close()
    return problems


@pytest.mark.skipif(os.environ.get("T2V_STRESS") != "1", reason="set T2V_STRESS=1 to run")
@pytest.mark.parametrize("seed", range(ROUNDS))
def test_random_concurrent_commands_keep_seq_order_and_replay(seed: int) -> None:
    assert one_round(seed) == []
