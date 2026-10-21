import type { Page } from '@playwright/test';
import { commandAnswered, expect, holdCommands, lastReply, sendMessage, test } from './fixtures';

// ── Helpers shared by the tests below ──

/** Sign in to the account-mode backend the way account.spec.ts does. */
async function signInAccount(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await page.getByLabel('Email').fill('ana@example.com');
  await page.getByLabel('Password').fill('pw');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
}

interface ApproveBody {
  id?: string;
  type?: string;
  toolCallId?: string;
  action?: string;
  feedback?: string;
  updatedInput?: unknown;
}

/** Every `approve` command POSTed to `/command`, in order, as the SERVER actually saw it —
 * the ground truth for "what did the wire carry", not just what the UI shows. */
function captureApprovals(page: Page): ApproveBody[] {
  const captured: ApproveBody[] = [];
  page.on('request', (request) => {
    if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
    const body = request.postDataJSON() as ApproveBody | null;
    if (body?.type === 'approve') captured.push(body);
  });
  return captured;
}

test.describe('the approval card (client tool, FakeController)', () => {
  test('shows the tool, its description and arguments, and no warning for a tool that is not destructive', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    const card = page.locator('.t2v-approval');
    await expect(card).toContainText('Run load_sample_data');
    await expect(card).toContainText('Load the MRHead sample volume into the scene?');
    await expect(card.locator('pre')).toContainText('"name": "MRHead"');
    await expect(card.getByRole('alert')).toHaveCount(0);
    await expect(card.getByRole('button')).toHaveText(['Edit', 'Allow once', 'Always allow', 'Deny']);
    // Stop is offered while the card waits: a turn waiting on an approval is running.
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible();
  });

  test('Allow once runs the tool and the reply continues', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page).locator('.t2v-tool[data-state="used"]')).toContainText('Used load_sample_data');
    await expect(lastReply(page)).toContainText('MRHead is loaded.');
  });

  test('Deny sends the reason', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('Wrong volume');
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    const step = lastReply(page).locator('.t2v-tool[data-state="denied"]');
    await expect(step).toContainText('Denied load_sample_data');
    await step.getByRole('button').click();
    await expect(step.locator('.t2v-tool-result')).toHaveText('Wrong volume');
    await expect(lastReply(page)).toContainText('Understood: nothing was loaded.');
  });

  test('Stop on a waiting card denies it as cancelled', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await page.getByRole('button', { name: 'Stop' }).click();
    const step = lastReply(page).locator('.t2v-tool[data-state="denied"]');
    await step.getByRole('button').click();
    await expect(step.locator('.t2v-tool-result')).toHaveText('Cancelled by the user');
  });

  test('edited arguments: Always allow is off, invalid JSON blocks Allow once', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    const editor = page.getByLabel('Arguments (JSON)');
    await editor.fill('{"name": "CTChest"}');
    await expect(page.getByRole('button', { name: 'Always allow' })).toBeDisabled();
    await expect(page.getByText('Edits apply to this call only')).toBeVisible();
    await editor.fill('{"name": ');
    await expect(page.getByText('Not valid JSON — fix it or deny.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Allow once' })).toBeDisabled();
    await page.getByRole('button', { name: 'Discard edits' }).click();
    await expect(page.getByRole('button', { name: 'Always allow' })).toBeEnabled();
  });

  // While an approval is waiting,
  // the live region must not say "Working…" — that copy belongs to a running tool call,
  // not a decision only the end-user can make. It must instead say the assistant is
  // asking for permission, so a screen reader user is not left thinking nothing is
  // happening. See ui/Thread.tsx's LiveRegion.
  test('the live region announces that the assistant is asking for permission, not "Working…"', async ({ page, backend }) => {
    await page.goto(backend.url);
    // Scoped to Thread's own top-level region, a DIRECT child of .t2v-thread: Account.tsx's
    // sign-in form has one too, unrelated to the thread's own announcements, and
    // Composer.tsx's own attachment-upload region is nested inside .t2v-thread as well.
    const live = page.locator('.t2v-thread > .t2v-sr-only');
    await sendMessage(page, '/approve load it');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await expect(live).toHaveText('The assistant wants to run load_sample_data and is waiting for your permission.');
  });
});

