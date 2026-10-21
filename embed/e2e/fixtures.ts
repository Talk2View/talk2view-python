/**
 * One real UI server per test (e2e/backend.py), so no test sees another's chat. The page
 * is the production build in ../src/talk2view/ui/static (`npm run build`).
 *
 * A test that fails gets backend.py's exit status and the tail of its stderr in its
 * output (`BackendLog`): a backend that crashed mid-test otherwise surfaces only as
 * whatever locator timed out after it. `T2V_E2E_DEBUG=1` also streams that stderr live.
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import { test as base, expect, type Page } from '@playwright/test';

const here = (path: string) => fileURLToPath(new URL(path, import.meta.url));
export const STATIC_DIR = here('../../src/talk2view/ui/static');

export interface HostCall {
  /** `skills_rescan`: the host's own rescan of its skill folders (`on_skills_list`,
   * router.py), fired every time `skillsList` reaches an account. An `open_external` of a
   * desktop sign-in's start URL is recorded as `<desktop sign-in>`, never the URL: it
   * holds a one-time handle. */
  host: 'open_external' | 'clipboard_set' | 'skills_rescan';
  value: string;
}

export interface Backend {
  url: string;
  /** What the page asked the host to do, in order. */
  hostCalls: HostCall[];
  /**
   * Which controller calls the page's commands reached, in order (fake mode). A click the
   * thread draws the same way either way — "Try again" on a turn that fails again, a
   * second opener click the controller refused — is visible only here.
   */
  controllerCalls: string[];
  /** Every turn timing the server delivered (account mode): `TurnTiming.as_dict()`. */
  turns: Array<Record<string, unknown>>;
  /** Every `POST /v1/audio/transcriptions` the account engine answered (account mode),
   * in order: `{model, filename, content_type, size, user, language?}`. */
  transcriptions: Array<Record<string, unknown>>;
  /** Send backend.py one control line (its `controls`), such as `attachments-off`. */
  control(line: string): void;
}

/** One line of backend.py's output: the URL, a host call, a controller call, a turn timing
 * or a transcription. */
interface BackendRecord {
  url?: string;
  host?: HostCall['host'];
  value?: string;
  controller?: string;
  turn?: Record<string, unknown>;
  transcription?: Record<string, unknown>;
}

/** How much of backend.py's stderr a failed test keeps: its last lines, bounded both ways. */
const STDERR_LINES = 200;
const STDERR_CHARS = 32768;

/** What backend.py wrote to stderr (the last `STDERR_LINES` lines), and how it ended. */
class BackendLog {
  private lines: string[] = [];
  private chars = 0;
  /** Set once the process has ended, with `code`/`signal` as Node reports them. */
  ended: { code: number | null; signal: NodeJS.Signals | null; beforeTeardown: boolean } | null = null;

  add(line: string): void {
    this.lines.push(line);
    this.chars += line.length + 1;
    while (this.lines.length > STDERR_LINES || (this.chars > STDERR_CHARS && this.lines.length > 1)) {
      this.chars -= this.lines.shift()!.length + 1;
    }
  }

  status(): string {
    if (!this.ended) return 'backend.py was still running.';
    const how = this.ended.signal ? 'signal ' + this.ended.signal : 'exit code ' + String(this.ended.code);
    const when = this.ended.beforeTeardown ? 'while the test was still using it' : 'after the test closed its stdin';
    return 'backend.py ended with ' + how + ', ' + when + '.';
  }

  report(): string {
    const tail = this.lines.length > 0 ? this.lines.join('\n') : '(nothing)';
    return this.status() + '\nbackend.py stderr, last ' + String(this.lines.length) + ' lines:\n' + tail + '\n';
  }
}

