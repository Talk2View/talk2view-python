"""talk2view.ui.hub: one seq line, per-page queues with coalescing, replay, resync."""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

import talk2view.ui.hub as hub_module
from talk2view.cancel import CancelToken
from talk2view.controller import ChatController
from talk2view.state import ChatSnapshot, OpBatch, OpError, initial_snapshot
from talk2view.ui.hub import (
    CHAT_KEYS,
    MAX_CHAT_BUFFER,
    EventHub,
    TooManyClientsError,
    merge_ops,
)

from .controller_fakes import FakeT2V, make_controller, stop_chunk, text_chunk, tool_call_chunk
from .ui_helpers import Gap, PageModel


def chat_only(state: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in state.items() if k in CHAT_KEYS}


def controller_state(controller: ChatController) -> Dict[str, Any]:
    snap = dict(controller.snapshot())
    snap.pop("seq")
    return snap


def reply(t2v: FakeT2V, *chunks: str) -> None:
    t2v.chat_scripts.append([text_chunk(c) for c in chunks] + [stop_chunk()])


@pytest.fixture
def joined() -> Any:
    t2v = FakeT2V()
    controller = make_controller(t2v)
    hub = EventHub()
    unsubscribe = hub.join(controller)
    yield t2v, controller, hub
    unsubscribe()
    controller.close()


def test_chat_keys_are_the_chat_snapshot_keys() -> None:
    assert CHAT_KEYS == set(initial_snapshot()) - {"seq"}
    assert "messages" in CHAT_KEYS and "skills" in CHAT_KEYS and "auth" not in CHAT_KEYS


def test_set_slice_is_one_batch_and_only_on_change() -> None:
    hub = EventHub()
    hub.set_slice("theme", {"mode": "light", "tokens": {}})
    assert hub.seq == 1
    hub.set_slice("theme", {"mode": "light", "tokens": {}})
    assert hub.seq == 1
    hub.set_slice("theme", {"mode": "dark", "tokens": {}})
    assert hub.snapshot() == {"seq": 2, "state": {"theme": {"mode": "dark", "tokens": {}}}}
    for key in ("messages", "isLoading", "seq"):
        with pytest.raises(ValueError):
            hub.set_slice(key, 1)
    with pytest.raises(TypeError):
        hub.set_slice("bad", object())
    assert hub.seq == 2


def test_join_mirrors_the_controller_and_follows_a_turn(joined: Any) -> None:
    t2v, controller, hub = joined
    hub.set_slice("auth", {"user": None})
    assert chat_only(hub.snapshot()["state"]) == controller_state(controller)
    before = hub.seq
    reply(t2v, "Hel", "lo")
    controller.send("hi")
    state = hub.snapshot()["state"]
    assert chat_only(state) == controller_state(controller)
    assert state["auth"] == {"user": None}
    assert [m["content"] for m in state["messages"]] == ["hi", "Hello"]
    assert hub.seq > before


def test_a_page_rebuilds_the_mirror_from_snapshot_then_ops(joined: Any) -> None:
    t2v, controller, hub = joined
    page = PageModel()
    client = hub.attach()
    assert page.pump(hub, client) == 1 and page.kinds == ["snapshot"]
    reply(t2v, "a", "b", "c")
    controller.send("one")
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "ops"  # the whole turn in one event...
    deltas = [op for op in json.loads(event[2])["ops"] if op["type"] == "append-text"]
    assert deltas == [  # ...and its three deltas in one op
        {"type": "append-text", "path": ["messages", 1, "content"], "value": "abc"}
    ]
    page.feed(*event)
    assert page.pump(hub, client) == 0
    assert page.state == hub.snapshot()["state"]
    assert page.seq == hub.seq


def test_a_page_that_keeps_up_gets_one_event_per_batch(joined: Any) -> None:
    t2v, controller, hub = joined
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    spans: List[int] = []

    def on_batch(_batch: OpBatch) -> None:
        # Runs after the hub's own subscriber: read at once, as a live page would.
        event = hub.next_event(client, 0.0)
        if event is not None and event[0] == "ops":
            payload = json.loads(event[2])
            spans.append(payload["seq"] - payload["base"])
            page.feed(*event)

    controller.subscribe(on_batch)
    reply(t2v, "x", "y", "z")
    controller.send("go")
    assert len(spans) >= 4 and set(spans) == {1}
    assert page.state == hub.snapshot()["state"]