/**
 * The same scripted approval, with the host marking the tool destructive. "Always allow"
 * is never offered then: one click would let the tool run unasked for the rest of the
 * conversation (a capture tool would go on uploading slice views). The rule itself is
 * `mayAlwaysAllow`, unit-tested; these two prove the card obeys it and still warns.
 */
/**
 * What "Always allow" owes the end-user: the decision is answered once and the turn goes
 * on. Whether the NEXT call is skipped is the host's to honour — `FakeController` records
 * `alwaysAllowedTools` and asks again anyway, and the 3D Slicer extension's host answers every
 * `always` as `once` — so the proof that the page stored it is the runtime guard's
 * `auto:always-allow` step, which reads `alwaysAllowedTools` off the page state. Still
 * open: the wire needs a flag for a host that will not remember, and the fake
 * should honour its own list.
 */
test.describe('Always allow', () => {
  test.use({ backendArgs: ['--chunk-delay', '0'] });

  test('answers the card once and the turn finishes', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Always allow' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page)).toContainText('MRHead is loaded.');
    await expect(lastReply(page).locator('.t2v-tool[data-state="used"]')).toHaveCount(1);
    // The fake asks again on the next call: that is the fake, not the page.
    await sendMessage(page, '/approve load it again');
    await expect(page.locator('.t2v-approval')).toBeVisible();
  });
});

test.describe('a destructive tool', () => {
  test.use({ backendArgs: ['--destructive-tool'] });

  test('warns and offers no “Always allow”', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    const card = page.locator('.t2v-approval');
    await expect(card.getByRole('alert')).toHaveText('This will modify your work: Loading sample data. Review before approving.');
    await expect(card.getByRole('button')).toHaveText(['Edit', 'Allow once', 'Deny']);
    await expect(page.getByRole('button', { name: 'Always allow' })).toHaveCount(0);
    // Editing arguments says nothing about a button that is not there.
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    await page.getByLabel('Arguments (JSON)').fill('{"name": "CTChest"}');
    await expect(page.getByText('Edits apply to this call only')).toHaveCount(0);
    // Allow once still runs it, and the next call asks again (nothing was remembered).
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(lastReply(page).locator('.t2v-tool[data-state="used"]')).toContainText('Used load_sample_data');
    await expect(lastReply(page)).toContainText('MRHead is loaded.'); // the turn is over: the composer takes a message again
    await sendMessage(page, '/approve load it again');
    await expect(page.locator('.t2v-approval')).toBeVisible();
  });
});

test.describe('a destructive tool with the host’s own warning', () => {
  test.use({ backendArgs: ['--destructive-tool', 'Deletes the volume from the scene.'] });

  test('shows that warning instead of the page’s', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    const card = page.locator('.t2v-approval');
    await expect(card.getByRole('alert')).toHaveText('Deletes the volume from the scene.');
    await expect(page.getByRole('button', { name: 'Always allow' })).toHaveCount(0);
  });
});

test.describe('an engine approval (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('offers Allow and Deny only, and shows the engine details', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await sendMessage(page, 'email the results');
    const card = page.locator('.t2v-approval[data-kind="engine"]');
    await expect(card).toContainText('Run gmail_send_email');
    await expect(card).toContainText('Send an email to alex@example.com');
    await expect(card.locator('dt')).toHaveText(['To', 'Subject']);
    await expect(card.locator('dd')).toHaveText(['alex@example.com', 'Scan results']);
    await expect(card.getByRole('button')).toHaveText(['Allow', 'Deny']);
    await card.getByRole('button', { name: 'Allow' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page)).toContainText('done');
  });

  // "Always allow" is only for
  // client-tool approvals. There is no such button on an engine approval's card (proved
  // above), but that alone does not prove the page's own approve handler would refuse to
  // SEND `always` if something else ever called it (a future control, a stray keyboard
  // shortcut, a bug). This watches the wire itself: whatever is clicked, an engine
  // approval never posts `action: "always"` to the server.
  test('never sends "always" for an engine approval, whatever is clicked', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    const approveActions: unknown[] = [];
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const body = request.postDataJSON() as { type?: string; action?: unknown } | null;
      if (body?.type === 'approve') approveActions.push(body.action);
    });
    await sendMessage(page, 'email the results');
    const card = page.locator('.t2v-approval[data-kind="engine"]');
    await expect(card.getByRole('button')).toHaveText(['Allow', 'Deny']);
    await card.getByRole('button', { name: 'Allow' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page)).toContainText('done');
    expect(approveActions).toEqual(['once']);
  });

  // An engine deny sends no feedback (proved above via the wire), but that alone does not
  // prove the CARD never shows a reason field for one in the first place. Python ignores
  // whatever a reason field would carry here, so showing it at all would be a
  // dead end that asks the end-user for something that goes nowhere.
  test('Deny on an engine approval denies immediately, with no reason view ever shown', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await sendMessage(page, 'email the results');
    const card = page.locator('.t2v-approval[data-kind="engine"]');
    await expect(card).toBeVisible();
    await card.getByRole('button', { name: 'Deny' }).click();
    await expect(page.getByLabel('Reason (optional, sent to the assistant)')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Confirm deny' })).toHaveCount(0);
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
  });
});