export const test = base.extend<{ backendArgs: string[]; backend: Backend }>({
  backendArgs: [[], { option: true }],
  backend: async ({ backendArgs }, use, testInfo) => {
    const child: ChildProcess = spawn(
      'uv',
      // `--extra dev`: account mode needs pytest-httpserver, and a project synced without
      // the extras would otherwise fail only once a spec asked for that mode.
      ['run', '--project', here('../..'), '--extra', 'dev', 'python', here('./backend.py'), '--static-dir', STATIC_DIR, ...backendArgs],
      { stdio: ['pipe', 'pipe', 'pipe'] },
    );
    const log = new BackendLog();
    let tearingDown = false;
    // `close`, not `exit`: it fires only once stderr is drained too, so the tail is whole.
    const closed = new Promise<void>((resolve) => {
      child.once('close', (code: number | null, signal: NodeJS.Signals | null) => {
        log.ended = { code, signal, beforeTeardown: !tearingDown };
        resolve();
      });
    });
    createInterface({ input: child.stderr! }).on('line', (line) => {
      log.add(line);
      if (process.env.T2V_E2E_DEBUG) process.stderr.write(line + '\n');
    });
    // Closing stdin on a backend that has already died must not raise an unhandled EPIPE.
    child.stdin!.on('error', () => undefined);

    const hostCalls: HostCall[] = [];
    const controllerCalls: string[] = [];
    const turns: Array<Record<string, unknown>> = [];
    const transcriptions: Array<Record<string, unknown>> = [];
    const url = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('backend.py printed no URL in 30 s.\n' + log.report())), 30000);
      void closed.then(() => {
        clearTimeout(timer);
        reject(new Error('backend.py ended before it printed its URL.\n' + log.report()));
      });
      createInterface({ input: child.stdout! }).on('line', (line) => {
        let record: BackendRecord;
        try {
          record = JSON.parse(line) as BackendRecord;
        } catch {
          return; // not one of backend.py's records: a warning, a traceback, uv's own output
        }
        if (record.url) {
          clearTimeout(timer);
          resolve(record.url);
        } else if (record.host) {
          hostCalls.push({ host: record.host, value: String(record.value) });
        } else if (record.controller) {
          controllerCalls.push(record.controller);
        } else if (record.turn) {
          turns.push(record.turn);
        } else if (record.transcription) {
          transcriptions.push(record.transcription);
        }
      });
    });
    await use({ url, hostCalls, controllerCalls, turns, transcriptions, control: (line) => void child.stdin!.write(line + '\n') });

    tearingDown = true;
    child.stdin!.end(); // backend.py stops when stdin closes
    if (log.ended === null) {
      const kill = setTimeout(() => child.kill('SIGKILL'), 5000);
      await closed;
      clearTimeout(kill);
    }
    if (testInfo.status !== testInfo.expectedStatus) {
      const report = log.report();
      // The attachment is for the HTML and JSON reports (the list reporter prints only its
      // first 300 characters, which is why the status comes first); the stderr write is
      // what the list reporter prints in full, under this test's title.
      await testInfo.attach('backend.py', { body: report, contentType: 'text/plain' });
      process.stderr.write(report);
    }
  },
});

export { expect };

/**
 * Type into the composer and press Enter, as a person would. Waits for Send first
 * (Composer.tsx renders it only while `!thread.isRunning`, Stop otherwise): a call right
 * as an earlier turn is ending can otherwise race the composer's re-enable, where
 * assistant-ui's own Enter handler skips `preventDefault()` while still `isRunning` and
 * inserts a newline instead of submitting (reproduced at
 * roughly 13% of runs of one spec before this wait was added).
 */
export async function sendMessage(page: Page, text: string): Promise<void> {
  await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
  const input = page.getByRole('textbox', { name: 'Message' });
  await input.fill(text);
  await input.press('Enter');
}

/** How late `holdCommands` answers: a slow runner's round trip, on every run, so a test
 * that fast-forwards `page.clock` before the page has its 202 fails here, not in CI. */
export const HELD_ANSWER_MS = 250;

/**
 * Fulfil the page's commands of `type` with 202 (after `HELD_ANSWER_MS`) without forwarding
 * them, as though each was queued and its result then lost, and return what the page posted.
 * Install it only once the page has settled. The page arms its 15 s command abort on
 * `page.clock` too, whenever the clock was installed: await `commandAnswered` before
 * fast-forwarding past it, or the jump aborts a POST still in flight.
 */
export async function holdCommands(page: Page, type: string): Promise<Array<Record<string, unknown>>> {
  const held: Array<Record<string, unknown>> = [];
  await page.route('**/command', async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown> | null;
    if (body?.type === type) {
      held.push(body);
      await new Promise((resolve) => setTimeout(resolve, HELD_ANSWER_MS));
      await route.fulfill({ status: 202 });
      return;
    }
    await route.continue();
  });
  return held;
}

/**
 * Resolves once the page has the 202 for its `type` command and has run what follows it, so
 * `page.clock` can then be fast-forwarded past the page's 15 s command abort. Start waiting
 * BEFORE the action that posts the command, and await it before the first fast-forward.
 */
export function commandAnswered(page: Page, type: string): Promise<void> {
  return (
    page
      .waitForResponse((response) => {
        if (!response.url().endsWith('/command')) return false;
        try {
          return (response.request().postDataJSON() as { type?: string } | null)?.type === type;
        } catch {
          return false;
        }
      })
      // One more round trip into the page: its fetch has resolved and cleared the abort.
      .then(() => page.evaluate(() => undefined))
  );
}

/** POST a command to the server from outside the page, with the page's own token. */
export async function postToServer(page: Page, url: string, body: Record<string, unknown>): Promise<void> {
  const token = await page.evaluate(() => sessionStorage.getItem('t2v_ui_token'));
  const res = await page.request.post(new URL('/command', url).href, {
    headers: { Authorization: 'Bearer ' + String(token) },
    data: body,
  });
  expect(res.status()).toBe(202);
}

/**
 * Fill the Account view's sign-in form and press Sign in (the view must already be open).
 * Moved from account.spec.ts, whose tests also sign in with a wrong password.
 */
export async function fillSignIn(page: Page, email: string, password: string): Promise<void> {
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
}

/** From the thread: open Account, sign in as ana@example.com, and wait to be back on the
 * thread (account mode). Moved from skills.spec.ts, shared with its "asking the assistant". */
export async function signIn(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await fillSignIn(page, 'ana@example.com', 'pw');
  await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
}

/** The newest assistant message. */
export function lastReply(page: Page) {
  return page.locator('.t2v-msg-assistant').last();
}