def test_merge_ops_folds_adjacent_append_text_on_one_path_only() -> None:
    first = {"type": "append-text", "path": ["messages", 1, "content"], "value": "a"}
    pending: List[Any] = []
    merge_ops(pending, [first])
    merge_ops(pending, [{"type": "append-text", "path": ["messages", 1, "content"], "value": "b"}])
    merge_ops(pending, [{"type": "append-text", "path": ["messages", 2, "content"], "value": "c"}])
    merge_ops(pending, [{"type": "set", "path": ["isLoading"], "value": False}])
    merge_ops(pending, [{"type": "append-text", "path": ["messages", 2, "content"], "value": "d"}])
    assert pending == [
        {"type": "append-text", "path": ["messages", 1, "content"], "value": "ab"},
        {"type": "append-text", "path": ["messages", 2, "content"], "value": "c"},
        {"type": "set", "path": ["isLoading"], "value": False},
        {"type": "append-text", "path": ["messages", 2, "content"], "value": "d"},
    ]
    assert first["value"] == "a"  # the op it was given is never mutated


def test_coalescing_never_touches_the_ring(joined: Any) -> None:
    t2v, controller, hub = joined
    client = hub.attach()
    PageModel().pump(hub, client)
    reply(t2v, "p", "q")
    controller.send("s")
    ring_before = json.dumps(list(hub._ring))
    hub.next_event(client, 0.0)
    assert json.dumps(list(hub._ring)) == ring_before


def test_overflow_turns_into_a_fresh_snapshot() -> None:
    hub = EventHub(max_pending_ops=3)
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    for i in range(5):
        hub.set_slice("s{}".format(i), i)
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "snapshot"
    page.feed(*event)
    assert page.state == hub.snapshot()["state"] and page.seq == 5


def test_a_page_too_far_behind_in_bytes_gets_a_snapshot() -> None:
    hub = EventHub(max_pending_bytes=200)
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    hub.set_slice("big", "x" * 150)
    hub.set_slice("big", "y" * 150)  # two ops, but past the byte bound
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "snapshot"
    page.feed(*event)
    assert page.state == hub.snapshot()["state"]


def test_the_ring_is_bounded_in_bytes_but_keeps_the_newest_batch() -> None:
    hub = EventHub(max_ring_bytes=300)
    for i in range(5):
        hub.set_slice("k", str(i) * 100)
    assert [seq for seq, _, _ in hub._ring] == [4, 5]
    assert hub._ring_bytes == sum(size for _, _, size in hub._ring) <= 300
    hub.set_slice("k", "z" * 1000)  # alone over the bound: still kept, as the newest
    assert [seq for seq, _, _ in hub._ring] == [6]
    stale = hub.attach(last_event_id=4)
    assert hub.next_event(stale, 0.0)[0] == "snapshot"  # type: ignore[index]
    fresh = hub.attach(last_event_id=5)
    assert hub.next_event(fresh, 0.0)[0] == "ops"  # type: ignore[index]


def test_last_event_id_replays_from_the_ring() -> None:
    hub = EventHub()
    for i in range(6):
        hub.set_slice("k", i)
    client = hub.attach(last_event_id=3)
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "ops"
    payload = json.loads(event[2])
    assert (payload["base"], payload["seq"]) == (3, 6)
    assert [op["value"] for op in payload["ops"]] == [3, 4, 5]


def test_a_current_last_event_id_gets_an_empty_ops_event_at_once() -> None:
    hub = EventHub()
    hub.set_slice("k", 1)
    client = hub.attach(last_event_id=1)
    assert hub.next_event(client, 0.0) == ("ops", 1, '{"seq":1,"base":1,"ops":[]}')


@pytest.mark.parametrize("since", [-1, 99, 0])
def test_an_unknown_or_evicted_last_event_id_gets_a_snapshot(since: int) -> None:
    hub = EventHub(ring_size=4)
    for i in range(8):
        hub.set_slice("k", i)
    client = hub.attach(last_event_id=since)
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "snapshot"


def test_the_ring_keeps_the_last_ring_size_batches() -> None:
    hub = EventHub(ring_size=4)
    for i in range(10):
        hub.set_slice("k", i)
    assert [seq for seq, _, _ in hub._ring] == [7, 8, 9, 10]
    ok = hub.attach(last_event_id=6)  # 7..10 are all there
    assert hub.next_event(ok, 0.0)[0] == "ops"  # type: ignore[index]
    old = hub.attach(last_event_id=5)  # 6 was evicted
    assert hub.next_event(old, 0.0)[0] == "snapshot"  # type: ignore[index]