// ── The tests below read the wire, not only the screen. ──

test.describe('reading the wire, not just the screen', () => {
  test('Allow once sends action "once"', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent.map((b) => b.action)).toEqual(['once']);
    expect(sent[0]).not.toHaveProperty('updatedInput');
  });

  test('Always allow sends action "always", never silently downgraded to "once"', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Always allow' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent.map((b) => b.action)).toEqual(['always']);
  });

  test('editing the arguments sends updatedInput with the typed value', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    await page.getByLabel('Arguments (JSON)').fill('{"name": "CTChest"}');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]?.updatedInput).toEqual({ name: 'CTChest' });
  });

  test('a reason is sent only with a denial, trimmed', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('  wrong volume  ');
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]?.feedback).toBe('wrong volume');
  });

  test('a blank or whitespace-only reason is never sent as feedback', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('   ');
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]).not.toHaveProperty('feedback');
  });

  test('a reason typed then abandoned via Back never reaches an Allow', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('wrong volume');
    await page.getByRole('button', { name: 'Back' }).click();
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]).not.toHaveProperty('feedback');
    expect(sent[0]?.action).toBe('once');
  });

  test('buttons come back after a refused approve, and answering again works', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    // wire.py caps `feedback` at 10,000 characters: a genuine server-side 400 refusal,
    // with no change to any read-only file.
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('x'.repeat(10001));
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.getByRole('button', { name: 'Confirm deny' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Back' })).toBeEnabled();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('short reason');
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page)).toContainText('Understood: nothing was loaded.');
  });
});

test.describe('state truly reverts, not just the view', () => {
  test('every button disables the instant one is clicked, deterministically (not a race with the network)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    const allowOnce = page.getByRole('button', { name: 'Allow once' });
    // The card visible means the INITIAL "/approve load it" send has already completed —
    // under heavy load (found running this spec with --repeat-each under a full worker
    // pool), `sendMessage`'s own Enter keypress can resolve before that send's underlying
    // fetch has actually reached the browser's network layer, and the route handler
    // below, registered right after, would catch that first send too and hang forever
    // waiting for a `release()` this test does not call until after Allow once — a flake
    // in this test's own sequencing, not in the product.
    await expect(allowOnce).toBeVisible();
    // A deterministic busy window: hold the approve command at the network layer instead
    // of racing a real (usually sub-millisecond, on localhost) round trip.
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route('**/command', async (route) => {
      await held;
      await route.continue();
    });
    await allowOnce.click();
    await expect(allowOnce).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Deny' })).toBeDisabled();
    await expect(page.getByRole('button', { name: 'Edit arguments' })).toBeDisabled();
    release();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
  });

  test('Discard edits reverts the draft itself, not only the view (edit again shows the original)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    await page.getByLabel('Arguments (JSON)').fill('{"name": "CTChest"}');
    await page.getByRole('button', { name: 'Discard edits' }).click();
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    await expect(page.getByLabel('Arguments (JSON)')).toHaveValue('{\n  "name": "MRHead"\n}');
  });
});

test.describe('one command per decision, even inside one JS task', () => {
  test('three synchronous clicks on the same button send exactly one approve', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.waitForSelector('.t2v-approval [data-action="once"]');
    await page.evaluate(() => {
      const btn = document.querySelector<HTMLButtonElement>('.t2v-approval [data-action="once"]');
      btn?.click();
      btn?.click();
      btn?.click();
    });
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent.length).toBe(1);
    expect(sent[0]?.action).toBe('once');
  });

  test('Allow once then Always allow in the same task still send exactly one approve', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    await page.waitForSelector('.t2v-approval [data-action="once"]');
    await page.evaluate(() => {
      document.querySelector<HTMLButtonElement>('.t2v-approval [data-action="once"]')?.click();
      document.querySelector<HTMLButtonElement>('.t2v-approval [data-action="always"]')?.click();
    });
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent.length).toBe(1);
  });
});

