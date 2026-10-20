import type { Page, Route } from '@playwright/test';
import { type Backend, commandAnswered, expect, holdCommands, postToServer, sendMessage, test } from './fixtures';

const ACCOUNT = ['--mode', 'account', '--desktop-sign-in'];
const TOO_LONG = 'That took too long. Try again.';
const DENIED = 'Sign-in was cancelled in the browser.';

/** How many times the host was asked to open a desktop sign-in. */
const opens = (backend: Backend) => backend.hostCalls.filter((c) => c.value === '<desktop sign-in>').length;

/** Open the Account view, and wait for the provider buttons. */
async function openAccount(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Continue with Apple' })).toBeEnabled();
}

/** Let every command through, and return the types of those the page posted. */
async function recordCommands(page: Page): Promise<string[]> {
  const types: string[] = [];
  await page.route('**/command', async (route) => {
    const body = route.request().postDataJSON() as { type?: string } | null;
    types.push(String(body?.type));
    await route.continue();
  });
  return types;
}

/**
 * The end-user confirms in the (held) browser. The pending mark shows before the host has
 * opened the browser (the SDK asks the engine for the start URL first), so wait for that.
 */
async function finishInBrowser(backend: Backend): Promise<void> {
  await expect.poll(() => backend.hostCalls.filter((c) => c.value === '<desktop sign-in>').length).toBe(1);
  backend.control('oauth-finish');
}

/** Three scripted clicks in one task: no re-render can land between them. */
async function tripleClick(page: Page, name: string): Promise<void> {
  await page.evaluate((label) => {
    const button = Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(
      (b) => b.textContent === label && b.closest('[aria-hidden="true"]') === null,
    );
    if (!button) throw new Error('no visible button ' + label);
    button.click();
    button.click();
    button.click();
  }, name);
}

test.describe('provider sign-in (account mode, desktop sign-in on)', () => {
  test.use({ backendArgs: ACCOUNT });

  test('Apple comes before Google, above the email form', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    const google = page.getByRole('button', { name: 'Continue with Google' });
    await expect(apple).toBeVisible();
    const a = (await apple.boundingBox())!;
    const g = (await google.boundingBox())!;
    const email = (await page.getByLabel('Email').boundingBox())!;
    expect(a.y).toBeLessThan(g.y);
    expect(g.y).toBeLessThan(email.y);
  });

  test('a sign-in opens the browser once, then the account shows', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).dblclick();
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible(); // back in the chat
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('desktop@example.com')).toBeVisible();
    expect(backend.hostCalls.filter((c) => c.value === '<desktop sign-in>')).toHaveLength(1);
    expect(backend.hostCalls.filter((c) => c.value.indexOf('/desktop/') !== -1)).toHaveLength(0);
  });
});

