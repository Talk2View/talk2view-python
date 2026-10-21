"""export_conversation / restore_conversation (ported from the TS index.ts pair)."""

from __future__ import annotations

from typing import List

import pytest

from talk2view import Decision
from talk2view.state import ConversationSnapshot
from talk2view.types import Attachment, ChatMessage

from .controller_fakes import (
    FakeT2V,
    Gate,
    error_chunk,
    interrupt_chunk,
    make_controller,
    stop_chunk,
    text_chunk,
)


def _two_turns(t2v: FakeT2V):  # type: ignore[no-untyped-def]
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [text_chunk("second reply"), stop_chunk()],
        [text_chunk("third reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("one")
    controller.send("two")
    return controller


def test_export_copies_messages_history_and_thread_id_without_is_streaming() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    snapshot = controller.export_conversation()
    assert [m["role"] for m in snapshot["messages"]] == ["user", "assistant", "user", "assistant"]
    assert all("isStreaming" not in m for m in snapshot["messages"])
    assert [(h["role"], h["content"]) for h in snapshot["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
        ("user", "two"), ("assistant", "second reply"),
    ]
    assert snapshot["threadId"] == controller.snapshot()["threadId"]
    # A copy: mutating it changes nothing in the controller.
    snapshot["messages"].clear()
    assert len(controller.snapshot()["messages"]) == 4


def test_export_mid_turn_drops_is_streaming_from_the_live_assistant_message() -> None:
    """``_two_turns`` runs every turn to completion, so the ``isStreaming`` pop at export
    time is never exercised there: every message is already finalised by the time it's
    exported. Parking on a ``Gate`` mid-reply proves the pop itself: a copy taken while a
    message is still streaming must never carry a cursor nothing goes on to write to."""
    t2v = FakeT2V()
    gate = Gate()
    t2v.chat_scripts = [[text_chunk("partial"), gate, text_chunk("more")]]
    controller = make_controller(
        t2v, executor=lambda fn: __import__("threading").Thread(target=fn, daemon=True).start()
    )
    controller.send("hi")
    assert gate.entered.wait(5)
    assert controller.snapshot()["messages"][-1]["isStreaming"] is True

    snapshot = controller.export_conversation()

    assert all("isStreaming" not in m for m in snapshot["messages"])
    assert snapshot["messages"][-1]["content"] == "partial"
    gate.release.set()
    assert controller.wait_idle(5)


def test_restore_replaces_the_conversation_and_the_next_send_replays_its_history() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    kept = controller.export_conversation()
    empty: ConversationSnapshot = {"messages": [], "history": [], "threadId": None}
    controller.restore_conversation(empty)
    assert controller.snapshot()["messages"] == []
    assert controller.snapshot()["threadId"] is None
    controller.restore_conversation(kept)
    snap = controller.snapshot()
    assert [m["content"] for m in snap["messages"]] == ["one", "first reply", "two", "second reply"]
    assert snap["isLoading"] is False and snap["error"] is None and snap["pendingApproval"] is None
    controller.send("three")
    assert [(h.role, h.content) for h in t2v.chat_calls[2]["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
        ("user", "two"), ("assistant", "second reply"),
    ]
    assert t2v.chat_calls[2]["message"] == "three"
    assert len(controller.snapshot()["messages"]) == 6


def test_restore_carries_the_last_user_messages_attachments_into_a_retry() -> None:
    """``_last_user_attachments`` is read straight off the restored snapshot's last user
    message (``controller.py``'s ``restore_conversation``): a retry after a restore must
    resend the same files, not lose them."""
    t2v = FakeT2V()
    t2v.chat_scripts = [[text_chunk("retried reply"), stop_chunk()]]
    controller = make_controller(t2v)
    restored: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "user", "content": "look at this",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "attachments": [
                    {"id": "a1", "filename": "scan.png", "mime_type": "image/png", "size_bytes": 9}
                ],
            },
        ],
        "history": [{"role": "user", "content": "look at this"}],
        "threadId": None,
    }

    controller.restore_conversation(restored)
    controller.retry()

    assert t2v.chat_calls[0]["message"] == "look at this"
    assert t2v.chat_calls[0]["attachments"] == [
        Attachment(id="a1", filename="scan.png", mime_type="image/png", size_bytes=9)
    ]


def test_restore_detaches_the_engine_session_and_forgets_always_allowed() -> None:
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [interrupt_chunk("delete_volume", "tc-1", {"name": "CT"})],
        [text_chunk("done"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("delete it")
    controller.approve(Decision(action="always"))
    assert controller.snapshot()["alwaysAllowedTools"] == ["delete_volume"]
    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    assert controller.snapshot()["alwaysAllowedTools"] == []
    assert t2v.detached == 1 and t2v.cleared == 0


def test_restore_mid_turn_abandons_the_stream_and_the_next_message_id_stays_unique() -> None:
    t2v = FakeT2V()
    gate = Gate()
    t2v.chat_scripts = [
        [text_chunk("partial"), gate, text_chunk("never")],
        [text_chunk("ok"), stop_chunk()],
    ]
    controller = make_controller(
        t2v, executor=lambda fn: __import__("threading").Thread(target=fn, daemon=True).start()
    )
    controller.send("slow")
    assert gate.entered.wait(5)
    restored: ConversationSnapshot = {
        "messages": [
            {"id": "msg_7_1", "role": "user", "content": "old",
             "timestamp": "2026-01-01T00:00:00.000Z"},
            {"id": "msg_8_1", "role": "assistant", "content": "older reply",
             "timestamp": "2026-01-01T00:00:01.000Z"},
        ],
        "history": [
            {"role": "user", "content": "old"}, {"role": "assistant", "content": "older reply"},
        ],
        "threadId": "th-old",
    }
    controller.restore_conversation(restored)
    gate.release.set()
    assert controller.wait_idle(5)
    assert [m["content"] for m in controller.snapshot()["messages"]] == ["old", "older reply"]
    controller.send("new")
    ids = [m["id"] for m in controller.snapshot()["messages"]]
    assert len(ids) == len(set(ids))
    assert int(ids[-1].split("_")[1]) > 8  # the counter moved past the restored ids


def test_retry_after_restore_cuts_history_at_the_restored_turn() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    kept = controller.export_conversation()
    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)
    controller.retry()
    call = t2v.chat_calls[2]
    assert call["message"] == "two"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
    ]


def test_edit_after_restore_cuts_history_at_the_edited_turn_past_a_failed_turn() -> None:
    """A rolled-back turn ("two" fails: no assistant text, so its entry is popped out of
    ``_history`` but stays on screen, ``_rollback_orphan_turn``) breaks pairing display
    messages with history entries by position: display has one more user message than
    history has user entries for the turns before "three". ``_marks_for`` must pair them by
    content instead, so an edit after a restore cuts at the same point a live edit would."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [error_chunk("boom")],
        [text_chunk("third reply"), stop_chunk()],
        [text_chunk("edited reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("one")
    controller.send("two")
    controller.send("three")
    kept = controller.export_conversation()
    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)

    three_id = next(
        m["id"] for m in controller.snapshot()["messages"] if m["content"] == "three"
    )
    controller.edit(three_id, "three-edited")

    call = t2v.chat_calls[3]
    assert call["message"] == "three-edited"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
    ]


def test_retry_after_restore_cuts_only_the_retried_turn_past_a_failed_turn() -> None:
    """Same setup as the edit test, plus a fourth, good turn: retrying "four" after a
    restore must drop only its own user+assistant pair, not the whole restored history."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [error_chunk("boom")],
        [text_chunk("third reply"), stop_chunk()],
        [text_chunk("fourth reply"), stop_chunk()],
        [text_chunk("retried reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("one")
    controller.send("two")
    controller.send("three")
    controller.send("four")
    kept = controller.export_conversation()
    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)

    controller.retry()

    call = t2v.chat_calls[4]
    assert call["message"] == "four"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
        ("user", "three"), ("assistant", "third reply"),
    ]


# ── carried marks disambiguate repeated user text ──
#
# Content matching alone cannot tell which of several identical user messages was rolled
# back: "hi" sent four times, the second failing with no assistant text. TS's own
# retryLastMessage has no marks at all -- it pops the trailing assistant then user entries
# from both _messages and _conversationHistory by structure -- and for this transcript
# that gets [hi, r1, hi, r3] (index.ts:1032-1046). That is also what a *live* Python retry
# sends (retry() cuts by _history_marks, set at send time, never reconstructed), so
# "live must match TS" already held; the gap was a *restored*
# conversation reproducing it, which is what these four tests cover.


def test_restore_uses_carried_marks_to_disambiguate_repeated_user_text() -> None:
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [error_chunk("boom")],
        [text_chunk("third reply"), stop_chunk()],
        [text_chunk("fourth reply"), stop_chunk()],
        [text_chunk("retried reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    kept = controller.export_conversation()
    assert "marks" in kept  # export now carries them

    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)
    controller.retry()

    call = t2v.chat_calls[4]
    assert call["message"] == "hi"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "hi"), ("assistant", "first reply"),
        ("user", "hi"), ("assistant", "third reply"),
    ]


