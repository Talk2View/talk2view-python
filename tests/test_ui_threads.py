"""talk2view.ui.threads: the list, the slice, the four commands, persistence, identity.

Every wait is asserted: a wait that times out fails the test. The deterministic tests hold
the debounce with a fake clock and hold the worker inside a controller call with a gate;
none of them races a sleep against a thread.
"""

from __future__ import annotations

import json
import random
import threading
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

import pytest

from talk2view import ChatController, MemoryStorage, Talk2View
from talk2view.controller import ControllerBusyError
from talk2view.conversations import MAX_IDENTITIES, storage_key
from talk2view.storage import TokenStorage
from talk2view.ui.hub import EventHub
from talk2view.ui.testing import FakeController
from talk2view.ui.threads import ThreadsState

from .ui_engine import AccountEngine
from .ui_helpers import wait_for


def inline(job: Any) -> None:
    job()


def slice_of(hub: EventHub) -> Dict[str, Any]:
    value: Dict[str, Any] = hub.snapshot()["state"]["threads"]
    return value


def listed(hub: EventHub) -> List[str]:
    return [item["id"] for item in slice_of(hub)["items"] if item["messageCount"] > 0]


def contents(messages: List[Dict[str, Any]]) -> List[str]:
    return [m["content"] for m in messages]


def on_screen(controller: Any) -> List[str]:
    return contents(controller.snapshot()["messages"])


def bucket(storage: TokenStorage, identity: str) -> Dict[str, List[str]]:
    """What the store holds for ``identity``: conversation id -> message contents."""
    raw = storage.get(storage_key("pk_test"))
    if raw is None:
        return {}
    found = json.loads(raw)["identities"].get(identity)
    if found is None:
        return {}
    return {c["id"]: contents(c["messages"]) for c in found["conversations"]}


