import { describe, expect, it } from 'vitest';
import {
  activeVoiceModel,
  authIdentityKey,
  canShowArguments,
  chipBlocksSend,
  deepEqual,
  defaultModelLabel,
  dictationFailureText,
  failureText,
  findResult,
  gate,
  hasUnsafeInteger,
  initials,
  isRunning,
  languagesFor,
  lastUserId,
  listedThreads,
  mayAlwaysAllow,
  modelPick,
  needsGuestStart,
  parseArguments,
  providerButtons,
  skillsSection,
  threadGroups,
  threadMessages,
  threadTitle,
  toDisplayMessage,
  toPendingApproval,
  UNTITLED_THREAD,
  uploadFailureText,
  whenLabel,
} from '../src/selectors';
import { EMPTY_STATE, type PageState, type PartnerConfig, type ToolPresentation, type WireMessage } from '../src/wire';

const EMPTY_CONFIG: PartnerConfig = {
  default_llm_model: null,
  default_stt_model: null,
  default_image_model: null,
  system_prompt: null,
  allowed_llm_models: null,
  allowed_stt_models: null,
  gated_llm_models: null,
  gated_stt_models: null,
  web_search_enabled: false,
};

const s = (over: Partial<PageState> = {}): PageState => ({ ...EMPTY_STATE, ...over });
const signedIn = { user: { id: 'u1', email: 'a@b.c' }, isAuthenticated: true, isAnonymous: false };
const user: WireMessage = { id: 'u1', role: 'user', content: 'hi', timestamp: '2026-09-20T10:00:00.000Z' };

describe('the guest gate (TS useAuthGate)', () => {
  it('lets a signed-out visitor chat as a guest when guests are allowed', () => {
    expect(gate(s())).toEqual({ gated: false, reason: null });
    expect(needsGuestStart(s())).toBe(true);
  });

  it('asks a signed-out visitor to sign in when guests are not allowed', () => {
    const state = s({ options: { ...EMPTY_STATE.options, allowAnonymous: false } });
    expect(gate(state)).toEqual({ gated: true, reason: null });
    expect(needsGuestStart(state)).toBe(false);
  });

  it('gives the reason: the engine refused a guest, or the allowance ran out', () => {
    expect(gate(s({ limits: { demoLimitReached: false, anonymousUnavailable: { type: 'captcha_required' } } })))
      .toEqual({ gated: true, reason: 'guest-limit' });
    const guest = { user: { id: 'anon', email: '' }, isAuthenticated: true, isAnonymous: true };
    expect(gate(s({ auth: guest, limits: { demoLimitReached: true, anonymousUnavailable: null } })))
      .toEqual({ gated: true, reason: 'demo-limit' });
  });

  it('never gates a signed-in account on anonymousUnavailable alone', () => {
    expect(gate(s({ auth: signedIn, limits: { demoLimitReached: false, anonymousUnavailable: { type: 'x' } } })).gated)
      .toBe(false);
  });

  it('names the allowance first when both limits are in: signing in is what lifts it', () => {
    expect(gate(s({ limits: { demoLimitReached: true, anonymousUnavailable: { type: 'captcha_required' } } })))
      .toEqual({ gated: true, reason: 'demo-limit' });
  });

  it('starts no guest once the engine has refused one', () => {
    // Without this, every send on a gated page would mint another guest against the
    // partner's daily allowance, and every one of them would be refused.
    expect(needsGuestStart(s({ limits: { demoLimitReached: false, anonymousUnavailable: { type: 'captcha_required' } } })))
      .toBe(false);
  });
});

