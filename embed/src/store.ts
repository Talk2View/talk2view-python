/**
 * The page's copy of the server's state, and every action the page takes. Python owns
 * the state: the store only applies `snapshot` and `ops` events
 * and sends commands. React reads it through `useSyncExternalStore`, and hears of a
 * change at most once per animation frame.
 *
 * It also keeps the few reactions that belong to no view, because a view may not be
 * mounted when they are due:
 * - a send while signed out, guests allowed, first starts a guest (`startAnonymous`), and
 *   so does an upload, which waits for that guest (`beforeUpload`);
 * - the first keystroke of an identity warms its key (`warmUp`);
 * - a guest limit lifted by a real sign-in re-sends the stopped turn (`reload`), once;
 * - a command that fails (its result, or an HTTP refusal) becomes the `notice` line,
 *   unless a view shows that command's outcome itself;
 * - an identity change clears the notice: it was about the identity before (not signed
 *   out to guest, which is the same visitor carrying on).
 * The page never blocks on a result: results only report.
 */
// A command id is built with crypto.randomUUID, which Chromium 83 has not: the store
// installs the polyfill itself rather than trusting whoever imported it to have done so.
import './polyfills';
import { applyOps, OpError } from './ops';
import { authIdentityKey, failureText, findResult, needsGuestStart } from './selectors';
import {
  browserEnv,
  connectEvents,
  INITIAL_CONN,
  postCommand,
  uploadFile,
  type ConnStatus,
  type OpsOutcome,
  type TransportEnv,
  type UploadResult,
} from './transport';
import {
  EMPTY_STATE,
  type Command,
  type CommandResult,
  type OpsEvent,
  type PageState,
  type SnapshotEvent,
} from './wire';

export interface PageModel {
  state: PageState;
  /** The seq `state` is at; null before the first snapshot. */
  seq: number | null;
  conn: ConnStatus;
  /** The last command that failed, for the notice line; null when there is none. */
  notice: string | null;
  /** Commands the server refused over HTTP (never queued, so no result will come), newest last. */
  refusals: CommandResult[];
  stats: { snapshots: number; batches: number; gaps: number; resyncs: number };
}

export const INITIAL_MODEL: PageModel = {
  state: EMPTY_STATE,
  seq: null,
  conn: INITIAL_CONN,
  notice: null,
  refusals: [],
  stats: { snapshots: 0, batches: 0, gaps: 0, resyncs: 0 },
};

/** A command's outcome: its result in the state, or its HTTP refusal; null while neither is in. */
export function resultOf(model: PageModel, id: string | null): CommandResult | null {
  if (!id) return null;
  return findResult(model.state, id) ?? model.refusals.find((r) => r.id === id) ?? null;
}

/** Commands whose outcome a view shows itself, or that fail harmlessly. */
const QUIET = new Set<Command['type']>([
  'login',
  'signup',
  'startAnonymous',
  'warmUp',
  'logout',
  'oauthStart',
  'oauthCancel',
  'oauthDismissError',
]);
/** How long the store watches for a command's result before forgetting it. */
export const WATCH_MS = 60000;
const SAW_LIMIT_KEY = 't2v_saw_limit';

export interface StoreOptions {
  env?: TransportEnv;
  /** Runs `flush` soon; defaults to the next animation frame, with a timer for a hidden page. */
  schedule?: (flush: () => void) => void;
  now?: () => number;
  newId?: () => string;
}

function defaultSchedule(flush: () => void): void {
  requestAnimationFrame(flush);
  // rAF does not run while the web view is hidden; never strand the state.
  window.setTimeout(flush, 250);
}

function readSawLimit(): boolean {
  try {
    return sessionStorage.getItem(SAW_LIMIT_KEY) === '1';
  } catch {
    return false;
  }
}

function writeSawLimit(on: boolean): void {
  try {
    if (on) sessionStorage.setItem(SAW_LIMIT_KEY, '1');
    else sessionStorage.removeItem(SAW_LIMIT_KEY);
  } catch {
    // The in-memory flag still serves this page load.
  }
}