class Clock:
    """Monotonic seconds that move only when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Gate:
    """Hold the worker inside the next ``exchange_conversation`` call: ``before`` the real
    call (so it runs only once released), or just after it returned."""

    def __init__(self, controller: Any, *, before: bool) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._armed = True
        real = controller.exchange_conversation

        def exchange(*args: Any, **kwargs: Any) -> Any:
            armed, self._armed = self._armed, False
            if armed and before:
                self.entered.set()
                assert self.release.wait(5.0)
            result = real(*args, **kwargs)
            if armed and not before:
                self.entered.set()
                assert self.release.wait(5.0)
            return result

        controller.exchange_conversation = exchange


def attached(
    controller: Any, storage: MemoryStorage, clock: Optional[Clock] = None
) -> Tuple[EventHub, ThreadsState]:
    hub = EventHub()
    if clock is None:
        threads = ThreadsState(controller, None, storage=storage, partner_key="pk_test")
    else:
        threads = ThreadsState(
            controller, None, storage=storage, partner_key="pk_test", clock=clock
        )
    threads.attach(hub)
    return hub, threads


def in_thread(job: Callable[[], None]) -> Tuple[threading.Thread, List[BaseException]]:
    """Run ``job`` the way the router does: on a thread of its own."""
    errors: List[BaseException] = []

    def run() -> None:
        try:
            job()
        except BaseException as exc:  # noqa: BLE001 -- handed to the test
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, errors


# ── The list, the slice, the commands ──


def test_attach_seeds_a_blank_current_thread_and_a_reply_lists_it_with_a_title() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    hub, threads = attached(controller, storage)
    try:
        current = slice_of(hub)["currentId"]
        assert current and listed(hub) == []
        controller.send("Load the MRHead sample and show it in 3D please")
        assert wait_for(lambda: listed(hub) == [current], timeout=6.0)
        item = slice_of(hub)["items"][0]
        assert item["title"] == "Load the MRHead sample and show it in 3D please"
        assert item["messageCount"] == 2
        assert wait_for(lambda: storage.get(storage_key("pk_test")) is not None, timeout=6.0)
        stored = json.loads(storage.get(storage_key("pk_test")) or "")
        assert stored["identities"]["none"]["conversations"][0]["id"] == current
    finally:
        threads.close()
        controller.close()


def test_new_thread_keeps_the_old_one_detaches_and_a_second_new_is_a_no_op() -> None:
    controller = FakeController(executor=inline, reply="ok")
    hub, threads = attached(controller, MemoryStorage())
    try:
        first = slice_of(hub)["currentId"]
        controller.send("hello")
        assert wait_for(lambda: listed(hub) == [first])
        threads.new_thread()
        second = slice_of(hub)["currentId"]
        assert second != first and controller.snapshot()["messages"] == []
        assert ("clear", ()) not in controller.calls  # detached, never deleted
        threads.new_thread()
        assert slice_of(hub)["currentId"] == second  # already empty: nothing stacks up
        assert listed(hub) == [first]
    finally:
        threads.close()
        controller.close()


def test_switch_restores_the_transcript_and_rename_delete_do_what_they_say() -> None:
    controller = FakeController(executor=inline, reply="ok")
    hub, threads = attached(controller, MemoryStorage())
    try:
        first = slice_of(hub)["currentId"]
        controller.send("one")
        assert wait_for(lambda: listed(hub) == [first])
        threads.new_thread()
        controller.send("two")
        second = slice_of(hub)["currentId"]
        assert wait_for(lambda: listed(hub) == [second, first])
        threads.switch(first)
        assert slice_of(hub)["currentId"] == first
        assert on_screen(controller)[0] == "one"
        threads.rename(second, "  Renamed  ")
        assert [i["title"] for i in slice_of(hub)["items"] if i["id"] == second] == ["Renamed"]
        assert listed(hub) == [second, first]  # renaming does not reorder
        with pytest.raises(KeyError):
            threads.switch("nope")
        with pytest.raises(KeyError):
            threads.rename("nope", "x")
        with pytest.raises(KeyError):
            threads.delete("nope")
        threads.delete(first)  # the current one: a blank takes its place
        assert first not in listed(hub) and controller.snapshot()["messages"] == []
        assert slice_of(hub)["currentId"] not in (first, second)
    finally:
        threads.close()
        controller.close()


def test_the_list_survives_a_restart_through_the_same_storage() -> None:
    storage = MemoryStorage()
    controller = FakeController(executor=inline, reply="ok")
    hub, threads = attached(controller, storage)
    controller.send("remember me")
    current = slice_of(hub)["currentId"]
    assert wait_for(lambda: listed(hub) == [current])
    threads.close()
    controller.close()
    again = FakeController(executor=inline, reply="ok")
    hub2, threads2 = attached(again, storage)
    try:
        # Nothing on screen: pick up where the device left off (TS start()).
        assert slice_of(hub2)["currentId"] == current
        assert on_screen(again) == ["remember me", "ok"]
        assert listed(hub2) == [current]
    finally:
        threads2.close()
        again.close()


def test_blanks_do_not_pile_up_in_the_list_or_the_store() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    hub, threads = attached(controller, storage)
    try:
        first = slice_of(hub)["currentId"]
        controller.send("one")
        threads.new_thread()  # a blank
        threads.switch(first)  # away from the blank
        threads.new_thread()  # another blank
        threads.switch(first)
        assert [item["id"] for item in slice_of(hub)["items"]] == [first]
        assert list(bucket(storage, "none")) == [first]
    finally:
        threads.close()
        controller.close()


# ── The debounce, deterministically (a fake clock) ──


def test_a_reply_is_written_once_the_debounce_passes_not_per_batch() -> None:
    controller = FakeController(executor=inline, reply="one two three four")
    storage = MemoryStorage()
    clock = Clock()
    hub, threads = attached(controller, storage, clock)
    try:
        current = slice_of(hub)["currentId"]
        controller.send("hello")
        assert threads._settle()  # noqa: SLF001
        assert bucket(storage, "none") == {}  # held: nothing written yet
        clock.advance(0.49)
        assert threads._settle()  # noqa: SLF001
        assert bucket(storage, "none") == {}
        clock.advance(0.02)
        assert threads._settle()  # noqa: SLF001
        assert bucket(storage, "none") == {current: ["hello", "one two three four"]}
    finally:
        threads.close()
        controller.close()


def test_new_chat_with_a_sync_pending_keeps_every_message_of_the_outgoing_thread() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    clock = Clock()
    hub, threads = attached(controller, storage, clock)
    try:
        first = slice_of(hub)["currentId"]
        controller.send("one")
        clock.advance(1.0)
        assert threads._settle()  # noqa: SLF001
        assert bucket(storage, "none")[first] == ["one", "ok"]
        controller.send("two")
        assert threads._settle()  # noqa: SLF001 -- the write for "two" is pending, not due
        assert bucket(storage, "none")[first] == ["one", "ok"]
        threads.new_thread()
        assert bucket(storage, "none")[first] == ["one", "ok", "two", "ok"]
        assert on_screen(controller) == [] and slice_of(hub)["currentId"] != first
    finally:
        threads.close()
        controller.close()


def test_new_chat_decides_empty_from_the_screen_not_from_the_last_sync() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    hub, threads = attached(controller, storage, Clock())
    try:
        first = slice_of(hub)["currentId"]
        controller.send("hello")  # never synced: the clock does not move
        threads.new_thread()
        assert slice_of(hub)["currentId"] != first
        assert bucket(storage, "none")[first] == ["hello", "ok"]
    finally:
        threads.close()
        controller.close()


@pytest.mark.parametrize("command", ["new_thread", "switch"])
def test_a_command_during_an_in_flight_sync_keeps_every_message(command: str) -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    clock = Clock()
    hub, threads = attached(controller, storage, clock)
    try:
        first = slice_of(hub)["currentId"]
        controller.send("one")
        threads.new_thread()
        other = slice_of(hub)["currentId"]
        controller.send("elsewhere")
        threads.switch(first)
        controller.send("two")
        # The timer's sync is in flight: it has exported and not yet returned.
        gate = Gate(controller, before=False)
        clock.advance(1.0)
        threads._poke()  # noqa: SLF001
        assert gate.entered.wait(5.0)
        controller.send("three")  # lands after that export
        if command == "new_thread":
            runner, errors = in_thread(threads.new_thread)
        else:
            runner, errors = in_thread(lambda: threads.switch(other))
        gate.release.set()
        runner.join(5.0)
        assert not runner.is_alive() and errors == []
        assert bucket(storage, "none")[first] == ["one", "ok", "two", "ok", "three", "ok"]
        assert slice_of(hub)["currentId"] != first
        assert on_screen(controller) == ([] if command == "new_thread" else ["elsewhere", "ok"])
    finally:
        threads.close()
        controller.close()


def test_close_writes_what_the_debounce_held_and_a_later_timer_does_nothing() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    clock = Clock()
    hub, threads = attached(controller, storage, clock)
    try:
        current = slice_of(hub)["currentId"]
        controller.send("last words")
        threads.close()
        assert bucket(storage, "none") == {current: ["last words", "ok"]}
        worker = threads._worker  # noqa: SLF001
        assert worker is not None and not worker.is_alive()
        written = storage.get(storage_key("pk_test"))
        controller.send("after close")
        clock.advance(60.0)
        threads._poke()  # noqa: SLF001
        assert threads._settle() is False  # noqa: SLF001 -- nothing is listening
        assert storage.get(storage_key("pk_test")) == written
        threads.new_thread()  # a command after close is a no-op, not a hang
        threads.close()  # and so is a second close
    finally:
        controller.close()


# ── Identity, against a real ChatController and the fake engine ──


@pytest.fixture
def t2v(account_engine: AccountEngine) -> Iterator[Talk2View]:
    client = Talk2View("pk_test", base_url=account_engine.url(), storage=MemoryStorage())
    yield client
    client.close()


def follows(
    t2v: Talk2View, clock: Optional[Clock] = None, executor: Any = inline
) -> Tuple[ChatController, EventHub, ThreadsState]:
    controller = ChatController(t2v, executor=executor)
    hub = EventHub()
    if clock is None:
        threads = ThreadsState(controller, t2v)
    else:
        threads = ThreadsState(controller, t2v, clock=clock)
    threads.attach(hub)
    return controller, hub, threads


def test_ana_bob_sign_out_ana_leaves_anas_thread_whole_in_her_bucket_every_time(
    t2v: Talk2View,
) -> None:
    controller, hub, threads = follows(t2v)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        controller.send("ana's question")
        ana_thread = slice_of(hub)["currentId"]
        assert wait_for(lambda: listed(hub) == [ana_thread], timeout=6.0)
        # Bob signs in with Ana mid-conversation (her second exchange not yet written).
        controller.send("ana's second")
        t2v.auth.login("bob@example.com", "pw")
        whole = ["ana's question", "ok", "ana's second", "ok"]
        assert wait_for(lambda: bucket(storage, "u:user-ana").get(ana_thread) == whole)
        assert wait_for(lambda: slice_of(hub)["currentId"] != ana_thread)
        assert listed(hub) == [] and controller.snapshot()["messages"] == []
        assert ana_thread not in bucket(storage, "u:user-bob")
        # Signed out: nobody's list, still not Ana's; Ana's bucket untouched.
        t2v.auth.logout()
        assert threads._settle()  # noqa: SLF001
        assert listed(hub) == [] and slice_of(hub)["currentId"] != ana_thread
        assert ana_thread not in bucket(storage, "none")
        assert bucket(storage, "u:user-ana") == {ana_thread: whole}
        # Ana again: her thread is back, whole.
        t2v.auth.login("ana@example.com", "pw")
        assert wait_for(lambda: ana_thread in listed(hub))
        assert bucket(storage, "u:user-ana")[ana_thread] == whole
        threads.switch(ana_thread)
        assert on_screen(controller) == whole
    finally:
        threads.close()
        controller.close()


def test_a_sign_out_mid_reply_keeps_what_had_streamed_in_the_outgoing_bucket(
    t2v: Talk2View, account_engine: AccountEngine
) -> None:
    controller, hub, threads = follows(t2v, executor=None)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        ana_thread = slice_of(hub)["currentId"]
        controller.send("slow question")
        assert account_engine.slow_started.wait(5.0)
        assert wait_for(lambda: "partial" in on_screen(controller))
        t2v.auth.logout()
        assert wait_for(
            lambda: bucket(storage, "u:user-ana").get(ana_thread) == ["slow question", "partial"]
        )
    finally:
        account_engine.release_slow.set()
        threads.close()
        controller.close()


def test_a_guest_who_signs_up_takes_their_conversation_into_the_account(
    t2v: Talk2View,
) -> None:
    controller, hub, threads = follows(t2v)
    storage = t2v.data_storage
    try:
        t2v.auth.start_anonymous()
        guest = t2v.auth.get_user()
        assert guest is not None
        controller.send("as a guest")
        guest_thread = slice_of(hub)["currentId"]
        assert wait_for(lambda: listed(hub) == [guest_thread], timeout=6.0)
        t2v.auth.signup("new@example.com", "pw")  # the fake engine 409s: signs in instead
        assert not t2v.auth.is_anonymous()
        assert wait_for(
            lambda: bucket(storage, "u:user-new").get(guest_thread) == ["as a guest", "ok"]
        )
        assert guest_thread not in bucket(storage, "u:" + guest.id)
        assert slice_of(hub)["currentId"] == guest_thread
        assert on_screen(controller) == ["as a guest", "ok"]
    finally:
        threads.close()
        controller.close()


def test_a_stored_conversation_never_follows_a_sign_in_into_an_account(
    t2v: Talk2View,
) -> None:
    # An earlier visitor's conversation, left in the signed-out bucket.
    storage = t2v.data_storage
    first = FakeController(executor=inline, reply="ok")
    _hub, earlier_list = attached(first, MemoryStorage())
    first.send("an earlier visitor's")
    earlier_list.close()
    first.close()
    stored = earlier_list._store  # noqa: SLF001 -- copy its bucket into this device's store
    ThreadsState(object(), None, storage=storage, partner_key="pk_test")._store.write(  # noqa: SLF001
        "none", stored.read("none"), None
    )
    earlier = next(iter(bucket(storage, "none")))
    controller, hub, threads = follows(t2v)
    try:
        assert slice_of(hub)["currentId"] == earlier  # picked up where the device left off
        assert on_screen(controller) == ["an earlier visitor's", "ok"]
        t2v.auth.login("ana@example.com", "pw")
        assert threads._settle()  # noqa: SLF001
        assert slice_of(hub)["currentId"] != earlier and listed(hub) == []
        assert controller.snapshot()["messages"] == []
        assert bucket(storage, "none")[earlier] == ["an earlier visitor's", "ok"]
        assert earlier not in bucket(storage, "u:user-ana")
    finally:
        threads.close()
        controller.close()


def test_a_sync_held_by_the_debounce_loses_nothing_across_a_sign_in(t2v: Talk2View) -> None:
    clock = Clock()
    controller, hub, threads = follows(t2v, clock)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        assert threads._settle()  # noqa: SLF001 -- the sign-in is taken: the blank carried
        controller.send("never synced")
        assert threads._settle()  # noqa: SLF001
        ana_thread = slice_of(hub)["currentId"]
        assert ana_thread not in bucket(storage, "u:user-ana")  # the debounce holds it
        t2v.auth.login("bob@example.com", "pw")
        assert threads._settle()  # noqa: SLF001
        assert bucket(storage, "u:user-ana")[ana_thread] == ["never synced", "ok"]
    finally:
        threads.close()
        controller.close()


def test_a_command_racing_an_identity_change_runs_again_after_it(t2v: Talk2View) -> None:
    clock = Clock()
    controller, hub, threads = follows(t2v, clock)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        controller.send("ana one")
        first = slice_of(hub)["currentId"]
        threads.new_thread()
        controller.send("ana two")
        second = slice_of(hub)["currentId"]
        # The switch is on the worker, about to exchange, when Bob signs in.
        gate = Gate(controller, before=True)
        runner, errors = in_thread(lambda: threads.switch(first))
        assert gate.entered.wait(5.0)
        t2v.auth.login("bob@example.com", "pw")  # Ana's conversation leaves the screen
        gate.release.set()
        runner.join(5.0)
        assert not runner.is_alive()
        # Refused, then run again in Bob's list, where Ana's thread does not exist.
        assert len(errors) == 1 and isinstance(errors[0], KeyError)
        assert bucket(storage, "u:user-ana") == {
            first: ["ana one", "ok"],
            second: ["ana two", "ok"],
        }
        assert first not in bucket(storage, "u:user-bob")
        assert controller.snapshot()["messages"] == []
        assert threads._settle()  # noqa: SLF001
        assert slice_of(hub)["currentId"] not in (first, second) and listed(hub) == []
    finally:
        threads.close()
        controller.close()


def test_new_chat_racing_a_sign_out_lands_in_the_signed_out_list(t2v: Talk2View) -> None:
    clock = Clock()
    controller, hub, threads = follows(t2v, clock)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        controller.send("ana's")
        ana_thread = slice_of(hub)["currentId"]
        gate = Gate(controller, before=True)
        runner, errors = in_thread(threads.new_thread)
        assert gate.entered.wait(5.0)
        t2v.auth.logout()
        gate.release.set()
        runner.join(5.0)
        assert not runner.is_alive() and errors == []
        assert bucket(storage, "u:user-ana") == {ana_thread: ["ana's", "ok"]}
        assert ana_thread not in bucket(storage, "none")
        assert listed(hub) == [] and controller.snapshot()["messages"] == []
    finally:
        threads.close()
        controller.close()


def test_close_racing_a_sign_out_still_writes_the_outgoing_conversation(
    t2v: Talk2View,
) -> None:
    clock = Clock()
    controller, hub, threads = follows(t2v, clock)
    storage = t2v.data_storage
    try:
        t2v.auth.login("ana@example.com", "pw")
        controller.send("ana's last")
        ana_thread = slice_of(hub)["currentId"]
        gate = Gate(controller, before=True)
        runner, errors = in_thread(threads.close)  # its final sync waits at the gate
        assert gate.entered.wait(5.0)
        t2v.auth.logout()
        gate.release.set()
        runner.join(5.0)
        assert not runner.is_alive() and errors == []
        assert bucket(storage, "u:user-ana") == {ana_thread: ["ana's last", "ok"]}
    finally:
        threads.close()
        controller.close()


# ── Relaunching: a signed-out visitor's first message ──


def launch(engine: AccountEngine, device: MemoryStorage) -> Tuple[Talk2View, Any, EventHub, Any]:
    """The 3D Slicer extension's defaults over one device store: guests start on the first send, the
    thread list is on, and the sign-in survives a relaunch (the tokens are in ``device``)."""
    t2v = Talk2View(
        "pk_test", base_url=engine.url(), storage=device, anonymous_auto_start=True
    )
    controller, hub, threads = follows(t2v)
    return t2v, controller, hub, threads


def quit_app(t2v: Talk2View, controller: ChatController, threads: ThreadsState) -> None:
    threads.close()
    controller.close()
    t2v.close()


def test_a_relaunch_never_drops_a_signed_out_visitors_first_message(
    account_engine: AccountEngine,
) -> None:
    device = MemoryStorage()
    quit_app(*_without_hub(launch(account_engine, device)))  # opened, nothing typed
    assert bucket(device, "none") == {}  # a blank is nobody's conversation: not stored
    t2v, controller, hub, threads = launch(account_engine, device)
    try:
        first = slice_of(hub)["currentId"]
        controller.send("my first question")
        assert on_screen(controller) == ["my first question", "ok"]
        assert controller.snapshot()["error"] is None and t2v.auth.is_anonymous()
        guest = t2v.auth.get_user()
        assert guest is not None
        assert threads._settle(flush=True)  # noqa: SLF001
        assert slice_of(hub)["currentId"] == first
        assert bucket(device, "u:" + guest.id) == {first: ["my first question", "ok"]}
        assert bucket(device, "none") == {}
    finally:
        quit_app(t2v, controller, threads)


def test_a_relaunch_over_an_earlier_signed_out_conversation_answers_the_first_message(
    account_engine: AccountEngine,
) -> None:
    """TS parity, where it does not drop the message: ``start()`` puts the newest stored
    signed-out conversation back on screen, not fresh (``use-conversations.ts:395-404``),
    and one read back from storage never walks into an account (``:209-216``). TS drops
    the first message here: ``sendMessage`` appends it, the guest session starts inside
    ``chat()``, and ``onAuthStateChange`` restores EMPTY (``:233-238``), which bumps the
    epoch the stream checks (``index.ts:896-911``). This SDK overrides that drop: a
    visitor becoming a guest carries what is on screen, so the message is answered, in
    the guest's bucket; the earlier conversation still never follows into an account."""
    device = MemoryStorage()
    earlier = FakeController(executor=inline, reply="ok")
    _hub, earlier_list = attached(earlier, device)
    earlier.send("an earlier visitor's")
    earlier_list.close()
    earlier.close()
    (earlier_id,) = bucket(device, "none")
    t2v, controller, hub, threads = launch(account_engine, device)
    try:
        assert slice_of(hub)["currentId"] == earlier_id
        assert on_screen(controller) == ["an earlier visitor's", "ok"]
        controller.send("my first question")
        whole = ["an earlier visitor's", "ok", "my first question", "ok"]
        assert on_screen(controller) == whole
        assert controller.snapshot()["error"] is None
        guest = t2v.auth.get_user()
        assert guest is not None and t2v.auth.is_anonymous()
        assert threads._settle(flush=True)  # noqa: SLF001
        assert bucket(device, "u:" + guest.id) == {earlier_id: whole}
        # It never walks into an account: signing up leaves it with the guest.
        t2v.auth.signup("new@example.com", "pw")
        assert threads._settle()  # noqa: SLF001
        assert controller.snapshot()["messages"] == []
        assert bucket(device, "u:" + guest.id) == {earlier_id: whole}
        assert earlier_id not in bucket(device, "u:user-new")
    finally:
        quit_app(t2v, controller, threads)