test.describe('an answer whose result never comes ', () => {
  test('frees the buttons after WATCH_MS, with a notice, and not before', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    const card = page.locator('.t2v-approval');
    await expect(card).toBeVisible(); // settled
    await holdCommands(page, 'approve');
    await page.clock.install();
    const answered = commandAnswered(page, 'approve');
    await card.getByRole('button', { name: 'Allow once' }).click();
    await expect(card.getByRole('button', { name: 'Allow once' })).toBeDisabled();
    await answered;
    await page.clock.fastForward(59000);
    await expect(card.getByRole('button', { name: 'Allow once' })).toBeDisabled();
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
    await page.clock.fastForward(2000);
    await expect(card.getByRole('button', { name: 'Allow once' })).toBeEnabled();
    await expect(card.getByRole('button', { name: 'Deny' })).toBeEnabled();
    await expect(page.locator('.t2v-notice-text')).toHaveText('That took too long. Try again.');
  });
});

test.describe('arguments with an integer too large for JavaScript', () => {
  test('says the numbers are rounded and offers no edit; Allow once sends the arguments untouched', async ({ page, backend }) => {
    const approvals = captureApprovals(page);
    await page.goto(backend.url);
    await sendMessage(page, '/bigargs now');
    const card = page.locator('.t2v-approval');
    // The page's JSON.parse rounded 12345678901234567891 on the way in: the card can only
    // show the rounded value, so it says so.
    await expect(card).toContainText('Some numbers are too large to show exactly.');
    await expect(card.getByRole('button')).toHaveText(['Allow once', 'Always allow', 'Deny']);
    await card.getByRole('button', { name: 'Allow once' }).click();
    await expect(card).toHaveCount(0);
    expect(approvals).toHaveLength(1);
    expect(approvals[0]).toMatchObject({ action: 'once' });
    expect(approvals[0]!.updatedInput).toBeUndefined(); // the server keeps the exact value
  });

  test('ordinary numbers show no such line, and can be edited', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    const card = page.locator('.t2v-approval');
    await expect(card.getByRole('button', { name: 'Edit arguments' })).toBeVisible();
    await expect(card).not.toContainText('too large to show exactly');
  });
});

test.describe('a stray reference from a replaced card', () => {
  test.use({ backendArgs: ['--chunk-delay', '0'] });

  test('a captured handle from the first card cannot corrupt answering the second', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    const stale = await page.evaluateHandle(() => document.querySelector('.t2v-approval [data-action="once"]'));
    await page.getByRole('button', { name: 'Always allow' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(lastReply(page)).toContainText('MRHead is loaded.');
    await sendMessage(page, '/approve load it again');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    const beforeStrayClick = sent.length;
    await page.evaluate((el) => (el as HTMLButtonElement | null)?.click(), stale);
    await page.waitForTimeout(300);
    // Whatever the stray click did or did not do, it must not have corrupted the SECOND
    // (current) approval: it is still there, unanswered, and answers normally.
    expect(sent.length).toBeLessThanOrEqual(beforeStrayClick + 1);
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
  });
});

test.describe('arguments the page cannot faithfully show', () => {
  test('shows "could not be displayed" and offers only Deny', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/badargs load it');
    const card = page.locator('.t2v-approval');
    await expect(card).toContainText('These arguments could not be displayed.');
    await expect(card.locator('pre')).toHaveCount(0);
    await expect(card.getByRole('button')).toHaveText(['Deny']);
    await card.getByRole('button', { name: 'Deny' }).click();
    await page.getByLabel('Reason (optional, sent to the assistant)').fill('cannot review this');
    await page.getByRole('button', { name: 'Confirm deny' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]?.action).toBe('deny');
  });
});