test.describe('while the browser is out', () => {
  test.use({ backendArgs: [...ACCOUNT, '--oauth-browser', 'hold'] });

  test('the view waits, hides the email form, and Cancel brings the buttons back', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Google' }).click();
    await expect(page.getByRole('status')).toContainText('Finish signing in with Google in your browser');
    await expect(page.getByLabel('Email')).toBeHidden();
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(page.getByRole('button', { name: 'Continue with Google' })).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0); // a cancel is not an error
  });

  test('finishing in the browser signs in', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toBeVisible();
    await finishInBrowser(backend);
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible();
  });

  test('three clicks in one task send one command, for a start and for Cancel', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Continue with Apple' })).toBeEnabled();
    const types = await recordCommands(page);
    await tripleClick(page, 'Continue with Apple');
    await expect(page.getByRole('status')).toContainText('Finish signing in with Apple in your browser');
    // A Cancel before the host has opened the browser now means it never opens: wait
    // for the one open this start makes, so the count below is exact.
    await expect.poll(() => opens(backend)).toBe(1);
    await tripleClick(page, 'Cancel');
    await expect(page.getByRole('button', { name: 'Continue with Apple' })).toBeVisible();
    expect(types.filter((t) => t === 'oauthStart')).toHaveLength(1);
    expect(types.filter((t) => t === 'oauthCancel')).toHaveLength(1);
    expect(backend.hostCalls.filter((c) => c.value === '<desktop sign-in>')).toHaveLength(1);
  });

  test('a refused start is said under the buttons, which come back for another try', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    await expect(apple).toBeEnabled();
    let starts = 0;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type !== 'oauthStart') return route.continue();
      starts++;
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'busy', message: 'raw server text' } }),
      });
    });
    await apple.click();
    await expect(page.getByRole('alert')).toHaveText('A sign-in is already waiting in your browser.');
    await expect(apple).toBeEnabled();
    await apple.click();
    await expect.poll(() => starts).toBe(2);
    await expect(page.locator('.t2v-banner')).toHaveCount(0); // quiet: no page notice either
    expect(backend.hostCalls.filter((c) => c.value === '<desktop sign-in>')).toHaveLength(0);
  });

  test('a start whose result never comes frees the buttons after WATCH_MS, and not before', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    await expect(apple).toBeEnabled(); // settled
    const held = await holdCommands(page, 'oauthStart');
    await page.clock.install();
    const answered = commandAnswered(page, 'oauthStart');
    await apple.click();
    await expect(apple).toBeDisabled();
    await expect(apple).toHaveAttribute('aria-busy', 'true');
    await answered;
    await page.clock.fastForward(59000);
    await expect(apple).toBeDisabled();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.clock.fastForward(2000);
    await expect(apple).toBeEnabled();
    await expect(apple).toHaveAttribute('aria-busy', 'false');
    // Said, as the email form says it: not a silent re-enable.
    await expect(page.getByRole('alert')).toHaveText(TOO_LONG);
    expect(held).toHaveLength(1);
  });

  test('a Cancel whose result never comes frees the button after WATCH_MS, and not before', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    const cancel = page.getByRole('button', { name: 'Cancel' });
    await expect(cancel).toBeEnabled(); // settled: the start's result is in
    const held = await holdCommands(page, 'oauthCancel');
    await page.clock.install();
    const answered = commandAnswered(page, 'oauthCancel');
    await cancel.click();
    await expect(cancel).toBeDisabled();
    await answered;
    await page.clock.fastForward(59000);
    await expect(cancel).toBeDisabled();
    await page.clock.fastForward(2000);
    await expect(cancel).toBeEnabled();
    expect(held).toHaveLength(1);
  });

  test('focus: Continue by keyboard lands on the waiting view, Tab on Cancel, Cancel on the section', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    const google = page.getByRole('button', { name: 'Continue with Google' });
    await google.focus();
    await page.keyboard.press('Enter');
    // Nothing Enter can act on: the waiting view, named by its line.
    await expect(page.getByRole('group', { name: 'Finish signing in with Google in your browser' })).toBeFocused();
    await page.keyboard.press('Tab');
    const cancel = page.getByRole('button', { name: 'Cancel' });
    await expect(cancel).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('group', { name: 'Sign in with Apple or Google' })).toBeFocused();
  });

  test('focus: when it was not lost, the end of an attempt leaves it where it is', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toBeVisible();
    await expect.poll(() => opens(backend)).toBe(1);
    const settings = page.getByRole('button', { name: 'Settings' });
    await settings.focus();
    backend.control('switch-off');
    backend.control('oauth-finish'); // the attempt fails
    await expect(page.getByRole('alert')).toBeVisible();
    await page.waitForTimeout(300);
    await expect(settings).toBeFocused();
  });

  for (const how of ['double', 'held'] as const) {
    test('a ' + how + ' Enter on Continue starts once and never cancels', async ({ page, backend }) => {
      await page.goto(backend.url);
      await openAccount(page);
      const types = await recordCommands(page);
      await page.getByRole('button', { name: 'Continue with Apple' }).focus();
      if (how === 'held') {
        await page.keyboard.down('Enter');
        for (let i = 0; i < 8; i++) {
          await page.waitForTimeout(35);
          await page.keyboard.down('Enter'); // a repeat: keydown with repeat true
        }
        await page.keyboard.up('Enter');
      } else {
        await page.keyboard.press('Enter');
        await page.waitForTimeout(120);
        await page.keyboard.press('Enter');
      }
      await expect(page.getByRole('status')).toHaveText('Finish signing in with Apple in your browser');
      await page.waitForTimeout(1000);
      expect(types).toEqual(['oauthStart']);
    });
  }

  test('the waiting line is announced (its live region mounts empty) and never claims the browser opened', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.evaluate(() => {
      const seen: string[] = [];
      (window as unknown as { __statusAtMount: string[] }).__statusAtMount = seen;
      new MutationObserver((records) => {
        for (const r of records) {
          r.addedNodes.forEach((n) => {
            if (!(n instanceof Element)) return;
            const regions = [n, ...Array.from(n.querySelectorAll('*'))].filter((el) => el.getAttribute('role') === 'status');
            regions.forEach((el) => seen.push(el.textContent ?? ''));
          });
        }
      }).observe(document.body, { childList: true, subtree: true });
    });
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toHaveText('Finish signing in with Apple in your browser');
    expect(await page.evaluate(() => (window as unknown as { __statusAtMount: string[] }).__statusAtMount)).toEqual(['']);
    await expect(page.locator('.t2v-oauth-pending')).not.toContainText('opened a browser');
    await expect(page.locator('.t2v-oauth-pending')).not.toContainText('We opened');
  });

  test('a busy refusal that lost the race to another start does not outlive that attempt', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    let held: Route | null = null;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'oauthStart' && held === null) {
        held = route;
        return;
      }
      await route.continue();
    });
    await apple.click();
    await expect.poll(() => held !== null).toBe(true);
    // Another start (another page on the same server, say) wins the race.
    await postToServer(page, backend.url, { id: 'other-1', type: 'oauthStart', provider: 'google' });
    await expect(page.getByRole('status')).toContainText('Google');
    await held!.continue(); // this page's start now reaches the server: busy
    await page.waitForTimeout(500);
    await page.getByRole('button', { name: 'Cancel' }).click();
    await expect(apple).toBeVisible();
    await page.waitForTimeout(300);
    await expect(page.getByRole('alert')).toHaveCount(0); // nothing is waiting
  });

  test('a refusal is about the identity it was given to: it goes when that changes', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type !== 'oauthStart') return route.continue();
      await route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'busy', message: 'raw server text' } }),
      });
    });
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toHaveText('A sign-in is already waiting in your browser.');
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await sendMessage(page, 'hello'); // signed out becomes a guest
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('You’re chatting as a guest.', { exact: false })).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('a sign-in that finishes while Settings is open leaves Settings open', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toBeVisible();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
    await finishInBrowser(backend);
    await expect(page.locator('.t2v-dot')).toBeVisible(); // signed in
    await page.waitForTimeout(300);
    await expect(page.getByRole('heading', { name: 'Settings', level: 1 })).toBeVisible();
  });

  test('a switch turned off mid-attempt ends it with its message, and no buttons', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toContainText('Finish signing in');
    await expect.poll(() => opens(backend)).toBe(1);
    backend.control('switch-off');
    backend.control('oauth-finish');
    const alert = page.getByRole('alert');
    await expect(alert).toContainText("Sign-in with Apple or Google isn't turned on for this app.");
    await expect(page.getByRole('button', { name: /Continue with/ })).toHaveCount(0);
    await expect(page.getByRole('status')).toHaveCount(0);
    // Keyboard Dismiss: with no provider button left, focus goes to the email field.
    await alert.getByRole('button', { name: 'Dismiss' }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('alert')).toHaveCount(0);
    // Nothing of the section is left: the form's heading, not the email field.
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeFocused();
  });

  test('a pending sign-in for a provider the page does not know is named neutrally', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    backend.control('pending-unknown');
    await expect(page.getByRole('status')).toHaveText('Finish signing in with your account in your browser');
    await expect(page.locator('.t2v-oauth-pending')).not.toContainText('github');
  });
});