export class PageStore {
  readonly token: string;
  private readonly env: TransportEnv;
  private readonly schedule: (flush: () => void) => void;
  private readonly now: () => number;
  private readonly newId: () => string;
  private pending: PageModel = INITIAL_MODEL;
  private published: PageModel = INITIAL_MODEL;
  private scheduled = false;
  private readonly listeners = new Set<() => void>();
  private stream: { close(): void } | null = null;
  private readonly watched = new Map<string, { type: Command['type']; at: number }>();
  private warmedFor: string | null = null;
  /** The guest start already on its way, if any: never mint a second guest (see `send`). */
  private guestStarting: { id: string; at: number } | null = null;
  /** Uploads waiting for that guest (`beforeUpload`), told how the start ended. */
  private guestWaiters: Array<(ok: boolean) => void> = [];
  private posting: Promise<unknown> = Promise.resolve();
  private sawLimit: boolean;
  /** `authIdentityKey` of the state last seen; null before the first. */
  private identity: string | null = null;
  /** The user id of that identity: null when it was signed out (or before the first). */
  private identityUser: string | null = null;

  constructor(token: string, options: StoreOptions = {}) {
    this.token = token;
    this.env = options.env ?? browserEnv();
    this.schedule = options.schedule ?? defaultSchedule;
    this.now = options.now ?? (() => Date.now());
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.sawLimit = readSawLimit();
  }

  // ── Reading ──

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  get = (): PageModel => this.published;

  /** The newest model, before React has been told of it (for event handlers). */
  latest(): PageModel {
    return this.pending;
  }

  // ── The stream ──

  start(): void {
    if (this.stream) this.stream.close();
    this.stream = connectEvents(
      this.token,
      {
        seq: () => this.pending.seq,
        snapshot: (event) => this.onSnapshot(event),
        ops: (event) => this.onOps(event),
        status: (conn) => this.update({ ...this.pending, conn }),
      },
      this.env,
    );
  }

  stop(): void {
    if (this.stream) this.stream.close();
    this.stream = null;
  }

  private onSnapshot(event: SnapshotEvent): void {
    const state: PageState = { ...EMPTY_STATE, ...event.state };
    const stats = { ...this.pending.stats, snapshots: this.pending.stats.snapshots + 1 };
    this.update({ ...this.pending, state, seq: event.seq, stats });
  }

  private onOps(event: OpsEvent): OpsOutcome {
    const model = this.pending;
    if (model.seq === null || event.base !== model.seq) {
      this.update({ ...model, stats: { ...model.stats, gaps: model.stats.gaps + 1 } });
      return 'gap';
    }
    let state: PageState;
    try {
      state = applyOps(model.state, event.ops);
    } catch (err) {
      if (!(err instanceof OpError)) throw err;
      console.warn('[t2v] an op did not apply; taking a fresh snapshot:', err.message);
      this.update({ ...model, stats: { ...model.stats, resyncs: model.stats.resyncs + 1 } });
      return 'broken';
    }
    this.update({ ...model, state, seq: event.seq, stats: { ...model.stats, batches: model.stats.batches + 1 } });
    return 'applied';
  }

  // ── Acting ──

  /**
   * Send a command; returns its id (its result, if any, arrives in `state.results`).
   * Commands are posted one after another: the router runs them in the order they were
   * queued, so a guest start queued first is the guest the next send uses.
   */
  command(command: Command): string {
    const id = this.newId();
    if (this.pending.conn.phase === 'over') {
      // The chat is over. The server would answer 503 `stopped`, and that refusal would
      // run stop() and report `over` all over again: answer for it instead.
      this.record(id, command.type, { type: 'stopped', message: 'This chat has stopped.' });
      return id;
    }
    if (!QUIET.has(command.type)) this.watched.set(id, { type: command.type, at: this.now() });
    const posted = this.posting.then(() => postCommand(this.token, id, command, this.env));
    this.posting = posted.then(
      () => undefined,
      () => undefined,
    );
    void posted.then((refusal) => {
      if (!refusal) return;
      this.watched.delete(id);
      if (this.guestStarting?.id === id) {
        this.guestStarting = null;
        this.settleGuestWaiters(false);
      }
      this.record(id, command.type, refusal);
      if (refusal.type === 'stopped' || refusal.status === 401 || refusal.status === 403) {
        this.stop();
        this.update({ ...this.pending, conn: { ...this.pending.conn, phase: 'over', reason: refusal.type } });
        return;
      }
      console.warn('[t2v] command refused:', command.type, refusal.type, refusal.message);
      if (!QUIET.has(command.type)) this.setNotice(failureText({ type: command.type, error: refusal }));
    });
    return id;
  }

