"""ChatController.follow_identity and exchange_conversation: what a conversation list needs
to keep each end-user's conversations whole across sign-ins, sign-outs and switches.

A real ``Talk2View`` against the stateful fake engine, as ``test_controller_identity``.
"""

from __future__ import annotations

import threading
import time
from typing import Iterator, List

import pytest

from talk2view import MemoryStorage, Talk2View
from talk2view.controller import ChatController, ControllerClosedError, IdentityChange

from .controller_fakes import inline_executor
from .fake_engine import FakeEngine


@pytest.fixture
def t2v(engine: FakeEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=engine.url(), storage=MemoryStorage())
    yield client
    client.close()


@pytest.fixture
def controller(t2v: Talk2View) -> Iterator[ChatController]:
    chat = ChatController(t2v, executor=inline_executor)
    yield chat
    chat.close()


def contents(snapshot: object) -> List[str]:
    assert isinstance(snapshot, dict)
    return [m["content"] for m in snapshot["messages"]]


def follow(controller: ChatController) -> "tuple[IdentityChange, List[IdentityChange]]":
    heard: List[IdentityChange] = []
    now, _off = controller.follow_identity(heard.append)
    return now, heard


def test_follow_identity_reports_the_identity_now_and_every_change_in_epoch_order(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    now, heard = follow(controller)
    assert (now.user_id, now.anonymous, now.forgotten) == ("user-ana", False, None)
    t2v.auth.login("bob@example.com", "pw")
    t2v.auth.logout()
    assert [(c.epoch, c.user_id) for c in heard] == [
        (now.epoch + 1, "user-bob"),
        (now.epoch + 2, None),
    ]


def test_a_sign_out_reports_the_conversation_it_dropped_exactly_as_it_stood(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    controller.send("ana's question")
    assert controller.wait_idle(5.0)
    _now, heard = follow(controller)
    t2v.auth.logout()
    assert len(heard) == 1 and heard[0].user_id is None
    assert contents(heard[0].forgotten) == ["ana's question", "ok"]
    assert heard[0].forgotten is not None and len(heard[0].forgotten["history"]) == 2
    assert controller.snapshot()["messages"] == []  # and it is gone from the screen


def test_a_switch_mid_turn_reports_what_had_streamed_so_far(
    engine: FakeEngine, t2v: Talk2View
) -> None:
    chat = ChatController(t2v)  # a worker thread: the turn parks mid-reply
    try:
        t2v.auth.login("ana@example.com", "pw")
        _now, heard = follow(chat)
        chat.send("slow please")
        assert engine.slow_started.wait(5.0)
        t2v.auth.login("bob@example.com", "pw")
        assert len(heard) == 1 and heard[0].forgotten is not None
        assert contents(heard[0].forgotten)[0] == "slow please"
        assert "isStreaming" not in heard[0].forgotten["messages"][-1]
    finally:
        engine.release_slow.set()
        chat.close()


def test_a_sign_in_from_signed_out_keeps_the_conversation_and_says_so(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.start_anonymous()
    controller.send("as a guest")
    assert controller.wait_idle(5.0)
    _now, heard = follow(controller)
    t2v.auth.signup("new@example.com", "pw")  # the fake engine 409s: signs in instead
    assert len(heard) == 1 and heard[0].user_id == "user-new" and heard[0].forgotten is None
    assert contents(controller.snapshot()) == ["as a guest", "ok"]


def test_carry_false_makes_a_sign_in_from_a_guest_drop_the_conversation_and_report_it(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.start_anonymous()
    controller.send("an earlier visitor's")
    assert controller.wait_idle(5.0)
    now, heard = follow(controller)
    assert controller.exchange_conversation(None, epoch=now.epoch, carry=False) is not None
    t2v.auth.signup("new@example.com", "pw")
    assert len(heard) == 1 and contents(heard[0].forgotten) == ["an earlier visitor's", "ok"]
    assert controller.snapshot()["messages"] == []
    # Dropping it resets the flag: a conversation started now carries again.
    t2v.auth.logout()
    t2v.auth.start_anonymous()
    controller.send("mine")
    assert controller.wait_idle(5.0)
    t2v.auth.login("ana@example.com", "pw")
    assert heard[-1].forgotten is None and contents(controller.snapshot()) == ["mine", "ok"]


def test_exchange_exports_and_restores_in_one_step(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    now, _heard = follow(controller)
    controller.send("one")
    assert controller.wait_idle(5.0)
    kept = controller.export_conversation()
    controller.send("two")
    assert controller.wait_idle(5.0)
    outgoing = controller.exchange_conversation(kept, epoch=now.epoch)
    assert contents(outgoing) == ["one", "ok", "two", "ok"]
    assert contents(controller.snapshot()) == ["one", "ok"]
    assert contents(controller.exchange_conversation(None, epoch=now.epoch)) == ["one", "ok"]


def test_exchange_after_an_identity_change_does_nothing_and_returns_none(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    now, _heard = follow(controller)
    t2v.auth.login("bob@example.com", "pw")
    controller.send("bob's")
    assert controller.wait_idle(5.0)
    empty = {"messages": [], "history": [], "threadId": None}
    assert controller.exchange_conversation(empty, epoch=now.epoch) is None
    assert contents(controller.snapshot()) == ["bob's", "ok"]  # untouched


def test_exchange_refuses_a_bad_snapshot_before_changing_anything(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    now, _heard = follow(controller)
    controller.send("one")
    assert controller.wait_idle(5.0)
    bad = {
        "messages": [{"id": "msg_1", "role": "system", "content": "x", "timestamp": "t"}],
        "history": [],
        "threadId": None,
    }
    with pytest.raises(ValueError):
        controller.exchange_conversation(bad, epoch=now.epoch)  # type: ignore[arg-type]
    assert contents(controller.snapshot()) == ["one", "ok"]


def test_exchange_after_close_exports_but_never_restores(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    now, _heard = follow(controller)
    controller.send("one")
    assert controller.wait_idle(5.0)
    controller.close()
    assert contents(controller.exchange_conversation(None, epoch=now.epoch)) == ["one", "ok"]
    with pytest.raises(ControllerClosedError):
        controller.exchange_conversation(
            {"messages": [], "history": [], "threadId": None}, epoch=now.epoch
        )
    with pytest.raises(ControllerClosedError):
        controller.follow_identity(lambda change: None)


def test_an_identity_change_is_delivered_ahead_of_the_batch_that_empties_the_screen(
    t2v: Talk2View, controller: ChatController
) -> None:
    t2v.auth.login("ana@example.com", "pw")
    controller.send("ana's")
    assert controller.wait_idle(5.0)
    order: List[str] = []
    lock = threading.Lock()

    def on_change(change: IdentityChange) -> None:
        with lock:
            order.append("identity")

    def on_batch(batch: object) -> None:
        with lock:
            order.append("batch")

    controller.follow_identity(on_change)
    controller.subscribe(on_batch)
    t2v.auth.logout()
    assert order[:2] == ["identity", "batch"]


def test_off_stops_the_callback(t2v: Talk2View, controller: ChatController) -> None:
    heard: List[IdentityChange] = []
    _now, off = controller.follow_identity(heard.append)
    off()
    t2v.auth.login("ana@example.com", "pw")
    assert heard == []


def test_close_still_delivers_a_change_recorded_but_not_yet_delivered(
    t2v: Talk2View, controller: ChatController
) -> None:
    """A delivery holds the emit window; a sign-out on another thread records its change
    and waits for the window; the controller is closed meanwhile. The follower still hears
    the change, with the conversation it dropped, exactly once."""
    t2v.auth.login("ana@example.com", "pw")
    controller.send("ana's")
    assert controller.wait_idle(5.0)
    heard: List[IdentityChange] = []
    now, _off = controller.follow_identity(heard.append)
    holding, release = threading.Event(), threading.Event()

    def hold(batch: object) -> None:
        if not holding.is_set():
            holding.set()
            assert release.wait(5.0)

    controller.subscribe(hold)
    blocker = threading.Thread(target=controller.clear_error)  # its batch holds the window
    controller._rec.set(["error"], "x")  # noqa: SLF001 -- give clear_error an op to deliver
    blocker.start()
    assert holding.wait(5.0)
    signing_out = threading.Thread(target=t2v.auth.logout)
    signing_out.start()
    deadline = time.monotonic() + 5.0
    while controller._identity_epoch == now.epoch:  # noqa: SLF001 -- recorded, not delivered
        assert time.monotonic() < deadline
        time.sleep(0.001)
    closing = threading.Thread(target=controller.close)
    closing.start()
    release.set()
    for thread in (blocker, signing_out, closing):
        thread.join(5.0)
        assert not thread.is_alive()
    assert len(heard) == 1 and heard[0].user_id is None
    assert contents(heard[0].forgotten) == ["ana's", "ok"]


def test_becoming_a_guest_inside_a_send_never_abandons_that_send(engine: FakeEngine) -> None:
    """Even over a conversation its list will not let follow a sign-in (``carry=False``):
    nobody becoming a guest is the person at the keyboard, and here it is the guest start
    inside this very send."""
    t2v = Talk2View(
        "pk_test", base_url=engine.url(), storage=MemoryStorage(), anonymous_auto_start=True
    )
    chat = ChatController(t2v, executor=inline_executor)
    try:
        now, heard = follow(chat)
        assert chat.exchange_conversation(None, epoch=now.epoch, carry=False) is not None
        chat.send("my first question")
        assert chat.wait_idle(5.0)
        assert t2v.auth.is_anonymous()
        assert len(heard) == 1 and heard[0].anonymous and heard[0].forgotten is None
        assert contents(chat.snapshot()) == ["my first question", "ok"]
        assert chat.snapshot()["error"] is None
        # The flag still guards a sign-in into an account.
        t2v.auth.signup("new@example.com", "pw")
        assert contents(heard[-1].forgotten) == ["my first question", "ok"]
    finally:
        chat.close()
        t2v.close()
