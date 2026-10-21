/**
 * Pure reads of the page state: what the views derive (the guest gate
 * among them) and the small mappings between the wire and the TS SDK's types.
 * No React, no DOM: every rule here is unit-tested.
 */
import type { DisplayMessage, PendingApproval, ToolStep } from '@sdk/types';
import type {
  AuthSlice,
  CommandResult,
  OAuthProvider,
  PageState,
  SkillsState,
  ThreadItem,
  ToolPresentation,
  WireAudioModel,
  WireMessage,
  WirePendingApproval,
  WireToolStep,
} from './wire';

/** A turn waiting on an approval is still in progress. */
export function isRunning(s: PageState): boolean {
  return s.isLoading || s.pendingApproval !== null;
}

/**
 * A stable key for "who is this right now": the user id, `isAuthenticated` and
 * `isAnonymous` together — two different accounts, a guest and a real one, and signed-out
 * and any account, all compare unequal. One definition, shared by everything that must
 * react to "the identity changed" (Account.tsx clears a typed password; Composer.tsx
 * clears a draft and any picked files) — a second, independently-typed copy of this
 * string is exactly the kind of drift that stops catching a real identity change.
 */
export function authIdentityKey(auth: Pick<AuthSlice, 'user' | 'isAuthenticated' | 'isAnonymous'>): string {
  return `${auth.user?.id ?? ''}:${String(auth.isAuthenticated)}:${String(auth.isAnonymous)}`;
}

export type GateReason = 'demo-limit' | 'guest-limit' | null;

/** TS `useAuthGate` (chat/auth-gate.tsx), over the `auth`, `options` and `limits` slices. */
export function gate(s: PageState): { gated: boolean; reason: GateReason } {
  const { demoLimitReached, anonymousUnavailable } = s.limits;
  const gated =
    (!s.auth.isAuthenticated && (!s.options.allowAnonymous || anonymousUnavailable !== null)) ||
    demoLimitReached;
  const reason: GateReason = demoLimitReached ? 'demo-limit' : anonymousUnavailable !== null ? 'guest-limit' : null;
  return { gated, reason };
}

/** TS `REASON_COPY`. */
export const REASON_COPY: Record<Exclude<GateReason, null>, string> = {
  'guest-limit': 'Guest chat isn’t available right now. Sign in to keep going.',
  'demo-limit': 'You’ve reached the guest limit. Sign in or create a free account to keep going.',
};

/** What a busy latch says when its result has not come within `WATCH_MS` (store.ts). */
export const TOO_LONG = 'That took too long. Try again.';

/** The gate reason line when there is none of `REASON_COPY`'s own (gated by
 * `!options.allowAnonymous` alone, with no demo/guest limit reached to name). */
export const SIGN_IN_REQUIRED = 'This app asks you to sign in before you chat.';

/** The Account view's avatar letters: the first letter of each of the first two
 * `.`/`_`/`-`-separated parts of the email's local part, uppercased. */
