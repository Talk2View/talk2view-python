import type { Page } from '@playwright/test';
import { commandAnswered, expect, holdCommands, lastReply, postToServer, sendMessage, test } from './fixtures';

/** What Settings says while no skill register has succeeded yet (selectors.ts `skillsSection`). */
const AWAITING_REGISTER = 'These skills reach the assistant with your next message.';

async function signIn(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await page.getByLabel('Email').fill('ana@example.com');
  await page.getByLabel('Password').fill('pw');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
}

test.describe('Settings (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('signed out, the model picker and skills are hidden outright, not merely disabled', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByLabel('AI model')).toHaveCount(0);
    await expect(page.getByRole('checkbox', { name: /scan-report/ })).toHaveCount(0);
    await expect(page.getByText('Sign in to choose a model and skills.')).toBeVisible();
    // Text size is a local preference: it stays available and works signed out too.
    await expect(page.getByLabel('Text size')).toBeEnabled();
    // Signed out, `skillsList` must not even be sent — an account-less guest
    // gets a 401 for it, which would otherwise surface as a stray "Session expired" notice.
    await page.waitForTimeout(300);
    expect(backend.hostCalls.some((c) => c.host === 'skills_rescan')).toBe(false);
    await expect(page.getByText('Session expired')).toHaveCount(0);
  });

  test('text size works signed out, unlike the model and skills', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    const size = page.getByLabel('Text size');
    await size.selectOption('large');
    await expect(size).toHaveValue('large');
    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).fontSize)).toBe('17px');
  });

  test('opening Settings signed in asks the host to rescan its skill folders (skillsList)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    expect(backend.hostCalls.some((c) => c.host === 'skills_rescan')).toBe(false); // not asked yet
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect.poll(() => backend.hostCalls.some((c) => c.host === 'skills_rescan')).toBe(true);
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

  test('skills: listed, and turned off and on', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    // The checkbox is listed whether the SDK's start-up register (0.5 s after the
    // backend starts) reached the engine before or after this sign-in, so this waits for
    // the checkbox itself (below), never on that timer. Before the fix it was hidden
    // behind "Skills are off" whenever the register lost the race.
    // The accessible name is the skill's name ALONE (an explicit
    // `aria-label`), not the label's full text content (name + badge + description) —
    // `exact: true` fails if that override were ever lost.
    const skill = page.getByRole('checkbox', { name: 'scan-report', exact: true });
    await expect(skill).toBeChecked();
    await skill.uncheck();
    // `aria-busy` is the settle state made observable. Waiting for
    // it to clear, still on this SAME page (no reload), proves the optimistic box does
    // not stay stuck forever once its own command's result — here, a success — is known.
    await expect(skill).not.toHaveAttribute('aria-busy', 'true');
    await page.reload(); // the choice is the server's, not the page's
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('checkbox', { name: 'scan-report', exact: true })).not.toBeChecked();
    await page.getByRole('checkbox', { name: 'scan-report', exact: true }).check();
    // The immediate optimistic state alone would satisfy a bare
    // `toBeChecked()` here without proving the server agreed. A second reload only ever
    // sees the server's own state, so this only passes once it actually applied there.
    await page.reload();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('checkbox', { name: 'scan-report', exact: true })).toBeChecked();
  });

  test('platform skills are listed read-only, under "From this app\'s publisher"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('button', { name: 'Settings' })).toBeVisible(); // sanity: still on Settings
    // Waits for this visit's listing (skillsList) to answer, the state this needs. It no
    // longer depends on whether the start-up register won the race with sign-in.
    await expect(page.getByText('From this app’s publisher')).toBeVisible();
    await expect(page.getByText('team-brief')).toBeVisible();
    // Read-only: no checkbox for it, and no pointer affordance.
    await expect(page.getByRole('checkbox', { name: /team-brief/ })).toHaveCount(0);
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
    await expect(page.getByLabel('Text size')).toBeEnabled(); // never gated on the list at all
    await expect(picker).toBeEnabled({ timeout: 5000 }); // the list catches up
  });
});