test.describe('the card fits the visible thread, not the whole browser window', () => {
  // The thread's genuinely available strip: from the header's bottom to the top of the
  // sticky composer footer, which paints over anything still scrolled under it. Not
  // `0..innerHeight` — an element can sit fully inside the browser viewport and still be
  // covered by the composer, which the previous version of this spec never caught
  // (measured: Deny once sat at y=349 in a 480-tall window, "inside the
  // viewport" by that measure while painted over by the composer underneath it).
  async function visibleStrip(page: Page): Promise<{ top: number; bottom: number }> {
    const header = (await page.locator('.t2v-chat-header').boundingBox())!;
    const footer = (await page.locator('.t2v-footer').boundingBox())!;
    return { top: header.y + header.height, bottom: footer.y };
  }

  /** The element's own box sits inside the visible strip, AND its centre resolves back to
   * it (or a descendant) through `elementFromPoint` — not whatever is painted over it. */
  async function visibleAndUncovered(locator: ReturnType<Page['locator']>, strip: { top: number; bottom: number }): Promise<void> {
    const box = await locator.boundingBox();
    expect(box, 'element has no box (not rendered?)').not.toBeNull();
    expect(box!.y, 'top is above the visible strip (under the header)').toBeGreaterThanOrEqual(strip.top - 0.5);
    expect(box!.y + box!.height, 'bottom is below the visible strip (under the composer)').toBeLessThanOrEqual(strip.bottom + 0.5);
    const centre = { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 };
    const uncovered = await locator.evaluate(
      (el, { x, y }) => {
        const hit = document.elementFromPoint(x, y);
        return hit !== null && (el === hit || el.contains(hit) || hit.contains(el));
      },
      centre,
    );
    expect(uncovered, 'painted over at its own centre point').toBe(true);
  }

  for (const [w, h] of [
    [420, 700],
    [480, 800],
  ] as const) {
    test.describe(`${w}x${h}: everything fits, with no scrolling`, () => {
      test.use({ backendArgs: ['--destructive-tool'] });

      for (const [name, trigger] of [
        ['a 400-deep argument', '/deepargs load it'],
        ['a 1 MB string argument', '/hugearg load it'],
      ] as const) {
        test(`${name}: the title, the destructive warning, the details and the buttons are all visible and uncovered`, async ({
          page,
          backend,
        }) => {
          await page.setViewportSize({ width: w, height: h });
          await page.goto(backend.url);
          await sendMessage(page, trigger);
          await expect(page.locator('.t2v-approval')).toBeVisible();
          const strip = await visibleStrip(page);
          await visibleAndUncovered(page.locator('.t2v-approval-title'), strip);
          await visibleAndUncovered(page.locator('.t2v-warning').first(), strip);
          await visibleAndUncovered(page.locator('.t2v-approval-details, .t2v-tool-args').first(), strip);
          await visibleAndUncovered(page.getByRole('button', { name: 'Allow once' }), strip);
          await visibleAndUncovered(page.getByRole('button', { name: 'Deny' }), strip);
        });
      }
    });
  }

  test.describe('320x480: the buttons and a destructive warning stay reachable once the card settles', () => {
    test.use({ backendArgs: ['--destructive-tool'] });

    for (const [name, trigger] of [
      ['a 400-deep argument', '/deepargs load it'],
      ['a 1 MB string argument', '/hugearg load it'],
    ] as const) {
      test(`${name}: Allow, Deny and the destructive warning are visible and uncovered`, async ({ page, backend }) => {
        await page.setViewportSize({ width: 320, height: 480 });
        await page.goto(backend.url);
        await sendMessage(page, trigger);
        await expect(page.locator('.t2v-approval')).toBeVisible();
        const strip = await visibleStrip(page);
        await visibleAndUncovered(page.locator('.t2v-warning').first(), strip);
        await visibleAndUncovered(page.getByRole('button', { name: 'Allow once' }), strip);
        await visibleAndUncovered(page.getByRole('button', { name: 'Deny' }), strip);
      });
    }
  });

  test.describe('320x480: the buttons stay reachable even with an off-contract tool name', () => {
    for (const [name, trigger] of [
      ['a 400-deep argument', '/deepargs load it'],
      ['a 1 MB string argument', '/hugearg load it'],
      ['a 300-character tool name', '/longname load it'],
    ] as const) {
      test(`${name}: Allow and Deny are visible and uncovered; the title stays reachable by scrolling the thread`, async ({
        page,
        backend,
      }) => {
        await page.setViewportSize({ width: 320, height: 480 });
        await page.goto(backend.url);
        await sendMessage(page, trigger);
        await expect(page.locator('.t2v-approval')).toBeVisible();
        const strip = await visibleStrip(page);
        await visibleAndUncovered(page.getByRole('button', { name: 'Allow once' }), strip);
        await visibleAndUncovered(page.getByRole('button', { name: 'Deny' }), strip);
        // "Reachable": the title may be scrolled off above the header (the action row
        // took priority), but scrolling the thread itself back up brings it into view —
        // it is not lost, only out of the immediately visible strip.
        await page.locator('.t2v-viewport').evaluate((el) => {
          el.scrollTop = 0;
        });
        await expect(page.locator('.t2v-approval-title')).toBeVisible();
      });
    }
  });
});