def test_heartbeat_close_detach_and_the_client_cap() -> None:
    hub = EventHub(max_clients=2)
    first = hub.attach()
    hub.next_event(first, 0.0)
    started = time.monotonic()
    assert hub.next_event(first, 0.05) == ("hb", 0, "")
    assert time.monotonic() - started >= 0.04
    second = hub.attach()
    with pytest.raises(TooManyClientsError):
        hub.attach()
    hub.detach(second)
    assert hub.client_count == 1
    assert hub.next_event(second, 0.0) is None
    hub.close()
    assert hub.next_event(first, 1.0) is None
    with pytest.raises(TooManyClientsError):
        hub.attach()


def test_a_waiting_page_wakes_on_a_batch_from_another_thread() -> None:
    hub = EventHub()
    client = hub.attach()
    hub.next_event(client, 0.0)
    timer = threading.Timer(0.05, lambda: hub.set_slice("k", 1))
    timer.start()
    started = time.monotonic()
    event = hub.next_event(client, 5.0)
    timer.join()
    assert event is not None and event[0] == "ops"
    assert time.monotonic() - started < 2.0  # woken by the batch, not by the timeout


class ScriptedSource:
    """A chat source whose snapshot() lets batches through first: the join race."""

    def __init__(self) -> None:
        self.callback: Optional[Callable[[OpBatch], None]] = None
        self.during_snapshot: List[OpBatch] = []
        self.snap: ChatSnapshot = initial_snapshot()

    def subscribe(
        self, on_batch: Callable[[OpBatch], None], *, deliver_on_main: bool = False
    ) -> Callable[[], None]:
        assert deliver_on_main is False
        self.callback = on_batch
        return lambda: None

    def snapshot(self) -> ChatSnapshot:
        assert self.callback is not None
        for batch in self.during_snapshot:
            self.callback(batch)
        return self.snap


def loading(seq: int, value: bool) -> OpBatch:
    return {"seq": seq, "ops": [{"type": "set", "path": ["isLoading"], "value": value}]}


def test_join_replays_only_batches_newer_than_the_snapshot() -> None:
    source = ScriptedSource()
    source.snap = dict(initial_snapshot(), seq=5, isLoading=True)  # type: ignore[assignment]
    source.during_snapshot = [loading(5, True), loading(6, False)]
    hub = EventHub()
    hub.join(source)
    assert hub.snapshot()["state"]["isLoading"] is False
    assert source.callback is not None
    source.callback(loading(7, True))
    assert hub.snapshot()["state"]["isLoading"] is True


def test_a_gap_asks_for_one_resync_outside_the_lock_and_rebase_converges() -> None:
    source = ScriptedSource()
    asked: List[int] = []
    hub = EventHub(on_resync=lambda: asked.append(hub.seq))  # re-enters the hub: no deadlock
    hub.join(source)
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    callback = source.callback
    assert callback is not None
    # Through _finishes: if on_resync ever ran under the lock, its re-entry would hang here.
    assert _finishes(lambda: callback(loading(1, True)))
    assert _finishes(lambda: callback(loading(3, False)))  # 2 went missing
    assert len(asked) == 1
    assert _finishes(lambda: callback(loading(4, True)))  # buffered while unbased: no second ask
    assert len(asked) == 1
    source.snap = dict(initial_snapshot(), seq=4, isLoading=True)  # type: ignore[assignment]
    assert _finishes(lambda: hub.rebase(source))
    page.pump(hub, client)
    assert page.state == hub.snapshot()["state"]
    assert page.state is not None and page.state["isLoading"] is True


def test_a_batch_that_does_not_apply_also_rebases() -> None:
    source = ScriptedSource()
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    assert source.callback is not None
    source.callback({"seq": 1, "ops": [{"type": "set", "path": ["messages", 5], "value": {}}]})
    assert asked == [True]


def test_a_full_chat_buffer_keeps_the_newest_batches() -> None:
    """Past MAX_CHAT_BUFFER an unbased hub drops the oldest batches: the newest are the ones
    a snapshot taken after them can still need (here the last one, cut after it)."""
    source = ScriptedSource()
    last = MAX_CHAT_BUFFER + 10
    source.during_snapshot = [loading(seq, seq == last) for seq in range(1, last + 1)]
    source.snap = dict(initial_snapshot(), seq=last - 1)  # type: ignore[assignment]
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    assert hub.snapshot()["state"]["isLoading"] is True
    assert asked == []


def _finishes(fn: Callable[[], None], timeout: float = 2.0) -> bool:
    """Run ``fn`` on a daemon thread. False when it is still running after ``timeout`` s: a
    thread that re-enters the hub's lock while holding it never ends."""
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    thread.join(timeout)
    return not thread.is_alive()


