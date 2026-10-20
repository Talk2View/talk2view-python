"""Op batches: apply_batch is pure, OpRecorder keeps state and ops in lockstep."""

from __future__ import annotations

import copy
import random
from typing import Any, Dict, List

import pytest

from talk2view.state import (
    ChatSnapshot,
    DisplayMessage,
    OpBatch,
    OpError,
    OpRecorder,
    SeqGapError,
    apply_batch,
    initial_snapshot,
)


def _message(i: int, role: str = "assistant") -> DisplayMessage:
    msg: DisplayMessage = {
        "id": "msg_{}".format(i),
        "role": "assistant" if role == "assistant" else "user",
        "content": "",
        "timestamp": "2026-09-17T00:00:00.000Z",
    }
    if role == "assistant":
        msg["isStreaming"] = True
    return msg


def _random_mutation(rng: random.Random, rec: OpRecorder, counter: List[int]) -> None:
    """One controller-like mutation, chosen at random."""
    messages = rec.state["messages"]
    choice = rng.randrange(12)
    if choice == 0 or not messages:
        counter[0] += 1
        rec.append_message(_message(counter[0], rng.choice(["user", "assistant"])))
    elif choice in (1, 2, 3):
        index = rng.randrange(len(messages))
        rec.append_text(["messages", index, "content"], rng.choice(["a", "bc ", "é", "\U0001f600"]))
    elif choice == 4:
        rec.set(["messages", rng.randrange(len(messages)), "isStreaming"], rng.random() < 0.5)
    elif choice == 5:
        index = rng.randrange(len(messages))
        steps = messages[index].get("steps")
        step: Dict[str, Any] = {"name": "t{}".format(rng.randrange(3)), "status": "running"}
        if steps is None:
            rec.set(["messages", index, "steps"], [step])
        else:
            rec.set(["messages", index, "steps", len(steps)], step)
    elif choice == 6:
        index = rng.randrange(len(messages))
        steps = messages[index].get("steps")
        if steps:
            which = rng.randrange(len(steps))
            rec.set(["messages", index, "steps", which], {**steps[which], "status": "used"})
    elif choice == 7:
        rec.set(["messages", rng.randrange(len(messages)), "plan"], "- [ ] step")
    elif choice == 8:
        rec.replace_messages(list(messages[: rng.randrange(len(messages) + 1)]))
    elif choice == 9:
        rec.set(["isLoading"], rng.random() < 0.5)
        rec.set(["error"], rng.choice([None, "boom"]))
    elif choice == 10:
        rec.set(
            ["pendingApproval"],
            rng.choice(
                [
                    None,
                    {"toolCallId": "c1", "toolName": "t", "arguments": {"a": 1}, "description": ""},
                ]
            ),
        )
        rec.set(["agentStatus"], rng.choice([None, {"type": "thinking", "message": "..."}]))
    else:
        rec.set(["threadId"], rng.choice([None, "thread_1"]))
        rec.set(["alwaysAllowedTools"], rng.sample(["a", "b", "c"], rng.randrange(4)))


