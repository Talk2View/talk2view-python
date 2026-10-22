"""talk2view.conversations: the TS conversation-store.ts shape, caps and rules."""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, Optional

from talk2view import MemoryStorage
from talk2view.conversations import (
    MAX_CHARS,
    MAX_CONVERSATIONS,
    MAX_IDENTITIES,
    MAX_STORED_MESSAGES,
    MAX_STORED_TITLE,
    Conversation,
    ConversationStore,
    identity_key,
    storage_key,
    title_from,
)
from talk2view.state import ConversationSnapshot, DisplayMessage


def _message(i: int, role: str = "user", content: str = "hello") -> DisplayMessage:
    return {  # type: ignore[return-value]
        "id": "msg_{}_1".format(i), "role": role, "content": content,
        "timestamp": "2026-09-24T00:00:00.000Z",
    }


def _conversation(n: int, updated_at: int, content: str = "hello") -> Conversation:
    snapshot: ConversationSnapshot = {
        "messages": [_message(n, content=content)],
        "history": [{"role": "user", "content": content}],
        "threadId": None,
    }
    return Conversation(
        id="c_{}".format(n), title=title_from(snapshot["messages"][0]),
        created_at=updated_at, updated_at=updated_at, snapshot=snapshot,
    )


def _raw_conversation(n: int) -> Dict[str, Any]:
    """A minimal on-disk ``StoredConversation`` -- for seeding a file directly via
    ``storage.set``, as ``write()``'s LRU pass reads identity buckets without deep-parsing
    each conversation."""
    return {
        "id": "c_{}".format(n),
        "title": "hello",
        "createdAt": 1000 + n,
        "updatedAt": 1000 + n,
        "threadId": None,
        "messages": [_message(n)],
        "history": [{"role": "user", "content": "hello"}],
    }


def test_key_and_identity_match_the_typescript_store() -> None:
    assert storage_key("pk_live_x") == "t2v_chat_conversations_v1:pk_live_x"
    assert identity_key("u-1") == "u:u-1"
    assert identity_key(None) == "none"
    assert identity_key("") == "none"


def test_title_from_the_first_user_message_cut_at_a_word() -> None:
    assert title_from(None) is None
    assert title_from(_message(1, content="  Load   the\nMRHead sample ")) == (
        "Load the MRHead sample"
    )
    long = "Segment the liver and report its volume in millilitres please now"
    assert title_from(_message(1, content=long)) == (
        "Segment the liver and report its volume in millilitres…"
    )
    assert title_from(_message(1, content="x" * 70)) == "x" * 60 + "…"
    with_file: DisplayMessage = dict(  # type: ignore[assignment]
        _message(1, content="   "),
        attachments=[
            {"id": "a", "filename": "scan.png", "mime_type": "image/png", "size_bytes": 1}
        ],
    )
    assert title_from(with_file) == "scan.png"


def test_title_from_is_total_on_a_message_a_host_built_by_hand() -> None:
    """``title_from`` must not raise on inputs the store itself never hands out (a host
    that calls it directly, or a stored message a future validation gap lets through):
    ``content`` missing or not a string, and an attachments list whose first element is
    not a dict."""
    no_content: DisplayMessage = {"id": "m", "role": "user", "timestamp": "t"}  # type: ignore[typeddict-item]
    assert title_from(no_content) is None
    none_content: DisplayMessage = {"id": "m", "role": "user", "content": None, "timestamp": "t"}  # type: ignore[typeddict-item]
    assert title_from(none_content) is None
    bad_attachment: DisplayMessage = dict(  # type: ignore[assignment]
        _message(1, content="   "), attachments=["scan.png"]
    )
    assert title_from(bad_attachment) is None


def test_write_then_read_round_trips_the_typescript_json_shape() -> None:
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    assert store.write("u:ana", [_conversation(1, 1000), _conversation(2, 2000)], "c_2") is True
    raw = json.loads(storage.get("t2v_chat_conversations_v1:pk_test") or "")
    assert raw["v"] == 1
    bucket = raw["identities"]["u:ana"]
    assert [c["id"] for c in bucket["conversations"]] == ["c_2", "c_1"]  # newest first
    assert set(bucket["conversations"][0]) == {
        "id", "title", "createdAt", "updatedAt", "threadId", "messages", "history",
    }
    assert bucket["conversations"][0]["messages"][0]["timestamp"] == "2026-09-24T00:00:00.000Z"
    read = store.read("u:ana")
    assert [c.id for c in read] == ["c_2", "c_1"]
    assert read[0].snapshot["history"] == [{"role": "user", "content": "hello"}]
    assert store.read("u:bob") == []


