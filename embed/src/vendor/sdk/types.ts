// vendored-from: Talk2View/talk2view-typescript src/types.ts
// vendored-commit: b042b005611c59e8bc6fbd304ea1f028d11fb88b
// local-delta: types.ts.delta.diff
//
// A copy of the public TS SDK's types, plus one local delta, declared in full in
// types.ts.delta.diff beside this file. The delta is this page's own addition:
// PendingApproval.kind and PendingApproval.details, the
// ApprovalDetail type, and ToolStep.display. Why: the Python SDK's wire carries them
// (engine approvals and tool displays), and the canonical repository does not
// have them yet. CI checks that this body is the recorded commit plus that diff, and warns
// when canonical main has changed the file since the commit
// (scripts/check-vendored-sdk.mjs). Re-vendor rather than edit (README.md, "Where the TS
// SDK code comes from").
// end of vendoring header
// ── Configuration ──

export interface T2VConfig {
  partnerKey: string;
  baseUrl?: string;
  /** @deprecated Voice requests now route through baseUrl. This option will be removed in a future version. */
  voiceApiUrl?: string;
  model?: string;
  /** Timeout in milliseconds for HTTP requests. Defaults to 30000 (30s). Does not apply to SSE streams after connection. */
  requestTimeout?: number;
  /** Enable debug logging to console. Logs streaming events, tool calls, history, and state changes. */
  debug?: boolean;
  /** Auto-start an anonymous demo session on first chat() if not authenticated. Defaults to true. */
  anonymousAutoStart?: boolean;
}

// ── Auth ──

export interface LoginRequest {
  email: string;
  password: string;
}

export interface SignupRequest {
  email: string;
  password: string;
}

export interface User {
  id: string;
  email: string;
  user_metadata?: Record<string, unknown>;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: User | null;
  user_api_key?: string;
  is_anonymous?: boolean;
  key_pending?: boolean;
}

/** Result of {@link T2VAuth.signup}. */
export interface SignupOutcome {
  /** Set when a session exists (immediate sign-in or 409→login fallback). */
  user: User | null;
  /** True while the emailed confirmation link is unclicked. For a guest
   *  convert the anonymous session stays live and upgrades in place. */
  confirmationRequired: boolean;
}

export interface RefreshResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

// ── Sessions ──

export interface CreateSessionResponse {
  session_id: string;
  thread_id: string;
  model: string;
}

// ── Attachments ──

/** Metadata returned by POST /v1/attachments (Talk2View.uploadAttachment). */
export interface Attachment {
  id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
}

export interface TextContentPart {
  type: 'text';
  text: string;
}

/** References an uploaded attachment by id — the server resolves the bytes. */
export interface AttachmentContentPart {
  type: 'attachment';
  attachment_id: string;
}

export type MessageContentPart = TextContentPart | AttachmentContentPart;

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content: string | MessageContentPart[];
  tool_call_id?: string;
}

export interface SendMessageRequest {
  messages: ChatMessage[];
  stream?: boolean;
  temperature?: number;
  model?: string;
  /**
   * The client renders platform-tool activity, so the server emits tool_event
   * chunks (tool_start / tool_end) for its server-side tools. Defaults to
   * false: omit it and no tool_event reaches the client. Also accepted on
   * /resume.
   */
  supports_tool_events?: boolean;
  /**
   * The client can render generated images. Gates REGISTRATION of the
   * generate_image tool — omit it and the model has no image tool at all.
   * Send it together with `supports_tool_events: true`, because the images
   * themselves arrive as tool_event display kinds ('image' / 'image_failure',
   * see ImageToolDisplay), whose bytes you then fetch from
   * GET /v1/attachments/{id}/content. Also accepted on /resume.
   */
  supports_image_generation?: boolean;
  /**
   * Image model id (mode image_generation, see GET /v1/images/models). Omit for
   * the partner's default, then the platform default. Unknown → 400
   * image_model_unknown.
   */
  image_model?: string;
}

// ── Streaming ──

