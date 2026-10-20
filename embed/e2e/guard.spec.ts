/**
 * The runtime guard's own page (dist-guard, `npm run build:guard`) in a modern Chromium:
 * the same probes and scripted run the Slicer driver judges on Chromium 83 in CI, and the
 * same way home for the results (the `copy` command to the host).
 */
import { fileURLToPath } from 'node:url';
import type { Page } from '@playwright/test';
import { expect, test } from './fixtures';

const GUARD_DIR = fileURLToPath(new URL('../dist-guard', import.meta.url));
const MARKER = 't2v-guard-results:';
/** What the scripted run logs when it stops because its results already went home. */
const STOPPED = 't2v-guard: the results already went home';

const onWindow = (page: Page) => page.evaluate(() => (window as { __t2vGuardResults?: string }).__t2vGuardResults);
const reports = (backend: { hostCalls: { value: string }[] }) => backend.hostCalls.filter((c) => c.value.startsWith(MARKER));

test.use({ backendArgs: ['--static-dir', GUARD_DIR] });
test.setTimeout(180000);

test('the scripted run passes every probe and every step', async ({ page, backend }) => {
  await page.goto(backend.url + '&auto=1');
  await expect.poll(() => backend.hostCalls.some((c) => c.value.startsWith(MARKER)), { timeout: 150000 }).toBe(true);
  const results = JSON.parse(backend.hostCalls.find((c) => c.value.startsWith(MARKER))!.value.slice(MARKER.length));

  const failedProbes = results.probes.filter((p: { status: string }) => p.status === 'FAIL');
  expect(failedProbes).toEqual([]);
  // A guard that gave up sends `auto: null` with the reason in `live.errors`; say so here
  // rather than let the next line fail as a bare TypeError.
  expect(results.auto, 'the guard never ran the script: ' + JSON.stringify(results.live.errors)).not.toBeNull();
  const steps = results.auto.steps.map((s: { id: string; status: string; detail: string }) => [s.id, s.status, s.detail]);
  expect(steps.filter((s: string[]) => s[1] !== 'PASS')).toEqual([]);
  expect(steps.map((s: string[]) => s[0])).toEqual([
    'auto:connected', 'auto:send', 'auto:stream', 'auto:markdown-dom', 'auto:link', 'auto:copy',
    'auto:approval-card', 'auto:allow-once', 'auto:deny', 'auto:edit-arguments', 'auto:always-allow',
    'auto:cancel-stream', 'auto:cancel-approval', 'auto:error-retry', 'auto:views', 'auto:new-chat',
    'auto:reload', 'auto:guest-limit',
  ]);
  // `live` counts THIS page load only; the first load's counters are the reload's own, and
  // an error that happens before the reload is visible nowhere else.
  expect(results.live.errors).toEqual([]);
  expect(results.live.errorCount).toBe(0);
  expect(results.auto.beforeReload.errors).toEqual([]);
  expect(results.auto.beforeReload).toMatchObject({ errorCount: 0, cspViolations: 0, gaps: 0 });
  expect(backend.hostCalls).toContainEqual({ host: 'open_external', value: 'https://slicer.readthedocs.io/en/latest/' });
  expect(backend.hostCalls.some((c) => c.host === 'clipboard_set' && c.value.startsWith('## Segmentation plan'))).toBe(true);

  // The driver's last resort when no command reaches the host: the same payload, left on
  // the window for it to read with the web view's own JS evaluation. It is written before
  // the handover, so by the time results have arrived it is always already there.
  const sent = reports(backend);
  expect(sent).toHaveLength(1); // exactly one report: the driver judges the first
  expect(await onWindow(page)).toBe(sent[0]!.value);

  // A reload starts a new document, and the window copy goes with the old one. The load
  // that finds the report already sent puts it back, and runs nothing.
  const sends = backend.controllerCalls.length;
  await page.reload();
  await page.waitForFunction(() => (window as { __t2vGuardResults?: string }).__t2vGuardResults !== undefined, null, { timeout: 10000 });
  expect(await onWindow(page)).toBe(sent[0]!.value);
  await page.waitForTimeout(1000); // a run that had started again would have sent by now
  expect(reports(backend)).toHaveLength(1);
  expect(backend.controllerCalls).toHaveLength(sends);
});

