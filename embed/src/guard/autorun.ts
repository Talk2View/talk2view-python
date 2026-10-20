/**
 * The scripted run the runtime guard's driver asks for with `#t=<token>&auto=1`.
 * The backend is `talk2view.ui.testing`'s
 * FakeController behind the real UIServer, so the script is FakeController's:
 * `/approve` asks for load_sample_data, `/fail` ends in an error, `/limit` runs a guest out.
 *
 * Every step drives the REAL controls (types into the composer, clicks Send, Stop, the
 * approval buttons, the header), because "do they work on Chromium 83" is the question.
 * A step whose control does not respond is a FAIL, and the run carries on, so one CI run
 * reports every broken control. One step reloads the page mid-stream: the report so far is
 * parked in sessionStorage and the run resumes after the reload.
 *
 * Three rules hold everywhere here, because a PASS has to mean what the judge will
 * take it to mean:
 * - Nothing waits a guessed number of milliseconds. Every wait is a bounded poll on a
 *   condition, and its timeout says what was missing. The page answers a click with a
 *   command, never a promise, so the condition is usually the state the server sends back.
 * - The STATE saying a turn ended is never taken as the DOM having drawn it. React commits
 *   a frame or more later, and a software-rendered web view under Xvfb rather more than
 *   that, so a step that judges the DOM polls the DOM (`settle`).
 * - A selector is scoped to the pane that is on show. Thread and Account never unmount
 *   (App.tsx hides them with `visibility: hidden` + `aria-hidden`), so a document-wide
 *   query happily matches a view nobody can see, and a step asserting "the gate appeared"
 *   would pass on a page that never showed it.
 */
import type { PageStore } from '../store';
import type { PageState, WireMessage } from '../wire';
import { monitors } from './monitors';

export type AutoStatus = 'PASS' | 'FAIL';

export interface AutoStep {
  id: string;
  status: AutoStatus;
  detail: string;
  /** Milliseconds since this page load started (or resumed) the run. */
  ms: number;
}

export interface AutoReport {
  startedAt: string;
  finishedAt: string | null;
  reloads: number;
  steps: AutoStep[];
  /** The first page load's counters: a reload resets them. */
  beforeReload: { errorCount: number; errors: string[]; cspViolations: number; gaps: number; messageCount: number } | null;
}

const STATE_KEY = 't2v_guard_state';
export const AUTO_KEY = 't2v_guard_auto';
const TOOL = 'load_sample_data';
const LINK = 'https://slicer.readthedocs.io/en/latest/';
/** How long the DOM is given to catch up with a state change before a step judges it. */
const DRAW_MS = 5000;

/**
 * True when THIS page load was launched with `&auto=1` in the fragment, rather than
 * resuming the scripted reload through sessionStorage. Read at import, because main.tsx
 * strips the fragment moments later. A launch is a fresh run, so a report parked by an
 * earlier run in this origin is stale and must never be resumed into.
 */