export interface ChatCompletionChunkDelta {
  role?: string;
  content?: string;
  /** The model's reasoning (Gemini thought summaries, Claude thinking); only when the request set supports_reasoning. */
  reasoning_content?: string;
}

export interface ChatCompletionChunkChoice {
  index: number;
  delta: ChatCompletionChunkDelta;
  finish_reason?: string | null;
}

export interface ToolCallInterrupt {
  type: 'tool_call';
  tool_name: string;
  tool_call_id: string;
  arguments: Record<string, unknown>;
}

export interface AgentStatus {
  type: string;
  message: string;
}

/**
 * Structured error info on a streaming chunk.
 * `message` is also mirrored into `delta.content` for OpenAI-shaped consumers.
 * `type` (exception class name) and `detail` (repr) are debug aids — log them
 * in devtools, do not surface to end users.
 */
export interface ChunkError {
  type: string;
  message: string;
  detail?: string;
}

/**
 * Lifecycle of one server-side platform tool call. Only present when the
 * request set `supports_tool_events: true`. `display` is a tool-declared,
 * render-safe object (e.g. `{kind: 'places', …}` from the Google Maps tools, `{kind: 'image', …}` / `{kind: 'image_failure', …}` from generate_image — see ImageToolDisplay).
 */
export interface ToolEvent {
  type: 'tool_start' | 'tool_end';
  tool_name: string;
  tool_call_id: string;
  arguments?: Record<string, unknown>;
  status?: 'ok' | 'error' | 'denied';
  display?: Record<string, unknown>;
}

/**
 * `display` of a successful `generate_image` tool_end (`kind: 'image'`).
 * `image` is a path relative to the engine base URL — fetch it with your normal
 * auth headers (it is owner-scoped; 404 when missing, expired or foreign).
 * `width`/`height` are null when the header could not be read.
 */
export interface GeneratedImageDisplay {
  kind: 'image';
  attachment_id: string;
  image: string;
  filename: string;
  mime_type: string;
  width: number | null;
  height: number | null;
  prompt: string;
  model: string;
  source_image_id: string | null;
}

/** `display` of a failed `generate_image` tool_end (`status: 'error'`). Unknown reasons render generically. */
export interface ImageFailureDisplay {
  kind: 'image_failure';
  reason: 'content_filter' | 'provider_error' | 'timeout' | 'budget_exceeded' | (string & {});
}

export type ImageToolDisplay = GeneratedImageDisplay | ImageFailureDisplay;

export interface ChatCompletionChunk {
  id: string;
  object: string;
  created: number;
  model: string;
  choices: ChatCompletionChunkChoice[];
  thread_id?: string;
  interrupt?: ToolCallInterrupt;
  status?: AgentStatus;
  tool_event?: ToolEvent;
  todos?: string;
  error?: ChunkError;
}

// ── Chat Events (public API) ──

export type ChatEvent =
  | { type: 'text'; content: string }
  | { type: 'tool_call'; toolName: string; toolCallId: string; arguments: Record<string, unknown> }
  | { type: 'approval_required'; toolName: string; toolCallId: string; arguments: Record<string, unknown>; description: string }
  | { type: 'approval_result'; toolName: string; decision: HumanDecision['action'] }
  | { type: 'todos'; content: string }
  | { type: 'done'; threadId: string }
  | {
      type: 'error';
      message: string;
      /**
       * Machine-readable error category. Most values are engine-emitted, e.g.
       * `budget_exceeded` (anonymous demo budget exhausted). One is SDK-emitted:
       * `session_lost` — the chat session was lost mid-turn, or while a tool
       * result or approval resume was in flight, and the SDK deliberately did
       * not resend, because a client tool handler may already have run.
       */
      errorType?: string;
      detail?: string;
    }
  | { type: 'status'; status: string; message: string };

// ── Human-in-the-Loop ──

/**
 * Permission result from a ToolPermissionCallback.
 * Mirrors Claude Agent SDK's PermissionResultAllow / PermissionResultDeny.
 */