test('a run that outlives its budget sends one report and stops instead of reloading', async ({ page, backend }) => {
  // The real path, not a planted flag: the self-test runs out of time mid-run and sends
  // what it has, while the scripted run it gave up waiting on carries on. That run must
  // not reload the page (it would wipe the window copy and start a second report). The
  // override exists for this spec only and can only shorten the budget (src/guard/index.ts).
  await page.addInitScript(() => {
    (window as { __t2vGuardTestBudgetMs?: number }).__t2vGuardTestBudgetMs = 3000;
  });
  let loads = 0;
  page.on('load', () => {
    loads += 1;
  });
  const stopped = page.waitForEvent('console', { predicate: (m) => m.text().includes(STOPPED), timeout: 60000 });
  await page.goto(backend.url + '&auto=1');
  await stopped;
  await expect.poll(() => reports(backend).length, { timeout: 20000 }).toBe(1);

  const sent = reports(backend)[0]!.value;
  const results = JSON.parse(sent.slice(MARKER.length));
  expect(results.live.errors.join(' | ')).toContain('did not finish within'); // the budget really ran out
  expect(loads).toBe(1); // the scripted reload never happened
  expect(await onWindow(page)).toBe(sent);
  expect(reports(backend)).toHaveLength(1);
  // Nor did the message that would have started it. The orphaned run stops before
  // the reload block's send, so nothing lands in the chat after the report went home.
  await expect(page.locator('.t2v-msg-user .t2v-bubble', { hasText: 'hello after the reload' })).toHaveCount(0);
});

test('a fresh launch starts a new run even where an earlier one already sent its report', async ({ page, backend }) => {
  // What a second `&auto=1` navigation in the same web view finds: the last run's flag
  // and report. Planted once, before the first load only.
  await page.addInitScript(() => {
    try {
      if (sessionStorage.getItem('planted') === null) {
        sessionStorage.setItem('planted', '1');
        sessionStorage.setItem('t2v_guard_sent', '1');
        sessionStorage.setItem('t2v_guard_results', 't2v-guard-results:{"stale":true}');
      }
    } catch {
      // without storage there is nothing stale to find
    }
  });
  await page.goto(backend.url + '&auto=1');
  await expect.poll(() => backend.controllerCalls.includes('send'), { timeout: 20000 }).toBe(true);
  expect(await onWindow(page)).toBeUndefined(); // the stale report was forgotten, never restored
});

test('a load that finds its report already sent runs nothing and puts the report back', async ({ page, backend }) => {
  // The load a reload racing the handover would start: the run is still marked as in
  // progress, but its report is out. No fragment, so nothing clears the flag.
  await page.addInitScript(() => {
    try {
      sessionStorage.setItem('t2v_guard_auto', '1');
      sessionStorage.setItem('t2v_guard_sent', '1');
      sessionStorage.setItem('t2v_guard_results', 't2v-guard-results:{"schema":1}');
    } catch {
      // the assertions below say what storage there was
    }
  });
  await page.goto(backend.url);
  await expect(page.locator('.t2v-input')).toBeVisible();
  expect(await onWindow(page)).toBe('t2v-guard-results:{"schema":1}');
  await page.waitForTimeout(3000); // a resumed run would have sent 'hello' long before now
  expect(backend.controllerCalls).toEqual([]);
  expect(reports(backend)).toEqual([]);
});

test('a page that never draws a thread still sends its results home', async ({ page, backend }) => {
  // The likeliest shape of a Chromium 83 breakage is a throw while the page renders, and
  // the guard's own monitors are then the only witness. Breaking `<textarea>` creation
  // stops the composer mounting, which is what the guard waits for: without a watchdog it
  // would wait for ever and CI would report nothing but its own timeout.
  await page.addInitScript(() => {
    const real = document.createElement.bind(document);
    document.createElement = ((tag: string, ...rest: unknown[]) => {
      if (String(tag).toLowerCase() === 'textarea') throw new Error('guard-watchdog-probe: textarea is broken');
      return real(tag, ...(rest as []));
    }) as typeof document.createElement;
  });
  await page.goto(backend.url + '&auto=1');
  await expect.poll(() => backend.hostCalls.some((c) => c.value.startsWith(MARKER)), { timeout: 120000 }).toBe(true);
  const results = JSON.parse(backend.hostCalls.find((c) => c.value.startsWith(MARKER))!.value.slice(MARKER.length));

  // The same shape the guard's judge already reads: no second format to teach it.
  expect(results.schema).toBe(1);
  expect(results.probes).toEqual([]);
  expect(results.auto).toBeNull();
  expect(results.live.errorCount).toBeGreaterThan(0);
  expect(results.live.errors.join(' | ')).toContain('never drew a thread');
});