def test_a_logger_that_reads_the_hub_does_not_deadlock_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """Log handlers and filters are host code: the hub logs a lost chat line after its lock
    is released, as it calls ``on_resync``. (A filter, not a handler: a handler stuck on the
    lock would hold its own lock and hang ``logging.shutdown`` at exit.)"""
    source = ScriptedSource()
    hub = EventHub()
    hub.join(source)
    seen: List[int] = []

    def reads_the_hub(record: logging.LogRecord) -> bool:
        seen.append(hub.seq)
        return True

    real_apply_op = hub_module._apply_op

    def fails_on_the_last_key(root: Dict[str, Any], op: Any) -> None:
        if op["path"] == ["isAnonymous"]:
            raise OpError("injected")
        real_apply_op(root, op)

    ui_logger = logging.getLogger("talk2view.ui")
    ui_logger.addFilter(reads_the_hub)
    try:
        callback = source.callback
        assert callback is not None
        assert _finishes(lambda: callback(loading(2, True)))  # a gap: 1 went missing
        with monkeypatch.context() as patch:  # a snapshot that does not apply, under the lock
            patch.setattr(hub_module, "_apply_op", fails_on_the_last_key)
            snapshot = dict(initial_snapshot(), seq=2)
            assert _finishes(lambda: hub.base_chat(snapshot))  # type: ignore[arg-type]
        source.snap = dict(initial_snapshot(), seq=2)  # type: ignore[assignment]
        hub.rebase(source)
        bad: OpBatch = {"seq": 3, "ops": [{"type": "set", "path": ["messages", 5], "value": {}}]}
        assert _finishes(lambda: callback(bad))  # a batch that does not apply
    finally:
        ui_logger.removeFilter(reads_the_hub)
    assert seen == [1, 2, 3]


def test_set_slice_never_calls_into_the_callers_value_under_the_lock() -> None:
    hub = EventHub()
    hub.set_slice("prefs", {"a": 1})
    seen: List[int] = []

    class ReadsTheHub(Dict[str, Any]):
        def __eq__(self, other: object) -> bool:
            seen.append(hub.seq)
            return dict.__eq__(self, other)

    assert _finishes(lambda: hub.set_slice("prefs", ReadsTheHub(a=1)))
    assert hub.seq == 1  # the same JSON: no new batch
    assert seen == []


def _set_error(seq: int, value: Any) -> OpBatch:
    return {"seq": seq, "ops": [{"type": "set", "path": ["error"], "value": value}]}


def test_the_chat_line_reads_what_it_is_handed_before_it_locks() -> None:
    """A chat value's own code (a dict subclass's ``items()``) runs before the lock is taken,
    in a snapshot and in a batch; a value whose ``items()`` raises becomes a placeholder."""
    seen: List[int] = []
    hub = EventHub(on_resync=lambda: None)

    class ReadsTheHub(Dict[str, Any]):
        def items(self) -> Any:
            seen.append(hub.seq)  # under the hub's lock this would never return
            return dict.items(self)

    class Raises(Dict[str, Any]):
        def items(self) -> Any:
            raise RuntimeError("no items")

    source = ScriptedSource()
    source.snap = dict(initial_snapshot(), error=ReadsTheHub(a=1))  # type: ignore[assignment]
    assert _finishes(lambda: hub.join(source))
    callback = source.callback
    assert callback is not None
    assert _finishes(lambda: callback(_set_error(1, ReadsTheHub(b=2))))
    assert hub.snapshot()["state"]["error"] == {"b": 2}
    assert _finishes(lambda: callback(_set_error(2, Raises(c=3))))
    assert hub.snapshot()["state"]["error"] == "<Raises>"
    assert len(seen) == 2 and hub._chat_seq == 2


def test_slice_keys_and_chat_keys_are_plain_str_under_the_lock() -> None:
    """A str subclass's ``__hash__`` and ``__eq__`` are the caller's code: a slice key and a
    snapshot key become an exact ``str`` before the lock is taken."""
    hub = EventHub()

    class ReadsTheHub(str):
        def __hash__(self) -> int:
            _ = hub.seq  # under the hub's lock this would never return
            return str.__hash__(self)

        def __eq__(self, other: object) -> bool:
            _ = hub.seq
            return str.__eq__(self, other)

    assert _finishes(lambda: hub.set_slice(ReadsTheHub("theme"), 1))
    snapshot: Dict[Any, Any] = dict(initial_snapshot())
    snapshot[ReadsTheHub("extra")] = 1  # a chat key of the caller's own making
    source = ScriptedSource()
    source.snap = snapshot  # type: ignore[assignment]
    assert _finishes(lambda: hub.join(source))
    refused: List[ValueError] = []

    def slice_on_that_chat_key() -> None:
        try:
            hub.set_slice("extra", 2)  # compared with the stored chat keys, under the lock
        except ValueError as exc:
            refused.append(exc)

    assert _finishes(slice_on_that_chat_key)
    assert len(refused) == 1
    state = hub.snapshot()["state"]
    assert state["theme"] == 1 and state["extra"] == 1