def test_a_signed_in_relaunch_carries_on_the_same_thread(account_engine: AccountEngine) -> None:
    device = MemoryStorage()
    t2v, controller, hub, threads = launch(account_engine, device)
    t2v.auth.login("ana@example.com", "pw")
    controller.send("before")
    ana_thread = slice_of(hub)["currentId"]
    quit_app(t2v, controller, threads)
    t2v, controller, hub, threads = launch(account_engine, device)
    try:
        assert t2v.auth.get_user() is not None and not t2v.auth.is_anonymous()
        assert slice_of(hub)["currentId"] == ana_thread
        controller.send("after")
        assert on_screen(controller) == ["before", "ok", "after", "ok"]
        assert threads._settle(flush=True)  # noqa: SLF001
        assert bucket(device, "u:user-ana") == {ana_thread: ["before", "ok", "after", "ok"]}
    finally:
        quit_app(t2v, controller, threads)


def _without_hub(
    launched: Tuple[Talk2View, Any, EventHub, Any],
) -> Tuple[Talk2View, Any, Any]:
    t2v, controller, _hub, threads = launched
    return t2v, controller, threads


# ── A list that cannot keep up: errors, not silent success ──


def test_a_command_the_worker_cannot_reach_in_time_raises_timeout() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    hub = EventHub()
    threads = ThreadsState(controller, None, storage=storage, partner_key="pk_test", timeout=0.2)
    threads.attach(hub)
    try:
        controller.send("hello")
        gate = Gate(controller, before=True)
        with pytest.raises(TimeoutError):
            threads.new_thread()  # the worker is held inside its exchange
        assert gate.entered.is_set()
        gate.release.set()
        assert threads._settle()  # noqa: SLF001 -- it still ran, in its turn
        assert on_screen(controller) == []
    finally:
        threads.close()
        controller.close()