export type PermissionResult =
  | { type: 'allow'; updatedInput?: Record<string, unknown> }
  | { type: 'deny'; message?: string };

/**
 * Async callback for programmatic per-call tool permission decisions.
 * Equivalent to Claude Agent SDK's `can_use_tool` callback.
 */
export type ToolPermissionCallback = (
  toolName: string,
  args: Record<string, unknown>,
) => Promise<PermissionResult>;

/**
 * Internal result from T2VTools.checkPermission().
 */
export type PermissionCheckResult =
  | { action: 'allow'; updatedInput?: Record<string, unknown> }
  | { action: 'require_approval' }
  | { action: 'deny'; message?: string };

export interface PendingApproval {
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  description: string;
  /**
   * Who asked. `'client'` (or absent): an SDK-side permission check on a client tool,
   * answered with once / always / deny. `'engine'`: an engine-enforced approval of a
   * platform tool (ADR 0004), answered with allow or deny only. Set by the Python SDK
   * today; the TS client does not request engine approvals yet.
   */
  kind?: 'client' | 'engine';
  /** The engine's label/value summary of an engine approval. */
  details?: ApprovalDetail[];
}

/**
 * One line of an engine approval's summary. `label` and `value` are engine-supplied
 * text, not markup — render them as plain text, never as HTML.
 */
export interface ApprovalDetail {
  label: string;
  value: string;
}

export interface HumanDecision {
  action: 'once' | 'always' | 'deny';
  /** Optional corrective feedback (typically for 'deny'). */
  feedback?: string;
  /** Optional updated arguments (for 'once' or 'always'). */
  updatedInput?: Record<string, unknown>;
}

// ── Tools ──

export interface ClientToolParameter {
  type: string;
  description: string;
  enum?: string[];
}

export interface ClientToolSchema {
  name: string;
  description: string;
  parameters: {
    type: 'object';
    properties: Record<string, ClientToolParameter>;
    required?: string[];
    additionalProperties?: boolean;
  };
  return_direct?: boolean;
  /**
   * Permission check before tool execution.
   * - `true`: always requires human approval (shows approval card)
   * - `false` / omitted: auto-execute
   * - callback: programmatic per-call decision (like Claude Agent SDK's can_use_tool)
   */
  permission?: boolean | ToolPermissionCallback;
}

export type ToolHandler = (args: Record<string, unknown>) => Promise<string>;

export interface ClientTool extends ClientToolSchema {
  execute: ToolHandler;
}

export interface RegisterToolsResponse {
  registered: string[];
  count: number;
}

// ── Skills ──

export interface UserSkill {
  name: string;
  description: string;
  content: string;
}

export interface RegisterSkillsResponse {
  registered: string[];
  count: number;
}

// ── Models ──

export interface Model {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  /**
   * Human-friendly display name (e.g. "Qwen: Qwen3.8 Flash"). Null or absent
   * when the catalog has none — render `name ?? id`.
   */
  name?: string | null;
}

export interface ModelsResponse {
  object: string;
  data: Model[];
}

// ── Audio ──

export interface TranscriptionResponse {
  text: string;
}

/** A speech-to-text model, as listed by GET /v1/audio/models. */
export interface AudioModel extends Model {
  /**
   * ISO 639-1 codes the model can transcribe. Null or absent means
   * unrestricted — offer your full language list.
   */
  supported_languages?: string[] | null;
  /** Short human blurb (e.g. "Fast voice typing"); null or absent when none. */
  description?: string | null;
}

export interface AudioModelsResponse {
  object: string;
  data: AudioModel[];
}

// ── Images (OpenAI Images API shape) ──

export interface ImageGenerationRequest {
  model?: string;
  prompt: string;
  /** Only 1 is supported; anything else → 400 unsupported_parameter. */
  n?: 1;
  /** OpenAI WxH size; mapped to aspect_ratio when aspect_ratio is omitted. */
  size?: string;
  response_format?: 'url' | 'b64_json';
  aspect_ratio?: '1:1' | '16:9' | '9:16' | '4:3' | '3:4';
}