test.describe('the approval title is bounded, not clamped', () => {
  // TS clamps neither the title nor the "Always allow" hint (tool-fallback.tsx:225 draws
  // the full name in a plain <p>); a 2-line clamp hid the tail of ordinary names, on the
  // string the end-user is approving. A scrolling max-height, the same pattern the card
  // already uses for the prompt, keeps a hostile name from growing the card without
  // limit while leaving a real one fully readable.
  test('a real 36-character name is fully readable at 320 wide, with no internal scroll needed', async ({ page, backend }) => {
    await page.setViewportSize({ width: 320, height: 480 });
    await page.goto(backend.url);
    await sendMessage(page, '/realname load it');
    const title = page.locator('.t2v-approval-title');
    await expect(title).toContainText('segment_volume_with_totalsegmentator');
    // Exactly 0: max-height is calc(3 * 1.625em), a whole
    // number of lines at the inherited line-height, not a plain 4.5em (2.77 lines, which
    // cut descenders on this very name's third line and needed a tolerance here).
    const overflow = await title.evaluate((el) => el.scrollHeight - el.clientHeight);
    expect(overflow, 'the name needed its own internal scroll to read in full').toBe(0);
  });

  test('a 300-character tool name stays bounded, not growing the card without limit', async ({ page, backend }) => {
    await page.setViewportSize({ width: 320, height: 480 });
    await page.goto(backend.url);
    await sendMessage(page, '/longname load it');
    const title = page.locator('.t2v-approval-title');
    await expect(title).toBeVisible();
    // calc(3 * 1.625em) at 14px/1.625 = 68.25px (app.css): three whole lines, whatever
    // the actual text needs beyond that scrolls inside the box instead of pushing the
    // rest of the card down. 14px, not the 13px this bound was first measured against
    // (.t2v-approval's own font-size was raised to match TS's text-sm on the card,
    // which .t2v-approval-title inherits, having none of its own).
    const box = (await title.boundingBox())!;
    expect(box.height, 'the title grew past its own bound').toBeLessThanOrEqual(70);
    const overflow = await title.evaluate((el) => el.scrollHeight - el.clientHeight);
    expect(overflow, 'a 300-character name should need its own internal scroll').toBeGreaterThan(0);
  });
});

test.describe('untrusted text is shown, never a spoof', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('a bidi override and a zero-width space read as visible escapes', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signInAccount(page);
    await sendMessage(page, 'spoof it');
    const card = page.locator('.t2v-approval[data-kind="engine"]');
    await expect(card).toContainText('open\\u202Efile'); // the TOOL NAME
    await expect(card).toContainText('Open \\u202Etxt.exe'); // the PROMPT
    await expect(card.locator('pre')).toContainText('pa\\u200Bth'); // an argument KEY
    await expect(card.locator('pre')).toContainText('report\\u202Egnp.exe'); // an argument VALUE
    await expect(card.locator('dd')).toContainText('report\\u202Egnp.exe'); // a DETAIL value
    // Scoped to Thread's own top-level region, a DIRECT child of .t2v-thread (see the
    // note above: Composer.tsx nests a second .t2v-sr-only inside .t2v-thread for
    // attachment announcements).
    const live = page.locator('.t2v-thread > .t2v-sr-only');
    await expect(live).toContainText('open\\u202Efile'); // the LIVE REGION
    await card.getByRole('button', { name: 'Deny' }).click();
  });
});