describe('thread derivations', () => {
  it('is running while loading or while an approval waits', () => {
    expect(isRunning(s())).toBe(false);
    expect(isRunning(s({ isLoading: true }))).toBe(true);
    expect(isRunning(s({ pendingApproval: { toolCallId: 'c', toolName: 't', arguments: {}, description: '' } }))).toBe(true);
  });

  it('lends an errored turn an empty assistant message', () => {
    expect(threadMessages(s({ messages: [user], error: 'Boom' }))).toEqual([
      user,
      { id: 'u1:error', role: 'assistant', content: '', timestamp: user.timestamp },
    ]);
    const reply: WireMessage = { id: 'a1', role: 'assistant', content: 'x', timestamp: user.timestamp };
    const messages = [user, reply];
    expect(threadMessages(s({ messages, error: 'Boom' }))).toBe(messages);
    expect(lastUserId(s({ messages }))).toBe('u1');
  });

  it('revives the timestamp once per wire message', () => {
    const first = toDisplayMessage(user);
    expect(first.timestamp).toEqual(new Date('2026-09-20T10:00:00.000Z'));
    expect(toDisplayMessage(user)).toBe(first);
    expect(toDisplayMessage({ ...user, timestamp: 'nonsense' }).timestamp.getTime()).toBe(0);
  });

  it('fails an approval kind closed', () => {
    const base = { toolCallId: 'c', toolName: 't', arguments: {}, description: 'd' };
    expect(toPendingApproval(base)!.kind).toBe('client');
    expect(toPendingApproval({ ...base, kind: 'client' })!.kind).toBe('client');
    expect(toPendingApproval({ ...base, kind: 'engine' })!.kind).toBe('engine');
    expect(toPendingApproval({ ...base, kind: 'something-new' })!.kind).toBe('engine');
    expect(toPendingApproval(null)).toBeNull();
  });
});

describe('results and copy', () => {
  it('finds the newest result with an id', () => {
    const results = [
      { id: 'a', type: 'send', ok: false },
      { id: 'a', type: 'send', ok: true },
    ];
    expect(findResult(s({ results }), 'a')).toEqual({ id: 'a', type: 'send', ok: true });
    expect(findResult(s({ results }), null)).toBeNull();
  });

  it('shows known failures in plain words, and the message for anything else', () => {
    expect(failureText({ type: 'send', error: { type: 'busy', message: 'A run is already in progress' } }))
      .toBe('The assistant is still working on the last message.');
    expect(failureText({ type: 'login', error: { type: 'authentication_error', message: 'Invalid email or password' } }))
      .toBe('Invalid email or password');
    expect(failureText({ type: 'x' })).toBe('Something went wrong.');
  });

  it('an Undo whose name was taken again says so', () => {
    expect(failureText({ type: 'skillRestore', error: { type: 'skill_exists', message: 'a-b' } }))
      .toBe("A skill named a-b exists again, so Undo can't bring the deleted one back.");
  });

  it('a dictationStart busy is never the send-queue copy', () => {
    expect(failureText({ type: 'dictationStart', error: { type: 'busy', message: 'A dictation is already in progress.' } }))
      .toBe('A dictation is already in progress.');
  });

  it('names the two thread errors', () => {
    expect(failureText({ type: 'threadSwitch', error: { type: 'unknown_thread', message: 'No chat with id x.' } }))
      .toBe('That chat is gone. Open Chats to pick another.');
    expect(failureText({ type: 'threadNew', error: { type: 'thread_list_disabled', message: 'no' } }))
      .toBe('This app keeps no chat history.');
  });

  it('names the partner default when it is listed', () => {
    const models = { list: [{ id: 'm-fast', object: 'model', created: 1, owned_by: 't', name: 'Fast' }], selected: null };
    const config = { ...(EMPTY_STATE.config ?? {}), default_llm_model: 'm-fast' } as PageState['config'];
    expect(defaultModelLabel(s({ models, config }))).toBe('Default — Fast');
    expect(defaultModelLabel(s({ models }))).toBe('Default');
  });

  it('only sends back edited arguments that are a JSON object', () => {
    expect(parseArguments('{"name": "CT"}')).toEqual({ name: 'CT' });
    expect(parseArguments('[1]')).toBeNull();
    expect(parseArguments('null')).toBeNull();
    expect(parseArguments('{')).toBeNull();
  });
});

