"""Tests for attachment upload + structured message content."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from talk2view import Talk2View
from talk2view.sessions import T2VSession
from talk2view.tools import T2VTools
from talk2view.types import (
    Attachment,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
)

ATTACHMENT = Attachment(
    id="att_1", filename="scan.png", mime_type="image/png", size_bytes=4
)


def _stop_chunk() -> ChatCompletionChunk:
    return ChatCompletionChunk(
        choices=[
            ChatCompletionChunkChoice(
                delta=ChatCompletionChunkDelta(content="ok"),
                finish_reason="stop",
            )
        ],
        thread_id="thr_456",
    )


def _make_session() -> tuple:  # type: ignore[type-arg]
    client = MagicMock()
    tools = MagicMock(spec=T2VTools)
    tools.has_handler.return_value = False
    client.stream_request.return_value = iter([_stop_chunk()])
    session = T2VSession(
        session_id="ses_123",
        thread_id="thr_456",
        model="gpt-4.1-mini",
        client=client,
        tools=tools,
    )
    return session, client


class TestSendMessageAttachments:
    def test_plain_content_unchanged(self) -> None:
        session, client = _make_session()
        list(session.send_message("hello"))

        body = client.stream_request.call_args[0][1]
        assert body["messages"] == [{"role": "user", "content": "hello"}]

    def test_attachments_build_content_parts(self) -> None:
        session, client = _make_session()
        list(session.send_message("look", attachments=[ATTACHMENT]))

        body = client.stream_request.call_args[0][1]
        assert body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {"type": "attachment", "attachment_id": "att_1"},
                ],
            }
        ]

    def test_empty_text_omits_text_part(self) -> None:
        session, client = _make_session()
        list(session.send_message("", attachments=[ATTACHMENT]))

        body = client.stream_request.call_args[0][1]
        assert body["messages"][0]["content"] == [
            {"type": "attachment", "attachment_id": "att_1"}
        ]


class TestUploadAttachment:
    def _make_t2v(self) -> tuple:  # type: ignore[type-arg]
        t2v = Talk2View(partner_key="pk_test")
        client = MagicMock()
        client.upload_request.return_value = {
            "id": "att_1",
            "filename": "scan.png",
            "mime_type": "image/png",
            "size_bytes": 4,
            "created_at": "2026-06-11T00:00:00+00:00",
        }
        t2v._client = client
        return t2v, client

    def test_upload_bytes(self) -> None:
        t2v, client = self._make_t2v()
        result = t2v.upload_attachment(
            b"\x89PNG", filename="scan.png", mime_type="image/png"
        )

        assert result == ATTACHMENT
        endpoint = client.upload_request.call_args[0][0]
        files = client.upload_request.call_args.kwargs["files"]
        assert endpoint == "/v1/attachments"
        assert files == {"file": ("scan.png", b"\x89PNG", "image/png")}

    def test_upload_path_guesses_mime(self, tmp_path: Path) -> None:
        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"%PDF")
        t2v, client = self._make_t2v()

        t2v.upload_attachment(file_path)

        files = client.upload_request.call_args.kwargs["files"]
        assert files == {"file": ("report.pdf", b"%PDF", "application/pdf")}