  /** A command the server never queued: no result will come, so the store keeps its own. */
  private record(id: string, type: Command['type'], error: { type: string; message: string }): void {
    const refused: CommandResult = { id, type, ok: false, error: { type: error.type, message: error.message } };
    this.update({ ...this.pending, refusals: [...this.pending.refusals, refused].slice(-20) });
  }

  /**
   * The composer's send (and a welcome suggestion): a guest first when needed, then the
   * message.
   *
   * Only ever ONE guest start. `auth.start_anonymous` mints a NEW guest every time, and
   * the welcome screen stays up until the first message lands — a whole engine round
   * trip — so a second click would replace the guest mid-turn: the results slice emptied,
   * an uploaded attachment orphaned under the old identity, one more of the partner's
   * daily guest allowance spent, and the second send refused as busy. The latch is
   * cleared by `react()` as soon as the start lands (signed in, refused, or a result), by
   * `command()` when the POST itself is refused, and by time as a backstop.
   */
  send(text: string, attachmentIds: string[] = []): string {
    // Never decide this from EMPTY_STATE. It allows guests, and `auth.start_anonymous` has
    // no signed-in guard, so a send in the moment before the first snapshot would replace
    // a real account with a guest. Until then the server decides what a send does.
    if (this.pending.seq !== null && needsGuestStart(this.pending.state) && !this.startingGuest()) {
      this.guestStarting = { id: this.command({ type: 'startAnonymous' }), at: this.now() };
    }
    return this.command({ type: 'send', text, attachmentIds });
  }

  /**
   * Called before an upload (runtime.ts). Signed out with guests allowed, the upload needs
   * an identity to be filed under, so the guest is started first, exactly as `send` does
   * (one guest, never two). Resolves true once the auth slice shows it, false once the
   * start is refused or has not landed within `WATCH_MS`, and true at once when no guest
   * is needed.
   */
  beforeUpload(): Promise<boolean> {
    // Over: `upload` answers for the server itself. Before the first snapshot: never
    // decide from EMPTY_STATE (see `send`).
    if (this.pending.conn.phase === 'over' || this.pending.seq === null) return Promise.resolve(true);
    if (!needsGuestStart(this.pending.state)) return Promise.resolve(true);
    if (!this.startingGuest()) {
      this.guestStarting = { id: this.command({ type: 'startAnonymous' }), at: this.now() };
    }
    return new Promise<boolean>((resolve) => {
      let done = false;
      const settle = (ok: boolean) => {
        if (done) return;
        done = true;
        this.env.clearTimeout(timer);
        resolve(ok);
      };
      const timer = this.env.setTimeout(() => settle(false), WATCH_MS);
      this.guestWaiters.push(settle);
    });
  }

  private settleGuestWaiters(ok: boolean): void {
    const waiters = this.guestWaiters;
    this.guestWaiters = [];
    waiters.forEach((settle) => settle(ok));
  }

  /** Is a guest start still in flight? Expires after `WATCH_MS`, so it can never stick. */
  private startingGuest(): boolean {
    const starting = this.guestStarting;
    if (starting === null) return false;
    if (this.now() - starting.at > WATCH_MS) {
      this.guestStarting = null;
      return false;
    }
    return true;
  }

  /** Called on every keystroke in the composer: the first one per identity warms its key. */
  typed(): void {
    const auth = this.pending.state.auth;
    const who = auth.user?.id;
    if (!auth.isAuthenticated || !who || this.warmedFor === who) return;
    this.warmedFor = who;
    this.command({ type: 'warmUp' });
  }