describe('the AI model picker (Settings.tsx)', () => {
  const listed = { list: [{ id: 'm-fast', object: 'model', created: 1, owned_by: 't', name: 'Fast' }], selected: null };

  it('shows the partner default with no pick and an empty list', () => {
    expect(modelPick(s())).toEqual({ value: '', orphan: null, loaded: false });
  });

  it('a listed pick: no orphan, loaded true', () => {
    expect(modelPick(s({ models: { ...listed, selected: 'm-fast' } }))).toEqual({
      value: 'm-fast',
      orphan: null,
      loaded: true,
    });
  });

  it('a refused pick does not stick: value comes from models.selected, never a stale preference', () => {
    // app_state.py's select_model/_forget_pick reverts preferences.model to the previous
    // pick on a refusal, but even a mismatched preference must never override what the
    // server actually applied.
    expect(modelPick(s({ models: { ...listed, selected: 'm-fast' }, preferences: { model: 'm-deep' } })).value).toBe(
      'm-fast',
    );
  });

  it('an empty list never reads a stored pick as "no longer offered" — before the list loads, or once list_models fails for good', () => {
    expect(modelPick(s({ preferences: { model: 'm-deep' } }))).toEqual({ value: 'm-deep', orphan: null, loaded: false });
    expect(modelPick(s({ models: { list: [], selected: 'm-deep' } }))).toEqual({
      value: 'm-deep',
      orphan: null,
      loaded: false,
    });
  });

  it('a loaded list without the stored pick shows it as orphaned', () => {
    expect(modelPick(s({ models: listed, preferences: { model: 'm-deep' } }))).toEqual({
      value: 'm-deep',
      orphan: 'm-deep',
      loaded: true,
    });
  });
});

describe('upload refusals', () => {
  it('asks for the file again when the account changed during the upload', () => {
    expect(uploadFailureText('account_changed', 'The account changed during the upload. Upload the file again.'))
      .toBe('The account changed during the upload. Attach the file again.');
    expect(uploadFailureText('too_large', '')).toBe('That file is too large to attach.');
    expect(uploadFailureText('sign_in_required', 'Sign in to continue.')).toBe('Sign in to continue.');
  });

  // uploadFile (transport.ts) reports a timed-out or dropped connection the same way
  // postCommand does: `{ type: 'network_error', message: String(err) }`. A raw
  // `AbortError: signal is aborted without reason` is not something to show a person, so
  // this owns its own copy rather than falling back to the raw JS error text.
  it('never shows the raw error for a network failure or an upload that timed out', () => {
    expect(uploadFailureText('network_error', 'AbortError: signal is aborted without reason'))
      .toBe('Talk2View could not be reached. Check your connection and try again.');
  });
});

// One definition, used by Composer.tsx AND Account.tsx, so a
// second independently-typed copy can never drift from what "the identity changed" means.
describe('authIdentityKey', () => {
  const ana = { user: { id: 'u-ana', email: 'ana@example.com' }, isAuthenticated: true, isAnonymous: false };
  const bob = { user: { id: 'u-bob', email: 'bob@example.com' }, isAuthenticated: true, isAnonymous: false };
  const guest = { user: { id: 'anon-1', email: '' }, isAuthenticated: true, isAnonymous: true };
  const signedOut = { user: null, isAuthenticated: false, isAnonymous: false };

  it('differs for every distinct identity: two real accounts, a guest, and signed out', () => {
    const keys = [ana, bob, guest, signedOut].map(authIdentityKey);
    expect(new Set(keys).size).toBe(4);
  });

  it('is the same value for the same identity, even a fresh object', () => {
    expect(authIdentityKey(ana)).toBe(authIdentityKey({ ...ana, user: { ...ana.user } }));
  });
});

describe('“Always allow”', () => {
  const tool = (over: Partial<ToolPresentation> = {}): ToolPresentation => ({
    activity: 'Loading sample data',
    destructive: false,
    warning: null,
    ...over,
  });

  it('is offered for a client tool the host has not marked destructive', () => {
    expect(mayAlwaysAllow('client', tool())).toBe(true);
    expect(mayAlwaysAllow(undefined, tool())).toBe(true);
    expect(mayAlwaysAllow('client', undefined)).toBe(true); // no entry: not destructive
  });

  it('is never offered for a destructive tool', () => {
    expect(mayAlwaysAllow('client', tool({ destructive: true }))).toBe(false);
    expect(mayAlwaysAllow(undefined, tool({ destructive: true }))).toBe(false);
  });

  it('is never offered for an engine approval, whatever the tool', () => {
    expect(mayAlwaysAllow('engine', tool())).toBe(false);
    expect(mayAlwaysAllow('something-new', tool())).toBe(false); // an unknown kind fails closed
    expect(mayAlwaysAllow('', tool())).toBe(false); // and so does an empty one: only "client" is a client
  });
});