test.describe('a pick made while its own command is still queued', () => {
  // skillsList (GET /v1/skills) runs on the same single router thread as every other
  // command: a slow response queues anything sent right after Settings opens behind it —
  // the same window a review probe used to catch an unsettled value.
  test.use({ backendArgs: ['--mode', 'account', '--slow-skills', '2'] });

  test('the text size select shows the pick at once, settling once the queued command resolves', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const size = page.getByLabel('Text size');
    await size.selectOption('large');
    // The pick shows at once — well before the queued command could possibly have a
    // result back — proving it is the OPTIMISTIC value, not (yet) what the server applied.
    await expect(size).toHaveValue('large');
    await expect(size).toHaveAttribute('aria-busy', 'true');
    await expect.poll(() => page.evaluate(() => getComputedStyle(document.body).fontSize)).toBe('17px');
    await expect(size).not.toHaveAttribute('aria-busy', 'true'); // settled once the result is in
  });

  test('the model picker shows the pick at once too, settling once the queued command resolves', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
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

test.describe('skills disabled for this session', () => {
  test.use({ backendArgs: ['--mode', 'account', '--skills-disabled'] });

  test('"Skills are off for this app.", and no checkboxes even if some were about to render', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    // Deterministic: the fixture refuses the register as well as the listing,
    // as a real engine does, so no register can flip `available` back to true. The page
    // says "off" only once this visit's listing has answered; this waits for that line.
    await expect(page.getByText('Skills are off for this app.')).toBeVisible();
    await expect(page.getByRole('checkbox', { name: /scan-report/ })).toHaveCount(0);
  });
});

test.describe('the host\'s start-up register was refused before anyone signed in', () => {
  // Talk2Slicer's main path: Slicer starts and adds its skills, the SDK's register goes
  // out signed out and gets a 401, and the person signs in through the page later.
  // Nothing re-sends the register on sign-in, so `skills.available` stays false until the
  // next message flushes it. --register-signed-out makes that refusal happen before the
  // page loads, instead of racing the 0.5 s debounce.
  test.use({ backendArgs: ['--mode', 'account', '--register-signed-out'] });

  test('Settings lists the local skills and says when they reach the assistant, never "off"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByText('From this app’s publisher')).toBeVisible(); // this visit's listing has answered
    await expect(page.getByRole('checkbox', { name: 'scan-report', exact: true })).toBeChecked();
    await expect(page.getByText(AWAITING_REGISTER)).toBeVisible();
    await expect(page.getByText('Skills are off for this app.')).toHaveCount(0);

    // The line tells the truth: the next message's turn registers them, and it goes.
    await page.getByRole('button', { name: 'Settings' }).click();
    await sendMessage(page, 'hello');
    await expect(lastReply(page)).not.toBeEmpty();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByText(AWAITING_REGISTER)).toHaveCount(0);
    await expect(page.getByRole('checkbox', { name: 'scan-report', exact: true })).toBeChecked();
  });
});

test.describe('the listing never answers with a catalogue (M-C)', () => {
  // The start-up register was refused, so no register has succeeded; and this visit's
  // listing is refused too, so nothing proves skills are on for this partner.
  test.use({ backendArgs: ['--mode', 'account', '--register-signed-out'] });

  test('the local skills are listed, but nothing promises they reach the assistant', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'skillsList') {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: { type: 'network_error', message: 'x' } }),
        });
        return;
      }
      await route.continue();
    });
    await page.getByRole('button', { name: 'Settings' }).click();
    // The listing's refusal has come back (its notice), so the section is in its final form.
    await expect(page.locator('.t2v-settings .t2v-notice-text')).toHaveText(
      'Talk2View could not be reached. Check your connection and try again.',
    );
    await expect(page.getByRole('checkbox', { name: 'scan-report', exact: true })).toBeChecked();
    await expect(page.getByText(AWAITING_REGISTER)).toHaveCount(0);
    await expect(page.getByText('Skills are off for this app.')).toHaveCount(0);
  });
});

