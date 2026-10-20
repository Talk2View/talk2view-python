/**
 * The UI server's wire, as the page reads it: the UI server's wire contract
 * (src/talk2view/ui/server.py and src/talk2view/ui/wire.py). Key casing is exact. The chat keys reuse the TS SDK's types with the
 * Python extensions; `timestamp` travels as an ISO-8601 string and is revived to a Date
 * before convert.ts sees a message (selectors.ts).
 */
import type {
  Attachment,
  DisplayMessage,
  PendingApproval,
  ToolStep,
} from '@sdk/types';

export type PathKey = string | number;

export interface Op {
  type: 'set' | 'append-text';
  path: PathKey[];
  value: unknown;
}

/** `event: ops`. Apply only when `base` is the seq the page holds (several batches may be folded). */
export interface OpsEvent {
  seq: number;
  base: number;
  ops: Op[];
}

/** `event: snapshot`: replace everything. */
export interface SnapshotEvent {
  seq: number;
  state: PageState;
}

// ── Chat keys (ChatController.snapshot() without seq) ──

export interface WireToolStep extends ToolStep {
  toolCallId?: string;
  display?: Record<string, unknown>;
  source?: 'client' | 'platform';
}

export interface WireMessage extends Omit<DisplayMessage, 'timestamp' | 'steps'> {
  timestamp: string;
  steps?: WireToolStep[];
}

export interface ApprovalDetail {
  label: string;
  value: string;
}

/** `kind` absent means a client tool; any other value than "client" is not one (fail closed). */
export interface WirePendingApproval extends Omit<PendingApproval, 'kind' | 'details'> {
  kind?: string;
  details?: ApprovalDetail[];
}

export interface AgentStatus {
  type: string;
  message: string;
}

export interface LocalSkill {
  name: string;
  description: string;
  enabled: boolean;
  origin: 'code' | 'dir';
  label: string;
  path?: string;
  overridesPlatform?: boolean;
}

export interface PlatformSkill {
  name: string;
  description: string;
  source: string;
}

export interface SkillsState {
  local: LocalSkill[];
  platform: PlatformSkill[] | null;
  registered: string[];
  available: boolean;
  needsSessionReset: boolean;
}

// ── App keys (AppState) ──

export type OAuthProvider = 'apple' | 'google';

export interface AuthSlice {
  user: { id: string; email: string; user_metadata?: Record<string, unknown> } | null;
  isAuthenticated: boolean;
  isAnonymous: boolean;
  /** A provider sign-in waiting in the end-user's browser. Absent from an older server. */
  oauthPending?: { provider: string } | null;
  /** The last provider sign-in's failure (a cancel is none). Absent from an older server. */
  oauthError?: { type: string; message: string } | null;
}

export interface WireModel {
  id: string;
  object: string;
  created: number;
  owned_by: string;
  name: string | null;
}

export interface ModelsSlice {
  list: WireModel[];
  /** The end-user's own listed choice; null means the default. */
  selected: string | null;
}

export interface PartnerConfig {
  default_llm_model: string | null;
  default_stt_model: string | null;
  default_image_model: string | null;
  system_prompt: string | null;
  allowed_llm_models: string[] | null;
  allowed_stt_models: string[] | null;
  gated_llm_models: string[] | null;
  gated_stt_models: string[] | null;
  web_search_enabled: boolean;
}

export interface Capabilities {
  audio: boolean;
  oauth: boolean;
  /** Display order, Apple first. Absent from an older server. */
  oauthProviders?: string[];
  clipboard: boolean;
}

export interface ThemeSlice {
  mode: 'light' | 'dark';
  tokens: Record<string, string>;
}

export type FontSize = 'small' | 'medium' | 'large';

export interface Preferences {
  model?: string;
  sttModel?: string;
  sttLanguage?: string;
  fontSize?: FontSize;
}

export interface CommandResult {
  id: string | null;
  type: string;
  ok: boolean;
  data?: Record<string, unknown>;
  error?: { type: string; message: string };
}

export interface ChatOptions {
  welcome: { heading: string | null; suggestions: string[] };
  features: {
    dictation: boolean;
    attachments: boolean;
    settings: boolean;
    account: boolean;
    threadList: boolean;
    /** "Continue with Apple / Google" (`ChatFeatures.provider_sign_in`). Absent from an older server. */
    providerSignIn?: boolean;
  };
  allowAnonymous: boolean;
  resetPasswordUrl: string | null;
  footer: string | null;
}

export interface ToolPresentation {
  activity: string | null;
  destructive: boolean;
  warning: string | null;
}

export interface Limits {
  demoLimitReached: boolean;
  anonymousUnavailable: { type: string } | null;
}

/** `snapshot.state`: one flat object; ops address it from the root. */
export interface PageState {
  messages: WireMessage[];
  isLoading: boolean;
  error: string | null;
  pendingApproval: WirePendingApproval | null;
  agentStatus: AgentStatus | null;
  threadId: string | null;
  alwaysAllowedTools: string[];
  skills: SkillsState;
  isAnonymous: boolean;
  auth: AuthSlice;
  models: ModelsSlice;
  config: PartnerConfig | null;
  capabilities: Capabilities;
  theme: ThemeSlice;
  preferences: Preferences;
  results: CommandResult[];
  options: ChatOptions;
  tools: Record<string, ToolPresentation>;
  limits: Limits;
}

/** What the page shows before the first snapshot, and fills in for a key a snapshot lacks. */
export const EMPTY_STATE: PageState = {
  messages: [],
  isLoading: false,
  error: null,
  pendingApproval: null,
  agentStatus: null,
  threadId: null,
  alwaysAllowedTools: [],
  skills: { local: [], platform: null, registered: [], available: false, needsSessionReset: false },
  isAnonymous: false,
  auth: { user: null, isAuthenticated: false, isAnonymous: false, oauthPending: null, oauthError: null },
  models: { list: [], selected: null },
  config: null,
  capabilities: { audio: false, oauth: false, oauthProviders: [], clipboard: false },
  theme: { mode: 'light', tokens: {} },
  preferences: {},
  results: [],
  options: {
    welcome: { heading: null, suggestions: [] },
    features: {
      dictation: true,
      attachments: true,
      settings: true,
      account: true,
      threadList: true,
      providerSignIn: true,
    },
    allowAnonymous: true,
    resetPasswordUrl: null,
    footer: null,
  },
  tools: {},
  limits: { demoLimitReached: false, anonymousUnavailable: null },
};

/** The commands the page sends (`POST /command`); `id` is added by the transport. */
export type Command =
  | { type: 'send'; text: string; attachmentIds: string[] }
  | { type: 'cancel' }
  | {
      type: 'approve';
      toolCallId: string;
      action: 'once' | 'always' | 'deny';
      feedback?: string;
      updatedInput?: Record<string, unknown>;
    }
  | { type: 'reload' }
  | { type: 'newSession' }
  | { type: 'clearError' }
  | { type: 'selectModel'; modelId: string | null }
  | { type: 'login'; email: string; password: string }
  | { type: 'signup'; email: string; password: string }
  | { type: 'logout' }
  | { type: 'startAnonymous' }
  | { type: 'warmUp' }
  | { type: 'setFontSize'; fontSize: FontSize | null }
  | { type: 'copy'; text: string }
  | { type: 'openLink'; url: string }
  | { type: 'skillsList' }
  | { type: 'skillEnable'; name: string }
  | { type: 'skillDisable'; name: string }
  | { type: 'oauthStart'; provider: OAuthProvider }
  | { type: 'oauthCancel' }
  | { type: 'oauthDismissError' };

export type { Attachment };