describe('canShowArguments (the approval card fails closed on undisplayable arguments)', () => {
  it('shows a plain object, including an empty one and none at all', () => {
    expect(canShowArguments({ name: 'MRHead' })).toBe(true);
    expect(canShowArguments({})).toBe(true); // a genuinely parameter-less call
    expect(canShowArguments(undefined)).toBe(true); // no `arguments` field at all
  });

  it('refuses a string (arguments "rm -rf /" must not read as "{}")', () => {
    expect(canShowArguments('rm -rf /')).toBe(false);
  });

  it('refuses an array, a number, a boolean and null', () => {
    expect(canShowArguments(['name', 'MRHead'])).toBe(false);
    expect(canShowArguments(42)).toBe(false);
    expect(canShowArguments(true)).toBe(false);
    expect(canShowArguments(null)).toBe(false);
  });

  it('refuses an object JSON.stringify cannot serialise', () => {
    const circular: Record<string, unknown> = { name: 'MRHead' };
    circular.self = circular;
    expect(canShowArguments(circular)).toBe(false);
  });
});

describe('deepEqual (so an escaped-but-unedited draft reads as unchanged)', () => {
  it('is true for identical primitives and for two equal plain objects', () => {
    expect(deepEqual('MRHead', 'MRHead')).toBe(true);
    expect(deepEqual(42, 42)).toBe(true);
    expect(deepEqual(null, null)).toBe(true);
    expect(deepEqual({ name: 'MRHead' }, { name: 'MRHead' })).toBe(true);
  });

  it('is true regardless of key order', () => {
    expect(deepEqual({ a: 1, b: 2 }, { b: 2, a: 1 })).toBe(true);
  });

  it('is true for nested objects and arrays', () => {
    expect(deepEqual({ a: [1, { b: 'x' }] }, { a: [1, { b: 'x' }] })).toBe(true);
  });

  it('is false for a changed value, a missing key, an extra key, or a different type', () => {
    expect(deepEqual({ name: 'MRHead' }, { name: 'CTChest' })).toBe(false);
    expect(deepEqual({ name: 'MRHead' }, {})).toBe(false);
    expect(deepEqual({ name: 'MRHead' }, { name: 'MRHead', extra: 1 })).toBe(false);
    expect(deepEqual({ name: 'MRHead' }, 'MRHead')).toBe(false);
    expect(deepEqual([1, 2], [1, 2, 3])).toBe(false);
    expect(deepEqual([1, 2], { 0: 1, 1: 2 })).toBe(false);
  });

  it('treats a raw character and its \\uXXXX-escaped, then re-parsed, form as equal', () => {
    // The exact case this exists for: the approval card's editor is seeded with an
    // ESCAPED form of the original arguments. Both, once JSON.parse'd, must compare
    // equal, or an untouched draft would read as "edited".
    const raw = { path: 'a' + String.fromCharCode(0x202e) + 'b' };
    const escaped = JSON.parse('{"path": "a\\u202Eb"}') as Record<string, unknown>;
    expect(deepEqual(raw, escaped)).toBe(true);
  });
});

describe('the skills section (Settings)', () => {
  const skill = { name: 'scan-report', description: 'd', enabled: true, origin: 'code' as const, label: 'App' };
  const catalogue = [{ name: 'team-brief', description: 'd', source: 'partner' }];

  it('says "off" only once the listing has answered AND the session refused both routes', () => {
    expect(skillsSection({ local: [skill], platform: null, available: false }, true)).toEqual({
      off: true,
      awaitingRegister: false,
    });
  });

  it('is not off before this visit\'s listing has answered: nothing is known yet', () => {
    expect(skillsSection({ local: [skill], platform: null, available: false }, false)).toEqual({
      off: false,
      awaitingRegister: false, // no promise either, until the listing proves skills are on
    });
  });

  it('makes no promise when the listing never answered with a catalogue', () => {
    // Not yet answered, or answered with a failure: `listed` is false, whatever `platform` holds.
    expect(skillsSection({ local: [skill], platform: catalogue, available: false }, false).awaitingRegister).toBe(false);
    expect(skillsSection({ local: [skill], platform: null, available: false }, false).awaitingRegister).toBe(false);
  });

  it('lists the local skills after a refused sign-out register, with the quiet line', () => {
    // The SDK's first register fired before sign-in and got a 401: `available` is still
    // false, but the listing answered with a catalogue, so skills are plainly on.
    expect(skillsSection({ local: [skill], platform: catalogue, available: false }, true)).toEqual({
      off: false,
      awaitingRegister: true,
    });
    // An engine with no catalogue at all answers the listing with an empty one.
    expect(skillsSection({ local: [skill], platform: [], available: false }, true).off).toBe(false);
  });

  it('drops the quiet line once a register has succeeded, or when nothing is enabled', () => {
    expect(skillsSection({ local: [skill], platform: catalogue, available: true }, true).awaitingRegister).toBe(false);
    expect(skillsSection({ local: [{ ...skill, enabled: false }], platform: null, available: false }, false).awaitingRegister).toBe(false);
    expect(skillsSection({ local: [], platform: catalogue, available: false }, true).awaitingRegister).toBe(false);
  });
});