test.describe('an untrusted skill name and description', () => {
  test.use({ backendArgs: ['--mode', 'account', '--huge-skill'] });

  test('never widens the 480px view', async ({ page, backend }) => {
    await page.setViewportSize({ width: 480, height: 800 });
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const view = page.locator('.t2v-settings');
    await expect(view).toBeVisible();
    const [scrollWidth, clientWidth] = await view.evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
    // The description is clamped, with a way to read the rest (ToolCall.tsx's pattern).
    const more = page.getByRole('button', { name: 'Show more' }).first();
    await expect(more).toBeVisible();
    await more.click();
    await expect(page.getByRole('button', { name: 'Show less' }).first()).toBeVisible();
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
    await expect(page.getByLabel('Text size')).toBeVisible();
    // A view change moves focus to the page's own title (`tabIndex=-1`),
    // which already names the new view, rather than leaving it on the button just hidden.
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeFocused();
    await btn.click();
    await expect(btn).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByLabel('Text size')).toHaveCount(0);
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
    await expect(page.getByLabel('Text size')).toBeVisible(); // actually left Account
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
    await expect(page.getByLabel('Text size')).toHaveCount(0);
  });

  test('reopening Settings after the pull-back stays open despite an unrelated state change', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.locator('.t2v-approval')).toBeVisible(); // pulled back once, already
    await expect(page.getByLabel('Text size')).toHaveCount(0);
    await page.getByRole('button', { name: 'Settings' }).click(); // reopen; the SAME approval is still pending
    const size = page.getByLabel('Text size');
    await expect(size).toBeVisible();
    await size.selectOption('large'); // a model.state change unrelated to the approval
    // Wait for the ROUND TRIP to actually land — model.state only changes once a result
    // for this command is recorded, exactly the moment a buggy effect keyed on the whole
    // state (not just approvalId) would refire. `aria-busy` clearing is that proof: it
    // settles on ANY result (FakeController has no account, so this one is a refusal, not
    // a success — the round trip still happened). Checking too early would pass even
    // under the bug, since the assertion below only needs to be true once, not to stay so.
    await expect(size).not.toHaveAttribute('aria-busy', 'true');
    await expect(size).toBeVisible(); // still on Settings: the effect must not have refired
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
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
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
    await expect(page.getByLabel('Text size')).toBeVisible(); // actually left Account
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

test.describe('a skill checkbox is described by its description', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('aria-describedby points to the description text', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const checkbox = page.getByRole('checkbox', { name: 'scan-report', exact: true });
    const describedBy = await checkbox.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    await expect(page.locator('#' + describedBy!)).toContainText('Write a scan report.');
  });
});

test.describe('needsSessionReset', () => {
  // The real trigger: an engine that does not honour skill-register's `replace` still
  // holds an already-registered skill after it is disabled — --needs-session-reset makes
  // the fixture's own engine behave that way, so this is a genuine reproduction, not a
  // value poked onto the skills object directly. controller.py's own set_skills_state
  // self-heals the flag by recycling the session the INSTANT it sees it, within one round
  // trip — unless a run is active or an approval is pending, which is why disabling the
  // skill while the "explain in detail" approval is still waiting is what makes the hint
  // observable at all, not disabling it on its own (it recycles away too fast to see).
  test.use({ backendArgs: ['--mode', 'account', '--needs-session-reset'] });

  test('disabling an already-registered skill while an approval is pending shows the "forgets a skill" hint', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await signIn(page);
    // scan-report must have reached the engine once before disabling it can leave it
    // "held". The message below makes sure of it, whichever way the start-up register
    // (0.5 s after the backend starts) raced this sign-in: `chat()` flushes any pending
    // register before its turn, and waits out one already in flight. No sleep.
    await sendMessage(page, 'explain in detail');
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(
      page.getByText('The assistant forgets a skill you turned off when this chat is next idle.'),
    ).toHaveCount(0); // not yet — nothing has been disabled
    await page.getByRole('checkbox', { name: 'scan-report', exact: true }).uncheck();
    await expect(
      page.getByText('The assistant forgets a skill you turned off when this chat is next idle.'),
    ).toBeVisible();
  });
});

