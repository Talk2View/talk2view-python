/**
 * The runtime guard's self-test, built into `dist-guard/` by
 * `npm run build:guard` in place of ./noop.ts; the shipped page never contains it.
 *
 * On `#t=<token>&auto=1` (the Slicer driver's URL) it waits for the thread, runs the
 * probes and the scripted run, and hands the results to the host with the `copy` command
 * (the driver's host writes them to disk), so they travel the real command path.
 */
import { resultOf, type PageStore } from '../store';
import { isAutoRun, launchedFromFragment, runAuto, showThread, type AutoReport } from './autorun';
import { monitors, recordGuardFailure, setStreaming } from './monitors';
import { runProbes, type Probe } from './probes';

/** The prefix the driver's host looks for in `clipboard_set`. */
export const RESULTS_MARKER = 't2v-guard-results:';

/**
 * Set the moment the results are ready. The driver judges the FIRST report it is handed,
 * so there must never be a second: a self-test whose budget ran out sends what it has, but
 * the scripted run it gave up waiting on is not cancellable. The flag is how that run
 * knows to stop (it asks before every step and, above all, before its scripted reload),
 * and how any load that still follows knows it has nothing to run.
 *
 * It belongs to one launch. A fresh `&auto=1` navigation is a new run and clears it;
 * without that, a second launch in the same web view would be silently inert.
 */
const SENT_KEY = 't2v_guard_sent';
/** The payload itself, kept with the flag so a later load can put it back on the window. */
const RESULTS_KEY = 't2v_guard_results';

/** Where the driver reads the results when no command could get them home. */
const WINDOW_KEY = '__t2vGuardResults';

function alreadySent(): boolean {
  try {
    return sessionStorage.getItem(SENT_KEY) === '1';
  } catch {
    return false; // no sessionStorage: this load is the only one that could have sent
  }
}

function markSent(payload: string): void {
  try {
    sessionStorage.setItem(SENT_KEY, '1'); // first: the flag matters more than the copy
    sessionStorage.setItem(RESULTS_KEY, payload);
  } catch {
    // no storage, or the payload does not fit: this load's window copy still stands
  }
}

function forgetSent(): void {
  try {
    sessionStorage.removeItem(SENT_KEY);
    sessionStorage.removeItem(RESULTS_KEY);
  } catch {
    // nothing stored that could be read back
  }
}

/** A load that finds the results already sent (one that raced the handover, or a plain
 * reload after the run) puts them back where the driver's last resort looks: a reload
 * starts a new document, and the window copy went with the old one. */
function restoreSentResults(): void {
  if (!alreadySent()) return;
  try {
    const payload = sessionStorage.getItem(RESULTS_KEY);
    if (payload) (window as unknown as Record<string, string>)[WINDOW_KEY] = payload;
  } catch {
    // nothing to restore
  }
}

// All read now, at import: main.tsx strips the fragment right after. The order matters: a
// fresh launch forgets the last run's report BEFORE anything could restore it.
if (launchedFromFragment()) forgetSent();
restoreSentResults();
const AUTO = isAutoRun();

export interface GuardResults {
  schema: 1;
  when: string;
  userAgent: string;
  chromiumMajor: number | null;
  viewport: { w: number; h: number; dpr: number };
  probes: Probe[];
  live: {
    errorCount: number;
    errors: string[];
    cspViolations: number;
    jank: typeof monitors.jank;
    heap: { supported: boolean; first: number | null; last: number | null; max: number | null };
    conn: ReturnType<PageStore['latest']>['conn'];
    stats: ReturnType<PageStore['latest']>['stats'];
  };
  /** The newest reply's text, for the driver to compare with the controller's. */
  lastReplyText: string;
  auto: AutoReport | null;
}

function collect(store: PageStore, probes: Probe[], auto: AutoReport | null): GuardResults {
  const model = store.latest();
  const major = /Chrome\/(\d+)/.exec(navigator.userAgent);
  const replies = model.state.messages.filter((m) => m.role === 'assistant');
  const heap = monitors.heap;
  return {
    schema: 1,
    when: new Date().toISOString(),
    userAgent: navigator.userAgent,
    chromiumMajor: major ? Number(major[1]) : null,
    viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
    probes,
    live: {
      errorCount: monitors.errorCount,
      errors: monitors.errors.slice(),
      cspViolations: monitors.cspViolations,
      jank: { ...monitors.jank },
      heap: { supported: heap.supported, first: heap.first, last: heap.last, max: heap.max },
      conn: model.conn,
      stats: model.stats,
    },
    lastReplyText: replies.length ? replies[replies.length - 1]!.content : '',
    auto,
  };
}

const frame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

/** Two frames, one after the other: two `requestAnimationFrame`s asked for in the same
 * turn both run on the NEXT frame, which is one frame, not two. */
const twoFrames = async (): Promise<void> => {
  await frame();
  await frame();
};

/** How long the page has to draw a thread at all before the guard gives up on it. */
const MOUNT_MS = 30000;
/** How long the two settling frames have. A view that stops answering
 * `requestAnimationFrame` — a paint that never happens under software rendering — would
 * otherwise wait for ever here, outside every other deadline, and send nothing at all. */
const FRAME_MS = 5000;
/** The probes' own share of the budget. */
const PROBE_MS = 30000;
/**
 * Everything the self-test does on THIS page load — waiting for the thread, the probes and
 * the scripted run together. The driver waits 300 s for the results to come home, so
 * the guard gives up well inside that and sends what it has: a page that hangs must
 * produce a report saying so, not a bare CI timeout with nothing in it.
 */
const DEFAULT_BUDGET_MS = 100000;