export interface ImageEditRequest {
  model?: string;
  prompt: string;
  /** An attachment the user owns (an upload or a previously generated image). */
  source_attachment_id: string;
  response_format?: 'url' | 'b64_json';
}

export interface ImageData {
  /** Attachment content path, relative to the engine base URL. */
  url: string;
  b64_json?: string | null;
  revised_prompt?: string | null;
  attachment_id: string;
  width?: number | null;
  height?: number | null;
}

export interface ImagesResponse {
  created: number;
  data: ImageData[];
  usage: Record<string, unknown>;
}

export interface ImageModel {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  name?: string | null;
  description?: string | null;
  cost_tier?: string | null;
}

export interface ImageModelsResponse {
  object: string;
  data: ImageModel[];
}

// ── Partner Config ──

export interface PartnerConfig {
  default_llm_model: string | null;
  default_stt_model: string | null;
  default_image_model: string | null;
  system_prompt: string | null;
}

// ── User Preferences ──

export interface UserPreferences {
  model?: string;
  sttModel?: string;
  sttLanguage?: string;
  fontSize?: 'small' | 'medium' | 'large';
}

// ── Errors ──

export interface APIErrorBody {
  error: {
    type: string;
    message: string;
    code?: string;
  };
}

// ── Display / UI ──

export interface ToolStep {
  name: string;
  status: 'used' | 'denied' | 'running';
  args?: Record<string, unknown>;
  result?: string;
  /** A tool event's render-safe `display` (e.g. ImageToolDisplay). Set by the Python SDK today. */
  display?: Record<string, unknown>;
}

export interface DisplayMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  isStreaming?: boolean;
  /** Files attached to a user message (rendered as chips in the bubble). */
  attachments?: Attachment[];
  /** Markdown checklist from the agent's planning tool (write_todos). */
  plan?: string;
  /** Completed tool call steps (rendered as collapsible inline steps). */
  steps?: ToolStep[];
}

/**
 * A whole conversation, taken out of the client and put back later.
 *
 * All three parts are needed to carry on where it left off: `messages` is what
 * the end-user sees, `history` is the clean user/assistant alternation replayed
 * to the model with every turn, and `threadId` is what the engine last called
 * this thread. Nothing here depends on the engine still remembering it — the
 * transcript travels with the next message.
 *
 * @see Talk2View.exportConversation
 * @see Talk2View.restoreConversation
 */
export interface ConversationSnapshot {
  /** The transcript as displayed, segmented the way the thread renders it. */
  messages: DisplayMessage[];
  /** What the model is told, before this turn's own message is appended. */
  history: ChatMessage[];
  /** The engine's id for this thread, or null if no turn has finished yet. */
  threadId: string | null;
}

export interface T2VEventMap {
  [key: string]: unknown[];
  messagesChange: [DisplayMessage[]];
  loadingChange: [boolean];
  errorChange: [string | null];
  approvalChange: [PendingApproval | null];
  statusChange: [AgentStatus | null];
  threadIdChange: [string | null];
  alwaysAllowedChange: [ReadonlySet<string>];
  demoLimitReached: [];
  /** Anonymous sign-in was refused (partner setting, daily anonymous cap, or captcha). */
  anonymousUnavailable: [reason: string];
  /** A lost chat session was replaced; the turn was resent on the new one. */
  sessionRecovered: [sessionId: string];
}

// ── Theme ──

export interface Talk2ViewTheme {
  accent?: string;
  accentForeground?: string;
  bg?: string;
  foreground?: string;
  muted?: string;
  border?: string;
  userBubble?: string;
  userForeground?: string;
  error?: string;
  radius?: number;
  font?: string;
  fontMono?: string;
  surface?: string;
  surfaceHover?: string;
  /** Box-shadow used for elevated surfaces (composer, dropdowns, cards). */
  shadow?: string;
}
