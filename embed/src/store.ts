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
 *   out to guest, which is the same visitor carrying on);
 * - a turn it sent is timed (`TurnProbe`): Send to the command's POST, and the first text
 *   op to it painted, reported with one `metrics` command once the turn is over.
 * The page never blocks on a result: results only report.
 */
// A command id is built with crypto.randomUUID, which Chromium 83 has not: the store
// installs the polyfill itself rather than trusting whoever imported it to have done so.
import './polyfills';
import { applyOps, OpError } from './ops';
import { authIdentityKey, failureText, findResult, lastUserId, needsGuestStart } from './selectors';
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

/** One button in the notice line (the Skills view's Undo). */
export interface NoticeAction {
  label: string;
  onClick: () => void;
}

/** How long a notice with an action (Undo) stays up ("Undo for 6 s"). */
export const NOTICE_ACTION_MS = 6000;

export interface PageModel {
  state: PageState;
  /** The seq `state` is at; null before the first snapshot. */
  seq: number | null;
  conn: ConnStatus;
  /** The last command that failed, for the notice line; null when there is none. */
  notice: string | null;
  /** The notice's one button, if it has one; null otherwise (and always with no notice). */
  noticeAction: NoticeAction | null;
  /** Commands the server refused over HTTP (never queued, so no result will come), newest last. */
  refusals: CommandResult[];
  stats: { snapshots: number; batches: number; gaps: number; resyncs: number };
}

export const INITIAL_MODEL: PageModel = {
  state: EMPTY_STATE,
  seq: null,
  conn: INITIAL_CONN,
  notice: null,
  noticeAction: null,
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
  'metrics',
  // The Skills view shows these outcomes itself (a form's field errors, an import's
  // Replace / Keep both), and turns any other failure into the notice on its own.
  'skillSave',
  'skillImport',
  'skillRead',
]);
/** How long a finished turn waits for its first text to be painted before it is reported
 * without it (a hidden web view runs no animation frames). */
export const PAINT_WAIT_MS = 1000;
/** How long the store watches for a command's result before forgetting it. */
export const WATCH_MS = 60000;
const SAW_LIMIT_KEY = 't2v_saw_limit';

export interface StoreOptions {
  env?: TransportEnv;
  /** Runs `flush` soon; defaults to the next animation frame, with a timer for a hidden page. */
  schedule?: (flush: () => void) => void;
  now?: () => number;
  newId?: () => string;
  /** A monotonic clock for turn timings; defaults to `performance.now()`. */
  perf?: () => number;
  /** Runs `fn` on the next animation frame; defaults to `requestAnimationFrame`. */
  frame?: (fn: () => void) => void;
}

/**
 * One turn the page sent, being timed (talk2view.metrics). Nothing the end-user typed or
 * the model said is kept: marks, and the turn's user message id to name it by.
 */
interface TurnProbe {
  sentAt: number;
  postAt: number | null;
  /** The last user message id when Send was pressed: the next new one is this turn's. */
  before: string | null;
  userId: string | null;
  firstOpAt: number | null;
  firstOpWall: number | null;
  paintMs: number | null;
  ended: boolean;
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
  private readonly perf: () => number;
  private readonly frame: (fn: () => void) => void;
  private probe: TurnProbe | null = null;
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
  /** The timer that ends a notice's action (`showNotice`), while one is up. */
  private noticeTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(token: string, options: StoreOptions = {}) {
    this.token = token;
    this.env = options.env ?? browserEnv();
    this.schedule = options.schedule ?? defaultSchedule;
    this.now = options.now ?? (() => Date.now());
    this.newId = options.newId ?? (() => crypto.randomUUID());
    this.perf = options.perf ?? (() => performance.now());
    this.frame =
      options.frame ??
      ((fn) => {
        if (typeof requestAnimationFrame === 'function') requestAnimationFrame(fn);
        else setTimeout(fn, 16);
      });
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
    this.follow(state);
    return 'applied';
  }

  // ── Acting ──