/**
 * One e2e spec (guard.spec.ts) needs a budget that runs out mid-run, to drive the real
 * path this file guards against: results sent, then a run that is still going. It sets
 * this global with Playwright's addInitScript, before any page script runs. It is not a
 * knob anyone else can reach: this module never ships (the page's build aliases `#guard`
 * to noop.ts), nothing in a URL, storage or a host message can set a global, the Slicer
 * driver injects no script, and it can only SHORTEN the budget, never lengthen it.
 */
const TEST_BUDGET_KEY = '__t2vGuardTestBudgetMs';

function budgetMs(): number {
  const override = (window as unknown as Record<string, unknown>)[TEST_BUDGET_KEY];
  return typeof override === 'number' && override > 0 && override < DEFAULT_BUDGET_MS ? override : DEFAULT_BUDGET_MS;
}

const BUDGET_MS = budgetMs();

/** How long to wait for the host to answer the command carrying the results. */
const DELIVER_MS = 15000;

/**
 * Run `work`, but never for longer than `ms`. On a timeout (or a rejection) the failure
 * goes into the monitors — so `live.errorCount` is not 0 and `live.errors` names it — and
 * `fallback()` stands in. The work itself is not cancellable; it simply stops being
 * waited on, and since `runAuto` only ever appends to a report the guard already holds,
 * what is sent is what the run got through.
 */
function withDeadline<T>(work: Promise<T>, ms: number, what: string, fallback: () => T): Promise<T> {
  return new Promise<T>((resolve) => {
    let settled = false;
    const finish = (value: T) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      resolve(value);
    };
    const timer = window.setTimeout(() => {
      if (settled) return;
      recordGuardFailure(what + ' did not finish within ' + String(Math.round(ms)) + ' ms');
      finish(fallback());
    }, Math.max(0, ms));
    void work.then(finish, (err: unknown) => {
      recordGuardFailure(what + ' threw: ' + String(err));
      finish(fallback());
    });
  });
}

/** The thread, or false once `ms` has run out. */
async function waitForThread(store: PageStore, ms: number): Promise<boolean> {
  const deadline = performance.now() + ms;
  while (store.latest().seq === null || !document.querySelector('.t2v-input')) {
    if (performance.now() > deadline) return false;
    await sleep(50);
  }
  return true;
}

/**
 * Hand the results to the host. A command is the page's only way home, so a refusal is
 * worth one retry. If the chat is over — the server gone, a 401, a 403 — there is no way
 * home at all: the store answers locally, nothing reaches the driver, and it sees
 * its own timeout with no report. Two last resorts are left for the driver to dig the
 * payload out of: `window.__t2vGuardResults`, which it reads with the web view's own JS
 * evaluation, and the console, which a host that captures console messages keeps in its
 * log. Results recovered either way are a FAILED run — the way home is part of what the
 * guard tests — so both exist only to say what went wrong, never to rescue a verdict.
 */
async function deliver(store: PageStore, payload: string): Promise<void> {
  for (let attempt = 0; attempt < 2; attempt++) {
    const id = store.command({ type: 'copy', text: payload });
    const deadline = performance.now() + DELIVER_MS;
    let result = resultOf(store.latest(), id);
    while (result === null && performance.now() < deadline) {
      await sleep(50);
      result = resultOf(store.latest(), id);
    }
    if (result && result.ok) return;
    recordGuardFailure(
      'the results command was ' + (result ? 'refused (' + String(result.error && result.error.type) + ')' : 'never answered') +
        ' on attempt ' + String(attempt + 1),
    );
    if (attempt === 0) await sleep(500);
  }
  console.error(payload); // already carries RESULTS_MARKER: never prefix it twice
}

/** Where guard.spec.ts reaches `showThread()` directly, since the scripted run's own 18
 * steps never leave it anything to recover from (see autorun.ts). Guard build only. */
const SHOW_THREAD_KEY = '__t2vGuardShowThread';

export function installGuard(store: PageStore): void {
  (window as unknown as Record<string, unknown>)[SHOW_THREAD_KEY] = showThread;
  store.subscribe(() => {
    const s = store.get().state;
    setStreaming(s.isLoading && s.messages.some((m) => m.isStreaming === true));
  });
  if (!AUTO || alreadySent()) return;
  void (async () => {
    const started = performance.now();
    const left = () => BUDGET_MS - (performance.now() - started);
    let probes: Probe[] = [];
    // Whatever the scripted run has recorded so far, kept outside it so that a run which
    // hangs or dies still comes home with the steps it did get through.
    let latest: AutoReport | null = null;
    if (await waitForThread(store, MOUNT_MS)) {
      // Measure the thread two frames after it mounts — bounded, like everything else.
      await withDeadline<void>(twoFrames(), Math.min(FRAME_MS, left()), 'the settling frames', () => undefined);
      probes = await withDeadline(runProbes(store), Math.min(PROBE_MS, left()), 'the probes', () => []);
      await withDeadline<AutoReport | null>(
        runAuto(
          store,
          (report) => {
            latest = report;
          },
          alreadySent,
        ),
        left(),
        'the scripted run',
        () => latest,
      );
    } else {
      recordGuardFailure('the page never drew a thread within ' + String(MOUNT_MS) + ' ms');
    }
    const payload = RESULTS_MARKER + JSON.stringify(collect(store, probes, latest));
    // Both of these happen BEFORE the handover, never after it. The scripted run this
    // load may still be carrying can reload the page at any moment, and the load that
    // follows must find the flag already set, and the driver the payload already there.
    (window as unknown as Record<string, string>)[WINDOW_KEY] = payload;
    markSent(payload);
    await deliver(store, payload);
  })();
}