test.describe('a refused skill toggle rolls back, with a notice', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('the box returns to checked, aria-busy clears, and the refusal shows in Settings', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const skill = page.getByRole('checkbox', { name: 'scan-report', exact: true });
    await expect(skill).toBeChecked();
    // Installed only after the page has settled: an early route can catch an earlier,
    // unrelated command under load (a past flake).
    let refused = 0;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'skillDisable') {
        refused += 1;
        await route.fulfill({
          status: 400,
          contentType: 'application/json',
          body: JSON.stringify({ error: { type: 'unknown_skill', message: 'No skill named that.' } }),
        });
        return;
      }
      await route.continue();
    });
    // A click, not uncheck(): uncheck() also asserts the box changed, and a refusal this
    // fast can roll it back before that check runs (seen once in 580 runs at 24 workers).
    // That the toggle really went out, and was refused, is what `refused` proves.
    await skill.click();
    await expect.poll(() => refused).toBe(1);
    await expect(skill).toBeChecked(); // rolled back to the server's own (unchanged) value
    await expect(skill).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('.t2v-settings .t2v-notice')).toContainText('No skill named that.');
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
    const size = page.getByLabel('Text size');
    const answered = commandAnswered(page, 'setFontSize');
    await size.selectOption('large');
    await expect(size).toHaveValue('large');
    await expect(size).toHaveAttribute('aria-busy', 'true');
    await answered;
    await page.clock.fastForward(59000);
    await expect(size).toHaveValue('large');
    await expect(size).toHaveAttribute('aria-busy', 'true');
    await page.clock.fastForward(2000);
    await expect(size).toHaveValue('medium'); // fell back to the server's own value
    await expect(size).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('.t2v-settings .t2v-notice')).toContainText('That took too long. Try again.');
  });

  test('a skill toggle', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.getByRole('button', { name: 'Settings' }).click();
    const skill = page.getByRole('checkbox', { name: 'scan-report', exact: true });
    await expect(skill).toBeChecked();
    // This used to need the start-up register to win the race
    // with sign-in, or "Skills are off" hid the checkbox. It now waits for the state it
    // needs: the checkbox (above), and this visit's listing answered (below), so Settings'
    // own opening traffic has settled before the clock is installed.
    await expect(page.getByText('From this app’s publisher')).toBeVisible();
    // Installed only once that traffic has settled. The command below is armed on this
    // clock too (COMMAND_MS, transport.ts), so the fast-forward waits for its 202
    // (commandAnswered): a jump while it is in flight aborts it instead of ever reaching
    // the timeout below.
    await page.clock.install();
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'skillDisable') {
        await route.fulfill({ status: 202 });
        return;
      }
      await route.continue();
    });
    const answered = commandAnswered(page, 'skillDisable');
    await skill.uncheck();
    await expect(skill).not.toBeChecked();
    await expect(skill).toHaveAttribute('aria-busy', 'true');
    await answered;
    await page.clock.fastForward(59000);
    await expect(skill).not.toBeChecked();
    await expect(skill).toHaveAttribute('aria-busy', 'true');
    await page.clock.fastForward(2000);
    await expect(skill).toBeChecked(); // fell back to the server's own (unchanged) value
    await expect(skill).not.toHaveAttribute('aria-busy', 'true');
    await expect(page.locator('.t2v-settings .t2v-notice')).toContainText('That took too long. Try again.');
  });
});
