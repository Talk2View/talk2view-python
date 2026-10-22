import type { Page } from '@playwright/test';
import { commandAnswered, expect, holdCommands, postToServer, sendMessage, test } from './fixtures';

async function signIn(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await page.getByLabel('Email').fill('ana@example.com');
  await page.getByLabel('Password').fill('pw');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
}

test.describe('Settings (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('signed out, the model picker is hidden outright, not merely disabled', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByLabel('AI model')).toHaveCount(0);
    await expect(page.locator('.t2v-guest-card').getByRole('button', { name: 'Sign in' })).toBeVisible();
    // Text size is a local preference: it stays available and works signed out too.
    await expect(page.getByRole('group', { name: 'Text size' }).getByRole('button', { name: 'Medium' })).toBeEnabled();
  });

  test('text size works signed out, unlike the model', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    const group = page.getByRole('group', { name: 'Text size' });
    await group.getByRole('button', { name: 'Large' }).click();
    await expect(group.getByRole('button', { name: 'Large' })).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).fontSize)).toBe('18px');
  });

  test('the model picker: the partner default, a pick, and back to Default', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const picker = page.getByLabel('AI model');
    await expect(picker.locator('option')).toHaveText(['Default — Fast', 'Fast', 'm-deep']);
    await expect(picker).toHaveValue('');
    await picker.selectOption('m-deep');
    await expect(picker).toHaveValue('m-deep');
    await page.reload(); // the pick is the server's, not the page's
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByLabel('AI model')).toHaveValue('m-deep');
    await page.getByLabel('AI model').selectOption('');
    await expect(page.getByLabel('AI model')).toHaveValue('');
    // Default must send `modelId: null`, never the `''` sentinel — a
    // reload only ever sees the server's own applied model, so this only passes if the
    // server actually understood "Default", not merely that the page's own select reset.
    await page.reload();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByLabel('AI model')).toHaveValue('');
  });
});

test.describe('the model list has not loaded yet', () => {
  // GET /v1/models populates models.list on its own background refresh: this is what
  // actually creates the "not loaded" window (unlike a slow skillsList — see below).
  test.use({ backendArgs: ['--mode', 'account', '--slow-models', '2'] });

  test('the picker stays disabled until the list has loaded; text size is never gated on it', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const picker = page.getByLabel('AI model');
    // Signed in, but the list has not loaded yet: disabled on THAT alone, not the
    // signed-out gate (the two used to be one entangled condition).
    await expect(picker).toBeDisabled();
    await expect(page.getByRole('group', { name: 'Text size' }).getByRole('button', { name: 'Medium' })).toBeEnabled(); // never gated on the list at all
    await expect(picker).toBeEnabled({ timeout: 5000 }); // the list catches up
  });
});

test.describe('a pick made while a queued command is still in flight', () => {
  // skillsList (GET /v1/skills) runs on the same single router thread as every other
  // command: a slow response queues anything sent right after it behind it — the same
  // window a probe once used to catch an unsettled value. Settings no longer
  // sends skillsList itself (the Skills view does), so this opens Skills first to
  // start the slow call, then switches to Settings while it is still in flight.
  test.use({ backendArgs: ['--mode', 'account', '--slow-skills', '2'] });

  test('the text size select shows the pick at once, settling once the queued command resolves', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Skills', exact: true }).click(); // starts the slow skillsList
    await page.getByRole('button', { name: 'Settings' }).click();
    const group = page.getByRole('group', { name: 'Text size' });
    await group.getByRole('button', { name: 'Large' }).click();
    // The pick shows at once — well before the queued command could possibly have a
    // result back — proving it is the OPTIMISTIC value, not (yet) what the server applied.
    await expect(group.getByRole('button', { name: 'Large' })).toHaveAttribute('aria-pressed', 'true');
    await expect(group).toHaveAttribute('aria-busy', 'true');
    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).fontSize)).toBe('18px');
    await expect(group).not.toHaveAttribute('aria-busy', 'true'); // settled once the result is in
  });

  test('the model picker shows the pick at once too, settling once the queued command resolves', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Skills', exact: true }).click(); // starts the slow skillsList
    await page.getByRole('button', { name: 'Settings' }).click();
    const picker = page.getByLabel('AI model');
    await expect(picker).toBeEnabled(); // models.list loads on its own, unaffected by --slow-skills
    await picker.selectOption('m-deep');
    await expect(picker).toHaveValue('m-deep');
    await expect(picker).toHaveAttribute('aria-busy', 'true');
    await expect(picker).not.toHaveAttribute('aria-busy', 'true'); // settled once the result is in
    await expect(picker).toHaveValue('m-deep'); // and it is still what was picked, not reverted
  });
});

