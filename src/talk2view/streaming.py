"""SSE stream parser — converts httpx response into chunks."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, List, Optional

import httpx

from .types import (
    AgentStatus,
    ApprovalDetail,
    ApprovalRequest,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    StreamError,
    ToolCallInterrupt,
    ToolEvent,
)

_DONE_MARKER = "[DONE]"


def _as_dict(raw: Any) -> Dict[str, Any]:
    """The engine serialises absent objects as null — treat those as empty."""
    return raw if isinstance(raw, dict) else {}


def _parse_choice(raw: Dict[str, Any]) -> ChatCompletionChunkChoice:
    delta_raw = _as_dict(raw.get("delta"))
    delta = ChatCompletionChunkDelta(
        role=delta_raw.get("role"),
        content=delta_raw.get("content"),
        reasoning_content=delta_raw.get("reasoning_content"),
        annotations=delta_raw.get("annotations"),
    )
    return ChatCompletionChunkChoice(
        index=raw.get("index", 0),
        delta=delta,
        finish_reason=raw.get("finish_reason"),
    )


def _parse_approval(raw: Dict[str, Any]) -> ApprovalRequest:
    details: List[ApprovalDetail] = []
    for row in raw.get("details") or []:
        if isinstance(row, dict):
            details.append(
                ApprovalDetail(
                    label=str(row.get("label", "")),
                    value=str(row.get("value", "")),
                )
            )
    return ApprovalRequest(
        type=raw.get("type", "approval_request"),
        tool_name=raw.get("tool_name", ""),
        tool_call_id=raw.get("tool_call_id", ""),
        arguments=_as_dict(raw.get("arguments")),
        summary=raw.get("summary") or "",
        details=details,
    )


def _parse_tool_event(raw: Dict[str, Any]) -> ToolEvent:
    arguments = raw.get("arguments")
    display = raw.get("display")
    status = raw.get("status")
    return ToolEvent(
        type=raw.get("type", ""),
        tool_name=raw.get("tool_name", ""),
        tool_call_id=raw.get("tool_call_id", ""),
        arguments=arguments if isinstance(arguments, dict) else None,
        status=status if isinstance(status, str) else None,
        display=display if isinstance(display, dict) else None,
    )


def _parse_chunk(data: Dict[str, Any]) -> ChatCompletionChunk:
    choices: List[ChatCompletionChunkChoice] = []
    for raw_choice in data.get("choices") or []:
        if isinstance(raw_choice, dict):
            choices.append(_parse_choice(raw_choice))

    interrupt = None
    if data.get("interrupt"):
        raw_int = _as_dict(data["interrupt"])
        interrupt = ToolCallInterrupt(
            type=raw_int.get("type", "tool_call"),
            tool_name=raw_int.get("tool_name", ""),
            tool_call_id=raw_int.get("tool_call_id", ""),
            arguments=_as_dict(raw_int.get("arguments")),
        )

    status = None
    if data.get("status"):
        raw_status = _as_dict(data["status"])
        status = AgentStatus(
            type=raw_status.get("type", ""),
            message=raw_status.get("message", ""),
        )

    approval = None
    if data.get("approval"):
        approval = _parse_approval(_as_dict(data["approval"]))

    tool_event = None
    if data.get("tool_event"):
        tool_event = _parse_tool_event(_as_dict(data["tool_event"]))

    error = None
    if data.get("error"):
        raw_error = _as_dict(data["error"])
        detail = raw_error.get("detail")
        request_id = raw_error.get("request_id")
        error = StreamError(
            type=raw_error.get("type") or "unknown_error",
            message=raw_error.get("message") or "",
            detail=detail if isinstance(detail, str) else None,
            request_id=request_id if isinstance(request_id, str) else None,
        )

    return ChatCompletionChunk(
        id=data.get("id", ""),
        object=data.get("object", "chat.completion.chunk"),
        created=data.get("created", 0),
        model=data.get("model", ""),
        choices=choices,
        thread_id=data.get("thread_id"),
        interrupt=interrupt,
        status=status,
        todos=data.get("todos"),
        approval=approval,
        tool_event=tool_event,
        error=error,
    )


def _data_payload(line: str) -> Optional[str]:
    """Payload of an SSE ``data`` line, or None for anything else.

    Per the EventStream spec the space after the colon is optional and a single
    one is removed if present, so ``data:{json}`` and ``data: {json}`` are
    equivalent. Comment lines (``: t2v stream open``), ``event:``/``id:`` fields
    and blank separators all return None.
    """
    trimmed = line.strip()
    if not trimmed.startswith("data:"):
        return None
    payload = trimmed[5:]
    if payload.startswith(" "):
        payload = payload[1:]
    return payload


def _decode_chunk(payload: str) -> Optional[ChatCompletionChunk]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None  # Skip malformed JSON
    if not isinstance(data, dict):
        return None  # A bare JSON scalar/list is not a chunk
    return _parse_chunk(data)


def parse_sse_stream(response: httpx.Response) -> Iterator[ChatCompletionChunk]:
    """Parse an SSE stream from an httpx response into ChatCompletionChunk objects.

    Handles the engine's framing:
        : t2v stream open          (comment preamble — ignored)
        data: {"id": "...", ...}   (also accepted without the space)
        data: [DONE]               (normal end)

    A stream that pauses for a tool call or an approval ends WITHOUT ``[DONE]``;
    running out of bytes is therefore a normal end, and a final line that lacks
    its trailing newline is still parsed.
    """
    # iter_text() decodes incrementally, so a multi-byte character split
    # across two network reads is reassembled rather than corrupted.
    yield from parse_sse_text(response.iter_text())


def parse_sse_text(texts: Iterable[str]) -> Iterator[ChatCompletionChunk]:
    """:func:`parse_sse_stream` over already-decoded text. It stops at ``[DONE]`` without
    closing ``texts``, so a caller holding that iterator can read the body to its end."""
    buffer = ""
    for text in texts:
        buffer += text
        lines = buffer.split("\n")
        buffer = lines.pop()  # Keep incomplete last line

        for line in lines:
            payload = _data_payload(line)
            if payload is None:
                continue
            if payload == _DONE_MARKER:
                return
            chunk = _decode_chunk(payload)
            if chunk is not None:
                yield chunk

    # Flush a trailing line that arrived without its newline.
    payload = _data_payload(buffer)
    if payload is not None and payload != _DONE_MARKER:
        chunk = _decode_chunk(payload)
        if chunk is not None:
            yield chunk