  /**
   * Upload one file (`POST /upload`, outside the command queue: a refusal must reach the
   * composer at once, not wait behind other commands). `signal`, passed through to
   * `uploadFile`, is the adapter's own way to abort an upload still running (Remove, an
   * identity change, New chat). A 401, an unexempted 403, or `stopped` means the same
   * thing here as it does for a command (`command()` above): the chat is over, ended the
   * same way. 403 `attachments_disabled` is not that — the host simply does not take
   * files right now, which the composer already shows as a plain notice; treating it as
   * fatal too would end the whole chat over a feature flag.
   */
  upload(file: File, signal?: AbortSignal): Promise<UploadResult> {
    if (this.pending.conn.phase === 'over') {
      return Promise.resolve({ ok: false, status: 503, type: 'stopped', message: 'This chat has stopped.' });
    }
    return uploadFile(this.token, file, this.env, signal).then((result) => {
      if (!result.ok) {
        const fatal =
          result.type === 'stopped' ||
          result.status === 401 ||
          (result.status === 403 && result.type !== 'attachments_disabled');
        if (fatal) {
          this.stop();
          this.update({ ...this.pending, conn: { ...this.pending.conn, phase: 'over', reason: result.type } });
        }
      }
      return result;
    });
  }

  /** Put a line in the notice (a refused upload, a failed command). */
  showNotice(notice: string): void {
    this.setNotice(notice);
  }

  dismissNotice(): void {
    this.setNotice(null);
  }

  private setNotice(notice: string | null): void {
    if (this.pending.notice !== notice) this.update({ ...this.pending, notice });
  }

  // ── Publishing ──

  private update(next: PageModel): void {
    const before = this.pending;
    this.pending = next;
    if (next.state !== before.state) this.react(next.state);
    if (this.scheduled) return;
    this.scheduled = true;
    this.schedule(this.flush);
  }

  private flush = (): void => {
    if (!this.scheduled) return;
    this.scheduled = false;
    this.published = this.pending;
    this.listeners.forEach((listener) => listener());
  };

  /** The reactions that belong to no view (see the module comment). */
  private react(state: PageState): void {
    // A notice names the previous identity's attachment, model or refusal: once someone
    // else is on show (a sign-in, a sign-out, the next guest), it no longer applies. Not
    // when a signed-out visitor becomes a guest: that is the same visitor carrying on (the
    // controller keeps the conversation, and the composer keeps its draft and chips, by
    // the same rule), and the guest may have been started by the very upload a refusal in
    // the same pick is about.
    const identity = authIdentityKey(state.auth);
    if (identity !== this.identity) {
      const previousHadUser = this.identityUser !== null;
      if (previousHadUser && this.pending.notice !== null) this.pending = { ...this.pending, notice: null };
      this.identity = identity;
      this.identityUser = state.auth.user?.id ?? null;
    }
    const { demoLimitReached, anonymousUnavailable } = state.limits;
    const starting = this.guestStarting;
    const started = starting ? findResult(state, starting.id) : null;
    if (starting && (state.auth.isAuthenticated || anonymousUnavailable !== null || started !== null)) {
      this.guestStarting = null;
      this.settleGuestWaiters(state.auth.isAuthenticated || started?.ok === true);
    }
    if (demoLimitReached || anonymousUnavailable !== null) {
      if (!this.sawLimit) {
        this.sawLimit = true;
        writeSawLimit(true);
      }
    } else if (this.sawLimit) {
      // The limits are empty again. A real account signing in lifts them, and the turn the
      // limit stopped is sent again. But they are also lifted whenever another account
      // comes on show — a guest signing out, the next guest, a refresh token the engine
      // rejected — and re-sending there would make `retry()` delete an answer the
      // end-user already has and run that turn, and bill it, a second time.
      const real = state.auth.isAuthenticated && !state.auth.isAnonymous;
      this.sawLimit = false;
      writeSawLimit(false);
      if (real) this.command({ type: 'reload' });
    }

    if (this.watched.size === 0) return;
    const now = this.now();
    for (const [id, watch] of this.watched) {
      const result = findResult(state, id);
      if (result) {
        this.watched.delete(id);
        if (!result.ok) this.pending = { ...this.pending, notice: failureText(result) };
      } else if (now - watch.at > WATCH_MS) {
        this.watched.delete(id);
      }
    }
  }
}