  /**
   * Send a command; returns its id (its result, if any, arrives in `state.results`).
   * Commands are posted one after another: the router runs them in the order they were
   * queued, so a guest start queued first is the guest the next send uses.
   */
  command(command: Command, onPost?: () => void): string {
    const id = this.newId();
    if (this.pending.conn.phase === 'over') {
      // The chat is over. The server would answer 503 `stopped`, and that refusal would
      // run stop() and report `over` all over again: answer for it instead.
      this.record(id, command.type, { type: 'stopped', message: 'This chat has stopped.' });
      return id;
    }
    if (!QUIET.has(command.type)) this.watched.set(id, { type: command.type, at: this.now() });
    const posted = this.posting.then(() => {
      if (onPost) onPost();
      return postCommand(this.token, id, command, this.env);
    });
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
    if (this.pending.seq === null || this.pending.conn.phase === 'over') {
      return this.command({ type: 'send', text, attachmentIds });
    }
    // A probe not reported yet (its send was refused, say) is simply replaced.
    const probe: TurnProbe = {
      sentAt: this.perf(),
      postAt: null,
      before: lastUserId(this.pending.state),
      userId: null,
      firstOpAt: null,
      firstOpWall: null,
      paintMs: null,
      ended: false,
    };
    this.probe = probe;
    return this.command({ type: 'send', text, attachmentIds, timing: true }, () => {
      probe.postAt = this.perf();
    });
  }

  /** Follow the timed turn through a new state: its user message, its first text, its end. */
  private follow(state: PageState): void {
    const probe = this.probe;
    if (!probe || probe.ended) return;
    if (probe.userId === null) {
      const id = lastUserId(state);
      if (id === null || id === probe.before) return;
      probe.userId = id;
    }
    if (probe.firstOpAt === null) {
      const at = state.messages.findIndex((m) => m.id === probe.userId);
      if (at >= 0 && state.messages.slice(at + 1).some((m) => m.role === 'assistant' && m.content)) {
        probe.firstOpAt = this.perf();
        probe.firstOpWall = this.now();
        // This frame publishes the state and React renders it; the next one runs after
        // that render was painted.
        this.frame(() =>
          this.frame(() => {
            if (probe.firstOpAt !== null) probe.paintMs = this.perf() - probe.firstOpAt;
            this.report(probe, false);
          }),
        );
      }
    }
    if (!state.isLoading && state.pendingApproval === null) {
      probe.ended = true;
      this.report(probe, false);
      if (this.probe === probe) this.env.setTimeout(() => this.report(probe, true), PAINT_WAIT_MS);
    }
  }

  /** One `metrics` command per timed turn, once it is over and its first text painted. */
  private report(probe: TurnProbe, force: boolean): void {
    if (this.probe !== probe || !probe.ended || probe.userId === null) return;
    if (!force && probe.firstOpAt !== null && probe.paintMs === null) return;
    this.probe = null;
    const round = (ms: number | null) => (ms === null ? null : Math.max(0, Math.round(ms * 10) / 10));
    this.command({
      type: 'metrics',
      messageId: probe.userId,
      sendToPostMs: round(probe.postAt === null ? null : probe.postAt - probe.sentAt),
      firstOpToPaintMs: round(probe.paintMs),
      firstOpAtMs: probe.firstOpWall,
    });
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

  /**
   * Put a line in the notice (a refused upload, a failed command). With `action`, the
   * notice carries that one button (Undo) and goes by itself after NOTICE_ACTION_MS —
   * unless another notice replaced it first, which that timer then leaves alone.
   */
  showNotice(notice: string, action?: NoticeAction): void {
    this.setNotice(notice, action ?? null);
    if (this.noticeTimer !== null) {
      clearTimeout(this.noticeTimer);
      this.noticeTimer = null;
    }
    if (action) {
      this.noticeTimer = setTimeout(() => {
        this.noticeTimer = null;
        if (this.pending.noticeAction === action) this.setNotice(null);
      }, NOTICE_ACTION_MS);
    }
  }

  dismissNotice(): void {
    this.setNotice(null);
  }

  private setNotice(notice: string | null, noticeAction: NoticeAction | null = null): void {
    if (this.pending.notice !== notice || this.pending.noticeAction !== noticeAction) {
      this.update({ ...this.pending, notice, noticeAction });
    }
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
      if (previousHadUser && this.pending.notice !== null) this.pending = { ...this.pending, notice: null, noticeAction: null };
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
        // A row the list still showed for a few ms after the identity or the
        // list itself moved on (store.ts's own module doc, "the threads slice ... can
        // arrive after other slices") is a staleness artefact, not a real failure — the
        // next threads slice (already on its way) drops the row on its own, so a click that
        // lands on one refreshes quietly instead of alarming over something already fixed.
        const stale = result.type === 'threadSwitch' && result.error?.type === 'unknown_thread';
        if (!result.ok && !stale) this.pending = { ...this.pending, notice: failureText(result), noticeAction: null };
      } else if (now - watch.at > WATCH_MS) {
        this.watched.delete(id);
      }
    }
  }
}