def test_restore_uses_carried_marks_for_edit_with_repeated_user_text() -> None:
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [error_chunk("boom")],
        [text_chunk("third reply"), stop_chunk()],
        [text_chunk("fourth reply"), stop_chunk()],
        [text_chunk("edited reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    kept = controller.export_conversation()

    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)
    third_hi_id = [m["id"] for m in controller.snapshot()["messages"] if m["role"] == "user"][2]
    controller.edit(third_hi_id, "hi-edited")

    call = t2v.chat_calls[4]
    assert call["message"] == "hi-edited"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "hi"), ("assistant", "first reply"),
    ]


def test_restore_falls_back_correctly_for_repeated_user_text_without_stored_marks() -> None:
    """A TS-written (or older) snapshot has no ``marks`` key at all. The hardened
    ``_marks_for`` fallback (only match-and-consume a user message directly followed by an
    assistant message) must still reproduce the live/TS answer for the four-"hi" repro."""
    t2v = FakeT2V()
    t2v.chat_scripts = [
        [text_chunk("first reply"), stop_chunk()],
        [error_chunk("boom")],
        [text_chunk("third reply"), stop_chunk()],
        [text_chunk("fourth reply"), stop_chunk()],
        [text_chunk("retried reply"), stop_chunk()],
    ]
    controller = make_controller(t2v)
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    controller.send("hi")
    kept = controller.export_conversation()
    kept.pop("marks", None)  # simulate a snapshot TS wrote, or an older one

    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(kept)
    controller.retry()

    call = t2v.chat_calls[4]
    assert call["message"] == "hi"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "hi"), ("assistant", "first reply"),
        ("user", "hi"), ("assistant", "third reply"),
    ]