def test_write_then_read_round_trips_marks() -> None:
    """``marks`` (round-tripped from ``ChatController.export_conversation``'s optional
    field) must survive a write/read cycle: it is what lets a restored conversation
    disambiguate repeated user text."""
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    with_marks = _conversation(1, 1000)
    with_marks.snapshot["marks"] = {"msg_1_1": 0}

    store.write("u:ana", [with_marks], "c_1")

    raw = json.loads(storage.get("t2v_chat_conversations_v1:pk_test") or "")
    assert raw["identities"]["u:ana"]["conversations"][0]["marks"] == {"msg_1_1": 0}
    read = store.read("u:ana")
    assert read[0].snapshot["marks"] == {"msg_1_1": 0}


def test_read_drops_malformed_marks_but_keeps_the_conversation() -> None:
    """The store's shape check for ``marks`` is looser than ``restore_conversation``'s
    semantic one (right ids, in bounds, non-decreasing) -- that happens again at restore
    time against whatever messages actually survived parsing. Here, only the shape (a dict
    of message id to a non-boolean int) is checked; anything else drops the key, not the
    conversation."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "messages": [_message(1)],
                                "history": [],
                                "marks": ["not", "a", "dict"],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    assert "marks" not in read[0].snapshot


def test_read_never_throws_and_drops_what_does_not_validate() -> None:
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(key, "not json")
    assert store.read("u:ana") == []
    storage.set(key, json.dumps({"v": 1, "identities": {"u:ana": {"updatedAt": 1, "conversations": [
        {"id": "", "messages": []},
        {"id": "ok", "title": "", "createdAt": "x", "updatedAt": 5, "threadId": 3,
         "messages": [{"id": "m", "role": "user", "content": "hi", "timestamp": "bad"},
                      {"id": "m2", "role": "tool", "content": "x"}, "junk"],
         "history": [{"role": "user", "content": "hi"}, {"role": "robot", "content": "x"},
                     {"role": "assistant", "content": 5}]},
    ]}}}))
    read = store.read("u:ana")
    assert len(read) == 1 and read[0].id == "ok" and read[0].title is None
    assert isinstance(read[0].created_at, int) and read[0].updated_at == 5
    assert read[0].snapshot["threadId"] is None
    assert [m["id"] for m in read[0].snapshot["messages"]] == ["m"]
    assert read[0].snapshot["messages"][0]["timestamp"] == "1970-01-01T00:00:00.000Z"
    assert read[0].snapshot["history"] == [{"role": "user", "content": "hi"}]


def test_read_does_not_accept_a_json_boolean_as_a_timestamp() -> None:
    """``isinstance(True, int)`` is true in Python, so a JSON boolean would otherwise read
    as 1/0; TS's ``typeof x === 'number'`` rejects it. A boolean must fall back to "not
    given" exactly as a string or a missing key does."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": True,
                        "conversations": [
                            {
                                "id": "ok",
                                "createdAt": True,
                                "updatedAt": False,
                                "messages": [],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    assert not isinstance(read[0].created_at, bool) and isinstance(read[0].created_at, int)
    assert not isinstance(read[0].updated_at, bool)
    assert read[0].updated_at == read[0].created_at  # False fell back to created_at, not 0


def test_read_keeps_a_literal_zero_timestamp_rather_than_treating_it_as_missing() -> None:
    """``0`` is a valid (if unusual) epoch millisecond value, not a stand-in for "absent" --
    a falsy-value default (``x or fallback``) would wrongly replace it."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "createdAt": 0,
                                "updatedAt": 0,
                                "messages": [],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    assert read[0].created_at == 0
    assert read[0].updated_at == 0


def test_read_drops_a_malformed_attachment_and_an_unsafe_message_id() -> None:
    """The store's own promise is "never throws / never trusts": a message whose
    attachments are not the exact shape ``Attachment(**a)`` needs, or whose id's counter
    suffix ``bump_message_counter`` cannot safely parse, must never reach a caller that
    trusts what the store hands back (``restore_conversation``'s own validation is a second
    line of defence, not the only one)."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "messages": [
                                    {
                                        "id": "m1",
                                        "role": "user",
                                        "content": "hi",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                        "attachments": ["scan.png"],
                                    },
                                    {
                                        "id": "msg_²_1",
                                        "role": "user",
                                        "content": "bad id",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                    },
                                    {
                                        "id": "m3",
                                        "role": "user",
                                        "content": "fine",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                        "attachments": [
                                            {
                                                "id": "a1",
                                                "filename": "scan.png",
                                                "mime_type": "image/png",
                                                "size_bytes": 1,
                                            }
                                        ],
                                    },
                                ],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    kept_ids = [m["id"] for m in read[0].snapshot["messages"]]
    assert kept_ids == ["m1", "m3"]  # the hostile-id message dropped
    assert "attachments" not in read[0].snapshot["messages"][0]  # the hostile attachment dropped
    assert read[0].snapshot["messages"][1]["attachments"] == [
        {"id": "a1", "filename": "scan.png", "mime_type": "image/png", "size_bytes": 1}
    ]


def test_read_drops_a_malformed_step_and_keeps_the_message_and_its_good_steps() -> None:
    """``convert.ts``'s ``stepPart`` reads
    ``step.status``/``step.name`` on every stored step, unguarded, and the newest stored
    conversation is restored on every start -- so one malformed step used to blank the
    chat forever, not just misrender one message. The store drops the bad step and keeps
    the good ones and the message, the same rule ``attachments`` already gets."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "messages": [
                                    {
                                        "id": "m1",
                                        "role": "assistant",
                                        "content": "did it",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                        "steps": [
                                            None,
                                            "a string, not a step",
                                            {"name": "get_scene", "status": "not-a-real-status"},
                                            {"name": "get_scene", "status": "used"},
                                            {"name": "bad_args", "status": "used", "args": "nope"},
                                        ],
                                    },
                                    {
                                        "id": "m2",
                                        "role": "assistant",
                                        "content": "only bad ones",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                        "steps": [{"status": "used"}],  # no "name"
                                    },
                                ],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    messages = read[0].snapshot["messages"]
    assert messages[0]["steps"] == [{"name": "get_scene", "status": "used"}]
    assert "steps" not in messages[1]  # nothing survived: the key is dropped, not `[]`


def test_read_drops_a_non_string_plan_and_keeps_the_message() -> None:
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "messages": [
                                    {
                                        "id": "m1",
                                        "role": "assistant",
                                        "content": "steps",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                        "plan": ["not", "a", "string"],
                                    }
                                ],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert "plan" not in read[0].snapshot["messages"][0]


def test_read_bounds_a_stored_title_at_max_stored_title() -> None:
    """A stored ``title`` is a string a hand-edited or corrupt
    file could make any length; ``threadRename`` itself never sends more than 200
    (``wire.py``'s own cap), so that is the bound kept on read too."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    huge_title = "x" * 10_000
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {"id": "ok", "title": huge_title, "messages": [], "history": []}
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert read[0].title == huge_title[:MAX_STORED_TITLE]
    assert len(read[0].title or "") == MAX_STORED_TITLE


def test_read_bounds_the_messages_kept_from_one_conversation() -> None:
    """A hand-edited or corrupt file could claim an unbounded
    ``messages`` list for one conversation; bounded on read to comfortably more than
    ``MAX_CHARS`` could ever actually hold, keeping the newest (the active tail an
    end-user would recognise and keep talking in), same direction as the write path's own
    LRU (newest kept, oldest dropped)."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    over = MAX_STORED_MESSAGES + 5
    messages = [
        {
            "id": "msg_{}_1".format(i),
            "role": "user",
            "content": "m{}".format(i),
            "timestamp": "2026-09-24T00:00:00.000Z",
        }
        for i in range(over)
    ]
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [{"id": "ok", "messages": messages, "history": []}],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    kept = read[0].snapshot["messages"]
    assert len(kept) == MAX_STORED_MESSAGES
    # The tail, not the head: message 5 dropped, the last one (over - 1) kept.
    assert kept[0]["content"] == "m5"
    assert kept[-1]["content"] == "m{}".format(over - 1)


def test_read_keeps_a_message_whose_id_has_a_non_numeric_msg_prefix() -> None:
    """``msg_welcome`` never reaches ``int()`` either way -- it is not a digit run at all --
    so it is an opaque, host-assigned id, not a hostile one. Only a digit run that is
    non-ASCII or past cpython's ``int()`` limit is hostile."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    store = ConversationStore(storage, "pk_test")
    storage.set(
        key,
        json.dumps(
            {
                "v": 1,
                "identities": {
                    "u:ana": {
                        "updatedAt": 1,
                        "conversations": [
                            {
                                "id": "ok",
                                "messages": [
                                    {
                                        "id": "msg_welcome",
                                        "role": "assistant",
                                        "content": "hi there",
                                        "timestamp": "2026-09-24T00:00:00.000Z",
                                    },
                                ],
                                "history": [],
                            }
                        ],
                    }
                },
            }
        ),
    )
    read = store.read("u:ana")
    assert len(read) == 1
    assert [m["id"] for m in read[0].snapshot["messages"]] == ["msg_welcome"]


def test_write_keeps_other_identities_and_drops_the_lru_ones_past_eight() -> None:
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    for i in range(9):
        store.write("u:{}".format(i), [_conversation(i, 1000 + i)], None)
    raw = json.loads(storage.get("t2v_chat_conversations_v1:pk_test") or "")
    assert len(raw["identities"]) == MAX_IDENTITIES
    assert "u:0" not in raw["identities"] and "u:8" in raw["identities"]


def test_write_ranks_a_ts_written_files_identities_by_updated_at_not_key_order() -> None:
    """Re-assigning an existing key in a JS object does not move it, so a file the
    TypeScript SDK wrote keeps first-insertion key order -- never recency. A Python write
    must still evict by ``updatedAt`` (TS's own rule), not by treating that on-disk key
    order as if it were recency."""
    storage = MemoryStorage()
    key = "t2v_chat_conversations_v1:pk_test"
    # "u:0" is first in key order but carries the NEWEST updatedAt; "u:1" is oldest.
    identities = {
        "u:{}".format(i): {
            "updatedAt": 9000 if i == 0 else 1000 + i,
            "conversations": [_raw_conversation(i)],
        }
        for i in range(8)
    }
    storage.set(key, json.dumps({"v": 1, "identities": identities}))
    store = ConversationStore(storage, "pk_test")

    store.write("u:8", [_conversation(8, 9999)], None)

    raw = json.loads(storage.get(key) or "")
    assert len(raw["identities"]) == MAX_IDENTITIES
    assert "u:1" not in raw["identities"]  # the oldest updatedAt, despite key position 1
    assert "u:0" in raw["identities"]  # the newest updatedAt, despite key position 0
    assert "u:8" in raw["identities"]


def test_write_drops_seven_others_not_eight_when_this_identity_ends_up_empty() -> None:
    """TS's ``slice(MAX_IDENTITIES - 1)`` runs regardless of whether the identity being
    written survives, so writing an empty list for a ninth identity still costs the others
    a slot: 7 kept, not 8."""
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    for i in range(8):
        store.write("u:{}".format(i), [_conversation(i, 1000 + i)], None)
    store.write("u:8", [], None)  # nothing to keep for u:8 itself
    raw = json.loads(storage.get("t2v_chat_conversations_v1:pk_test") or "")
    assert "u:8" not in raw["identities"]
    assert len(raw["identities"]) == MAX_IDENTITIES - 1
    assert "u:0" not in raw["identities"]  # still the one evicted (oldest updatedAt)


def test_write_keeps_the_newest_fifty_and_the_size_cap_never_drops_the_current_one() -> None:
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    many = [_conversation(i, i) for i in range(60)]
    assert store.write("u:ana", many, "c_59") is True
    assert [c.id for c in store.read("u:ana")][:2] == ["c_59", "c_58"]
    assert len(store.read("u:ana")) == MAX_CONVERSATIONS
    big = [_conversation(i, i, content="x" * 200_000) for i in range(5)]
    assert store.write("u:ana", big, "c_0") is True  # c_0 is the oldest AND the current one
    kept = [c.id for c in store.read("u:ana")]
    assert "c_0" in kept and len(kept) < 5
    assert len(storage.get("t2v_chat_conversations_v1:pk_test") or "") <= MAX_CHARS


def test_write_gives_up_and_removes_the_key_when_even_the_current_one_does_not_fit() -> None:
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    huge = [_conversation(1, 1, content="x" * (MAX_CHARS + 10))]
    assert store.write("u:ana", huge, "c_1") is False
    assert storage.get("t2v_chat_conversations_v1:pk_test") is None


def test_a_storage_that_raises_reads_empty_and_writes_false() -> None:
    class Broken:
        def get(self, key: str) -> str:
            raise OSError("no")

        def set(self, key: str, value: str) -> None:
            raise OSError("no")

        def delete(self, key: str) -> None:
            raise OSError("no")

    store = ConversationStore(Broken(), "pk_test")
    assert store.read("u:ana") == []
    assert store.write("u:ana", [_conversation(1, 1)], None) is False


def test_write_serialises_like_json_stringify_not_json_dumps_defaults() -> None:
    """The 512 KB cap has to measure the same bytes TS's own cap measures
    (``JSON.stringify``: no whitespace, non-ASCII kept raw as UTF-16 code units) or it
    binds far earlier here for non-Latin text -- ~5x earlier for CJK, since Python's
    ``json.dumps`` defaults escape every non-ASCII character to a 6-character
    ``\\uXXXX`` sequence and pad every separator with a space."""
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    cjk = "こんにちは" * 14  # 70 CJK characters, no ASCII
    store.write("u:ana", [_conversation(1, 1, content=cjk)], "c_1")
    raw = storage.get("t2v_chat_conversations_v1:pk_test") or ""
    assert cjk in raw  # kept raw, not \uXXXX-escaped
    assert ", " not in raw and ": " not in raw  # no separator padding
    # json.dumps defaults would blow well past this for the same content.
    assert len(raw) < 600


def test_write_is_serialised_against_a_concurrent_write() -> None:
    """``write()`` is a read-modify-write with no atomicity of its own below the storage
    layer: two callers racing on different identities (a debounce timer and an
    auth-change handler, say) must not lose either update. Without a lock of its own,
    the second call's read can land before the first call's write is visible, and its
    write then overwrites the first call's identity entirely."""
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    store.write("u:seed", [_conversation(0, 1)], None)

    reading = threading.Event()
    release = threading.Event()
    real_get = storage.get

    def slow_get(key: str) -> Optional[str]:
        value = real_get(key)
        reading.set()
        release.wait(5)
        return value

    storage.get = slow_get  # type: ignore[method-assign]

    thread_b = threading.Thread(
        target=lambda: store.write("u:b", [_conversation(2, 2)], None)
    )
    thread_b.start()
    assert reading.wait(5)  # b has read the file and is paused mid-write

    def release_soon() -> None:
        time.sleep(0.2)
        release.set()

    threading.Thread(target=release_soon).start()
    storage.get = real_get  # a's own read must not itself be slowed
    store.write("u:a", [_conversation(1, 1)], None)  # must not race past b's write

    thread_b.join(5)
    raw = json.loads(storage.get("t2v_chat_conversations_v1:pk_test") or "")
    assert {"u:seed", "u:a", "u:b"} <= set(raw["identities"])


def test_write_returns_false_rather_than_raise_on_a_conversation_json_cannot_serialise() -> None:
    """The class promises writes as False, never a raise, on anything that could not be
    stored -- including a host-built ``Conversation`` whose snapshot holds something
    ``json.dumps`` itself cannot serialise (``export_conversation`` never produces one, but
    ``write()`` takes a bare ``Conversation``, not only ones this SDK built)."""
    storage = MemoryStorage()
    store = ConversationStore(storage, "pk_test")
    unserialisable = _conversation(1, 1)
    unserialisable.snapshot["messages"][0]["content"] = {1, 2, 3}  # type: ignore[typeddict-item]

    assert store.write("u:ana", [unserialisable], None) is False
    assert storage.get("t2v_chat_conversations_v1:pk_test") is None