test.describe('Cancel, while the sign-in is still ending', () => {
  test.use({ backendArgs: [...ACCOUNT, '--oauth-browser', 'hold', '--slow-cancel', '1.5'] });

  test('no start is sent until the pending mark clears, then exactly one', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    const cancel = page.getByRole('button', { name: 'Cancel' });
    await expect(cancel).toBeEnabled(); // settled: the start's result is in
    const types = await recordCommands(page);
    // Cancel, and at once (same task) try every provider button there is.
    const clicked = await page.evaluate(() => {
      const visible = (b: HTMLButtonElement) => b.closest('[aria-hidden="true"]') === null;
      const buttons = Array.from(document.querySelectorAll<HTMLButtonElement>('button')).filter(visible);
      buttons.find((b) => b.textContent === 'Cancel')!.click();
      const providers = buttons.filter((b) => (b.textContent ?? '').indexOf('Continue with') !== -1);
      providers.forEach((b) => b.click());
      return providers.length;
    });
    expect(clicked).toBe(0); // the waiting view has no provider buttons to click
    // The server holds the pending mark for the slow end: still waiting, no button.
    await expect(page.getByRole('status')).toContainText('Finish signing in with Apple in your browser');
    await expect(page.getByRole('button', { name: /Continue with/ })).toHaveCount(0);
    await page.waitForTimeout(700);
    await expect(page.getByRole('button', { name: /Continue with/ })).toHaveCount(0);
    expect(types).toEqual(['oauthCancel']);
    // The mark clears: the buttons come back, and the first click is taken.
    const google = page.getByRole('button', { name: 'Continue with Google' });
    await google.click();
    await expect(page.getByRole('status')).toContainText('Finish signing in with Google in your browser');
    expect(types).toEqual(['oauthCancel', 'oauthStart']);
    await expect(page.getByRole('alert')).toHaveCount(0); // no busy refusal
  });
});