def test_restore_ignores_invalid_stored_marks_and_falls_back_safely() -> None:
    """A stored ``marks`` dict that is malformed (here: out of bounds for the carried
    history) must not be trusted: it is silently ignored, and ``_marks_for`` computes the
    marks instead, exactly as if ``marks`` had been absent."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    kept = controller.export_conversation()
    one_id = next(m["id"] for m in kept["messages"] if m["content"] == "one")
    two_id = next(m["id"] for m in kept["messages"] if m["content"] == "two")
    hostile = dict(kept)
    hostile["marks"] = {one_id: 0, two_id: 99}  # 99 is past the end of a 4-entry history

    controller.restore_conversation({"messages": [], "history": [], "threadId": None})
    controller.restore_conversation(hostile)  # must not raise or reject the whole restore
    controller.retry()

    call = t2v.chat_calls[2]
    assert call["message"] == "two"
    assert [(h.role, h.content) for h in call["history"]] == [
        ("user", "one"), ("assistant", "first reply"),
    ]


# ── restore_conversation on hostile/corrupt snapshots ──
#
# These bypass ConversationStore on purpose: the store already filters what it hands back
# (test_conversations.py), so restore_conversation must not trust its argument on its own,
# the way a host that builds a snapshot by hand, or a future caller, could still reach it
# with something malformed. Every case here must be a no-op (logged), never a raise and
# never a half-applied state -- validation happens before anything is touched.


def test_restore_rejects_a_unicode_digit_id_and_leaves_the_conversation_unchanged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``str.isdigit()`` is true for a superscript two, but ``int()`` raises on it -- this
    must never reach ``int()``, and the whole snapshot is treated as corrupt rather than
    partially trusted."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_²_1", "role": "user", "content": "hi",
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ],
        "history": [{"role": "user", "content": "hi"}],
        "threadId": "th-1",
    }
    caplog.set_level("WARNING", logger="talk2view.controller")

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before
    assert "restore_conversation" in caplog.text


def test_restore_accepts_an_id_with_a_non_numeric_msg_prefix() -> None:
    """``msg_welcome`` is not a digit run at all -- it never reaches ``int()`` either way
    -- so it is an opaque, host-assigned id, not a hostile one. Only a digit run that is
    non-ASCII or too long for ``int()`` (``msg_<superscript>_1``, ``msg_<5000 nines>_1``)
    is hostile; an id that merely isn't in the counter shape must restore fully."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    friendly: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_welcome", "role": "user", "content": "hi",
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ],
        "history": [{"role": "user", "content": "hi"}],
        "threadId": "th-1",
    }

    controller.restore_conversation(friendly)

    snap = controller.snapshot()
    assert [m["content"] for m in snap["messages"]] == ["hi"]
    assert snap["threadId"] == "th-1"