def test_detach_wakes_a_page_waiting_on_another_thread() -> None:
    hub = EventHub()
    client = hub.attach()
    hub.next_event(client, 0.0)
    timer = threading.Timer(0.05, lambda: hub.detach(client))
    timer.start()
    started = time.monotonic()
    event = hub.next_event(client, 5.0)
    timer.join()
    assert event is None
    assert time.monotonic() - started < 2.0


# ── Values JSON cannot carry: scrubbed on the chat line, refused as a slice ──

PATH_PLACEHOLDER = "<{}>".format(type(pathlib.Path("/a")).__name__)  # PosixPath / WindowsPath


def _strict(constant: str) -> Any:
    raise ValueError("the page's JSON.parse rejects " + constant)


def test_values_json_cannot_carry_reach_the_page_as_placeholders(joined: Any) -> None:
    t2v, controller, hub = joined
    asked: List[bool] = []
    hub.set_resync_handler(lambda: asked.append(True))
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    args = {"x": float("nan"), "p": pathlib.Path("/a")}  # host data in a tool call
    t2v.chat_scripts.append([tool_call_chunk("t", "c1", args), stop_chunk()])
    t2v.chat_scripts.append([tool_call_chunk("t", "c2", {"y": float("-inf")}), stop_chunk()])
    controller.send("go")
    controller.send("again")  # a non-finite float alone: json.dumps would take it
    while True:
        event = hub.next_event(client, 0.0)
        if event is None or event[0] == "hb":
            break
        json.loads(event[2], parse_constant=_strict)  # no bare NaN or Infinity on the wire
        page.feed(*event)
    assert page.state is not None
    assert page.state["messages"][1]["steps"][0]["args"] == {"x": "nan", "p": PATH_PLACEHOLDER}
    assert page.state["messages"][3]["steps"][0]["args"] == {"y": "-inf"}
    assert asked == []
    assert page.state == hub.snapshot()["state"]


def test_a_snapshot_json_cannot_carry_is_scrubbed_not_rebased_forever() -> None:
    """Such a snapshot used to fail to apply, ask for a rebase, and fail
    the same way on every rebase."""
    t2v = FakeT2V()
    controller = make_controller(t2v)
    t2v.chat_scripts.append([tool_call_chunk("t", "c1", {"p": pathlib.Path("/a")}), stop_chunk()])
    controller.send("go")  # the value is in the controller's state before the hub joins
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    unsubscribe = hub.join(controller)
    try:
        rebases = 0
        while asked and rebases < 20:  # the router answers every request with a rebase
            asked.pop()
            hub.rebase(controller)
            rebases += 1
        assert rebases == 0
        steps = hub.snapshot()["state"]["messages"][1]["steps"]
        assert steps[0]["args"] == {"p": PATH_PLACEHOLDER}
    finally:
        unsubscribe()
        controller.close()


def test_set_slice_refuses_nan_and_compares_json_not_python_equality() -> None:
    hub = EventHub()
    for bad in (float("nan"), {"v": float("inf")}):
        with pytest.raises(ValueError):
            hub.set_slice("x", bad)
    assert hub.seq == 0
    hub.set_slice("flag", True)
    hub.set_slice("flag", 1)  # True == 1 in Python; not on the page
    hub.set_slice("n", 1)
    hub.set_slice("n", 1.0)
    assert hub.seq == 4
    assert json.dumps(hub.snapshot()["state"]) == '{"flag": 1, "n": 1.0}'


# ── The chat line: one controller, rebases that never roll back, any failure resyncs ──


def test_a_rebase_never_rolls_the_mirror_back(joined: Any) -> None:
    """A batch that lands between the rebase's snapshot and its base."""
    t2v, controller, hub = joined
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    overtaken = controller.snapshot()
    reply(t2v, "hello")
    controller.send("hi")  # the whole turn reaches the hub after that snapshot was cut
    before = hub.snapshot()
    hub.base_chat(overtaken)
    hub.rebase(controller)  # based and current: nothing to do either
    assert hub.snapshot() == before and len(before["state"]["messages"]) == 2
    assert page.pump(hub, client) == 1  # the turn, and no whole-state batch after it


def test_a_hub_follows_one_controller() -> None:
    class Broken(ScriptedSource):
        def snapshot(self) -> ChatSnapshot:
            raise RuntimeError("no snapshot")

    hub = EventHub()
    with pytest.raises(RuntimeError, match="no snapshot"):
        hub.join(Broken())  # a join that failed leaves the hub free to join
    hub.join(ScriptedSource())
    with pytest.raises(RuntimeError, match="one chat controller"):
        hub.join(ScriptedSource())