export function initials(email: string): string {
  const local = email.split('@')[0] ?? '';
  return local
    .split(/[._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p.charAt(0).toUpperCase())
    .join('');
}

export const PROVIDER_NAMES: Record<OAuthProvider, string> = { apple: 'Apple', google: 'Google' };
const PROVIDER_ORDER: OAuthProvider[] = ['apple', 'google'];

/**
 * The provider buttons to show, Apple first (Apple asks to be no less prominent): none
 * unless the integrator left them on (`features.providerSignIn`) and the server says the
 * engine offers desktop sign-in for this partner (`capabilities.oauth`). A key an older
 * server does not send reads as off.
 */
export function providerButtons(s: PageState): OAuthProvider[] {
  if (s.options.features.providerSignIn !== true || !s.capabilities.oauth) return [];
  const offered = s.capabilities.oauthProviders ?? [];
  return PROVIDER_ORDER.filter((p) => offered.indexOf(p) !== -1);
}

/** Signed out, guests allowed, and none refused yet: the page starts a guest before a send. */
export function needsGuestStart(s: PageState): boolean {
  return !s.auth.isAuthenticated && s.options.allowAnonymous && s.limits.anonymousUnavailable === null;
}

/**
 * The messages assistant-ui renders. A turn that failed before its first chunk has no
 * assistant message, and assistant-ui shows an error only on one, so lend it an empty one
 * (as TS assistant-ui/index.ts does).
 */
export function threadMessages(s: PageState): WireMessage[] {
  const last = s.messages[s.messages.length - 1];
  if (s.error && last && last.role !== 'assistant') {
    return [...s.messages, { id: last.id + ':error', role: 'assistant', content: '', timestamp: last.timestamp }];
  }
  return s.messages;
}

/** The last user message's id: the only turn `reload` can send again. */
export function lastUserId(s: PageState): string | null {
  for (let i = s.messages.length - 1; i >= 0; i--) {
    if (s.messages[i]!.role === 'user') return s.messages[i]!.id;
  }
  return null;
}

const revived = new WeakMap<WireMessage, DisplayMessage>();

function toStep(step: WireToolStep): ToolStep {
  return step as ToolStep;
}

/**
 * The TS `DisplayMessage` for a wire message: `timestamp` revived to a Date. Cached per
 * wire object, so a message the ops did not touch keeps its identity (and assistant-ui's
 * conversion cache).
 */
export function toDisplayMessage(m: WireMessage): DisplayMessage {
  let out = revived.get(m);
  if (!out) {
    const parsed = new Date(m.timestamp);
    out = {
      ...m,
      timestamp: Number.isNaN(parsed.getTime()) ? new Date(0) : parsed,
      ...(m.steps ? { steps: m.steps.map(toStep) } : {}),
    } as DisplayMessage;
    revived.set(m, out);
  }
  return out;
}

/** Only a `kind` of "client" (or none) is a client tool; anything else is treated as an engine approval. */
export function isClientApproval(a: Pick<WirePendingApproval, 'kind'>): boolean {
  return a.kind === undefined || a.kind === 'client';
}

/** The TS `PendingApproval` convert.ts takes, with `kind` failed closed. */
export function toPendingApproval(a: WirePendingApproval | null): PendingApproval | null {
  if (!a) return null;
  return {
    toolCallId: a.toolCallId,
    toolName: a.toolName,
    arguments: a.arguments,
    description: a.description,
    kind: isClientApproval(a) ? 'client' : 'engine',
    ...(a.details ? { details: a.details } : {}),
  };
}

/** A command's result, matched by id; the newest wins. */
export function findResult(s: PageState, id: string | null): CommandResult | null {
  if (!id) return null;
  for (let i = s.results.length - 1; i >= 0; i--) {
    if (s.results[i]!.id === id) return s.results[i]!;
  }
  return null;
}

/** What the page tells the end-user about a failed command. Unknown types show the server's message. */
export function failureText(result: Pick<CommandResult, 'type' | 'error'>): string {
  const error = result.error;
  const type = error?.type ?? 'unknown';
  switch (type) {
    case 'busy':
      // A dictationStart refused while one is already running is
      // not the send-queue's own "busy" — a double tap on the mic must never say the
      // assistant is working on a message.
      return result.type === 'dictationStart' ? 'A dictation is already in progress.' : 'The assistant is still working on the last message.';
    case 'closed':
    case 'stopped':
      return 'This chat has stopped. Open it again from the app.';
    case 'account_unavailable':
      return 'This chat is not connected to a Talk2View account.';
    case 'attachments_disabled':
      return 'This app does not take attachments.';
    case 'unknown_attachment':
      return 'An attachment was lost. Attach the file again.';
    case 'unknown_thread':
      return 'That chat is gone. Open Chats to pick another.';
    case 'thread_list_disabled':
      return 'This app keeps no chat history.';
    case 'skill_exists':
      // `skillRestore` (Undo) after the name was taken again; its message is the name.
      if (result.type === 'skillRestore') {
        return `A skill named ${error?.message ?? ''} exists again, so Undo can't bring the deleted one back.`;
      }
      return error?.message ? `A skill named ${error.message} already exists.` : 'That skill name is taken.';
    case 'network_error':
      return 'Talk2View could not be reached. Check your connection and try again.';
    default:
      return error?.message || 'Something went wrong.';
  }
}

/** Display name of a model, as TS Settings shows it. */
export function modelLabel(m: { id: string; name: string | null }): string {
  return m.name ?? m.id;
}

/** "Default — <name>" when the partner's default is listed, else "Default". */
export function defaultModelLabel(s: PageState): string {
  const id = s.config?.default_llm_model;
  const listed = id ? s.models.list.find((m) => m.id === id) : undefined;
  return listed ? 'Default — ' + modelLabel(listed) : 'Default';
}

/**
 * The AI model picker's value and orphan option (Settings.tsx). A stored
 * pick outside the list reads "no longer offered" only once the list has actually
 * loaded: an empty list — before `list_models` returns, or after it fails for good —
 * proves nothing (app_state.py keeps the pick applied regardless of an unreadable
 * list), so it must never read that way. `loaded` also drives the picker's `disabled`.
 * `value` prefers `models.selected` (what the server applied) over `preferences.model`
 * (the end-user's raw ask): a refused pick must not stick.
 */
export function modelPick(s: PageState): { value: string; orphan: string | null; loaded: boolean } {
  const list = s.models.list;
  const loaded = list.length > 0;
  const pick = s.models.selected ?? s.preferences.model ?? null;
  const known = pick !== null && list.some((m) => m.id === pick);
  return { value: pick ?? '', orphan: loaded && pick !== null && !known ? pick : null, loaded };
}

/**
 * What Settings' skills section says (signed in). "Off" needs evidence: a session with
 * skills disabled refuses the register AND the listing, so `available` is false and
 * `platform` is still null once this visit's listing (`skillsList`) has answered.
 * `available` false on its own proves nothing: the SDK's first register can fire before
 * anyone has signed in, be refused with a 401, and wait for the next message, whose turn
 * flushes it (`chat()` flushes before every turn). So the local skills are listed unless
 * the section is off. While no register has succeeded yet, one quiet line says they reach
 * the assistant with the next message, but only once the listing has answered with a
 * catalogue, the proof that skills are on: before it answers, or when it fails, the page
 * does not know, and a partner with skills disabled would refuse that register.
 */
export function skillsSection(
  skills: Pick<SkillsState, 'local' | 'platform' | 'available'>,
  listed: boolean,
): { off: boolean; awaitingRegister: boolean } {
  const off = listed && !skills.available && skills.platform === null;
  const skillsAreOn = listed && skills.platform !== null;
  return { off, awaitingRegister: skillsAreOn && !skills.available && skills.local.some((skill) => skill.enabled) };
}

/** The approval card's arguments editor: only a JSON object can be sent back changed. */
export function parseArguments(draft: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(draft);
    return value !== null && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/**
 * Structural equality over plain JSON values (objects, arrays, strings, numbers,
 * booleans, null) — the approval card's editor is seeded with the ESCAPED form of the
 * original arguments (`escapeForJson`, ToolCall.tsx), so "was this actually edited?" can
 * no longer be a string comparison against the raw, unescaped original: the two would
 * never be textually equal even when the user has not touched anything. Comparing the
 * PARSED values instead means an untouched draft (edited only by the seed swapping raw
 * characters for their escapes, which `JSON.parse` reads back as the same characters)
 * reads as unchanged, and only a real edit sends `updatedInput`.
 */
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  if (typeof a === 'object' && typeof b === 'object') {
    const ak = Object.keys(a as Record<string, unknown>);
    const bk = Object.keys(b as Record<string, unknown>);
    if (ak.length !== bk.length) return false;
    return ak.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEqual((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
  }
  return false;
}

/** Arguments as a person reads them. */
export function prettyArguments(args: unknown): string {
  if (args && typeof args === 'object' && Object.keys(args).length > 0) {
    try {
      return JSON.stringify(args, null, 2);
    } catch {
      // not serialisable: fall through
    }
  }
  return '{}';
}

/**
 * Can this approval's arguments be shown at all? Only a plain object (or none, meaning "no
 * parameters") is displayable; anything else — a string, an array, a number, a circular
 * object — is arguments `prettyArguments` cannot render, and it falls back to the SAME
 * "{}" a genuinely empty, parameter-less call produces: on the security boundary, that
 * reads as "nothing to review" when what really happened is the arguments were silently
 * dropped. The approval card fails closed on this rather than trust the fallback: no
 * "{}", and no Allow — only Deny (ToolCall.tsx).
 */
export function canShowArguments(args: unknown): boolean {
  if (args === undefined) return true;
  if (args === null || typeof args !== 'object' || Array.isArray(args)) return false;
  try {
    JSON.stringify(args);
    return true;
  } catch {
    return false;
  }
}

/**
 * Does `value` hold an integer too large for a JavaScript number? The page parses every
 * event with `JSON.parse`, which rounds such a number on the way in (Chromium 83 has no
 * reviver that sees the source text), so the approval card can only show it rounded, and
 * an edit would send the rounded value back. The tool itself, unedited, gets the exact
 * value: the server still holds it. Walks iteratively, so a deep value cannot overflow.
 */
export function hasUnsafeInteger(value: unknown): boolean {
  const stack: unknown[] = [value];
  const seen = new Set<object>();
  while (stack.length > 0) {
    const item = stack.pop();
    if (typeof item === 'number') {
      if (Number.isInteger(item) && !Number.isSafeInteger(item)) return true;
    } else if (item !== null && typeof item === 'object' && !seen.has(item)) {
      seen.add(item);
      for (const child of Array.isArray(item) ? item : Object.values(item)) stack.push(child);
    }
  }
  return false;
}

/**
 * Does a composer chip in this state hold Send back (Composer.tsx)? An upload still
 * running does, and so does a refused one (`incomplete`: removed at once, guarded anyway).
 * A chip ready to go (`requires-action`) does not, nor does one already sent (`complete`),
 * which assistant-ui puts back in the composer if a send throws.
 */
export function chipBlocksSend(status: string): boolean {
  return status !== 'requires-action' && status !== 'complete';
}

/** What an upload refusal tells the end-user. */
export function uploadFailureText(type: string, message: string): string {
  switch (type) {
    case 'account_changed':
      return 'The account changed during the upload. Attach the file again.';
    case 'too_large':
      return 'That file is too large to attach.';
    case 'unsupported_media_type':
      return 'Attach a PNG, JPEG, WebP, GIF or PDF file.';
    case 'attachments_disabled':
      return 'This app does not take attachments.';
    case 'network_error':
      return 'Talk2View could not be reached. Check your connection and try again.';
    default:
      return message || 'The file could not be attached.';
  }
}

/**
 * May the end-user answer this approval with "Always allow"?
 *
 * Only for a client tool the host has not marked `destructive`. One click on a
 * destructive tool would let it run unasked for the rest of the conversation — a capture
 * tool would go on uploading slice views, patient details in the corner text and all. An
 * engine approval never offers it either (it is per call, and Python ignores an
 * `always` there), and anything but `kind: 'client'` counts as an engine approval, so a
 * `kind` this page does not know fails closed.
 *
 * A tool with no presentation entry is not destructive (the `tools` slice).
 */
export function mayAlwaysAllow(kind: string | undefined, presentation: ToolPresentation | undefined): boolean {
  const client = kind === undefined || kind === 'client';
  return client && !presentation?.destructive;
}

/** TS conversation-store.ts UNTITLED. */
export const UNTITLED_THREAD = 'New chat';
const DAY_MS = 86_400_000;

/** The header's title: the current thread's, else "New chat" (TS chat.tsx:212). */
export function threadTitle(s: PageState): string {
  const threads = s.threads;
  if (!threads || threads.currentId === null) return UNTITLED_THREAD;
  const current = threads.items.find((item) => item.id === threads.currentId);
  return current?.title || UNTITLED_THREAD;
}

/** The threads the list shows: an empty conversation is not one yet (use-conversations.ts:247-253). */
export function listedThreads(s: PageState): ThreadItem[] {
  return (s.threads?.items ?? []).filter((item) => item.messageCount > 0);
}

/** A thread row's time, at the right: the time today, the weekday this week, else the date. */
export function whenLabel(updatedAt: number, now: number): string {
  const at = new Date(updatedAt);
  const today = new Date(now);
  const sameDay = at.getFullYear() === today.getFullYear() && at.getMonth() === today.getMonth() && at.getDate() === today.getDate();
  // The locale's own AM/PM casing ("8:50 AM" on this machine) isn't
  // what the mockup shows ("2:14 pm", en-GB) — normalise it rather than leave it locale-
  // dependent, so the row reads the same regardless of where the host runs.
  if (sameDay) return at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }).replace(/\s?(AM|PM)$/, (m) => m.toLowerCase());
  if (now - updatedAt < 6 * 24 * 60 * 60 * 1000) return at.toLocaleDateString([], { weekday: 'short' });
  return at.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

export type ThreadGroup = { label: 'Today' | 'Yesterday' | 'Earlier'; items: ThreadItem[] };

/** vendor/thread-list.aui.tsx useThreadListGroups: filter by title, bucket by day, newest first. */
export function threadGroups(items: ThreadItem[], now: number, query: string): ThreadGroup[] {
  const q = query.trim().toLowerCase();
  const date = new Date(now);
  const startOfToday = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const label = (at: number): ThreadGroup['label'] => (at >= startOfToday ? 'Today' : at >= startOfToday - DAY_MS ? 'Yesterday' : 'Earlier');
  const matched = items
    .filter((item) => !q || (item.title || UNTITLED_THREAD).toLowerCase().includes(q))
    .slice()
    .sort((a, b) => b.updatedAt - a.updatedAt);
  const groups: ThreadGroup[] = [];
  for (const item of matched) {
    const name = label(item.updatedAt);
    const last = groups[groups.length - 1];
    if (last && last.label === name) last.items.push(item);
    else groups.push({ label: name, items: [item] });
  }
  return groups;
}

/* Offered when the voice model does not restrict its languages (the wire's own
 * `supported_languages` is null or empty). TS settings.tsx's own list. */
export const COMMON_LANGUAGES = ['en', 'vi', 'zh', 'ja', 'ko', 'fr', 'de', 'es'];

/** The engine's fallback voice model — `DEFAULT_DICTATION_MODEL` in the SDK. */
export const FALLBACK_STT_MODEL = 'faster-whisper-base';

const displayNames = typeof Intl !== 'undefined' && 'DisplayNames' in Intl ? new Intl.DisplayNames(['en'], { type: 'language' }) : null;

/** "vi" → "Vietnamese"; the code itself when Intl does not know it (Chromium 83 has
 * DisplayNames since 81). */
export function languageName(code: string): string {
  try {
    return displayNames?.of(code) ?? code;
  } catch {
    return code;
  }
}

/** The languages to offer for a voice model, A–Z by name (TS settings.tsx `languagesFor`). */
export function languagesFor(model: WireAudioModel | undefined): Array<[string, string]> {
  const codes = model?.supported_languages?.length ? model.supported_languages : COMMON_LANGUAGES;
  return Array.from(new Set(codes)).map((code): [string, string] => [code, languageName(code)]).sort((x, y) => x[1].localeCompare(y[1]));
}

/** The voice model actually in use: the end-user's own choice, else the partner's default,
 * else the engine's fallback (TS settings.tsx). */
export function activeVoiceModel(s: PageState): WireAudioModel | undefined {
  const wanted = s.preferences.sttModel || s.config?.default_stt_model || FALLBACK_STT_MODEL;
  return s.audioModels.list.find((m) => m.id === wanted);
}

/** What the page tells the end-user about a failed dictation (TS settings.tsx / composer-extras.tsx). */
export function dictationFailureText(type: string, message: string): string {
  switch (type) {
    case 'microphone_denied':
      return 'The app was not allowed to use the microphone. Allow it in the system’s privacy settings, then try again.';
    case 'no_microphone':
      return 'No microphone was found.';
    case 'nothing_heard':
      return 'Nothing was heard. Check the microphone and its permission, then try again.';
    case 'sign_in_required':
      return 'Sign in, or send a message first, to dictate.';
    case 'dictation_unavailable':
      return 'Dictation is not available in this app.';
    case 'model_not_allowed':
    case 'model_gated':
      return message || 'This voice model is not available.';
    default:
      return message || 'The recording could not be transcribed.';
  }
}
