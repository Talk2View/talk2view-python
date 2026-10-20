"""Sessions — create, send messages with streaming, resume."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional

from .cancel import CancelToken
from .client import T2VClient
from .tools import T2VTools
from .types import (
    Attachment,
    Capabilities,
    ChatCompletionChunk,
    ChatEvent,
    ChatMessage,
    Decision,
    MessageContent,
)

# finish_reason values that indicate the stream is complete
_TERMINAL_REASONS = {"stop", "length", "content_filter"}


def build_user_content(
    content: str,
    attachments: Optional[List[Attachment]] = None,
) -> MessageContent:
    """Plain string normally; structured content parts with attachments."""
    if not attachments:
        return content
    parts: List[Dict[str, Any]] = []
    if content:
        parts.append({"type": "text", "text": content})
    parts.extend(
        {"type": "attachment", "attachment_id": a.id} for a in attachments
    )
    return parts


class T2VSession:
    """A chat session — wraps session_id/thread_id and provides send/resume."""

    def __init__(
        self,
        session_id: str,
        thread_id: str,
        model: str,
        client: T2VClient,
        tools: T2VTools,
        default_model: Optional[str] = None,
        capabilities: Optional[Capabilities] = None,
    ) -> None:
        self.id = session_id
        self.thread_id = thread_id
        self.model = model
        self._client = client
        self._tools = tools
        self._default_model = default_model
        self._capabilities = capabilities

    def _capability_fields(self, resume: bool) -> Dict[str, Any]:
        """Opt-in request flags. Empty by default, so the body is unchanged.

        ``supports_approval`` is a /messages-only field: the engine keeps it in
        the run's state, and its ResumeRequest schema has no such field.
        """
        caps = self._capabilities
        if caps is None:
            return {}
        fields: Dict[str, Any] = {}
        if caps.approval and not resume:
            fields["supports_approval"] = True
        if caps.tool_events:
            fields["supports_tool_events"] = True
        if caps.reasoning:
            fields["supports_reasoning"] = True
        if caps.image_generation:
            fields["supports_image_generation"] = True
        if caps.timezone:
            fields["timezone"] = caps.timezone
        if caps.surface:
            fields["surface"] = caps.surface
        return fields

    def _resume_endpoint(self) -> str:
        return "/v1/sessions/{}/resume".format(self.id)

    def _result_body(self, tool_call_id: str, result: str, is_error: bool) -> Dict[str, Any]:
        """Client-tool resume: ``result`` (+ ``is_error``), never ``approved``."""
        body: Dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "result": result,
            "is_error": is_error,
        }
        body.update(self._capability_fields(resume=True))
        return body

    def _approved_body(self, tool_call_id: str, approved: bool) -> Dict[str, Any]:
        """Engine-approval resume: ``approved`` only.

        The engine's validator requires exactly one of ``result``/``approved``,
        so this body must never carry a ``result`` key.
        """
        body: Dict[str, Any] = {"tool_call_id": tool_call_id, "approved": approved}
        body.update(self._capability_fields(resume=True))
        return body

    def send_message(
        self,
        content: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[ChatMessage]] = None,
        attachments: Optional[List[Attachment]] = None,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Send a message and stream the response.

        Tool calls are handled automatically if handlers are registered.
        Yields ChatEvent objects for the caller to render.

        Args:
            attachments: Files previously uploaded via
                Talk2View.upload_attachment() to attach to this message.
            cancel: ``cancel.cancel()`` from any thread aborts the stream; the
                iterator then raises ``Cancelled``.
        """
        messages: List[Dict[str, Any]] = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history:
            for msg in history:
                m: Dict[str, Any] = {"role": msg.role, "content": msg.content}
                if msg.tool_call_id:
                    m["tool_call_id"] = msg.tool_call_id
                messages.append(m)
        messages.append(
            {"role": "user", "content": build_user_content(content, attachments)}
        )

        body: Dict[str, Any] = {"messages": messages, "stream": True}
        if self._default_model:
            body["model"] = self._default_model
        body.update(self._capability_fields(resume=False))

        yield from self._process_stream(
            "/v1/sessions/{}/messages".format(self.id),
            body,
            cancel,
        )

    def resume_tool_call(
        self,
        tool_call_id: str,
        result: str,
        is_error: bool = False,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Resume after a tool call (for manual tool handling)."""
        yield from self._process_stream(
            self._resume_endpoint(),
            self._result_body(tool_call_id, result, is_error),
            cancel,
        )

    def respond_to_approval(
        self,
        approval: ChatEvent,
        decision: Decision,
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Answer an ``approval_required`` event (human-in-the-loop).

        ``approval`` is the ``approval_required`` ChatEvent itself.

        Client tool (``approval.kind == "client"``):
        - once / always: run the handler (with ``decision.updated_input`` if
          given), resume with its result. "always" is remembered by the layer
          above; here it behaves like "once".
        - deny: nothing runs; resume with an error result carrying the feedback.

        Platform tool (``approval.kind == "engine"``): the engine runs the tool
        itself, so resume with ``approved: bool`` and no ``result``.
        ``updated_input`` and ``feedback`` do not apply.
        """
        if approval.type != "approval_required" or not approval.tool_call_id:
            raise ValueError("respond_to_approval() needs an 'approval_required' ChatEvent")
        tool_name = approval.tool_name or ""
        tool_call_id = approval.tool_call_id

        # Fail closed through Decision.approved — the one place that rule
        # lives, so this module and Talk2View.respond_to_approval() can never
        # classify the same decision differently.
        approved = decision.approved
        reported_decision = decision.action if approved else "deny"

        if approval.kind == "engine":
            body = self._approved_body(tool_call_id, approved)
        elif not approved:
            denial = "User denied this action. {}".format(decision.feedback or "").strip()
            body = self._result_body(tool_call_id, denial, True)
        else:
            exec_args = (
                decision.updated_input
                if decision.updated_input is not None
                else (approval.arguments or {})
            )
            result, is_error = self._tools.execute_tool_call(tool_name, exec_args, cancel)
            body = self._result_body(tool_call_id, result, is_error)

        yield ChatEvent(
            type="approval_result",
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            decision=reported_decision,
        )
        yield from self._process_stream(self._resume_endpoint(), body, cancel)

    def _passive_events(self, chunk: ChatCompletionChunk) -> Iterator[ChatEvent]:
        """Events that never end or pause the stream."""
        if chunk.status:
            yield ChatEvent(
                type="status",
                status=chunk.status.type,
                message=chunk.status.message,
            )
        if chunk.todos:
            yield ChatEvent(type="todos", todos=chunk.todos)
        if chunk.tool_event:
            yield ChatEvent(
                type="tool_event",
                tool_name=chunk.tool_event.tool_name,
                tool_call_id=chunk.tool_event.tool_call_id,
                tool_event=chunk.tool_event,
            )

    def _content_events(self, chunk: ChatCompletionChunk) -> Iterator[ChatEvent]:
        """error / reasoning / text. An error chunk is never rendered as text."""
        if chunk.error:
            # The engine mirrors the message into delta.content for
            # OpenAI-shaped clients — read `error` first.
            yield ChatEvent(
                type="error",
                message=chunk.error.message,
                error_type=chunk.error.type,
                detail=chunk.error.detail,
                request_id=chunk.error.request_id,
            )
            return
        if not chunk.choices:
            return
        delta = chunk.choices[0].delta
        if delta.reasoning_content:
            yield ChatEvent(type="reasoning", content=delta.reasoning_content)
        if delta.content:
            yield ChatEvent(type="text", content=delta.content)

    def _process_stream(
        self,
        endpoint: str,
        body: Dict[str, Any],
        cancel: Optional[CancelToken] = None,
    ) -> Iterator[ChatEvent]:
        """Process an SSE stream with automatic tool call handling.

        Uses an iterative loop (not recursion) so multi-tool-call
        workflows don't grow the stack or hold open nested connections.
        """
        current_endpoint = endpoint
        current_body = body

        while True:
            resume_body: Dict[str, Any] = {}

            for chunk in self._client.stream_request(
                current_endpoint, current_body, cancel=cancel
            ):
                yield from self._passive_events(chunk)

                # Engine-enforced approval for a platform tool.
                # The stream ends here without [DONE]; the caller answers
                # through respond_to_approval().
                if chunk.approval:
                    yield ChatEvent(
                        type="approval_required",
                        kind="engine",
                        tool_name=chunk.approval.tool_name,
                        tool_call_id=chunk.approval.tool_call_id,
                        arguments=chunk.approval.arguments,
                        description=chunk.approval.summary,
                        details=[
                            {"label": d.label, "value": d.value}
                            for d in chunk.approval.details
                        ],
                    )
                    return

                # Client tool call interrupt
                if chunk.interrupt:
                    tool_name = chunk.interrupt.tool_name
                    tool_call_id = chunk.interrupt.tool_call_id
                    args = chunk.interrupt.arguments

                    check = self._tools.check_permission(tool_name, args, cancel)

                    if check.action == "require_approval":
                        # Pause for a human decision (approval card).
                        yield ChatEvent(
                            type="approval_required",
                            kind="client",
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                            arguments=args,
                            description=self._tools.get_description(tool_name),
                        )
                        return

                    if check.action != "allow":
                        # Programmatic denial — tell the model, run nothing.
                        # Allow-listed like respond_to_approval(): only "allow"
                        # executes, so an unrecognised action denies.
                        resume_body = self._result_body(
                            tool_call_id,
                            "Permission denied: {}".format(
                                check.message or "Tool not allowed"
                            ),
                            True,
                        )
                        break

                    exec_args = (
                        check.updated_input if check.updated_input is not None else args
                    )

                    # Emit tool_call event so the caller knows
                    yield ChatEvent(
                        type="tool_call",
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        arguments=exec_args,
                    )

                    if self._tools.has_handler(tool_name):
                        # Execute the tool locally, then resume in the while-loop
                        result, is_error = self._tools.execute_tool_call(
                            tool_name, exec_args, cancel
                        )
                        resume_body = self._result_body(tool_call_id, result, is_error)
                        break

                    # No handler — caller must handle manually
                    return

                yield from self._content_events(chunk)

                if chunk.choices and chunk.choices[0].finish_reason in _TERMINAL_REASONS:
                    yield ChatEvent(
                        type="done",
                        thread_id=chunk.thread_id or self.thread_id,
                    )
                    return
            else:
                # for-loop exhausted without break (nothing to resume)
                return

            # Continue with the resume request (tool call handled or denied)
            current_endpoint = self._resume_endpoint()
            current_body = resume_body