def test_a_batch_that_fails_partway_still_sends_the_ops_it_applied() -> None:
    """Two pages at the same seq must hold the same state."""
    source = ScriptedSource()
    hub = EventHub(on_resync=lambda: None)
    hub.join(source)
    old = PageModel()
    old_client = hub.attach()
    old.pump(hub, old_client)
    assert source.callback is not None
    source.callback(
        {
            "seq": 1,
            "ops": [
                {"type": "set", "path": ["isLoading"], "value": True},
                {"type": "set", "path": ["messages", 5, "x"], "value": 1},
            ],
        }
    )
    new = PageModel()
    new_client = hub.attach()
    new.pump(hub, new_client)
    old.pump(hub, old_client)
    assert old.seq == new.seq == hub.seq
    assert old.state == new.state == hub.snapshot()["state"]
    assert hub.snapshot()["state"]["isLoading"] is True


def test_no_error_in_a_chat_batch_escapes_or_leaves_the_line_based() -> None:
    """A value too deep for JSON is scrubbed; an op the mirror cannot even
    read loses the line like one that does not apply."""
    source = ScriptedSource()
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    assert source.callback is not None
    deep: Any = 0
    for _ in range(100_000):
        deep = [deep]
    source.callback({"seq": 1, "ops": [{"type": "set", "path": ["error"], "value": deep}]})
    assert asked == [] and hub._chat_seq == 1
    source.callback({"seq": 2, "ops": [{"type": "set"}]})  # type: ignore[typeddict-item]
    assert asked == [True] and hub._chat_seq is None