const FROM_FRAGMENT = /(?:^#|&)auto=1(?:&|$)/.test(location.hash);

/** True when this load is a fresh `&auto=1` launch (see FROM_FRAGMENT). */
export function launchedFromFragment(): boolean {
  return FROM_FRAGMENT;
}

/** What the run logs when it ends early because its results have already gone home. */
export const STOPPED_LINE = 't2v-guard: the results already went home, so the scripted run stops here';

let t0 = 0;
/** The length of the whole scripted reply, learnt in `auto:stream`; `auto:cancel-stream`
 * needs it to tell a stopped reply from one that simply finished. */
let fullReplyLength = 0;
/** Set when a step had to clean up after an earlier failure; folded into its detail. */
let recovery = '';

function noteRecovery(what: string): void {
  recovery = recovery ? recovery + '; ' + what : what;
}

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

async function waitFor<T>(what: string, timeoutMs: number, read: () => T | null | undefined | false): Promise<T> {
  const deadline = performance.now() + timeoutMs;
  for (;;) {
    const value = read();
    if (value) return value;
    if (performance.now() > deadline) throw new Error('timed out after ' + String(timeoutMs) + ' ms waiting for ' + what);
    await sleep(50);
  }
}

/**
 * Poll `read` until `ok` accepts what it returns, then return that value; on timeout
 * return the LAST reading instead of throwing, so a step still reports what the page
 * really showed rather than a bare "timed out".
 */
async function settle<T>(timeoutMs: number, read: () => T, ok: (value: T) => boolean): Promise<T> {
  const deadline = performance.now() + timeoutMs;
  let value = read();
  while (!ok(value) && performance.now() < deadline) {
    await sleep(50);
    value = read();
  }
  return value;
}

/** True while the scripted run owns the page (set by `&auto=1`, kept across the scripted reload). */
export function isAutoRun(): boolean {
  if (FROM_FRAGMENT) {
    try {
      sessionStorage.setItem(AUTO_KEY, '1');
    } catch {
      // the fragment says so for this load
    }
    return true;
  }
  try {
    return sessionStorage.getItem(AUTO_KEY) === '1';
  } catch {
    return false;
  }
}

function state(store: PageStore): PageState {
  return store.latest().state;
}

function lastAssistant(s: PageState): WireMessage | null {
  const last = s.messages[s.messages.length - 1];
  return last && last.role === 'assistant' ? last : null;
}

/** No run going and no card waiting: the controller takes one turn at a time. */
function idle(s: PageState): boolean {
  return !s.isLoading && s.pendingApproval === null;
}

/** The newest reply, once its run is over. */
function runEnded(s: PageState): WireMessage | null {
  const last = lastAssistant(s);
  return last && idle(s) && last.isStreaming === false ? last : null;
}

/** A command of this type the server has already answered. Commands are the page's only
 * way to reach the host, so this is how a step tells "the click did something" from
 * "the click did nothing" without asking the host, which the page cannot see. */
function answeredCommand(s: PageState, type: string): boolean {
  return s.results.some((r) => r.type === type && r.ok);
}

function buttonNamed(label: string, root: ParentNode = document): HTMLButtonElement | null {
  const byAria = root.querySelector<HTMLButtonElement>('button[aria-label="' + label + '"]');
  if (byAria) return byAria;
  return Array.from(root.querySelectorAll<HTMLButtonElement>('button')).find((b) => (b.textContent || '').trim() === label) || null;
}

/** The same, inside a card that may have gone by the time this poll runs. */
function buttonInside(selector: string, label: string): HTMLButtonElement | null {
  const root = document.querySelector(selector);
  return root ? buttonNamed(label, root) : null;
}

async function click(what: string, find: () => HTMLElement | null, timeoutMs = 4000): Promise<void> {
  const el = await waitFor('an enabled ' + what, timeoutMs, () => {
    const found = find();
    return found && !(found as HTMLButtonElement).disabled ? found : null;
  });
  el.click();
}

/** React tracks the value it last wrote, so a plain `el.value = x` is ignored: use the native setter. */
function typeInto(el: HTMLTextAreaElement | HTMLInputElement, text: string): void {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value')!.set!.call(el, text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

type Field = HTMLTextAreaElement | HTMLInputElement | HTMLSelectElement;

/** The control a `<label>` names, looked up inside `root`. Account never unmounts, so its
 * fields are in the DOM even while another view is on: a step scopes its lookups to the
 * pane it is driving rather than to the whole document. */
function fieldIn(root: ParentNode, label: string): Field | null {
  const found = Array.from(root.querySelectorAll('label')).find((l) => (l.textContent || '').trim() === label);
  const id = found ? found.getAttribute('for') : null;
  return id ? (document.getElementById(id) as Field | null) : null;
}

/** Is the pane App.tsx wraps this view in the one on show? A hidden pane keeps its DOM
 * (`visibility: hidden` + `aria-hidden`), so "gone" is never how a view closes, and
 * "present" is never proof that anybody can see it. */
function paneShows(selector: string): boolean {
  const el = document.querySelector(selector);
  const pane = el ? el.parentElement : null;
  return pane !== null && pane.getAttribute('aria-hidden') !== 'true';
}

function shownPane(selector: string): Element | null {
  return paneShows(selector) ? document.querySelector(selector) : null;
}

function lastReplyEl(): HTMLElement | null {
  const replies = document.querySelectorAll<HTMLElement>('.t2v-msg-assistant');
  return replies.length ? replies[replies.length - 1]! : null;
}

/** The Markdown a reply has actually drawn (never the "Working…" placeholder beside it). */
function drawnReplyText(): string {
  const el = lastReplyEl();
  const md = el ? el.querySelector('.t2v-md') : null;
  return md ? md.textContent || '' : '';
}

/**
 * Put the thread back on show. A step that failed part-way can leave the page on Settings
 * or Account, and every send after it would then type into a composer nobody can see —
 * one broken control reported as several.
 */
async function showThread(): Promise<void> {
  if (paneShows('.t2v-thread')) return;
  const pressed = document.querySelector<HTMLButtonElement>('.t2v-header button[aria-pressed="true"]');
  if (pressed && !pressed.disabled) {
    pressed.click();
    noteRecovery('closed ' + (pressed.getAttribute('aria-label') || 'a view') + ' to get back to the thread');
  }
  await waitFor('the thread pane on show', 5000, () => paneShows('.t2v-thread'));
}

/**
 * Get the controller back to idle after an earlier step left a run going or a card
 * waiting, so that ONE broken control is reported as one FAIL rather than as every step
 * after it. Stop is the control the end-user has, so try it first; `newSession` is the
 * server-side reset `clear()` honours even while busy, and it goes as a command because
 * the New chat BUTTON is disabled exactly when this is needed.
 */
async function recoverToIdle(store: PageStore): Promise<void> {
  const stop = buttonNamed('Stop');
  if (stop && !stop.disabled) {
    stop.click();
    noteRecovery('clicked Stop to end a turn an earlier step left running');
    if (await settle(5000, () => idle(state(store)), (v) => v)) return;
  }
  store.command({ type: 'newSession' });
  noteRecovery('sent newSession to clear a turn an earlier step left running');
  await settle(10000, () => idle(state(store)), (v) => v);
}

/** Type into the composer and click Send; resolves once the user message is in the state. */
async function send(store: PageStore, text: string): Promise<void> {
  await showThread();
  // The controller takes one turn at a time: a send into a busy one is refused with "The
  // assistant is still working on the last message." It ends a run (and clears its
  // pending approval) BEFORE it publishes the batch that says so, so the page's own view
  // of idle is a sound precondition — and a condition to poll, rather than a guessed wait.
  if (!idle(state(store))) await recoverToIdle(store);
  await waitFor('an idle chat', 10000, () => idle(state(store)));
  const before = state(store).messages.length;
  const input = await waitFor('the composer', 3000, () => document.querySelector<HTMLTextAreaElement>('.t2v-input'));
  input.focus();
  typeInto(input, text);
  await click('Send button', () => buttonNamed('Send'));
  await waitFor('the user message', 4000, () => state(store).messages.length > before);
}

function markdownDom(): { ok: boolean; detail: string } {
  const root = lastReplyEl();
  if (!root) return { ok: false, detail: 'no .t2v-msg-assistant in the DOM' };
  const missing = ['.t2v-md h2', '.t2v-md ol li', '.t2v-md table th', '.t2v-md pre code', '.t2v-md blockquote', '.t2v-md a[href]'].filter(
    (selector) => !root.querySelector(selector),
  );
  const td = root.querySelector('.t2v-md td');
  const pre = root.querySelector('.t2v-md pre');
  const border = td ? getComputedStyle(td).borderTopWidth : 'no td';
  const preBg = pre ? getComputedStyle(pre).backgroundColor : 'no pre';
  const styled = parseFloat(border) >= 1 && preBg === 'rgb(2, 28, 37)';
  return {
    ok: missing.length === 0 && styled,
    detail: (missing.length ? 'missing ' + missing.join(', ') : 'all six elements rendered') + '; td border ' + border + ', pre background ' + preBg,
  };
}

function park(report: AutoReport): void {
  sessionStorage.setItem(STATE_KEY, JSON.stringify(report));
}

function unpark(): AutoReport | null {
  try {
    const raw = sessionStorage.getItem(STATE_KEY);
    return raw ? (JSON.parse(raw) as AutoReport) : null;
  } catch {
    return null;
  }
}

/** The approval card's button for an action (data-action). */
function cardButton(action: string): HTMLButtonElement | null {
  return document.querySelector<HTMLButtonElement>('.t2v-approval [data-action="' + action + '"]');
}

/** The arguments editor, and the hints the card draws for the draft it currently holds.
 * Waiting for one of these is how a step knows the card has re-rendered for an edit
 * without asking the very button it is about to judge. */
function argsEditor(): HTMLTextAreaElement | null {
  const card = document.querySelector('.t2v-approval');
  return card ? (fieldIn(card, 'Arguments (JSON)') as HTMLTextAreaElement | null) : null;
}

function invalidHint(): Element | null {
  return document.querySelector('.t2v-approval .t2v-hint-error');
}

function editedHint(): Element | null {
  return Array.from(document.querySelectorAll('.t2v-approval .t2v-hint')).find(
    (el) => (el.textContent || '').indexOf('apply to this call only') !== -1,
  ) || null;
}

async function nextApproval(store: PageStore, text: string): Promise<void> {
  await send(store, text);
  await waitFor('the approval', 30000, () => state(store).pendingApproval);
  await waitFor('the approval card', 4000, () => cardButton('once'));
}

/**
 * Runs (or, after the scripted reload, resumes) the scenario. Never rejects: a failure
 * becomes a FAIL step. Resolves with the finished report. `onStep` sees the report after
 * every step, which is how the guard's watchdog still sends what the run got through when
 * the page dies or the whole self-test runs out of time (index.ts).
 *
 * `stopped` says the results have already been sent. A self-test that ran out of time
 * sends what it has and stops WAITING for this run, but cannot stop the run itself, so
 * the run asks: once it is true, every remaining step is skipped and, above all, the
 * scripted reload never happens. That reload would wipe the page the driver may still
 * need to read, and start the next load on a second report.
 */
export async function runAuto(
  store: PageStore,
  onStep?: (report: AutoReport) => void,
  stopped: () => boolean = () => false,
): Promise<AutoReport> {
  t0 = performance.now();
  // A fresh launch never resumes an older run's parked report. A new origin per launch
  // makes that theoretical, but a stale report would silently skip 16 of the 18 steps.
  if (FROM_FRAGMENT) {
    try {
      sessionStorage.removeItem(STATE_KEY);
    } catch {
      // nothing parked that could be read back anyway
    }
  }
  const parked = FROM_FRAGMENT ? null : unpark();
  const report: AutoReport = parked || {
    startedAt: new Date().toISOString(),
    finishedAt: null,
    reloads: 0,
    steps: [],
    beforeReload: null,
  };
  const note = (id: string, ok: boolean, detail: string) => {
    report.steps.push({
      id,
      status: ok ? 'PASS' : 'FAIL',
      detail: recovery ? '[recovered: ' + recovery + '] ' + detail : detail,
      ms: Math.round(performance.now() - t0),
    });
    recovery = '';
    if (onStep) onStep(report);
  };
  const step = async (id: string, body: () => Promise<[boolean, string]>) => {
    if (stopped()) return; // the report went home without this run: drive nothing more
    try {
      const [ok, detail] = await body();
      note(id, ok, detail);
    } catch (err) {
      note(id, false, String(err));
    }
  };

  if (!parked) {
    await step('auto:connected', async () => {
      await waitFor('the first snapshot', 10000, () => store.latest().seq !== null);
      return [true, store.latest().conn.mode];
    });

    // 1. The long reply: streamed, rendered as Markdown, with a link and Copy.
    let batchesAtStart = 0;
    let firstTextAt = 0;
    await step('auto:send', async () => {
      await send(store, 'hello');
      return [true, 'through the composer'];
    });
    await step('auto:stream', async () => {
      await waitFor('the first streamed text', 10000, () => {
        const m = lastAssistant(state(store));
        return m && m.content.length > 0;
      });
      batchesAtStart = store.latest().stats.batches;
      firstTextAt = performance.now();
      // What the page has DRAWN while the reply is still coming. Every other measure here
      // is the state the server sent, which a page that draws nothing until the turn ends
      // — or draws nothing at all — would satisfy just as well.
      const live = await settle(
        10000,
        () => {
          const m = lastAssistant(state(store));
          return { streaming: !!m && m.isStreaming === true, chars: drawnReplyText().length };
        },
        (r) => r.streaming && r.chars > 0,
      );
      const drew = live.streaming && live.chars > 0;
      const done = await waitFor('the reply to end', 60000, () => runEnded(state(store)));
      fullReplyLength = done.content.length;
      const batches = store.latest().stats.batches - batchesAtStart;
      const spread = Math.round(performance.now() - firstTextAt);
      return [
        done.content.length >= 400 && batches >= 10 && spread >= 300 && drew,
        String(done.content.length) + ' characters in ' + String(batches) + ' batches over ' + String(spread) + ' ms; ' +
          (drew ? String(live.chars) + ' characters drawn while still streaming' : 'NOTHING drawn while streaming'),
      ];
    });
    await step('auto:markdown-dom', async () => {
      // React commits a frame or more after the state says the run ended, and a slow,
      // software-rendered web view rather more than that: poll, and report the last
      // reading either way so a failure names the element that never arrived.
      const md = await settle(DRAW_MS, () => markdownDom(), (r) => r.ok);
      return [md.ok, md.ok ? md.detail : md.detail + ' (still missing after ' + String(DRAW_MS) + ' ms)'];
    });
    await step('auto:link', async () => {
      const at = location.href;
      let prevented: boolean | null = null;
      // Bubble phase on the document: React's own handler sits on the root container,
      // INSIDE this one, so by the time the click reaches here the page has had its say.
      // A capture-phase listener runs first and would always read false. Replies carry
      // target="_blank" (safeMarkdown.ts), so a missing preventDefault opens a window
      // this page cannot see, and `location.href` alone would never notice.
      const spy = (e: Event) => {
        prevented = e.defaultPrevented;
      };
      document.addEventListener('click', spy);
      try {
        await click('link in the reply', () => document.querySelector<HTMLElement>('.t2v-msg-assistant .t2v-md a[href="' + LINK + '"]'));
        // The link goes to the host as an `openLink` command, so its own answer is the
        // condition to wait on.
        await waitFor('the openLink command to be answered', 10000, () => answeredCommand(state(store), 'openLink'));
      } finally {
        document.removeEventListener('click', spy);
      }
      return [
        prevented === true && location.href === at,
        'defaultPrevented ' + String(prevented) + '; the page stayed at ' + location.pathname + '; the host got it through openLink',
      ];
    });
    await step('auto:copy', async () => {
      const reply = await waitFor('a reply to copy', DRAW_MS, () => lastReplyEl());
      await click('Copy button', () => buttonNamed('Copy', reply));
      await waitFor('the copy command to be answered', 10000, () => answeredCommand(state(store), 'copy'));
      return [true, 'clicked; the host clipboard is checked by the judge'];
    });

    // 2. The approval card.
    await step('auto:approval-card', async () => {
      await nextApproval(store, '/approve one');
      // The guard's tool is not destructive, so all four buttons are drawn and no warning is.
      const card = await settle(
        DRAW_MS,
        () => {
          const root = document.querySelector('.t2v-approval');
          return {
            actions: root ? Array.from(root.querySelectorAll('button[data-action]')).map((b) => b.getAttribute('data-action')) : [],
            warning: root ? root.querySelector('[role="alert"]') : null,
          };
        },
        (r) => r.actions.join(',') === 'once,always,deny,edit' && !r.warning,
      );
      const ok = card.actions.join(',') === 'once,always,deny,edit' && !card.warning;
      return [ok, 'buttons ' + card.actions.join(', ') + '; warning ' + JSON.stringify(card.warning ? card.warning.textContent : null)];
    });
    await step('auto:allow-once', async () => {
      await click('Allow once', () => cardButton('once'));
      const done = await waitFor('the allowed run to end', 30000, () => runEnded(state(store)));
      const stepUsed = !!done.steps && done.steps[0]!.status === 'used';
      const used = await settle(DRAW_MS, () => !!document.querySelector('.t2v-msg-assistant .t2v-tool[data-state="used"]'), (v) => v);
      return [
        stepUsed && used,
        'step ' + String(done.steps && done.steps[0]!.status) + '; USED row ' + String(used) +
          (used ? '' : ' (no .t2v-tool[data-state="used"] after ' + String(DRAW_MS) + ' ms)'),
      ];
    });
    await step('auto:deny', async () => {
      await nextApproval(store, '/approve two');
      await click('Deny', () => cardButton('deny'));
      const reason = await waitFor('the reason field', 3000, () => {
        const card = document.querySelector('.t2v-approval');
        return card ? (fieldIn(card, 'Reason for denying (optional, sent to the assistant)') as HTMLTextAreaElement | null) : null;
      });
      typeInto(reason, 'Wrong volume');
      await click('Confirm deny', () => cardButton('confirm-deny'));
      const done = await waitFor('the denied run to end', 30000, () => runEnded(state(store)));
      const denied = done.steps && done.steps[0];
      return [!!denied && denied.status === 'denied' && denied.result === 'Wrong volume', 'step ' + String(denied && denied.status) + ' / ' + String(denied && denied.result)];
    });
    await step('auto:edit-arguments', async () => {
      await nextApproval(store, '/approve three');
      await click('Edit arguments', () => cardButton('edit'));
      const editor = await waitFor('the arguments editor', 3000, () => argsEditor());
      // Each edit is followed by a wait for the card's OWN hint for the draft it now
      // holds — "Not valid JSON", then "your changes apply to this call only" — so the
      // button below is read from a card that has finished re-rendering, and never from
      // a guessed interval. The hints say nothing about the buttons: that is the assertion.
      typeInto(editor, '{"name": ');
      await waitFor('the invalid-JSON hint', DRAW_MS, () => invalidHint());
      const onceBlocked = !!cardButton('once')?.disabled;
      typeInto(editor, '{"name": "CTChest"}');
      await waitFor('the edited-arguments hint', DRAW_MS, () => editedHint());
      const alwaysOff = !!cardButton('always')?.disabled;
      await click('Discard edits', () => cardButton('edit'));
      await waitFor('the editor to close', DRAW_MS, () => argsEditor() === null);
      const alwaysBack = !cardButton('always')?.disabled;
      return [onceBlocked && alwaysOff && alwaysBack, 'invalid JSON blocks Allow once ' + String(onceBlocked) + '; edits turn Always allow off ' + String(alwaysOff) + '; discarding turns it back on ' + String(alwaysBack)];
    });
    await step('auto:always-allow', async () => {
      await click('Always allow', () => cardButton('always'));
      await waitFor('the always-allowed run to end', 30000, () => runEnded(state(store)));
      const remembered = state(store).alwaysAllowedTools.indexOf(TOOL) !== -1;
      return [remembered, 'alwaysAllowedTools ' + JSON.stringify(state(store).alwaysAllowedTools)];
    });

    // 3. Stop: while streaming, then while a card waits (= deny "Cancelled by the user").
    await step('auto:cancel-stream', async () => {
      await send(store, 'hello again');
      await waitFor('the first streamed text', 10000, () => {
        const m = lastAssistant(state(store));
        return m && m.content.length > 0;
      });
      await click('Stop button', () => buttonNamed('Stop'));
      await waitFor('the cancel command to be answered', 10000, () => answeredCommand(state(store), 'cancel'));
      const stopped = await waitFor('the run to stop', 10000, () => runEnded(state(store)));
      // A dead Stop leaves the reply to finish by itself, which ends the run just the
      // same: only the length tells the two apart. Three quarters of the whole reply is
      // far above anything a live Stop leaves behind (27-55 of 507 in practice) and far
      // below a reply that simply ran to the end.
      const ceiling = fullReplyLength > 0 ? Math.floor(fullReplyLength * 0.75) : 400;
      const cutShort = stopped.content.length < ceiling;
      const noStep = !stopped.steps || stopped.steps.length === 0;
      return [
        cutShort && noStep,
        'stopped after ' + String(stopped.content.length) + ' of ' + String(fullReplyLength) + ' characters (want under ' + String(ceiling) + '), no tool step ' + String(noStep),
      ];
    });
    await step('auto:cancel-approval', async () => {
      await nextApproval(store, '/approve four');
      await click('Stop button', () => buttonNamed('Stop'));
      const done = await waitFor('the cancelled run to end', 30000, () => runEnded(state(store)));
      const denied = done.steps && done.steps[0];
      return [!!denied && denied.status === 'denied' && denied.result === 'Cancelled by the user', 'step ' + String(denied && denied.status) + ' / ' + String(denied && denied.result)];
    });

    // 4. The error card: Try again, then Dismiss.
    await step('auto:error-retry', async () => {
      await send(store, '/fail please');
      await waitFor('the error card', 30000, () => document.querySelector('.t2v-error'));
      const users = state(store).messages.filter((m) => m.role === 'user').length;
      await click('Try again', () => buttonInside('.t2v-error', 'Try again'));
      // "Try again" answers with a `reload` command, so in its own task nothing has
      // changed yet and the failure still on screen would satisfy "it failed" at once —
      // the step would then prove nothing about the button. Watch the retried run START
      // (the controller drops the failed turn and sends the same user message again,
      // which clears `error`) before waiting for it to fail a second time.
      await waitFor('the retried run to start', 15000, () => state(store).error === null);
      await waitFor('the retried run to fail again', 30000, () => {
        const s = state(store);
        return s.error !== null && !s.isLoading && document.querySelector('.t2v-error') !== null;
      });
      const same = state(store).messages.filter((m) => m.role === 'user').length === users;
      await click('Dismiss', () => buttonInside('.t2v-error', 'Dismiss'));
      await waitFor('the error to clear', 10000, () => state(store).error === null && !document.querySelector('.t2v-error'));
      return [same, 'retried in place (' + String(users) + ' user messages), then dismissed'];
    });

    // 5. Settings and Account open, render and refuse sensibly (no account behind
    //    FakeController). Signed out, Settings hides the model picker and the whole
    //    skills section behind one hint line and keeps only the text size, which is a
    //    local preference — so that, not a disabled picker, is what "it renders" means
    //    here; the size itself still needs an account, and the page says so.
    await step('auto:views', async () => {
      await click('Settings', () => buttonNamed('Settings'));
      const settings = await waitFor('the Settings view', 4000, () => document.querySelector('.t2v-settings'));
      const shape = await settle(
        DRAW_MS,
        () => ({
          hinted: Array.from(settings.querySelectorAll('.t2v-hint')).some((p) => (p.textContent || '').indexOf('Sign in to choose a model and skills') !== -1),
          selects: settings.querySelectorAll('select').length,
        }),
        (r) => r.hinted && r.selects === 1,
      );
      const size = await waitFor('the Text size select', 3000, () => fieldIn(settings, 'Text size') as HTMLSelectElement | null);
      size.value = 'large';
      size.dispatchEvent(new Event('change', { bubbles: true }));
      // The refusal is read from the Settings view's OWN notice line, not from whichever
      // `.t2v-notice` the document happens to hold first — the hidden thread has one too.
      const notice = await waitFor('the refusal notice in Settings', 15000, () => settings.querySelector('.t2v-notice .t2v-notice-text'));
      const noticeText = notice.textContent || '';

      await click('Account', () => buttonNamed('Account'));
      const account = await waitFor('the Account view on show', 4000, () => shownPane('.t2v-account'));
      const email = await waitFor('the email field', 3000, () => fieldIn(account, 'Email') as HTMLInputElement | null);
      const password = await waitFor('the password field', 3000, () => fieldIn(account, 'Password') as HTMLInputElement | null);
      typeInto(email, 'ana@example.com');
      typeInto(password, 'pw');
      await click('Sign in', () => account.querySelector<HTMLButtonElement>('button[type="submit"]'));
      const error = await waitFor('the sign-in error', 15000, () => account.querySelector('[role="alert"]'));
      const errorText = error.textContent || '';

      // Back to the chat. Account never unmounts either, so the pane is HIDDEN rather
      // than gone: that is what closing a view looks like in this DOM.
      await click('Account (back to the chat)', () => buttonNamed('Account'));
      await waitFor('the thread back on show', 4000, () => paneShows('.t2v-thread') && !paneShows('.t2v-account'));
      await click('Dismiss (the notice)', () => buttonInside('.t2v-notice', 'Dismiss'));
      await waitFor('the notice to go', 4000, () => !document.querySelector('.t2v-notice'));

      const ok = shape.hinted && shape.selects === 1 && noticeText.indexOf('not connected') !== -1 && errorText.indexOf('not connected') !== -1;
      return [
        ok,
        'signed out: ' + String(shape.selects) + ' select(s), sign-in hint ' + String(shape.hinted) + '; text size refused with ' + JSON.stringify(noticeText) + '; sign-in says ' + JSON.stringify(errorText),
      ];
    });

    // 6. New chat.
    await step('auto:new-chat', async () => {
      await click('New chat', () => buttonNamed('New chat'));
      await waitFor('an empty thread', 10000, () => state(store).messages.length === 0 && !state(store).isLoading && document.querySelector('.t2v-welcome'));
      return [true, 'the thread is empty and the welcome is back'];
    });

    // 7. Reload mid-stream. Everything after this runs in the NEXT page load. An orphaned
    // run (its results already went home) must not even send the message: it would
    // land in the chat after the report and could read as a content problem.
    try {
      if (stopped()) {
        console.info(STOPPED_LINE);
        return report;
      }
      await send(store, 'hello after the reload');
      await waitFor('the stream to start', 10000, () => {
        const m = lastAssistant(state(store));
        return m && m.isStreaming === true && m.content.length > 0;
      });
      if (stopped()) {
        console.info(STOPPED_LINE);
        return report;
      }
      report.reloads += 1;
      report.beforeReload = {
        errorCount: monitors.errorCount,
        errors: monitors.errors.slice(),
        cspViolations: monitors.cspViolations,
        gaps: store.latest().stats.gaps,
        messageCount: state(store).messages.length,
      };
      park(report);
      location.reload();
      return await new Promise<AutoReport>(() => undefined); // the page is going away
    } catch (err) {
      note('auto:reload', false, 'before the reload: ' + String(err));
    }
  } else {
    await step('auto:reload', async () => {
      const expected = report.beforeReload ? report.beforeReload.messageCount : 0;
      await waitFor('the snapshot after the reload', 10000, () => store.latest().seq !== null && state(store).messages.length >= expected);
      const resumed = await waitFor('the reloaded run to end', 60000, () => runEnded(state(store)));
      // A snapshot restoring the conversation is not the page having drawn it: a thread
      // that renders no messages at all after a reload satisfies every state-only check.
      const dom = await settle(
        DRAW_MS,
        () => ({ rows: document.querySelectorAll('.t2v-msg').length, text: drawnReplyText() }),
        (r) => r.rows >= expected && r.text.indexOf('Unicode check') !== -1,
      );
      const drawn = dom.rows >= expected && dom.text.indexOf('Unicode check') !== -1;
      return [
        resumed.content.indexOf('Unicode check') !== -1 && drawn,
        String(state(store).messages.length) + ' of ' + String(expected) + ' messages restored; the stream ran on to ' + String(resumed.content.length) +
          ' characters; the thread drew ' + String(dom.rows) + ' message rows and ' + String(dom.text.length) + ' characters of reply',
      ];
    });
  }

  // 8. Last, because nothing lifts it behind FakeController: a guest runs out of allowance.
  await step('auto:guest-limit', async () => {
    await send(store, '/limit');
    // On the SHOWN Account pane: the gate's whole point is that the page puts Account in
    // front of the end-user. Account never unmounts, and it is given `reason` whether it
    // is on show or not, so a document-wide query finds this line on a page that never
    // opened the gate at all.
    const reason = await waitFor('the guest-limit reason on the Account view', 15000, () => {
      const account = shownPane('.t2v-account');
      return account ? account.querySelector('.t2v-reason') : null;
    });
    const threadGone = !paneShows('.t2v-thread');
    const said = reason.textContent || '';
    return [said.indexOf('guest limit') !== -1 && threadGone, JSON.stringify(said) + '; the thread pane is hidden ' + String(threadGone)];
  });

  report.finishedAt = new Date().toISOString();
  if (onStep) onStep(report);
  try {
    sessionStorage.removeItem(STATE_KEY);
    sessionStorage.removeItem(AUTO_KEY);
  } catch {
    // nothing to clear
  }
  return report;
}