def test_restore_rejects_an_id_with_too_many_digits_and_leaves_the_conversation_unchanged() -> None:
    """One digit past cpython's own ``int()`` string-conversion limit: never attempted."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_{}_1".format("9" * 4301), "role": "user", "content": "hi",
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ],
        "history": [{"role": "user", "content": "hi"}],
        "threadId": "th-1",
    }

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before


def test_restore_rejects_a_malformed_attachment_and_leaves_the_conversation_unchanged() -> None:
    """A non-dict attachment element would make ``Attachment(**a)`` raise ``TypeError`` --
    validated and rejected before ``restore_conversation`` touches any state, not raised
    from inside the locked mutation."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "user", "content": "",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "attachments": ["scan.png"],  # type: ignore[list-item]
            },
        ],
        "history": [],
        "threadId": None,
    }

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before


def test_restore_rejects_a_malformed_step_and_leaves_the_conversation_unchanged() -> None:
    """A stored step this malformed would
    otherwise reach the page's ``convert.ts`` verbatim -- ``step.status`` on a ``None``
    throws in render, and because the newest stored conversation is restored at every
    start, that blanks the chat on every launch until the file is deleted by hand. Rejected
    before anything is touched, the same as a malformed attachment."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "assistant", "content": "done",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "steps": [None],  # type: ignore[list-item]
            },
        ],
        "history": [],
        "threadId": None,
    }

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before


def test_restore_rejects_a_non_string_plan_and_leaves_the_conversation_unchanged() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "assistant", "content": "done",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "plan": ["not", "a", "string"],  # type: ignore[typeddict-item]
            },
        ],
        "history": [],
        "threadId": None,
    }

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before


def test_restore_accepts_well_formed_steps_and_a_plan() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    friendly: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "assistant", "content": "done",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "plan": "- [x] step one",
                "steps": [{"name": "get_scene_context", "status": "used", "result": "ok"}],
            },
        ],
        "history": [{"role": "assistant", "content": "done"}],
        "threadId": None,
    }

    controller.restore_conversation(friendly)

    snap = controller.snapshot()
    assert snap["messages"][0]["plan"] == "- [x] step one"
    assert snap["messages"][0]["steps"] == [
        {"name": "get_scene_context", "status": "used", "result": "ok"}
    ]


def test_restore_drops_a_non_string_tool_call_id_from_a_history_entry() -> None:
    """``_chat_message`` (``controller.py``) passed a history
    entry's ``tool_call_id`` straight into ``ChatMessage`` untyped. A ``system`` entry
    (accepted; only ``tool`` is rejected outright) with a non-string one must restore, but
    without carrying it: exported again, the key is absent, same as any entry that never
    had one (``_history_entry`` only writes the key when it is not None)."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    hostile: ConversationSnapshot = {
        "messages": [],
        "history": [
            {"role": "system", "content": "a note", "tool_call_id": 12345},  # type: ignore[typeddict-item]
        ],
        "threadId": None,
    }

    controller.restore_conversation(hostile)

    exported = controller.export_conversation()
    assert exported["history"] == [{"role": "system", "content": "a note"}]


