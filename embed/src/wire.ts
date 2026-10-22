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
  origin: 'code' | 'dir' | 'bundled' | 'user';
  label: string;
  path?: string;
  overridesPlatform?: boolean;
  /** Absent from an older server. */
  editable?: boolean;
  hasExtraFiles?: boolean;
  problems?: { field: string; message: string }[];
  /**
   * A `user`/`bundled` skill's on-disk entry (its folder's name, or a flat file's stem):
   * what `skillSave.replaces`, `skillDelete`, `skillRestore`, `skillCustomize` and
   * `skillRead` send as their `name`. Usually equal to `name`; differs for a hand-made
   * skill whose frontmatter name is not its folder's. Absent from an older server (send
   * `name` then).
   */
  key?: string;
  /** A bundled skill the end-user's own skill of the same name replaces: listed only
   * ("Replaced by yours"), never toggled or customized. Absent from an older server. */
  overridden?: boolean;
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
  /** Absent from an older server. */
  store?: { writable: boolean; canOpenFolder: boolean };
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
  /** Whether the host can show a file picker for `skillImport`. Absent from an older server. */
  skillImport?: boolean;
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
  welcome: { heading: string | null; subheading?: string | null; suggestions: string[] };
  features: {
    dictation: boolean;
    attachments: boolean;
    settings: boolean;
    account: boolean;
    threadList: boolean;
    /** "Continue with Apple / Google" (`ChatFeatures.provider_sign_in`). Absent from an older server. */
    providerSignIn?: boolean;
    /** The Skills view, and the assistant's `save_skill` tool. Absent from an older server. */
    skills?: boolean;
  };
  allowAnonymous: boolean;
  resetPasswordUrl: string | null;
  footer: string | null;
  /** The app's name (`ChatOptions.app_name`): the Skills view heads the app's own skills
   * "From <name>", else "App skills". Absent from an older server. */
  appName?: string | null;
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

export interface ThreadItem {
  id: string;
  /** From the first user message, or the end-user's rename; null until there is one. */
  title: string | null;
  /** Epoch ms of the last change: orders the list and the Today/Yesterday/Earlier groups. */
  updatedAt: number;
  messageCount: number;
}

/** `threads`: the conversation list (talk2view.ui.threads). Absent from an older server, and
 * when `features.threadList` is off. */
export interface ThreadsSlice {
  currentId: string | null;
  items: ThreadItem[];
}

export interface WireAudioModel {
  id: string;
  name: string | null;
  supported_languages: string[] | null;
  description: string | null;
}

/** `audioModels`: `t2v.voice.list_models()`, mirrored (app_state.py `_audio_model`). Always
 * present; `list` is empty signed out, still loading, or after a failed read. `state`
 * tells those apart: "loading" until the first read answers,
 * "ready" once it has (even with nothing in it — a partner's real empty catalogue), or
 * "error" when the read itself failed; reset to "loading" on every auth change. */
export interface AudioModelsSlice {
  list: WireAudioModel[];
  state: 'loading' | 'ready' | 'error';
}

/** `dictation`: `talk2view.ui.dictation.Dictation`'s own slice. Absent when the host has no
 * microphone (`capabilities.audio` false) — there is then no `Dictation` to publish one. */
export interface DictationSlice {
  phase: 'idle' | 'listening' | 'transcribing';
  /** The last clip's text, once, by `seq`; `reason` is `"max_duration"` for the ten-minute
   * auto-stop (Python only: TS has no cap), else `null` for a manual stop. */
  transcript: { seq: number; text: string; reason: 'max_duration' | null } | null;
  error: { type: string; message: string } | null;
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
  /** Absent from an older server, or when `features.threadList` is off (app_state.py). */
  threads?: ThreadsSlice;
  audioModels: AudioModelsSlice;
  /** Absent from an older server, or when the host has no microphone. */
  dictation?: DictationSlice;
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
      skills: true,
    },
    allowAnonymous: true,
    resetPasswordUrl: null,
    footer: null,
    appName: null,
  },
  tools: {},
  limits: { demoLimitReached: false, anonymousUnavailable: null },
  audioModels: { list: [], state: 'loading' },
};

/** The commands the page sends (`POST /command`); `id` is added by the transport. */
export type Command =
  | { type: 'send'; text: string; attachmentIds: string[]; timing?: boolean }
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
  | {
      type: 'skillSave';
      name?: string;
      description?: string;
      instructions?: string;
      markdown?: string;
      replaces?: string;
      overwrite?: boolean;
    }
  // `replaces` above and `name` in the four below are the skill's on-disk `key`
  // (LocalSkill.key), never a path: the SDK refuses anything that is not one of its
  // listed skills' keys (`unknown_skill`).
  | { type: 'skillDelete'; name: string }
  | { type: 'skillRestore'; name: string }
  | { type: 'skillCustomize'; name: string }
  | { type: 'skillImport'; resolve?: 'replace' | 'keepBoth'; description?: string }
  | { type: 'skillOpenFolder' }
  | { type: 'skillRead'; name: string }
  | { type: 'threadNew' }
  | { type: 'threadSwitch'; threadId: string }
  | { type: 'threadRename'; threadId: string; title: string }
  | { type: 'threadDelete'; threadId: string }
  | { type: 'selectSttModel'; modelId: string | null }
  | { type: 'selectSttLanguage'; language: string | null }
  | { type: 'dictationStart' }
  | { type: 'dictationStop' }
  | { type: 'dictationCancel' }
  | { type: 'oauthStart'; provider: OAuthProvider }
  | { type: 'oauthCancel' }
  | { type: 'oauthDismissError' }
  | {
      /** The page's part of a turn it sent with `timing` (talk2view.metrics). */
      type: 'metrics';
      messageId: string;
      sendToPostMs: number | null;
      firstOpToPaintMs: number | null;
      firstOpAtMs: number | null;
    };

export type { Attachment };