describe('hasUnsafeInteger (the approval card)', () => {
  it('finds an integer past Number.MAX_SAFE_INTEGER anywhere in the arguments', () => {
    expect(hasUnsafeInteger({ id: 12345678901234567891 })).toBe(true);
    expect(hasUnsafeInteger({ a: [{ b: -(2 ** 60) }] })).toBe(true);
  });

  it('passes ordinary values, safe integers and fractions', () => {
    expect(hasUnsafeInteger({ n: Number.MAX_SAFE_INTEGER, f: 1.5, s: '12345678901234567891', x: null })).toBe(false);
    expect(hasUnsafeInteger(undefined)).toBe(false);
  });

  it('walks a deep value without recursion', () => {
    let deep: unknown = 2 ** 60;
    for (let i = 0; i < 20000; i++) deep = { nested: deep };
    expect(hasUnsafeInteger(deep)).toBe(true);
  });
});

describe('chipBlocksSend (the composer)', () => {
  it('holds Send back for an upload still running, or one refused', () => {
    expect(chipBlocksSend('running')).toBe(true);
    expect(chipBlocksSend('incomplete')).toBe(true);
  });

  it('lets Send through for a chip ready to go, or one assistant-ui put back after a failed send', () => {
    expect(chipBlocksSend('requires-action')).toBe(false);
    expect(chipBlocksSend('complete')).toBe(false);
  });
});

function withProviders(over: Partial<{ oauth: boolean; offered: string[]; feature: boolean }>): PageState {
  const { oauth = true, offered = ['apple', 'google'], feature = true } = over;
  return {
    ...EMPTY_STATE,
    capabilities: { ...EMPTY_STATE.capabilities, oauth, oauthProviders: offered },
    options: { ...EMPTY_STATE.options, features: { ...EMPTY_STATE.options.features, providerSignIn: feature } },
  };
}

describe('providerButtons', () => {
  it('lists Apple before Google whatever order the server sends', () => {
    expect(providerButtons(withProviders({ offered: ['google', 'apple'] }))).toEqual(['apple', 'google']);
  });
  it('shows nothing when the engine offers nothing, the integrator turned them off, or a provider is unknown', () => {
    expect(providerButtons(withProviders({ oauth: false }))).toEqual([]);
    expect(providerButtons(withProviders({ feature: false }))).toEqual([]);
    expect(providerButtons(withProviders({ offered: ['github'] }))).toEqual([]);
  });
  it('fails closed on a server that predates the keys', () => {
    const old = withProviders({});
    delete (old.options.features as Partial<typeof old.options.features>).providerSignIn;
    delete (old.capabilities as Partial<typeof old.capabilities>).oauthProviders;
    expect(providerButtons(old)).toEqual([]);
  });
});