def test_a_rebase_that_raises_is_asked_for_again_at_most_once_a_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(hub_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class Broken(ScriptedSource):
        def snapshot(self) -> ChatSnapshot:
            raise RuntimeError("no snapshot")

    source = ScriptedSource()
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    callback = source.callback
    assert callback is not None
    callback(loading(2, True))  # a gap: asked at once
    with pytest.raises(RuntimeError):
        hub.rebase(Broken())  # the owner's answer failed: the hub still waits, unbased
    callback(loading(3, False))
    now[0] += 0.9
    callback(loading(4, True))
    assert len(asked) == 1  # not again within the second
    now[0] += 0.2
    callback(loading(5, False))
    callback(loading(6, True))
    assert len(asked) == 2  # once more, then quiet again
    source.snap = dict(initial_snapshot(), seq=6, isLoading=True)  # type: ignore[assignment]
    hub.rebase(source)
    callback(loading(8, True))  # a new gap after a rebase that worked: asked at once
    assert len(asked) == 3


# ── Forgetting the replay history: a sign-out or a switch ──


def first_kind(hub: EventHub, last_event_id: int) -> str:
    client = hub.attach(last_event_id=last_event_id)
    event = hub.next_event(client, 0.0)
    hub.detach(client)
    assert event is not None
    return event[0]


def private(seq: int) -> OpBatch:
    return {"seq": seq, "ops": [{"type": "set", "path": ["error"], "value": "ANA-PRIVATE"}]}


def test_forget_replay_sends_every_older_resume_a_snapshot() -> None:
    hub = EventHub()
    for i in range(5):
        hub.set_slice("k", i)
    assert first_kind(hub, 2) == "ops"
    hub.forget_replay()
    assert list(hub._ring) == []
    for since in (0, 2, 4):
        assert first_kind(hub, since) == "snapshot"
    assert first_kind(hub, 5) == "ops"  # a page already at the current seq is current
    assert hub.snapshot()["state"] == {"k": 4}  # the mirror itself is untouched


def test_nothing_is_kept_for_replay_until_a_rebase_passes_the_forget() -> None:
    """A batch cut before the controller dropped the conversation can still reach the hub
    after ``forget_replay()`` (delivered from inside a delivery window): it goes to the
    mirror and to live pages, never into the ring, until a rebase read after the forget."""
    source = ScriptedSource()
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    callback = source.callback
    assert callback is not None
    callback(loading(1, True))
    hub.forget_replay()
    assert asked == [True]  # the rebase that ends the forgetting is asked for at once
    callback(private(2))  # late: cut before the forget
    hub.set_slice("auth", {"user": None})
    assert list(hub._ring) == []
    assert hub.snapshot()["state"]["error"] == "ANA-PRIVATE"  # the mirror follows the line
    for since in range(hub.seq):
        assert first_kind(hub, since) == "snapshot"
    callback({"seq": 3, "ops": [{"type": "set", "path": ["error"], "value": None}]})
    source.snap = dict(initial_snapshot(), seq=3)  # type: ignore[assignment]
    hub.rebase(source)  # read after the forget: from here the ring collects again
    after = hub.seq
    callback(loading(4, False))
    assert first_kind(hub, after) == "ops"
    assert first_kind(hub, after - 1) == "snapshot"
    assert "ANA-PRIVATE" not in json.dumps(list(hub._ring))


def test_a_rebase_read_before_a_forget_is_dropped_not_applied() -> None:
    """The snapshot a rebase read before ``forget_replay()`` may hold the conversation that
    was dropped since: it is never applied, and the rebase the forget asked for is."""
    hub: EventHub

    class ForgetsDuringTheSnapshot(ScriptedSource):
        def snapshot(self) -> ChatSnapshot:
            stale = dict(initial_snapshot(), seq=5, error="ANA-PRIVATE")
            hub.forget_replay()  # the sign-out lands while the rebase is under way
            return stale  # type: ignore[return-value]

    source = ScriptedSource()
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.join(source)
    callback = source.callback
    assert callback is not None
    callback(loading(2, True))  # a gap: the hub is unbased and asks for a rebase
    assert asked == [True]
    hub.rebase(ForgetsDuringTheSnapshot())
    assert asked == [True, True]  # the forget asked for a rebase of its own
    assert hub._chat_seq is None and hub.snapshot()["state"]["error"] is None
    assert "ANA-PRIVATE" not in json.dumps(list(hub._ring))
    source.snap = dict(initial_snapshot(), seq=6)  # type: ignore[assignment]
    hub.rebase(source)  # read after the forget: applied, and the ring collects again
    assert hub._chat_seq == 6
    after = hub.seq
    callback(loading(7, True))
    assert first_kind(hub, after) == "ops"


def test_forget_replay_asks_for_its_rebase_outside_the_lock() -> None:
    seen: List[int] = []
    hub = EventHub(on_resync=lambda: seen.append(hub.seq))  # re-enters the hub
    hub.set_slice("k", 1)
    assert _finishes(hub.forget_replay)
    assert seen == [1]


def test_forget_replay_on_a_closed_hub_does_nothing() -> None:
    asked: List[bool] = []
    hub = EventHub(on_resync=lambda: asked.append(True))
    hub.set_slice("k", 1)
    hub.close()
    hub.forget_replay()
    assert asked == [] and len(hub._ring) == 1


def test_every_bound_is_at_least_one() -> None:
    for name in (
        "ring_size",
        "max_ring_bytes",
        "max_pending_ops",
        "max_pending_bytes",
        "max_clients",
    ):
        with pytest.raises(ValueError, match=name):
            EventHub(**{name: 0})


def test_byte_bounds_count_utf8_bytes() -> None:
    """300 emoji are 1200 bytes, not 300 characters."""
    hub = EventHub(max_pending_bytes=1000, max_ring_bytes=1000)
    client = hub.attach()
    hub.next_event(client, 0.0)
    hub.set_slice("x", "\U0001f600" * 300)
    event = hub.next_event(client, 0.0)
    assert event is not None and event[0] == "snapshot"  # the queue passed its bound
    [(_, ops, size)] = list(hub._ring)  # alone over the ring's bound: kept, as the newest
    compact = json.dumps(ops, ensure_ascii=False, separators=(",", ":"))
    assert size == hub._ring_bytes == len(compact.encode("utf-8")) > 1200
    hub.set_slice("s", json.loads('"\\ud83d"'))  # a lone surrogate is measured, not raised on
    assert hub.seq == 2


def test_the_chat_buffer_is_bounded_in_bytes_too() -> None:
    source = ScriptedSource()
    hub = EventHub(max_pending_bytes=300)
    hub.join(source)
    assert source.callback is not None
    source.callback(loading(2, True))  # a gap: from here the hub buffers
    for seq in range(3, 13):
        op = {"type": "set", "path": ["error"], "value": str(seq) * 50}
        source.callback({"seq": seq, "ops": [op]})  # type: ignore[list-item]
    buffered = list(hub._chat_buffer)
    assert hub._chat_buffer_bytes == sum(size for _, _, size in buffered) <= 300
    assert buffered[-1][0] == 12  # the newest are kept
    source.snap = dict(initial_snapshot(), seq=11)  # type: ignore[assignment]
    hub.rebase(source)
    assert hub.snapshot()["state"]["error"] == "12" * 50


def test_a_page_that_cannot_apply_an_ops_event_reconnects_for_a_snapshot() -> None:
    """The page's contract: an ops event that does not apply means reconnecting WITHOUT
    Last-Event-ID. With it, the ring replays the very op that failed, forever."""
    source = ScriptedSource()
    message = {"id": "m1", "role": "assistant", "content": "", "timestamp": "t"}
    source.snap = dict(initial_snapshot(), messages=[message])  # type: ignore[assignment]
    hub = EventHub()
    hub.join(source)
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    assert page.state is not None
    page.state["messages"] = []  # the page's copy went wrong somehow
    delta = {"type": "append-text", "path": ["messages", 0, "content"], "value": "hi"}
    assert source.callback is not None
    source.callback({"seq": 1, "ops": [delta]})  # type: ignore[list-item]
    replay = hub.attach(last_event_id=page.seq)  # what reconnecting WITH Last-Event-ID gets:
    assert json.loads(hub.next_event(replay, 0.0)[2])["ops"] == [delta]  # type: ignore[index]
    hub.detach(replay)
    assert page.pump(hub, client) == 1  # the snapshot on the new stream
    assert page.resyncs == 1 and page.kinds == ["snapshot", "ops", "snapshot"]
    assert page.client is not client and hub.client_count == 1
    assert page.state == hub.snapshot()["state"]


def test_a_page_keeps_to_its_live_stream_after_a_resync() -> None:
    """Passing the caller's original stream again reads the replacement a resync opened; a
    stream the caller has not passed before is adopted, and the one it replaces detached."""
    source = ScriptedSource()
    message = {"id": "m1", "role": "assistant", "content": "", "timestamp": "t"}
    source.snap = dict(initial_snapshot(), messages=[message])  # type: ignore[assignment]
    hub = EventHub()
    hub.join(source)
    page = PageModel()
    client = hub.attach()
    page.pump(hub, client)
    assert page.state is not None
    page.state["messages"] = []  # the page's copy went wrong somehow
    callback = source.callback
    assert callback is not None
    delta = {"type": "append-text", "path": ["messages", 0, "content"], "value": "x"}
    callback({"seq": 1, "ops": [delta]})  # type: ignore[list-item]
    page.pump(hub, client)  # the resync opens a replacement stream
    assert page.resyncs == 1 and page.client is not client
    callback({"seq": 2, "ops": [delta]})  # type: ignore[list-item]
    assert page.pump(hub, client) == 1  # the original again: the live replacement is read
    assert page.seq == hub.seq and page.state == hub.snapshot()["state"]
    assert page.pump(hub, page.client) == 0  # the replacement itself: the same stream
    assert hub.client_count == 1
    other = hub.attach()
    assert page.pump(hub, other) == 1  # a new stream: adopted (its snapshot)
    assert page.client is other and hub.client_count == 1  # never two streams


def test_a_page_detects_a_gap_it_is_handed() -> None:
    page = PageModel()
    page.feed("snapshot", 1, json.dumps({"seq": 1, "state": {}}))
    with pytest.raises(Gap):
        page.feed("ops", 3, json.dumps({"seq": 3, "base": 2, "ops": []}))


def test_threaded_stream_with_a_slow_reader_converges() -> None:
    """A real worker streams 300 deltas while a page reads slowly: it coalesces, never
    gaps, and ends on the controller's exact state."""
    t2v = FakeT2V()
    controller = ChatController(t2v)  # type: ignore[arg-type]  # default thread executor
    hub = EventHub()
    unsubscribe = hub.join(controller)
    page = PageModel()
    client = hub.attach()
    done = threading.Event()
    errors: List[BaseException] = []

    deadline = time.monotonic() + 30.0  # a reader that never catches up gives up, never hangs

    def read() -> None:
        try:
            while (not done.is_set() or page.seq != hub.seq) and time.monotonic() < deadline:
                event = hub.next_event(client, 0.05)
                if event is not None and event[0] != "hb":
                    page.feed(*event)
                time.sleep(0.002)
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    def slow_chunks(cancel: Optional[CancelToken]) -> None:
        time.sleep(0.0005)

    script: List[Any] = []
    for i in range(300):
        script += [text_chunk("w{} ".format(i)), slow_chunks]
    t2v.chat_scripts.append(script + [stop_chunk()])
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        controller.send("stream")
        assert controller.wait_idle(10.0)
        done.set()
        reader.join(10.0)
        assert not reader.is_alive() and errors == []
        assert page.state == hub.snapshot()["state"]
        assert chat_only(page.state or {}) == controller_state(controller)
        assert page.kinds.count("snapshot") == 1
        assert page.kinds.count("ops") < 300  # coalesced
    finally:
        done.set()
        unsubscribe()
        controller.close()