test.describe('"Edit arguments" does not re-introduce the spoof', () => {
  test('opening the editor shows the escaped form, never the raw bidi override', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/spoofargs load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    const editor = page.getByLabel('Arguments (JSON)');
    const value = await editor.inputValue();
    expect(value).toContain('\\u202E'); // the visible escape IS there
    expect(value).not.toContain(String.fromCharCode(0x202e)); // the raw character is NOT
  });

  test('an untouched draft sends no updatedInput at all', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/spoofargs load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    // "Always allow" is NOT off for an untouched draft, even though its text differs
    // from the raw original (it is the escaped form of the same value).
    await expect(page.getByRole('button', { name: 'Always allow' })).toBeEnabled();
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]).not.toHaveProperty('updatedInput');
  });

  test('a real edit sends the parsed value, the escape the user left in place decoding back to the real character', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/spoofargs load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    // A real edit: add a field, but leave the escaped path exactly as the editor showed
    // it — the user never sees, let alone retypes, the raw character.
    await page.getByLabel('Arguments (JSON)').fill('{"path": "/tmp/report\\u202Egnp.exe", "note": "reviewed"}');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    expect(sent[0]?.updatedInput).toEqual({
      path: '/tmp/report' + String.fromCharCode(0x202e) + 'gnp.exe',
      note: 'reviewed',
    });
  });

  test('Discard edits reverts to the escaped form again, not the raw spoof', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/spoofargs load it');
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    await page.getByLabel('Arguments (JSON)').fill('{"note": "typing something else entirely"}');
    await page.getByRole('button', { name: 'Discard edits' }).click();
    await page.getByRole('button', { name: 'Edit arguments' }).click();
    const value = await page.getByLabel('Arguments (JSON)').inputValue();
    expect(value).toContain('\\u202E');
    expect(value).not.toContain(String.fromCharCode(0x202e));
  });
});

test.describe('a platform tool that runs without approval', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  // The tool row is reached directly, never through "the last assistant message": once
  // the tool ends, the reply text after it opens a NEW assistant message by design
  // (src/talk2view/_reducer.py's `_on_text`: text after a tool call
  // starts a new segment, as in the TS SDK), so `lastReply()` stops containing the row
  // exactly when the call finishes. That is what makes a `lastReply()`-scoped assertion
  // report "element(s) not found" mid-turn — the row is still on the page, in the
  // segment that opened the turn.
  test('shows its activity and an elapsed ticker that keeps advancing (Capabilities.tool_events)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signInAccount(page);
    await sendMessage(page, 'search the web please');
    const tool = page.locator('.t2v-tool');
    await expect(tool).toHaveAttribute('data-state', 'running');
    await expect(tool).toContainText('Searching the web'); // ChatOptions tools[].activity
    await expect(tool.locator('.t2v-tool-elapsed')).toHaveText('1s', { timeout: 4000 });
    await expect(tool.locator('.t2v-tool-elapsed')).toHaveText('2s', { timeout: 4000 });
    await expect(tool.locator('.t2v-tool-elapsed')).toHaveText('3s', { timeout: 4000 });
    await expect(tool).toHaveAttribute('data-state', 'running');
  });

  // The other half of the same fixture: `tool_end` finishes the call. This turn is the
  // whole shape a platform tool takes on the wire — tool_start, a real pause, tool_end,
  // then the reply — and the page must follow it through to "Used", with the server
  // still serving afterwards. (An earlier round believed a tool_end killed the UI server
  // process whenever a live /events reader was attached, and shipped a fixture that sent
  // only tool_start. It does not: the process survives, exits 0 at teardown like every
  // other backend, and the step lands on `used`. What actually happened is the segment
  // rule above, which makes a `lastReply()`-scoped row vanish the moment the call ends.)
  test('ends as Used, the reply follows in a new message, and the server is still serving', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signInAccount(page);
    await sendMessage(page, 'search the web please');
    const tool = page.locator('.t2v-tool');
    await expect(tool).toHaveAttribute('data-state', 'running');
    await expect(tool).toHaveAttribute('data-state', 'used', { timeout: 20000 });
    await expect(tool).toContainText('Used');
    // The reply after the tool opens its own assistant message: two, and the tool row is
    // in the first of them, not the last.
    await expect(page.locator('.t2v-msg-assistant')).toHaveCount(2);
    await expect(lastReply(page)).toContainText('Here is what I found.');
    await expect(lastReply(page).locator('.t2v-tool')).toHaveCount(0);
    // The UI server is alive and answering, on the page's own connection.
    const ok = await page.evaluate(async () => {
      const response = await fetch('/healthz');
      return response.status;
    });
    expect(ok).toBe(200);
  });
});