describe('threads', () => {
  const day = 86_400_000;
  const now = new Date(2026, 8, 24, 15, 0, 0).getTime(); // local 15:00
  const startOfToday = new Date(2026, 8, 24).getTime();
  const items = [
    { id: 'a', title: 'Liver volume', updatedAt: startOfToday + 3_600_000, messageCount: 4 },
    { id: 'b', title: null, updatedAt: startOfToday - 1000, messageCount: 2 },
    { id: 'c', title: 'Old', updatedAt: startOfToday - 2 * day, messageCount: 1 },
    { id: 'd', title: null, updatedAt: now, messageCount: 0 },
  ];

  it('threadTitle is the current item title, else "New chat"', () => {
    expect(threadTitle(s({ threads: { currentId: 'a', items } }))).toBe('Liver volume');
    expect(threadTitle(s({ threads: { currentId: 'b', items } }))).toBe(UNTITLED_THREAD);
    expect(threadTitle(s({ threads: { currentId: null, items: [] } }))).toBe(UNTITLED_THREAD);
  });

  it('listedThreads leaves out empty conversations and keeps the order', () => {
    expect(listedThreads(s({ threads: { currentId: 'd', items } })).map((i) => i.id)).toEqual(['a', 'b', 'c']);
  });

  it('threadGroups buckets by day, filters by title, newest first', () => {
    const listed = items.filter((i) => i.messageCount > 0);
    expect(threadGroups(listed, now, '').map((g) => [g.label, g.items.map((i) => i.id)])).toEqual([
      ['Today', ['a']], ['Yesterday', ['b']], ['Earlier', ['c']],
    ]);
    expect(threadGroups(listed, now, 'LIVER').map((g) => g.items.map((i) => i.id))).toEqual([['a']]);
    expect(threadGroups(listed, now, 'new chat').map((g) => g.items.map((i) => i.id))).toEqual([['b']]);
    expect(threadGroups(listed, now, 'zzz')).toEqual([]);
  });
});

describe('whenLabel', () => {
  const now = new Date(2026, 8, 27, 14, 30).getTime();
  it('same day: the time', () => expect(whenLabel(new Date(2026, 8, 27, 9, 5).getTime(), now)).toMatch(/9:05/));
  it('this week: the weekday', () => expect(whenLabel(new Date(2026, 8, 25).getTime(), now)).toBe(new Date(2026, 8, 25).toLocaleDateString([], { weekday: 'short' })));
  it('older: day and month', () => expect(whenLabel(new Date(2026, 7, 3).getTime(), now)).toBe(new Date(2026, 7, 3).toLocaleDateString([], { day: 'numeric', month: 'short' })));
});

describe('initials', () => {
  it('two parts', () => expect(initials('ana.huynh@example.com')).toBe('AH'));
  it('one part', () => expect(initials('ana@example.com')).toBe('A'));
  it('empty', () => expect(initials('')).toBe(''));
});

describe('voice settings', () => {
  const scribe = { id: 'scribe_v2', name: 'Scribe v2', supported_languages: ['vi', 'en'], description: null };
  const whisper = { id: 'faster-whisper-base', name: null, supported_languages: null, description: null };

  it('languagesFor offers the model languages A–Z by name, else the eight common ones', () => {
    expect(languagesFor(scribe)).toEqual([['en', 'English'], ['vi', 'Vietnamese']]);
    expect(languagesFor(whisper).map(([code]) => code)).toEqual(['zh', 'en', 'fr', 'de', 'ja', 'ko', 'es', 'vi']);
    expect(languagesFor(undefined).length).toBe(8);
  });

  it('activeVoiceModel is the preference, else the partner default, else faster-whisper-base', () => {
    const list = [scribe, whisper];
    expect(activeVoiceModel(s({ audioModels: { list, state: 'ready' }, preferences: { sttModel: 'scribe_v2' } }))?.id).toBe('scribe_v2');
    expect(activeVoiceModel(s({ audioModels: { list, state: 'ready' }, config: { ...EMPTY_CONFIG, default_stt_model: 'scribe_v2' } }))?.id).toBe('scribe_v2');
    expect(activeVoiceModel(s({ audioModels: { list, state: 'ready' } }))?.id).toBe('faster-whisper-base');
    expect(activeVoiceModel(s({ audioModels: { list: [], state: 'ready' } }))).toBeUndefined();
  });

  it('dictationFailureText', () => {
    expect(dictationFailureText('no_microphone', '')).toBe('No microphone was found.');
    expect(dictationFailureText('model_not_allowed', 'Model x is not enabled for this partner')).toBe('Model x is not enabled for this partner');
    expect(dictationFailureText('weird', '')).toBe('The recording could not be transcribed.');
  });
});