def test_a_timed_out_attach_raises_and_the_list_then_stops_writing_nothing() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    threads = ThreadsState(controller, None, storage=storage, partner_key="pk_test", timeout=0.2)
    gate = Gate(controller, before=True)
    with pytest.raises(TimeoutError):
        threads.attach(EventHub())
    assert gate.entered.is_set()
    gate.release.set()
    worker = threads._worker  # noqa: SLF001
    assert worker is not None
    worker.join(5.0)
    assert not worker.is_alive()
    controller.send("after a failed start")  # nobody is subscribed any more
    assert storage.get(storage_key("pk_test")) is None
    threads.close()  # a no-op
    controller.close()


def test_a_message_reaches_the_list_before_the_debounce_writes_it() -> None:
    controller = FakeController(executor=inline, reply="ok")
    storage = MemoryStorage()
    hub, threads = attached(controller, storage, Clock())
    try:
        current = slice_of(hub)["currentId"]
        controller.send("Load MRHead")
        assert threads._settle()  # noqa: SLF001 -- the clock never moves
        assert listed(hub) == [current]
        assert slice_of(hub)["items"][0]["title"] == "Load MRHead"
        assert slice_of(hub)["items"][0]["messageCount"] == 2
        assert bucket(storage, "none") == {}  # written only once the debounce passes
    finally:
        threads.close()
        controller.close()