test.describe('a pending pick whose model leaves the list', () => {
  test.use({ backendArgs: ['--mode', 'account', '--drop-model-on-relogin'] });

  test('keeps an option of its own, so the picker goes on showing it until its result', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const picker = page.getByLabel('AI model');
    await expect(picker.locator('option')).toHaveText(['Default — Fast', 'Fast', 'm-deep']); // settled
    await holdCommands(page, 'selectModel'); // the pick's own result never comes
    await picker.selectOption('m-deep');
    await expect(picker).toHaveAttribute('aria-busy', 'true');
    // The same account signs in again, from outside the page: the SDK re-reads the list,
    // and the engine no longer offers m-deep.
    await postToServer(page, backend.url, { id: 'relogin-1', type: 'login', email: 'ana@example.com', password: 'pw' });
    await expect(picker.locator('option')).toHaveText(['Default — Fast', 'm-deep', 'Fast']);
    await expect(picker).toHaveValue('m-deep');
    await expect(picker).toHaveAttribute('aria-busy', 'true');
  });
});

test.describe('features.settings off', () => {
  test.use({ backendArgs: ['--no-settings'] });

  test('the Settings button does not appear', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('button', { name: 'Settings' })).toHaveCount(0);
  });
});

test.describe('the Settings button', () => {
  test('toggles the view, and aria-pressed reflects it', async ({ page, backend }) => {
    await page.goto(backend.url);
    const btn = page.getByRole('button', { name: 'Settings' });
    await expect(btn).toHaveAttribute('aria-pressed', 'false');
    await btn.click();
    await expect(btn).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByRole('group', { name: 'Text size' })).toBeVisible();
    // A view change moves focus to the page's own title (`tabIndex=-1`),
    // which already names the new view, rather than leaving it on the button just hidden.
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeFocused();
    await btn.click();
    await expect(btn).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByRole('group', { name: 'Text size' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeFocused();
  });
});

test.describe('Account keeps a half-typed sign-in across a trip through Settings', () => {
  test('Account never unmounts, unlike Settings', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('half-typed@example.com');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('group', { name: 'Text size' })).toBeVisible(); // actually left Account
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByLabel('Email')).toHaveValue('half-typed@example.com');
  });
});

test.describe('an approval while Settings is open', () => {
  test.use({ backendArgs: ['--chunk-delay', '0.02'] });

  test('brings the thread back', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await expect(page.getByRole('group', { name: 'Text size' })).toHaveCount(0);
  });

  test('reopening Settings after the pull-back stays open despite an unrelated state change', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.locator('.t2v-approval')).toBeVisible(); // pulled back once, already
    await expect(page.getByRole('group', { name: 'Text size' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Settings' }).click(); // reopen; the SAME approval is still pending
    const group = page.getByRole('group', { name: 'Text size' });
    await expect(group).toBeVisible();
    await group.getByRole('button', { name: 'Large' }).click(); // a model.state change unrelated to the approval
    // Wait for the ROUND TRIP to actually land — model.state only changes once a result
    // for this command is recorded, exactly the moment a buggy effect keyed on the whole
    // state (not just approvalId) would refire. `aria-busy` clearing is that proof: it
    // settles on ANY result (FakeController has no account, so this one is a refusal, not
    // a success — the round trip still happened). Checking too early would pass even
    // under the bug, since the assertion below only needs to be true once, not to stay so.
    await expect(group).not.toHaveAttribute('aria-busy', 'true');
    await expect(group).toBeVisible(); // still on Settings: the effect must not have refired
  });
});

test.describe('an approval while Account is open', () => {
  test.use({ backendArgs: ['--chunk-delay', '0.02'] });

  test('does not pull Account away — a half-typed sign-in survives', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('half-typed@example.com');
    // The approval arrives while we stay put: attached (Thread keeps running underneath,
    // hidden), not visible — Account is what must stay on screen.
    await expect(page.locator('.t2v-approval')).toBeAttached();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.getByLabel('Email')).toHaveValue('half-typed@example.com');
  });
});