test.describe('a refusal in the browser', () => {
  test.use({ backendArgs: [...ACCOUNT, '--oauth-browser', 'deny'] });

  test('is shown under the buttons, as text, until dismissed', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    const alert = page.getByRole('alert');
    await expect(alert).toContainText('Sign-in was cancelled in the browser.');
    await expect(page.getByRole('button', { name: 'Continue with Apple' })).toBeEnabled();
    // It stays across a trip to the chat and back (the pane stays mounted).
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(alert).toContainText('Sign-in was cancelled in the browser.');
    const types = await recordCommands(page);
    await alert.getByRole('button', { name: 'Dismiss' }).dblclick(); // one command, not two
    await expect(page.getByRole('alert')).toHaveCount(0);
    expect(types.filter((t) => t === 'oauthDismissError')).toHaveLength(1);
    // And three scripted clicks in one task, on the next failure's Dismiss.
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toContainText(DENIED);
    await tripleClick(page, 'Dismiss');
    await expect(page.getByRole('alert')).toHaveCount(0);
    expect(types.filter((t) => t === 'oauthDismissError')).toHaveLength(2);
  });

  test('an account change keeps it: only another attempt or Dismiss clears it', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toContainText('Sign-in was cancelled in the browser.');
    // Signed out becomes a guest: the first message starts one.
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await sendMessage(page, 'hello');
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('You’re chatting as a guest.', { exact: false })).toBeVisible();
    await expect(page.getByRole('alert')).toContainText('Sign-in was cancelled in the browser.');
  });

  test('a new attempt replaces the message', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toBeVisible();
    const types = await recordCommands(page);
    await page.getByRole('button', { name: 'Continue with Google' }).click();
    // The next attempt clears it at once; the same refusal then comes back as a new one:
    // a click taken, a second browser opened.
    await expect(page.getByRole('alert')).toContainText(DENIED);
    expect(types).toEqual(['oauthStart']);
    await expect.poll(() => opens(backend)).toBe(2);
  });

  test('an email sign-in keeps it too: it is there again after signing out', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toContainText(DENIED);
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expect(page.getByRole('alert')).toContainText(DENIED);
  });

  test('a guest whose attempt fails stays on Account, with the message', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello'); // a guest now
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('You’re chatting as a guest.', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toContainText(DENIED);
    await expect(page.getByRole('heading', { name: 'Account', level: 1 })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeHidden();
  });

  test('focus: a failure and a Dismiss land on the section, never on a button', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    const section = page.getByRole('group', { name: 'Sign in with Apple or Google' });
    await apple.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('alert')).toContainText(DENIED);
    await expect(section).toBeFocused(); // the waiting view that held it went away
    await page.getByRole('alert').getByRole('button', { name: 'Dismiss' }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('alert')).toHaveCount(0);
    await expect(section).toBeFocused();
  });

  test('Dismiss, then a second Enter, starts nothing', async ({ page, backend }) => {
    await page.goto(backend.url);
    await openAccount(page);
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('alert')).toContainText(DENIED);
    const types = await recordCommands(page);
    await page.getByRole('alert').getByRole('button', { name: 'Dismiss' }).focus();
    await page.keyboard.press('Enter');
    await page.waitForTimeout(120);
    await page.keyboard.press('Enter');
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.waitForTimeout(1000);
    expect(types).toEqual(['oauthDismissError']);
    expect(opens(backend)).toBe(1);
  });
});

test.describe('without the partner switch', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('there are no provider buttons', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByLabel('Email')).toBeVisible();
    await expect(page.getByRole('button', { name: /Continue with/ })).toHaveCount(0);
  });
});

test.describe('with the integrator’s feature off', () => {
  test.use({ backendArgs: [...ACCOUNT, '--no-provider-sign-in'] });

  test('there are no provider buttons, though the engine offers them', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByLabel('Email')).toBeVisible();
    await expect(page.getByRole('button', { name: /Continue with/ })).toHaveCount(0);
  });
});

test.describe('the guest gate', () => {
  test.use({
    backendArgs: [...ACCOUNT, '--oauth-browser', 'hold', '--anonymous-refusal', 'anonymous_daily_cap_reached'],
  });

  test('a visitor turned away as a guest signs in with Apple from the gate', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.locator('.t2v-reason')).toHaveText('Guest chat isn’t available right now. Sign in to keep going.');
    await page.getByRole('button', { name: 'Continue with Apple' }).click();
    await expect(page.getByRole('status')).toContainText('Finish signing in with Apple in your browser');
    await finishInBrowser(backend);
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible();
    await expect(page.locator('.t2v-reason')).toBeHidden();
  });
});
