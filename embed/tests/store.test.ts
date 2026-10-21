import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NOTICE_ACTION_MS, PAINT_WAIT_MS, PageStore, resultOf } from '../src/store';
import { EMPTY_STATE, type PageState } from '../src/wire';
import { FakeServer, json, settle, sse } from './fake-server';

function state(over: Partial<PageState> = {}): PageState {
  return { ...EMPTY_STATE, ...over };
}

const guest = { user: { id: 'anon-1', email: '' }, isAuthenticated: true, isAnonymous: true };
const ana = { user: { id: 'u-ana', email: 'ana@example.com' }, isAuthenticated: true, isAnonymous: false };
/** One `set` op as an SSE frame. */
const set = (seq: number, base: number, path: Array<string | number>, value: unknown) =>
  sse('ops', { seq, base, ops: [{ type: 'set', path, value }] });
/** Longer than the store's WATCH_MS, for the latch and the command watcher. */
const LATER = 61000;

/** The store reads `sessionStorage` when it is built, and Node has none. */
function fakeSessionStorage(initial: Record<string, string> = {}): Map<string, string> {
  const held = new Map(Object.entries(initial));
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => held.get(key) ?? null,
      setItem: (key: string, value: string) => void held.set(key, value),
      removeItem: (key: string) => void held.delete(key),
    },
  });
  return held;
}