test.describe('focus never leaves the page adrift', () => {
  test('the card is a labelled group', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await expect(page.getByRole('group', { name: /^Run / })).toBeVisible();
  });

  test('typing in the composer and pressing Enter while a card waits approves nothing', async ({ page, backend }) => {
    await page.goto(backend.url);
    const sent = captureApprovals(page);
    await sendMessage(page, '/approve load it');
    const input = page.getByRole('textbox', { name: 'Message' });
    await input.click();
    await input.fill('are you still there');
    await input.press('Enter');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    expect(sent.length).toBe(0);
  });

  test('focus returns to the composer once the card is answered', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeFocused();
  });

  test('Back returns focus to Deny, not to the page body', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await page.getByRole('button', { name: 'Back' }).click();
    await expect(page.getByRole('button', { name: 'Deny' })).toBeFocused();
  });

  test('the deny reason field takes focus as soon as it appears', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await page.getByRole('button', { name: 'Deny' }).click();
    await expect(page.getByLabel('Reason (optional, sent to the assistant)')).toBeFocused();
  });

  // Thread never unmounts (App.tsx keeps it alive, hidden, behind
  // Account/Settings), so this card can resolve while the end-user's focus is genuinely
  // on something else entirely — a half-typed sign-in field, say. Stealing it back to the
  // composer then would be worse than leaving it: the card no longer exists to receive
  // it, and whatever the end-user was doing gets interrupted for nothing they can see.
  test('does not steal focus from an unrelated control when the card resolves', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    // Focus something else entirely — still visible, still on Thread, nothing to do with
    // the approval — so a stolen focus is actually observable (an element genuinely
    // hidden, e.g. by Account's `visibility:hidden`, refuses focus() either way, which
    // would make a test built on that alone pass for the wrong reason). "New chat" is
    // NOT it: it disables — and so is blurred by the browser regardless of this fix —
    // the instant the resolved turn starts streaming its reply (`isLoading`). "Account"
    // is never disabled by anything a turn does.
    await page.evaluate(() => document.querySelector<HTMLButtonElement>('[aria-label="Account"]')?.focus());
    const account = page.getByRole('button', { name: 'Account' });
    await expect(account).toBeFocused();
    await page.evaluate(() => {
      document.querySelector<HTMLButtonElement>('.t2v-approval [data-action="once"]')?.click();
    });
    await expect(page.locator('.t2v-approval')).toHaveCount(0);
    await expect(account).toBeFocused();
  });
});

test.describe('"Always allow" says what it covers', () => {
  test('names the tool and how long it lasts ("Always allow: no more prompts for X in this chat.")', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    const hint = page.locator('.t2v-approval p.t2v-hint').last();
    await expect(hint).toContainText('load_sample_data');
    await expect(hint).toContainText('no more prompts');
    await expect(hint).toContainText('in this chat');
  });
});

test.describe('no "Always allow" hint when there is no such button', () => {
  test.use({ backendArgs: ['--destructive-tool'] });

  test('a destructive tool shows no hint about it either', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await expect(page.locator('.t2v-approval').getByText('no more prompts for')).toHaveCount(0);
  });
});

test.describe('a long prompt is expandable, not only clamped', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('Show more reveals the full text; Show less clamps it again', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signInAccount(page);
    await sendMessage(page, 'explain in detail please');
    const card = page.locator('.t2v-approval');
    await expect(card).toBeVisible();
    await expect(page.getByRole('button', { name: 'Show more' })).toBeVisible();
    const before = await card.locator('.t2v-approval-prompt').boundingBox();
    await page.getByRole('button', { name: 'Show more' }).click();
    const after = await card.locator('.t2v-approval-prompt').boundingBox();
    expect(after!.height).toBeGreaterThan(before!.height);
    await expect(page.getByRole('button', { name: 'Show less' })).toBeVisible();
    await page.getByRole('button', { name: 'Show less' }).click();
    await expect(page.getByRole('button', { name: 'Show more' })).toBeVisible();
  });
});

test.describe('a short prompt offers no expand toggle', () => {
  test('the scripted one-line description shows no "Show more"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Show more' })).toHaveCount(0);
  });
});