# ── Stress: random interleavings, bounded ──


class WatchedStorage(MemoryStorage):
    """Every write of the conversation store goes through ``on_write`` as it happens, so
    no state the store ever held escapes the checks (a sampling reader could miss one)."""

    def __init__(self, on_write: Callable[[str], None]) -> None:
        super().__init__()
        self._on_write = on_write

    def set(self, key: str, value: str) -> None:
        super().set(key, value)
        if key == storage_key("pk_test"):
            self._on_write(value)


def test_random_interleavings_keep_identities_apart_and_lose_nothing(
    account_engine: AccountEngine,
) -> None:
    """Commands (a router thread) and the host (sign-ins, sign-outs and sends, signed out
    included, where the send itself starts a guest session) interleave at random for about
    1.5 s. Every user message is tagged with the identity signed in when it was sent
    (sign-ins and sends share a lock). Checked on every write the store takes:

    - a message sent by an account is only ever in that account's bucket; one sent by a
      guest is never in the signed-out bucket or another guest's (a guest's, or a
      signed-out visitor's, may follow them into the account they sign in to);
    - no conversation a bucket holds ever loses a message it held before.

    At the end: no send is ever dropped -- every message sent is on screen, in the store,
    or was last seen in a conversation that was then deleted (only conversations not on
    screen are deleted, and those were written when they left it) -- and the slice's
    current conversation is exactly what is on screen and what the store holds for it."""
    seed = random.randrange(1 << 30)  # noqa: S311 -- a test seed, printed on failure
    rng = random.Random(seed)  # noqa: S311
    lock = threading.Lock()  # the checker's: it runs on the list's worker
    seen: Dict[Tuple[str, str], List[str]] = {}
    last_seen_in: Dict[str, Tuple[str, str]] = {}  # message -> (bucket, conversation)
    # Messages whose bucket the store's cap on identities evicted (least recently written
    # first, TS's rule) while they were in it: gone by design, not dropped.
    evicted: Set[str] = set()
    buckets_before: List[Set[str]] = [set()]
    failures: List[str] = []

    def check(raw: str) -> None:
        with lock:
            identities = json.loads(raw)["identities"]
            for identity, found in identities.items():
                for conversation in found["conversations"]:
                    texts = [m["content"] for m in conversation["messages"] if m["role"] == "user"]
                    for text in texts:
                        tag = text.split(" ", 1)[0]
                        last_seen_in[text] = (identity, conversation["id"])
                        if tag.startswith("u:user-") and identity != tag:
                            failures.append("{} holds {!r}".format(identity, text))
                        stranger = identity.startswith("u:anon-") and identity != tag
                        if tag.startswith("u:anon-") and (identity == "none" or stranger):
                            failures.append("{} holds {!r}".format(identity, text))
                    key = (identity, conversation["id"])
                    lost = [t for t in seen.get(key, []) if t not in texts]
                    if lost:
                        failures.append("{} lost {!r}".format(key, lost))
                    seen[key] = texts
            gone = buckets_before[0] - set(identities)
            if gone and len(buckets_before[0]) >= MAX_IDENTITIES:
                evicted.update(t for t, (where, _c) in last_seen_in.items() if where in gone)
                for key in [k for k in seen if k[0] in gone]:
                    del seen[key]  # a bucket written afresh later starts from nothing
            buckets_before[0] = set(identities)

    storage = WatchedStorage(check)
    t2v = Talk2View(
        "pk_test", base_url=account_engine.url(), storage=storage, anonymous_auto_start=True
    )
    def watch(controller: ChatController) -> None:
        """A signed-out visitor becoming a guest -- the start inside their own send -- must
        never take what is on screen away (it would abandon that send)."""
        seen_as: Dict[str, Any] = {"epoch": None, "user": None}
        seen_lock = threading.Lock()

        def on_change(change: Any) -> None:
            with seen_lock:
                if seen_as["epoch"] is None or change.epoch <= seen_as["epoch"]:
                    return  # at or below the baseline
                was = seen_as["user"]
                seen_as["epoch"], seen_as["user"] = change.epoch, change.user_id
            if was is None and change.anonymous and change.forgotten is not None:
                with lock:
                    failures.append("becoming a guest dropped {}".format(change.forgotten))

        now, _off = controller.follow_identity(on_change)
        with seen_lock:
            seen_as["epoch"], seen_as["user"] = now.epoch, now.user_id

    controller, hub, threads = follows(t2v, executor=None)
    watch(controller)
    app = {"controller": controller, "hub": hub, "threads": threads}  # the running launch
    launch_lock = threading.Lock()  # a relaunch swaps the whole app under it
    auth_lock = threading.Lock()
    stop = threading.Event()
    sent: List[str] = []
    deleted: List[str] = []
    counts = {
        "commands": 0, "identity": 0, "sends": 0, "signed-out sends": 0, "busy": 0,
        "relaunches": 0,
    }

    def commands() -> None:
        while not stop.is_set():
            with launch_lock:
                one_command(app["hub"], app["threads"])
            time.sleep(rng.random() * 0.01)

    def one_command(hub: EventHub, threads: ThreadsState) -> None:
        state = slice_of(hub)
        ids = [item["id"] for item in state["items"]]
        others = [i for i in ids if i != state["currentId"]]
        choice = rng.random()
        try:
            if choice < 0.25:
                threads.new_thread()
            elif choice < 0.65 and ids:
                threads.switch(rng.choice(ids))
            elif choice < 0.85 and ids:
                threads.rename(rng.choice(ids), "r{}".format(rng.randrange(100)))
            elif others:
                target = rng.choice(others)
                threads.delete(target)
                deleted.append(target)
        except KeyError:
            pass  # gone meanwhile: an identity change, or a delete
        counts["commands"] += 1

    def relaunch() -> None:
        """Quit and start again over the same device store (and so the same sign-in)."""
        with launch_lock:
            app["threads"].close()
            app["controller"].close()
            app["controller"], app["hub"], app["threads"] = follows(t2v, executor=None)
            watch(app["controller"])
            counts["relaunches"] += 1

    def host() -> None:
        while not stop.is_set():
            with auth_lock:
                controller = app["controller"]
                choice = rng.random()
                user = t2v.auth.get_user() if t2v.auth.is_authenticated() else None
                identity = "u:" + user.id if user is not None else "none"
                if choice < 0.15:
                    t2v.auth.login(rng.choice(["ana@example.com", "bob@example.com"]), "pw")
                    counts["identity"] += 1
                elif choice < 0.25:
                    t2v.auth.logout()
                    counts["identity"] += 1
                elif choice < 0.3:
                    relaunch()
                elif choice < 0.35:
                    t2v.auth.logout()  # quit signed out, come back, and type straight away
                    relaunch()
                    counts["identity"] += 1
                elif controller.wait_idle(0.5):
                    text = "{} {}".format(identity, len(sent))
                    try:
                        controller.send(text)
                    except ControllerBusyError:
                        counts["busy"] += 1  # a turn still unwinding: the next round tries
                    else:
                        sent.append(text)
                        counts["sends"] += 1
                        counts["signed-out sends"] += identity == "none"
            time.sleep(rng.random() * 0.01)

    workers = [threading.Thread(target=commands), threading.Thread(target=host)]
    try:
        for worker in workers:
            worker.start()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not failures:
            state = slice_of(app["hub"])
            if state["currentId"] not in [item["id"] for item in state["items"]]:
                with lock:
                    failures.append("the slice's current {} is not listed".format(state))
            time.sleep(0.01)
        stop.set()
        for worker in workers:
            worker.join(10.0)
            assert not worker.is_alive(), "seed {}".format(seed)
        controller, hub, threads = app["controller"], app["hub"], app["threads"]
        assert controller.wait_idle(5.0)
        screen = contents(controller.snapshot()["messages"])
        threads.close()  # the final sync, publish and write
        assert failures == [], "seed {}: {}".format(seed, failures[:5])
        assert counts["sends"] > 0 and counts["signed-out sends"] > 0, counts
        assert counts["commands"] > 0 and counts["identity"] > 0, counts
        assert counts["relaunches"] > 0, counts
        raw = storage.get(storage_key("pk_test"))
        stored = json.dumps(json.loads(raw)) if raw is not None else ""
        def accounted_for(text: str) -> bool:
            if text in screen or json.dumps(text) in stored:
                return True
            where = last_seen_in.get(text)
            if where is None:
                return False
            # Deleted with its conversation, or evicted with its whole bucket.
            return where[1] in deleted or text in evicted

        dropped = [text for text in sent if not accounted_for(text)]
        assert dropped == [], "seed {}: sends dropped: {}".format(seed, dropped[:5])
        user = t2v.auth.get_user() if t2v.auth.is_authenticated() else None
        identity = "u:" + user.id if user is not None else "none"
        current = slice_of(hub)["currentId"]
        count = [i["messageCount"] for i in slice_of(hub)["items"] if i["id"] == current]
        assert count == [len(screen)], "seed {}".format(seed)
        assert bucket(storage, identity).get(current, []) == screen, "seed {}".format(seed)
    finally:
        stop.set()
        app["threads"].close()
        app["controller"].close()
        t2v.close()