def test_restore_rejects_a_bad_role_and_a_non_string_content() -> None:
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()

    bad_role: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "system", "content": "hi",  # type: ignore[typeddict-item]
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ],
        "history": [],
        "threadId": None,
    }
    controller.restore_conversation(bad_role)
    assert controller.snapshot() == before

    bad_content: ConversationSnapshot = {
        "messages": [
            {
                "id": "msg_1_1", "role": "user", "content": 5,  # type: ignore[typeddict-item]
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
        ],
        "history": [],
        "threadId": None,
    }
    controller.restore_conversation(bad_content)
    assert controller.snapshot() == before


def test_restore_rejects_messages_that_share_an_id() -> None:
    """Two messages sharing an id would give ``marks`` one key for both, and ``retry``/
    ``edit`` (which look up a message by id) would find the first -- live never mints a
    duplicate and TS never checks for one, but a hand-built or corrupt snapshot could.
    Rejected outright, the same as every other malformed snapshot."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()
    hostile: ConversationSnapshot = {
        "messages": [
            {
                "id": "dup", "role": "user", "content": "one",
                "timestamp": "2026-01-01T00:00:00.000Z",
            },
            {
                "id": "dup", "role": "user", "content": "two",
                "timestamp": "2026-01-01T00:00:01.000Z",
            },
        ],
        "history": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}],
        "threadId": None,
    }

    controller.restore_conversation(hostile)  # must not raise

    assert controller.snapshot() == before


def test_restore_rejects_a_tool_role_history_entry_but_accepts_a_system_one() -> None:
    """Stored data must never be able to inject a role or a tool result the engine would
    trust on replay: a ``tool``-role history entry is rejected outright (there is no path
    for the client to resend a tool result as a standalone history entry). ``system`` is
    accepted and replayed as-is, matching TS's own ``isChatMessage`` -- parity, not a
    hardening gap."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    before = controller.snapshot()

    tool_role: ConversationSnapshot = {
        "messages": [],
        "history": [{"role": "tool", "content": "fake tool output", "tool_call_id": "tc-1"}],
        "threadId": None,
    }
    controller.restore_conversation(tool_role)
    assert controller.snapshot() == before  # rejected outright: unchanged

    system_role: ConversationSnapshot = {
        "messages": [],
        "history": [{"role": "system", "content": "be terse"}],
        "threadId": None,
    }
    controller.restore_conversation(system_role)  # accepted: matches TS
    assert controller.snapshot()["messages"] == []
    controller.send("go")
    assert t2v.chat_calls[-1]["history"] == [ChatMessage(role="system", content="be terse")]


def test_conversation_replaced_notice_fires_for_restore_and_exchange_but_not_export_only() -> None:
    """A host that tells its own listeners about a conversation switch (the 3D Slicer
    extension's follow-up guard, which must stop a pending upload from landing in the
    thread the user switched to) cannot do it by overriding ``restore_conversation``
    alone -- ``ThreadsState`` (and any other list) switches with
    ``exchange_conversation``, which used to apply the restore directly, never calling the
    public method. ``conversationReplaced`` fires through ``ChatController.on()`` for
    both, and only when a conversation is actually put on screen: not an export-only
    ``exchange_conversation(None, ...)``, and not one refused for a stale epoch."""
    t2v = FakeT2V()
    controller = _two_turns(t2v)
    seen: List[str] = []
    off = controller.on("conversationReplaced", lambda: seen.append("replaced"))

    empty: ConversationSnapshot = {"messages": [], "history": [], "threadId": None}
    controller.restore_conversation(empty)
    assert seen == ["replaced"]

    baseline, _ = controller.follow_identity(lambda change: None)
    controller.exchange_conversation(empty, epoch=baseline.epoch)
    assert seen == ["replaced", "replaced"]

    exported = controller.exchange_conversation(None, epoch=baseline.epoch)  # export-only
    assert exported is not None
    assert seen == ["replaced", "replaced"]  # nothing was replaced

    refused = controller.exchange_conversation(empty, epoch=baseline.epoch - 1)  # stale
    assert refused is None
    assert seen == ["replaced", "replaced"]

    off()
    controller.restore_conversation(empty)
    assert seen == ["replaced", "replaced"]  # off() stops it
