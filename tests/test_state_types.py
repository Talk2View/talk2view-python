"""Wire-shape tests for talk2view.state — key casing must equal the TS DisplayMessage."""

from __future__ import annotations

import json

from talk2view import state
from talk2view.state import (
    ChatSnapshot,
    DisplayMessage,
    PendingApproval,
    ToolStep,
    empty_skills_state,
    initial_snapshot,
)


class TestWireShape:
    def test_display_message_keys_match_ts_casing(self) -> None:
        assert DisplayMessage.__required_keys__ == {"id", "role", "content", "timestamp"}
        assert DisplayMessage.__optional_keys__ == {"isStreaming", "attachments", "plan", "steps"}

    def test_tool_step_keys(self) -> None:
        assert ToolStep.__required_keys__ == {"name", "status"}
        assert ToolStep.__optional_keys__ == {"args", "result", "toolCallId", "display", "source"}

    def test_pending_approval_keys(self) -> None:
        assert PendingApproval.__required_keys__ == {
            "toolCallId",
            "toolName",
            "arguments",
            "description",
        }
        assert PendingApproval.__optional_keys__ == {"kind", "details"}

    def test_attachment_keys_are_snake_case_like_the_engine(self) -> None:
        assert state.AttachmentDict.__required_keys__ == {
            "id",
            "filename",
            "mime_type",
            "size_bytes",
        }

    def test_chat_snapshot_keys(self) -> None:
        assert ChatSnapshot.__required_keys__ == {
            "seq",
            "messages",
            "isLoading",
            "error",
            "pendingApproval",
            "agentStatus",
            "threadId",
            "alwaysAllowedTools",
            "skills",
            "isAnonymous",
        }

    def test_op_and_batch_keys(self) -> None:
        assert state.Op.__required_keys__ == {"type", "path", "value"}
        assert state.OpBatch.__required_keys__ == {"seq", "ops"}


class TestFactories:
    def test_initial_snapshot(self) -> None:
        snap = initial_snapshot()
        assert snap == {
            "seq": 0,
            "messages": [],
            "isLoading": False,
            "error": None,
            "pendingApproval": None,
            "agentStatus": None,
            "threadId": None,
            "alwaysAllowedTools": [],
            "skills": empty_skills_state(),
            "isAnonymous": False,
        }

    def test_initial_snapshot_is_anonymous_flag(self) -> None:
        assert initial_snapshot(is_anonymous=True)["isAnonymous"] is True

    def test_empty_skills_state_shape(self) -> None:
        assert empty_skills_state() == {
            "local": [],
            "platform": None,
            "registered": [],
            "available": False,
            "needsSessionReset": False,
            "store": {"writable": False, "canOpenFolder": False},
        }

    def test_factories_return_fresh_objects(self) -> None:
        first = initial_snapshot()
        first["messages"].append(
            {"id": "m1", "role": "user", "content": "x", "timestamp": "2026-01-01T00:00:00.000Z"}
        )
        first["skills"]["registered"].append("x")
        assert initial_snapshot()["messages"] == []
        assert initial_snapshot()["skills"]["registered"] == []

    def test_snapshot_is_json_serialisable(self) -> None:
        assert json.loads(json.dumps(initial_snapshot()))["seq"] == 0