@pytest.mark.parametrize("seed", [1, 7, 42, 1297, 20260917])
def test_apply_ops_reproduces_snapshot(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 — deterministic test data, not cryptography
    initial = initial_snapshot()
    rec = OpRecorder(initial)
    replayed: ChatSnapshot = copy.deepcopy(initial)
    counter = [0]

    for _ in range(400):
        _random_mutation(rng, rec, counter)
        if rng.random() < 0.4:
            batch = rec.flush()
            if batch is not None:
                replayed = apply_batch(replayed, batch)
                assert replayed == rec.snapshot()

    last = rec.flush()
    if last is not None:
        replayed = apply_batch(replayed, last)
    assert replayed == rec.snapshot()
    assert initial == initial_snapshot()  # the recorder copied its seed; nothing leaked back


def test_seq_monotonic() -> None:
    rec = OpRecorder()
    assert rec.flush() is None  # nothing recorded -> no batch, seq untouched
    assert rec.state["seq"] == 0

    seqs: List[int] = []
    for i in range(5):
        rec.set(["error"], "e{}".format(i))
        batch = rec.flush()
        assert batch is not None
        seqs.append(batch["seq"])
        assert rec.flush() is None
    assert seqs == [1, 2, 3, 4, 5]
    assert rec.snapshot()["seq"] == 5


def test_apply_batch_rejects_a_seq_gap() -> None:
    snap = initial_snapshot()
    gap: OpBatch = {"seq": 2, "ops": [{"type": "set", "path": ["error"], "value": "x"}]}
    with pytest.raises(SeqGapError):
        apply_batch(snap, gap)


def test_apply_batch_does_not_mutate_its_arguments() -> None:
    snap = initial_snapshot()
    message = _message(1)
    batch: OpBatch = {"seq": 1, "ops": [{"type": "set", "path": ["messages", 0], "value": message}]}
    frozen_snap, frozen_batch = copy.deepcopy(snap), copy.deepcopy(batch)

    result = apply_batch(snap, batch)
    result["messages"][0]["content"] = "changed"

    assert snap == frozen_snap
    assert batch == frozen_batch
    assert message["content"] == ""


def test_append_text_on_missing_path_raises() -> None:
    rec = OpRecorder()
    with pytest.raises(OpError):
        rec.append_text(["messages", 0, "content"], "hello")  # no message 0
    rec.append_message(_message(1))
    with pytest.raises(OpError):
        rec.append_text(["messages", 0, "plan"], "hello")  # key not set yet
    with pytest.raises(OpError):
        rec.append_text(["messages", 0, "isStreaming"], "hello")  # not a string
    assert rec.flush() == {
        "seq": 1,
        "ops": [{"type": "set", "path": ["messages", 0], "value": _message(1)}],
    }


def test_structural_shrink_uses_whole_list_set() -> None:
    rec = OpRecorder()
    for i in range(3):
        rec.append_message(_message(i))
    rec.flush()

    rec.replace_messages(list(rec.state["messages"][:1]))
    batch = rec.flush()

    assert batch is not None
    assert batch["ops"] == [{"type": "set", "path": ["messages"], "value": [_message(0)]}]
    assert len(rec.state["messages"]) == 1


def test_set_past_the_end_of_a_list_raises() -> None:
    rec = OpRecorder()
    with pytest.raises(OpError):
        rec.set(["messages", 1], _message(1))  # only index == len appends


def test_ops_are_isolated_from_later_mutation() -> None:
    rec = OpRecorder()
    message = _message(1)
    rec.append_message(message)
    message["content"] = "mutated by the caller"
    rec.append_text(["messages", 0, "content"], "hi")
    batch = rec.flush()
    assert batch is not None
    assert batch["ops"][0]["value"]["content"] == ""
    assert rec.state["messages"][0]["content"] == "hi"


def test_empty_append_text_records_nothing() -> None:
    rec = OpRecorder()
    rec.append_message(_message(1))
    rec.flush()
    rec.append_text(["messages", 0, "content"], "")
    assert rec.pending is False


def test_unknown_op_type_and_seq_path_are_rejected() -> None:
    snap = initial_snapshot()
    with pytest.raises(OpError):
        apply_batch(snap, {"seq": 1, "ops": [{"type": "delete", "path": ["error"], "value": None}]})  # type: ignore[typeddict-item]
    with pytest.raises(OpError):
        apply_batch(snap, {"seq": 1, "ops": [{"type": "set", "path": ["seq"], "value": 9}]})
    with pytest.raises(OpError):
        apply_batch(snap, {"seq": 1, "ops": [{"type": "set", "path": [], "value": 9}]})


class TestApplyBatchRefusesMalformedPaths:
    """The defensive ``OpError`` branches of ``_apply_op``. A batch is data from another
    process (the chat page, a fixture), so every wrong shape must be a clean ``OpError``,
    never a ``TypeError``/``KeyError`` out of the applier -- and never a partial apply."""

    def _apply(self, op: Dict[str, Any]) -> None:
        snap = initial_snapshot()
        snap["messages"].append(_message(1))
        batch: OpBatch = {"seq": 1, "ops": [op]}  # type: ignore[typeddict-item]
        with pytest.raises(OpError):
            apply_batch(snap, batch)

    def test_stepping_into_a_scalar_or_with_the_wrong_key_type(self) -> None:
        # None has no children; a str is not a list index; neither is a bool.
        self._apply({"type": "set", "path": ["error", "x", "y"], "value": 1})
        self._apply({"type": "set", "path": ["messages", "0", "content"], "value": ""})
        self._apply({"type": "set", "path": ["messages", True, "content"], "value": ""})

    def test_set_on_a_list_needs_an_integer_index(self) -> None:
        self._apply({"type": "set", "path": ["messages", "0"], "value": _message(2)})
        self._apply({"type": "set", "path": ["messages", False], "value": _message(2)})

    def test_set_on_a_dict_needs_a_string_key(self) -> None:
        self._apply({"type": "set", "path": ["skills", 0], "value": []})
        self._apply({"type": "set", "path": ["messages", 0, 3], "value": "x"})

    def test_set_inside_a_scalar_is_refused(self) -> None:
        self._apply({"type": "set", "path": ["isLoading", "x"], "value": True})
        self._apply({"type": "set", "path": ["messages", 0, "content", 0], "value": "x"})

    def test_a_refused_op_leaves_the_snapshot_untouched(self) -> None:
        snap = initial_snapshot()
        frozen = copy.deepcopy(snap)
        batch: OpBatch = {
            "seq": 1,
            "ops": [
                {"type": "set", "path": ["error"], "value": "applied first"},
                {"type": "set", "path": ["skills", 0], "value": []},
            ],
        }
        with pytest.raises(OpError):
            apply_batch(snap, batch)
        assert snap == frozen  # apply_batch works on a copy: nothing leaks on failure