test.describe('a typed password never lingers (Account never unmounts)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('visiting Settings and back empties the field; the email is kept', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('hunter2-secret');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('group', { name: 'Text size' })).toBeVisible(); // actually left Account
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByLabel('Email')).toHaveValue('ana@example.com');
    // The live DOM property (what a script or runJavaScript would read), not the
    // attribute — which was never set either way, since the field is uncontrolled.
    await expect(page.getByLabel('Password')).toHaveValue('');
    expect(await page.getByLabel('Password').getAttribute('value')).toBeNull();
  });

  test('becoming a guest another way — sending a message — empties a typed password', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByLabel('Password').fill('hunter2-secret');
    await page.getByRole('button', { name: 'Account', exact: true }).click(); // toggles back to the thread
    await sendMessage(page, 'hello'); // mints a guest identity, not via this form at all
    await expect(page.locator('.t2v-msg-user').last()).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('You’re chatting as a guest')).toBeVisible(); // confirms the identity actually changed
    await expect(page.getByLabel('Password')).toHaveValue('');
  });
});

test.describe('a stale sign-in error', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('does not survive an unrelated identity change', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('wrong');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByText('Invalid email or password')).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click(); // -> the thread
    await sendMessage(page, 'hello'); // becomes a guest: a real identity change, unrelated to this error
    await expect(page.locator('.t2v-msg-user').last()).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click(); // -> Account again
    await expect(page.getByText('Invalid email or password')).toHaveCount(0);
  });
});

test.describe('hidden panes stay out of the accessibility tree', () => {
  test('aria-hidden on the hidden thread and the hidden Account pane', async ({ page, backend }) => {
    await page.goto(backend.url);
    const threadPane = page.locator('.t2v-thread').locator('xpath=..');
    // A plain attribute selector, not a role-based one: once aria-hidden is true, the
    // element drops out of the accessibility tree, so getByRole('region', ...) would no
    // longer find it — exactly the property being tested here. `[role="region"]` also
    // disambiguates from the header's OWN "Account" button, which carries the same label.
    const accountPane = page.locator('[role="region"][aria-label="Account"]').locator('xpath=..');
    await expect(threadPane).toHaveAttribute('aria-hidden', 'false');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(threadPane).toHaveAttribute('aria-hidden', 'true');
    await expect(accountPane).toHaveAttribute('aria-hidden', 'true');
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(accountPane).toHaveAttribute('aria-hidden', 'false');
    await expect(threadPane).toHaveAttribute('aria-hidden', 'true');
  });
});

test.describe('a correct sign-in ends on the chat even when its result lands after the auth slice', () => {
  // auth.login (auth.py) stores the tokens and notifies the auth-state listeners — which
  // is what pushes the auth-slice op — BEFORE it returns to the router, which records the
  // command's own result only once the call returns. On localhost the two land in the
  // same SSE frame; this flag delays only the return, separating them into different
  // frames — the window where the identity effect could see the slice change with no
  // result yet, and (before the fix) strand the view on Account.
  test.use({ backendArgs: ['--mode', 'account', '--late-login-result', '0.6'] });

  test('opening Account from the header and signing in correctly reaches the chat', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeVisible();
  });
});

test.describe('WATCH_MS: a result that never comes falls back to the server value', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('text size', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    // Installed only once Settings' own opening traffic has settled. The command below is
    // armed on this clock too (COMMAND_MS, transport.ts), so the fast-forward waits for its
    // 202 (commandAnswered): a jump while it is in flight aborts it instead of ever
    // reaching the WATCH_MS timeout below.
    await page.clock.install();
    // Fulfilled (202, "accepted") without ever reaching the real server: no result for
    // this command will ever arrive, exactly the case WATCH_MS exists for.
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'setFontSize') {
        await route.fulfill({ status: 202 });
        return;
      }
      await route.continue();
    });
    const group = page.getByRole('group', { name: 'Text size' });
    const large = group.getByRole('button', { name: 'Large' });
    const medium = group.getByRole('button', { name: 'Medium' });
    const answered = commandAnswered(page, 'setFontSize');
    await large.click();
    await expect(large).toHaveAttribute('aria-pressed', 'true');
    await expect(group).toHaveAttribute('aria-busy', 'true');
    await answered;
    await page.clock.fastForward(59000);
    await expect(large).toHaveAttribute('aria-pressed', 'true');
    await expect(group).toHaveAttribute('aria-busy', 'true');
    await page.clock.fastForward(2000);
    await expect(medium).toHaveAttribute('aria-pressed', 'true'); // fell back to the server's own value
    await expect(group).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('.t2v-settings .t2v-notice')).toContainText('That took too long. Try again.');
  });
});