describe('PageStore', () => {
  let server: FakeServer;
  let flushes: Array<() => void>;
  let store: PageStore;
  let ids: number;
  let clock: number;

  function makeStore(): PageStore {
    return new PageStore('tok', {
      env: server.env(),
      schedule: (flush) => flushes.push(flush),
      newId: () => 'c-' + String(++ids),
      now: () => clock,
    });
  }

  beforeEach(() => {
    server = new FakeServer();
    flushes = [];
    ids = 0;
    clock = 0;
    store = makeStore();
  });

  afterEach(() => {
    Reflect.deleteProperty(globalThis, 'sessionStorage');
  });

  /** Deliver events on the open stream, then publish. */
  async function deliver(...events: string[]): Promise<void> {
    server.streams[server.streams.length - 1]!.push(events.join(''));
    await settle();
    flushes.splice(0).forEach((flush) => flush());
  }

  it('publishes at most once per scheduled frame', async () => {
    store.start();
    await settle();
    let heard = 0;
    store.subscribe(() => heard++);
    server.streams[0]!.push(sse('snapshot', { seq: 1, state: state() }));
    server.streams[0]!.push(sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['isLoading'], value: true }] }));
    await settle();
    expect(store.get().seq).toBeNull(); // nothing published yet
    expect(flushes).toHaveLength(1);
    flushes.splice(0).forEach((flush) => flush());
    expect(heard).toBe(1);
    expect(store.get().seq).toBe(2);
    expect(store.get().state.isLoading).toBe(true);
  });

  it('fills a key the snapshot lacks from the empty state', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 3, state: { messages: [], isLoading: false } }));
    expect(store.get().state.limits).toEqual(EMPTY_STATE.limits);
  });

  it('answers a gap and a broken op without touching the state', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 3, state: state() }));
    const before = store.get().state;
    await deliver(sse('ops', { seq: 6, base: 5, ops: [] }));
    expect(store.get().stats.gaps).toBe(1);
    await settle();
    await deliver(sse('snapshot', { seq: 6, state: before }));
    await deliver(sse('ops', { seq: 7, base: 6, ops: [{ type: 'append-text', path: ['nope'], value: 'x' }] }));
    expect(store.get().stats.resyncs).toBe(1);
    expect(store.get().seq).toBe(6);
    // The resync comes so soon after the gap's that the transport floors it: wait for it.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(server.eventCalls().at(-1)!.headers['Last-Event-ID']).toBeUndefined();
  });

  it('starts a guest before the first send, and posts the send only once the start is queued', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    let queued!: (res: Response) => void;
    server.commandAnswers.push(new Promise<Response>((resolve) => (queued = resolve)));
    store.send('hello');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous']);
    queued(json(202, { ok: true, id: 'c-1' }));
    await settle();
    expect(server.commands()).toEqual([
      { id: 'c-1', type: 'startAnonymous' },
      { id: 'c-2', type: 'send', text: 'hello', attachmentIds: [], timing: true },
    ]);
  });

  it('starts ONE guest however fast the second send comes', async () => {
    // The welcome screen stays up until the first message lands, so a second click on a
    // suggestion arrives while the first start is still in flight. A second
    // `startAnonymous` would mint a second guest and abandon the first one's work.
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.send('Load MRHead');
    store.send('Load MRHead');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'send']);
  });

  describe('beforeUpload: a signed-out upload starts its guest first', () => {
    it('starts the guest, and resolves true once the auth slice shows it', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: state() }));
      let answer: boolean | null = null;
      void store.beforeUpload().then((ok) => (answer = ok));
      await settle();
      expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous']);
      expect(answer).toBeNull(); // not before the guest is on show
      await deliver(set(2, 1, ['auth'], { ...EMPTY_STATE.auth, ...guest }));
      await settle();
      expect(answer).toBe(true);
    });

    it('shares the guest a send is already starting: never a second one', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: state() }));
      store.send('hello');
      void store.beforeUpload();
      await settle();
      expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send']);
    });

    it('resolves false when the start is refused', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: state() }));
      server.commandAnswers.push(json(503, { ok: false, error: { type: 'busy', message: 'no' } }));
      await expect(store.beforeUpload()).resolves.toBe(false);
    });

    it('resolves true at once, with no command, when someone is signed in', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: state({ auth: { ...EMPTY_STATE.auth, ...ana } }) }));
      await expect(store.beforeUpload()).resolves.toBe(true);
      expect(server.commands()).toEqual([]);
    });
  });

  it('starts a guest again once the first start has landed', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.send('one');
    await settle();
    // The start was refused: the page is signed out again, so the next send may try once more.
    await deliver(
      sse('ops', {
        seq: 2,
        base: 1,
        ops: [
          {
            type: 'set',
            path: ['results'],
            value: [{ id: 'c-1', type: 'startAnonymous', ok: false, error: { type: 'rate_limited', message: 'Too many guests.' } }],
          },
        ],
      }),
    );
    store.send('two');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'startAnonymous', 'send']);
  });

  it('starts another guest once the first guest has signed out', async () => {
    // The only sign that the start landed is `auth`: the results of the identity that went
    // away were emptied with it.
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.send('one');
    await settle();
    await deliver(set(2, 1, ['auth'], guest));
    await deliver(sse('ops', { seq: 3, base: 2, ops: [
      { type: 'set', path: ['auth'], value: EMPTY_STATE.auth },
      { type: 'set', path: ['results'], value: [] },
    ] }));
    store.send('two');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'startAnonymous', 'send']);
  });

  it('starts another guest once the engine has refused one', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.send('one');
    await settle();
    await deliver(set(2, 1, ['limits', 'anonymousUnavailable'], { type: 'captcha_required' }));
    await deliver(set(3, 2, ['limits', 'anonymousUnavailable'], null)); // guests are allowed again
    store.send('two');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'startAnonymous', 'send']);
  });

  it('starts another guest once the first start has been refused outright', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(400, { ok: false, error: { type: 'bad_command', message: 'no' } }));
    store.send('one');
    await settle();
    store.send('two');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'startAnonymous', 'send']);
  });

  it('lets go of the latch after a minute, when the start is never answered', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.send('one'); // queued, but no result ever arrives
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send']);
    clock = LATER;
    store.send('two');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['startAnonymous', 'send', 'startAnonymous', 'send']);
  });

  it('starts no guest before the first snapshot', async () => {
    // The empty state allows guests, and `startAnonymous` has no signed-in guard: a send in
    // the moment before the first snapshot would replace a real account with a guest.
    store.start();
    await settle();
    store.send('hello');
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['send']);
  });

  it('sends straight away when guests are not allowed, or someone is signed in', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ options: { ...EMPTY_STATE.options, allowAnonymous: false } }) }));
    store.send('one', ['att-1']);
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['send']);
    expect(server.commands()[0]!.attachmentIds).toEqual(['att-1']);
  });

  it('warms each identity once, on its first keystroke, and never while signed out', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.typed();
    await deliver(set(2, 1, ['auth'], ana));
    store.typed();
    store.typed();
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['warmUp']);
  });

  it('re-sends the stopped turn once when a real sign-in lifts a guest limit', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ auth: guest, limits: { demoLimitReached: true, anonymousUnavailable: null } }) }));
    await deliver(set(2, 1, ['auth'], ana));
    await settle();
    expect(server.commands()).toEqual([]); // the limit is not lifted yet
    await deliver(set(3, 2, ['limits', 'demoLimitReached'], false));
    await deliver(set(4, 3, ['isLoading'], false));
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['reload']);
  });

  it('re-sends the stopped turn once when a provider sign-in lifts a guest limit', async () => {
    store.start();
    await settle();
    const waiting = { ...guest, oauthPending: { provider: 'apple' }, oauthError: null };
    await deliver(sse('snapshot', { seq: 1, state: state({ auth: waiting, limits: { demoLimitReached: true, anonymousUnavailable: null } }) }));
    await deliver(set(2, 1, ['auth'], { ...ana, oauthPending: { provider: 'apple' }, oauthError: null }));
    await deliver(set(3, 2, ['limits', 'demoLimitReached'], false));
    await deliver(set(4, 3, ['auth', 'oauthPending'], null));
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['reload']);
  });

  it('never re-sends a turn the end-user already has, when a guest signs out', async () => {
    // Guest 1 hits the limit and signs out; guest 2 chats normally; then a real sign-in.
    // The server lifts both limits whenever another account comes on show, not only for a
    // real sign-in, so a `sawLimit` that outlives the sign-out would send `reload` here —
    // and `retry()` deletes the answer guest 2 already has and runs that turn again.
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ auth: guest, limits: { demoLimitReached: true, anonymousUnavailable: null } }) }));
    await deliver(set(2, 1, ['auth'], EMPTY_STATE.auth)); // guest 1 signs out
    await deliver(set(3, 2, ['limits', 'demoLimitReached'], false)); // the server lifts the limits
    await deliver(set(4, 3, ['auth'], { ...guest, user: { id: 'anon-2', email: '' } })); // guest 2
    await deliver(set(5, 4, ['auth'], ana)); // and now a real account
    await settle();
    expect(server.commands()).toEqual([]);
  });

  it('never re-sends for a guest: only a real account lifts a guest limit', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ limits: { demoLimitReached: true, anonymousUnavailable: null } }) }));
    await deliver(set(2, 1, ['auth'], guest));
    await deliver(set(3, 2, ['limits', 'demoLimitReached'], false));
    await settle();
    expect(server.commands()).toEqual([]);
  });

  it('remembers a guest the engine refused, not only an allowance that ran out', async () => {
    store.start();
    await settle();
    await deliver(
      sse('snapshot', {
        seq: 1,
        state: state({ limits: { demoLimitReached: false, anonymousUnavailable: { type: 'captcha_required' } } }),
      }),
    );
    await deliver(set(2, 1, ['auth'], ana));
    await deliver(set(3, 2, ['limits', 'anonymousUnavailable'], null));
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['reload']);
  });

  it('re-sends the stopped turn after a page reload, and only once', async () => {
    // The limit was seen before the reload, so this page never sees one of its own.
    const held = fakeSessionStorage({ t2v_saw_limit: '1' });
    store = makeStore();
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ auth: ana }) }));
    await deliver(set(2, 1, ['isLoading'], true));
    await settle();
    expect(server.commands().map((c) => c.type)).toEqual(['reload']);
    expect(held.has('t2v_saw_limit')).toBe(false);
  });

  it('turns a failed result into the notice, but not for commands a view reports', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.command({ type: 'selectModel', modelId: 'm-x' }); // c-1
    store.command({ type: 'login', email: 'a@b.c', password: 'pw' }); // c-2
    const results = [
      { id: 'c-2', type: 'login', ok: false, error: { type: 'authentication_error', message: 'Wrong password' } },
      { id: 'c-1', type: 'selectModel', ok: false, error: { type: 'unknown_model', message: "Model 'm-x' is not available." } },
    ];
    await deliver(sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['results'], value: results }] }));
    expect(store.get().notice).toBe("Model 'm-x' is not available.");
    store.dismissNotice();
    flushes.splice(0).forEach((flush) => flush());
    expect(store.get().notice).toBeNull();
  });

  it('a notice can carry one action (Undo) that lasts NOTICE_ACTION_MS, then the notice goes', async () => {
    vi.useFakeTimers();
    try {
      const onClick = vi.fn();
      store.showNotice('Deleted a-b.', { label: 'Undo', onClick });
      flushes.splice(0).forEach((flush) => flush());
      expect(store.get().notice).toBe('Deleted a-b.');
      expect(store.get().noticeAction?.label).toBe('Undo');
      store.get().noticeAction!.onClick();
      expect(onClick).toHaveBeenCalledTimes(1);
      vi.advanceTimersByTime(NOTICE_ACTION_MS - 1);
      flushes.splice(0).forEach((flush) => flush());
      expect(store.get().notice).toBe('Deleted a-b.');
      vi.advanceTimersByTime(1);
      flushes.splice(0).forEach((flush) => flush());
      expect(store.get().notice).toBeNull();
      expect(store.get().noticeAction).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a later notice replaces the action, and its expiry never clears that later notice', async () => {
    vi.useFakeTimers();
    try {
      store.showNotice('Deleted a-b.', { label: 'Undo', onClick: () => undefined });
      store.showNotice('Something else.');
      flushes.splice(0).forEach((flush) => flush());
      expect(store.get().noticeAction).toBeNull();
      vi.advanceTimersByTime(NOTICE_ACTION_MS);
      flushes.splice(0).forEach((flush) => flush());
      expect(store.get().notice).toBe('Something else.');
    } finally {
      vi.useRealTimers();
    }
  });

  it('a failed result replaces an Undo notice without keeping its action', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.showNotice('Deleted a-b.', { label: 'Undo', onClick: () => undefined });
    store.command({ type: 'selectModel', modelId: 'm-x' }); // c-1
    const results = [{ id: 'c-1', type: 'selectModel', ok: false, error: { type: 'unknown_model', message: 'Nope.' } }];
    await deliver(sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['results'], value: results }] }));
    expect(store.get().notice).toBe('Nope.');
    expect(store.get().noticeAction).toBeNull();
  });

  it('a stale threadSwitch (unknown_thread) refreshes quietly; any other thread failure still shows a notice', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.command({ type: 'threadSwitch', threadId: 'gone' }); // c-1: a row the list still
    // showed for a moment after the server had already dropped it (module doc: "the
    // threads slice ... can arrive after other slices") — not a real failure.
    store.command({ type: 'threadRename', threadId: 't-1', title: 'x' }); // c-2: a genuine
    // failure on a different thread command, to prove this is not a blanket quiet.
    const results = [
      { id: 'c-1', type: 'threadSwitch', ok: false, error: { type: 'unknown_thread', message: 'No chat with id gone.' } },
      { id: 'c-2', type: 'threadRename', ok: false, error: { type: 'unknown_thread', message: 'No chat with id t-1.' } },
    ];
    await deliver(sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['results'], value: [results[0]] }] }));
    expect(store.get().notice).toBeNull();
    await deliver(sse('ops', { seq: 3, base: 2, ops: [{ type: 'set', path: ['results'], value: results }] }));
    expect(store.get().notice).toBe('That chat is gone. Open Chats to pick another.');
  });

  it('clears the notice when the identity changes, but not when signed out becomes a guest', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state({ auth: { ...EMPTY_STATE.auth, ...ana } }) }));
    store.showNotice('An attachment was lost. Attach the file again.');
    flushes.splice(0).forEach((flush) => flush());
    await deliver(set(2, 1, ['isLoading'], true)); // any other change keeps it
    expect(store.get().notice).toBe('An attachment was lost. Attach the file again.');
    await deliver(set(3, 2, ['auth'], { ...EMPTY_STATE.auth })); // ana signs out
    expect(store.get().notice).toBeNull();
    // Signed out to guest is the same visitor carrying on, and the guest may have been
    // started by the upload of a good file picked with the one this notice refused.
    store.showNotice('Attach a PNG, JPEG, WebP, GIF or PDF file.');
    flushes.splice(0).forEach((flush) => flush());
    await deliver(set(4, 3, ['auth'], { ...EMPTY_STATE.auth, ...guest })); // a guest starts
    expect(store.get().notice).toBe('Attach a PNG, JPEG, WebP, GIF or PDF file.');
    await deliver(set(5, 4, ['auth'], { ...EMPTY_STATE.auth, ...ana })); // the guest signs in as ana
    expect(store.get().notice).toBeNull();
  });

  it('forgets a command whose result never comes, and says nothing when it turns up late', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    store.command({ type: 'selectModel', modelId: 'm-x' }); // c-1
    clock = LATER;
    await deliver(set(2, 1, ['isLoading'], true)); // nothing for c-1: the store forgets it
    await deliver(sse('ops', {
      seq: 3,
      base: 2,
      ops: [{ type: 'set', path: ['results'], value: [{ id: 'c-1', type: 'selectModel', ok: false, error: { type: 'x', message: 'Too late.' } }] }],
    }));
    expect(store.get().notice).toBeNull();
  });

  it('records a refusal the server never queued as that command’s outcome', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(400, { ok: false, error: { type: 'bad_command', message: 'Not a command.' } }));
    const id = store.command({ type: 'reload' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    expect(resultOf(store.get(), id)).toEqual({
      id,
      type: 'reload',
      ok: false,
      error: { type: 'bad_command', message: 'Not a command.' },
    });
    expect(store.get().notice).toBe('Not a command.');
  });

  it('says nothing about a refused sign-in: the sign-in form reports it itself', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(400, { ok: false, error: { type: 'bad_command', message: 'Not a command.' } }));
    const id = store.command({ type: 'login', email: 'a@b.c', password: 'pw' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    expect(resultOf(store.get(), id)).toMatchObject({ ok: false });
    expect(store.get().notice).toBeNull();
  });

  it('says nothing about a refused provider sign-in, Cancel or Dismiss: the Account view reports them itself', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    // An HTTP refusal of each (the server never queued it)...
    for (const command of [
      { type: 'oauthStart', provider: 'apple' },
      { type: 'oauthCancel' },
      { type: 'oauthDismissError' },
    ] as const) {
      server.commandAnswers.push(json(400, { ok: false, error: { type: 'bad_command', message: 'Not a command.' } }));
      const id = store.command(command);
      await settle();
      flushes.splice(0).forEach((flush) => flush());
      expect(resultOf(store.get(), id)).toMatchObject({ ok: false });
    }
    expect(store.get().notice).toBeNull();
    // ...and a failed result of each.
    const start = store.command({ type: 'oauthStart', provider: 'google' });
    const cancel = store.command({ type: 'oauthCancel' });
    const dismiss = store.command({ type: 'oauthDismissError' });
    const results = [
      { id: start, type: 'oauthStart', ok: false, error: { type: 'busy', message: 'A sign-in is already waiting in your browser.' } },
      { id: cancel, type: 'oauthCancel', ok: false, error: { type: 'internal_error', message: 'Something went wrong.' } },
      { id: dismiss, type: 'oauthDismissError', ok: false, error: { type: 'internal_error', message: 'Something went wrong.' } },
    ];
    await deliver(sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['results'], value: results }] }));
    expect(store.get().notice).toBeNull();
  });

  it('is over when a command answers stopped', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(503, { ok: false, error: { type: 'stopped', message: 'This chat has stopped.' } }));
    store.command({ type: 'reload' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    expect(store.get().conn).toMatchObject({ phase: 'over', reason: 'stopped' });
  });

  it('is over when a command answers 401', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(401, { ok: false, error: { type: 'unauthorized', message: 'no' } }));
    store.command({ type: 'reload' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    expect(store.get().conn).toMatchObject({ phase: 'over', reason: 'unauthorized' });
  });

  it('posts nothing once the chat is over, and answers for the server', async () => {
    store.start();
    await settle();
    await deliver(sse('snapshot', { seq: 1, state: state() }));
    server.commandAnswers.push(json(503, { ok: false, error: { type: 'stopped', message: 'This chat has stopped.' } }));
    store.command({ type: 'reload' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    const id = store.command({ type: 'reload' });
    await settle();
    flushes.splice(0).forEach((flush) => flush());
    expect(server.commands()).toHaveLength(1);
    expect(resultOf(store.get(), id)).toMatchObject({ id, ok: false, error: { type: 'stopped' } });
  });

  // A 401/403/`stopped` refusal of `/upload` did not end the page,
  // unlike the identical refusal of a command (`command()` above) — the notice showed
  // "Missing or wrong token." and the chat carried on regardless.
  describe('upload', () => {
    const file = () => new File(['x'], 'a.png', { type: 'image/png' });

    it('ends the page on a 401 from /upload, the same as a command', async () => {
      server.uploadAnswer = () => json(401, { ok: false, error: { type: 'unauthorized', message: 'Missing or wrong token.' } });
      const result = await store.upload(file());
      expect(result).toMatchObject({ ok: false, status: 401 });
      expect(store.latest().conn.phase).toBe('over');
    });

    it('ends the page on a 403 forbidden from /upload', async () => {
      server.uploadAnswer = () => json(403, { ok: false, error: { type: 'forbidden', message: 'Not allowed.' } });
      await store.upload(file());
      expect(store.latest().conn.phase).toBe('over');
    });

    it('ends the page on a stopped 503 from /upload', async () => {
      server.uploadAnswer = () => json(503, { ok: false, error: { type: 'stopped', message: 'This chat has stopped.' } });
      await store.upload(file());
      expect(store.latest().conn.phase).toBe('over');
    });

    it('does not end the page on 403 attachments_disabled: a feature flag, not the whole chat refused', async () => {
      server.uploadAnswer = () =>
        json(403, { ok: false, error: { type: 'attachments_disabled', message: 'Attachments are turned off in this app.' } });
      const result = await store.upload(file());
      expect(result).toMatchObject({ ok: false, status: 403, type: 'attachments_disabled' });
      expect(store.latest().conn.phase).not.toBe('over');
    });

    it('answers a refusal for itself and posts nothing once the chat is already over', async () => {
      server.uploadAnswer = () => json(401, { ok: false, error: { type: 'unauthorized', message: 'Missing or wrong token.' } });
      await store.upload(file());
      expect(store.latest().conn.phase).toBe('over');
      const result = await store.upload(file());
      expect(result).toMatchObject({ ok: false, status: 503, type: 'stopped' });
      expect(server.calls.filter((c) => c.url === '/upload')).toHaveLength(1); // the second never reached the network
    });

    it('passes the caller’s abort signal straight through to uploadFile', async () => {
      server.uploadAnswer = () => new Promise<Response>(() => {});
      const controller = new AbortController();
      const answer = store.upload(file(), controller.signal);
      await settle();
      controller.abort();
      await expect(answer).resolves.toMatchObject({ ok: false, status: 0, type: 'network_error' });
    });
  });

  describe('turn timings', () => {
    let perf: number;
    let frames: Array<() => void>;
    const user = { id: 'msg_1_1', role: 'user', content: 'hello', timestamp: 't' };
    const reply = (content: string) => ({ id: 'msg_2_1', role: 'assistant', content, timestamp: 't', isStreaming: true });
    const signedIn = state({ auth: { ...ana, oauthPending: null, oauthError: null } });

    beforeEach(() => {
      perf = 100;
      frames = [];
      store = new PageStore('tok', {
        env: server.env(),
        schedule: (flush) => flushes.push(flush),
        newId: () => 'c-' + String(++ids),
        now: () => clock,
        perf: () => perf,
        frame: (fn) => frames.push(fn),
      });
    });

    /** Run the animation frames queued so far (not the ones they queue). */
    function tick(): void {
      frames.splice(0).forEach((fn) => fn());
    }

    const metrics = () => server.commands().filter((c) => c.type === 'metrics');

    it('reports Send to POST and the first text to its paint, once the turn is over', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: signedIn }));
      store.send('hello');
      perf = 104.25; // the POST goes out
      await settle();
      expect(server.commands().at(-1)).toMatchObject({ type: 'send', timing: true });
      await deliver(
        sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['messages'], value: [user, reply('')] }, { type: 'set', path: ['isLoading'], value: true }] }),
      );
      perf = 900;
      clock = 1790000000123;
      await deliver(set(3, 2, ['messages', 1, 'content'], 'Hi'));
      expect(frames).toHaveLength(1);
      await deliver(set(4, 3, ['isLoading'], false));
      expect(metrics()).toEqual([]); // not painted yet
      perf = 910;
      tick(); // the frame that renders it
      expect(metrics()).toEqual([]);
      perf = 925.5;
      tick(); // after the paint
      await settle();
      expect(metrics()).toEqual([
        { id: 'c-2', type: 'metrics', messageId: 'msg_1_1', sendToPostMs: 4.3, firstOpToPaintMs: 25.5, firstOpAtMs: 1790000000123 },
      ]);
      // One report per turn: later states change nothing.
      await deliver(set(5, 4, ['error'], null));
      await new Promise((resolve) => setTimeout(resolve, PAINT_WAIT_MS + 50));
      expect(metrics()).toHaveLength(1);
    });

    it('reports a turn with no text at its end, and waits out an approval', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: signedIn }));
      store.send('delete it');
      await settle();
      await deliver(
        sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['messages'], value: [user, reply('')] }, { type: 'set', path: ['isLoading'], value: true }] }),
      );
      const card = { toolCallId: 't', toolName: 'delete_volume', arguments: {}, description: '' };
      await deliver(
        sse('ops', { seq: 3, base: 2, ops: [{ type: 'set', path: ['pendingApproval'], value: card }, { type: 'set', path: ['isLoading'], value: false }] }),
      );
      expect(metrics()).toEqual([]); // paused, not over
      await deliver(set(4, 3, ['pendingApproval'], null));
      await settle();
      expect(metrics()).toEqual([
        { id: 'c-2', type: 'metrics', messageId: 'msg_1_1', sendToPostMs: 0, firstOpToPaintMs: null, firstOpAtMs: null },
      ]);
      expect(frames).toEqual([]);
    });

    it('reports without the paint when no frame runs (a hidden web view)', async () => {
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: signedIn }));
      store.send('hello');
      await settle();
      await deliver(
        sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['messages'], value: [user, reply('Hi')] }, { type: 'set', path: ['isLoading'], value: false }] }),
      );
      expect(metrics()).toEqual([]);
      await new Promise((resolve) => setTimeout(resolve, PAINT_WAIT_MS + 50));
      expect(metrics()).toEqual([
        { id: 'c-2', type: 'metrics', messageId: 'msg_1_1', sendToPostMs: 0, firstOpToPaintMs: null, firstOpAtMs: 0 },
      ]);
      tick();
      tick();
      await settle();
      expect(metrics()).toHaveLength(1);
    });

    it('times nothing before the first snapshot, and ignores the turns it did not send', async () => {
      store.send('early');
      await settle();
      expect(server.commands()).toEqual([{ id: 'c-1', type: 'send', text: 'early', attachmentIds: [] }]);
      store.start();
      await settle();
      await deliver(sse('snapshot', { seq: 1, state: signedIn }));
      await deliver(
        sse('ops', { seq: 2, base: 1, ops: [{ type: 'set', path: ['messages'], value: [user, reply('Hi')] }] }),
      );
      tick();
      tick();
      await settle();
      expect(metrics()).toEqual([]);
    });
  });

  it('mints a v4 command id where there is no crypto.randomUUID (Chromium 83)', async () => {
    // The store must not depend on main.tsx having imported the polyfills first: a second
    // entry point would otherwise throw on every command.
    const real = Object.getOwnPropertyDescriptor(globalThis, 'crypto')!;
    const source = globalThis.crypto as Crypto;
    const chromium83 = { getRandomValues: (a: Uint8Array) => source.getRandomValues(a) };
    Object.defineProperty(globalThis, 'crypto', { configurable: true, value: chromium83 });
    try {
      vi.resetModules();
      const fresh = new (await import('../src/store')).PageStore('tok', { env: server.env(), schedule: () => {} });
      const first = fresh.command({ type: 'reload' });
      const second = fresh.command({ type: 'reload' });
      await settle();
      expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      expect(second).not.toBe(first);
    } finally {
      Object.defineProperty(globalThis, 'crypto', real);
      vi.resetModules();
    }
  });
});
