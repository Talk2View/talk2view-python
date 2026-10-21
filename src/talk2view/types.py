"""Talk2View SDK data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union

# ── Auth ──


@dataclass
class User:
    id: str
    email: str
    user_metadata: Optional[Dict[str, Any]] = None


@dataclass
class TokenResponse:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 token type, not a password
    expires_in: int = 3600
    user: Optional[User] = None
    user_api_key: Optional[str] = None
    # True for a session started with /v1/auth/anonymous (engine TokenResponse).
    is_anonymous: bool = False


@dataclass(frozen=True)
class SignupOutcome:
    """Result of ``T2VAuth.signup``.

    user: set when a session exists now (immediate sign-in, or the 409 → login
        fallback of an anonymous convert). ``None`` while a convert awaits the
        emailed confirmation — the anonymous session stays live meanwhile.
    confirmation_required: True while the confirmation link is unclicked.
    """

    user: Optional[User]
    confirmation_required: bool


# ── Sessions ──


@dataclass
class CreateSessionResponse:
    session_id: str
    thread_id: str
    model: str


@dataclass
class Attachment:
    """Metadata returned by Talk2View.upload_attachment().

    Reference it in chat via send_message(..., attachments=[attachment]).
    """

    id: str
    filename: str
    mime_type: str
    size_bytes: int


@dataclass
class AttachmentContent:
    """Bytes of an attachment (``GET /v1/attachments/{id}/content``): an upload of
    this end-user, or an image the agent generated."""

    data: bytes
    mime_type: str


# Content is a plain string, or a list of content parts when attachments are
# present: {"type": "text", "text": ...} / {"type": "attachment", "attachment_id": ...}
MessageContent = Union[str, List[Dict[str, Any]]]


@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: MessageContent
    tool_call_id: Optional[str] = None


# ── Streaming ──


@dataclass
class ChatCompletionChunkDelta:
    role: Optional[str] = None
    content: Optional[str] = None
    # The model's reasoning; only when the request set supports_reasoning.
    reasoning_content: Optional[str] = None
    # Citations etc. (e.g. from web search); passed through untouched.
    annotations: Optional[List[Dict[str, Any]]] = None


@dataclass
class ChatCompletionChunkChoice:
    index: int = 0
    delta: ChatCompletionChunkDelta = field(default_factory=ChatCompletionChunkDelta)
    finish_reason: Optional[str] = None


@dataclass
class ToolCallInterrupt:
    type: str  # "tool_call"
    tool_name: str
    tool_call_id: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentStatus:
    type: str
    message: str


@dataclass
class ApprovalDetail:
    """One label/value row shown on an engine approval card."""

    label: str
    value: str


@dataclass
class ApprovalRequest:
    """Engine-enforced approval for a platform tool (``approval`` chunk field).

    Only sent when the request set ``supports_approval``. The stream ends after
    it without ``[DONE]``; resume with ``{tool_call_id, approved}``.
    """

    type: str  # "approval_request"
    tool_name: str
    tool_call_id: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    details: List[ApprovalDetail] = field(default_factory=list)


@dataclass
class ToolEvent:
    """Lifecycle of one server-side platform tool call (``tool_event`` field).

    Only sent when the request set ``supports_tool_events``. Fields the engine
    left off the wire are ``None`` here and omitted again by :meth:`to_dict`.
    """

    type: str  # "tool_start" | "tool_end"
    tool_name: str
    tool_call_id: str
    arguments: Optional[Dict[str, Any]] = None
    status: Optional[str] = None  # "ok" | "error" | "denied"
    display: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """Wire-shaped dict: absent fields are omitted, never null."""
        out: Dict[str, Any] = {
            "type": self.type,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
        }
        if self.arguments is not None:
            out["arguments"] = self.arguments
        if self.status is not None:
            out["status"] = self.status
        if self.display is not None:
            out["display"] = self.display
        return out


@dataclass
class StreamError:
    """Structured error on an SSE chunk (``error`` field).

    The engine mirrors ``message`` into ``delta.content`` for OpenAI-shaped
    clients; this SDK reads ``error`` first and never renders it as text.
    """

    type: str
    message: str
    detail: Optional[str] = None
    # The engine's id for the request the stream belongs to; quote it to support.
    request_id: Optional[str] = None


@dataclass
class ChatCompletionChunk:
    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = 0
    model: str = ""
    choices: List[ChatCompletionChunkChoice] = field(default_factory=list)
    thread_id: Optional[str] = None
    interrupt: Optional[ToolCallInterrupt] = None
    status: Optional[AgentStatus] = None
    todos: Optional[str] = None
    approval: Optional[ApprovalRequest] = None
    tool_event: Optional[ToolEvent] = None
    error: Optional[StreamError] = None


# ── Chat Events (public API) ──


@dataclass
class ChatEvent:
    """A high-level event yielded by chat() / send_message().

    type is one of: "text", "reasoning", "tool_call", "tool_event",
    "approval_required", "approval_result", "done", "error", "status", "todos"
    """

    type: str

    # text / reasoning event
    content: Optional[str] = None

    # tool_call / tool_event / approval_required / approval_result events
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    arguments: Optional[Dict[str, Any]] = None

    # done event
    thread_id: Optional[str] = None

    # error event
    message: Optional[str] = None
    error_type: Optional[str] = None
    detail: Optional[str] = None
    request_id: Optional[str] = None

    # status event
    status: Optional[str] = None
    # message field is reused for status message

    # todos event
    todos: Optional[str] = None

    # approval_required event. kind is "client" (a client tool registered with
    # permission=True; description = the tool's description) or "engine" (a
    # platform tool gated by the engine; description = the engine's summary,
    # details = its label/value rows).
    kind: Optional[str] = None
    description: Optional[str] = None
    details: Optional[List[Dict[str, str]]] = None

    # approval_result event: "once" | "always" | "deny". Its ``content`` is the tool's
    # result string, set only for a builtin installed with ``result_on_step=True``
    # (``save_skill``) once an approved call has run (capped at STEP_RESULT_MAX); None
    # otherwise, so a host tool's result is never exposed here.
    decision: Optional[str] = None

    # tool_event event (platform tool lifecycle; needs Capabilities.tool_events)
    tool_event: Optional[ToolEvent] = None


# ── Human-in-the-loop ──


@dataclass(frozen=True)
class Decision:
    """The end-user's answer to an ``approval_required`` event.

    action: "once" (run this call), "always" (run it, and the layer above
    remembers the tool for the session) or "deny".
    """

    action: Literal["once", "always", "deny"]
    feedback: Optional[str] = None
    updated_input: Optional[Dict[str, Any]] = None

    @property
    def approved(self) -> bool:
        """Whether this decision runs the call — the ONE rule, used everywhere.

        Allow-listed rather than "not deny", so an action that never reached
        the Literal (a typo, an un-normalised UI string) denies.
        """
        return self.action in ("once", "always")


@dataclass(frozen=True)
class Capabilities:
    """Opt-in engine features this client can render. All off by default, and
    nothing is sent for a flag that is off, so default request bodies are
    byte-identical to SDK 0.1.0.

    approval: engine-enforced approval of platform tools (``approval`` chunks).
    tool_events: platform-tool activity (``tool_event`` chunks).
    reasoning: the model's reasoning (``delta.reasoning_content``).
    image_generation: generated-image tool events.
    timezone: the user's IANA timezone, e.g. "Australia/Perth".
    surface: "keyboard" or "app" — the engine rejects any other value.
    """

    approval: bool = False
    tool_events: bool = False
    reasoning: bool = False
    image_generation: bool = False
    timezone: Optional[str] = None
    surface: Optional[Literal["keyboard", "app"]] = None


class _DefaultModel:
    """The type of ``DEFAULT_MODEL``."""

    def __repr__(self) -> str:
        return "DEFAULT_MODEL"


#: ``Talk2View.set_chat_defaults(model=DEFAULT_MODEL)``: send no model, so the engine uses
#: the partner's default (TS Settings' "Default"). ``model=None`` there means "leave as is".
DEFAULT_MODEL = _DefaultModel()


def _refuse_default_model(model: object, where: str) -> None:
    """``TypeError`` for ``DEFAULT_MODEL`` given to ``where``, which takes a model id: only
    ``set_chat_defaults`` understands it. Called before any request, a guest start
    included."""
    if isinstance(model, _DefaultModel):
        raise TypeError(
            "{} takes a model id, not DEFAULT_MODEL. For the partner's default chat model, "
            "call set_chat_defaults(model=DEFAULT_MODEL).".format(where)
        )


@dataclass(frozen=True)
class PermissionResult:
    """What a tool permission callback returns (TS ``PermissionResult``).

    Build one with :meth:`allow` or :meth:`deny`.
    """

    type: str  # "allow" | "deny"
    updated_input: Optional[Dict[str, Any]] = None
    message: Optional[str] = None

    @classmethod
    def allow(cls, updated_input: Optional[Dict[str, Any]] = None) -> "PermissionResult":
        return cls(type="allow", updated_input=updated_input)

    @classmethod
    def deny(cls, message: Optional[str] = None) -> "PermissionResult":
        return cls(type="deny", message=message)


@dataclass(frozen=True)
class PermissionCheck:
    """Result of ``T2VTools.check_permission`` (TS ``PermissionCheckResult``).

    ``action`` is "allow" (run it, with ``updated_input`` if set),
    "require_approval" (ask the end-user) or "deny" (``message`` says why).
    """

    action: str
    updated_input: Optional[Dict[str, Any]] = None
    message: Optional[str] = None


# ── Tools ──


@dataclass
class ClientToolParameter:
    type: str
    description: str = ""
    enum: Optional[List[str]] = None
    items: Optional[Dict[str, Any]] = None


@dataclass
class ClientToolSchema:
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=lambda: {
        "type": "object",
        "properties": {},
        "required": [],
    })
    return_direct: bool = False


@dataclass
class RegisterToolsResponse:
    registered: List[str] = field(default_factory=list)
    count: int = 0


# ── Skills ──


@dataclass
class RegisterSkillsResponse:
    """``POST /v1/skills/register`` → ``{registered, count, removed, replaced}``.

    ``replaced`` is None when the engine left the key out: an engine older than the
    ``replace`` flag, which merged the skills into the session instead.
    """

    registered: List[str] = field(default_factory=list)
    count: int = 0
    removed: List[str] = field(default_factory=list)
    replaced: Optional[bool] = None


# ── Partner configuration ──


@dataclass
class PartnerConfig:
    """``GET /v1/config`` — the engine's ``PartnerConfigResponse`` (nine fields; the
    TypeScript type lists four and is stale). ``None`` means no partner override.
    ``system_prompt`` is never populated: the partner prompt is injected server-side."""

    default_llm_model: Optional[str] = None
    default_stt_model: Optional[str] = None
    default_image_model: Optional[str] = None
    system_prompt: Optional[str] = None
    allowed_llm_models: Optional[List[str]] = None
    allowed_stt_models: Optional[List[str]] = None
    gated_llm_models: Optional[List[str]] = None
    gated_stt_models: Optional[List[str]] = None
    web_search_enabled: bool = False


# ── Models ──


@dataclass
class Model:
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "talk2view"
    # Human-friendly display name (e.g. "Qwen: Qwen3.8 Flash"). None when the
    # catalog has none — display `name or id`.
    name: Optional[str] = None


@dataclass
class ModelsResponse:
    object: str = "list"
    data: List[Model] = field(default_factory=list)


# ── Audio ──


@dataclass
class TranscriptionResponse:
    text: str
    # Pre-translation transcript, only set when translation was requested.
    source_text: Optional[str] = None
    # True when translated, False when translation was requested but failed
    # (``text`` is then the untranslated source), None when not requested.
    translated: Optional[bool] = None


@dataclass
class AudioModel(Model):
    """A speech-to-text model, as ``GET /v1/audio/models`` lists it (TS ``AudioModel``)."""

    # ISO 639-1 codes the model can transcribe. None means unrestricted: offer every language.
    supported_languages: Optional[List[str]] = None
    # A short human blurb (e.g. "Fast voice typing"); None when there is none.
    description: Optional[str] = None


@dataclass
class AudioModelsResponse:
    object: str = "list"
    data: List[AudioModel] = field(default_factory=list)
