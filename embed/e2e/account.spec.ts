import type { Page } from '@playwright/test';
import { commandAnswered, expect, fillSignIn, holdCommands, lastReply, postToServer, sendMessage, test } from './fixtures';

/** A computed property of the first element matching `selector` (looks.spec.ts's own
 * helper, duplicated rather than imported: importing a spec file re-registers its tests). */
async function computed(page: Page, selector: string, property: string): Promise<string> {
  return page.evaluate(
    ([sel, prop]) => {
      const el = document.querySelector(sel!);
      if (!el) throw new Error('no element matches ' + sel);
      return getComputedStyle(el).getPropertyValue(prop!);
    },
    [selector, property],
  );
}

const TOO_LONG = 'That took too long. Try again.';
/** A 1x1 PNG (attachments.spec.ts's own). */
const PNG = Buffer.from(
  '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c40000000049454e44ae426082',
  'hex',
);

test.describe('the guest gate (FakeController)', () => {
  test.describe('when the integrator allows no guests', () => {
    test.use({ backendArgs: ['--no-anonymous'] });

    test('sign-in replaces the thread, and a refusal shows under the form', async ({ page, backend }) => {
      await page.goto(backend.url);
      await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
      await expect(page.getByRole('textbox', { name: 'Message' })).toBeHidden();
      await fillSignIn(page, 'ana@example.com', 'pw');
      await expect(page.getByRole('alert')).toHaveText('This chat is not connected to a Talk2View account.');
    });
  });

  test('a guest out of allowance is asked to create an account', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/limit');
    await expect(page.locator('.t2v-reason')).toHaveText(
      'You’ve reached the guest limit. Sign in or create a free account to keep going.',
    );
    await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
    await expect(page.getByText('At least 8 characters.')).toBeVisible();
  });

  test('the gate closing under the end-user focuses the form, not the title or <body>', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/limit'); // typed: focus is in the composer, which the gate hides
    await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
    await expect(page.getByLabel('Email')).toBeFocused();
  });

  test('the reason is announced: its live region starts empty and fills when the gate closes', async ({ page, backend }) => {
    await page.goto(backend.url);
    // A live region reports changes while it is on the page, so it must be there, empty,
    // before the reason arrives: Account is always mounted, hidden behind the thread.
    const region = page.locator('.t2v-account [aria-live="polite"]');
    await expect(region).toHaveCount(1);
    await expect(region).toHaveText('');
    await sendMessage(page, '/limit');
    await expect(region).toHaveText('You’ve reached the guest limit. Sign in or create a free account to keep going.');
  });
});

test.describe('an account (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('a wrong password, then a sign-in, a chat and a sign-out', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await fillSignIn(page, 'ana@example.com', 'wrong');
    await expect(page.getByRole('alert')).toHaveText('Invalid email or password');
    // The submit button disabled itself while busy; focus is handed back to the
    // field the end-user has to retype, not dropped to <body>.
    await expect(page.getByLabel('Password')).toBeFocused();
    await expect(page.getByLabel('Password')).toHaveValue('');
    await expect(page.getByLabel('Email')).toHaveValue('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.locator('.t2v-dot')).toBeVisible();

    await sendMessage(page, 'hello');
    await expect(lastReply(page)).toHaveText(/^ok/);

    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByText('ana@example.com')).toBeVisible();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.locator('.t2v-dot')).toHaveCount(0);
  });

  test('a double click on Sign in shows the busy state and sends the command once', async ({ page, backend }) => {
    let logins = 0;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'login') {
        logins++;
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
      await route.continue();
    });
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).dblclick();
    await expect(page.getByRole('button', { name: 'Signing in…' })).toBeVisible();
    await expect(page.locator('.t2v-dot')).toBeVisible();
    expect(logins).toBe(1);
  });

  test('the fields are read-only, not disabled, while a sign-in is in flight', async ({ page, backend }) => {
    let release!: () => void;
    const released = new Promise<void>((resolve) => (release = resolve));
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible(); // settled
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'login') await released;
      await route.continue();
    });
    await fillSignIn(page, 'ana@example.com', 'pw');
    await expect(page.getByRole('button', { name: 'Signing in…' })).toBeDisabled();
    for (const field of [page.getByLabel('Email'), page.getByLabel('Password')]) {
      // readOnly keeps focus on the field; disabled would drop it to <body>.
      await expect(field).toHaveAttribute('readonly', '');
      await expect(field).toBeEnabled();
      await expect(field).toHaveAttribute('aria-busy', 'true');
    }
    release();
    await expect(page.locator('.t2v-dot')).toBeVisible();
  });

  test('a sign-in whose result never comes frees the form after WATCH_MS, and not before', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible(); // settled
    await holdCommands(page, 'login');
    await page.clock.install();
    const answered = commandAnswered(page, 'login');
    await fillSignIn(page, 'ana@example.com', 'pw');
    const submit = page.locator('.t2v-account form button[type="submit"]');
    await expect(submit).toHaveText('Signing in…');
    await answered;
    await page.clock.fastForward(59000);
    await expect(submit).toBeDisabled();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.clock.fastForward(2000);
    await expect(page.getByRole('alert')).toHaveText(TOO_LONG);
    await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeEnabled();
  });

  test('a failure arriving after the timeout shows its real reason, not the timeout', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible(); // settled
    const held = await holdCommands(page, 'login');
    await page.clock.install();
    const answered = commandAnswered(page, 'login');
    await fillSignIn(page, 'ana@example.com', 'wrong');
    await answered;
    await page.clock.fastForward(61000);
    await expect(page.getByRole('alert')).toHaveText(TOO_LONG);
    // The held command reaches the server now, late, under the page's own id.
    await postToServer(page, backend.url, held[0]!);
    await expect(page.getByRole('alert')).toHaveText('Invalid email or password');
  });

  test('a lost result does not greet a later sign-out with "That took too long"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible(); // settled
    const held = await holdCommands(page, 'login');
    await page.clock.install();
    const answered = commandAnswered(page, 'login');
    await fillSignIn(page, 'ana@example.com', 'pw');
    await answered;
    // The sign-in happens, but under another id: the auth slice signs ana in, and the
    // result the page is waiting for never comes.
    await postToServer(page, backend.url, { ...held[0]!, id: 'another-' + String(held[0]!.id) });
    await expect(page.locator('.t2v-dot')).toBeVisible();
    await expect(page.getByText('ana@example.com')).toBeVisible();
    await page.clock.fastForward(61000); // the lost result's WATCH_MS passes while signed in
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('a sign-out whose result never comes frees the button after WATCH_MS', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await fillSignIn(page, 'ana@example.com', 'pw');
    await expect(page.locator('.t2v-dot')).toBeVisible();
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByText('ana@example.com')).toBeVisible(); // settled
    await holdCommands(page, 'logout');
    await page.clock.install();
    const answered = commandAnswered(page, 'logout');
    await page.getByRole('button', { name: 'Sign out' }).click();
    const button = page.locator('.t2v-account .t2v-btn-block');
    await expect(button).toHaveText('Signing out…');
    await answered;
    await page.clock.fastForward(59000);
    await expect(button).toBeDisabled();
    await expect(page.getByRole('alert')).toHaveCount(0);
    await page.clock.fastForward(2000);
    await expect(page.getByRole('alert')).toHaveText(TOO_LONG);
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeEnabled();
  });

  test('a failed sign-out shows why, and the button works again', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await fillSignIn(page, 'ana@example.com', 'pw');
    await expect(page.locator('.t2v-dot')).toBeVisible();
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'logout') {
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: { type: 'network_error', message: 'x' } }),
        });
        return;
      }
      await route.continue();
    });
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('alert')).toHaveText('Talk2View could not be reached. Check your connection and try again.');
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeEnabled();
  });

  test('a sign-up that waits for its confirmation email', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Create an account' }).click();
    await page.getByLabel('Email').fill('new@example.com');
    await page.getByLabel('Password').fill('long enough');
    await page.getByRole('button', { name: 'Create account' }).click();
    await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
    await expect(page.getByText('new@example.com')).toBeVisible();
    await page.getByRole('button', { name: 'Back to the chat' }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
  });

  test('"Forgot it?" opens the integrator\'s page in the host', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Forgot it?' }).click();
    await expect.poll(() => backend.hostCalls).toEqual([{ host: 'open_external', value: 'https://example.com/reset' }]);
  });

  test('a signed-out visitor chats as a guest', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await page.getByRole('button', { name: 'Account' }).click();
    // The controller keeps a guest's conversation on sign-in, so the copy says so.
    await expect(
      page.getByText('You’re chatting as a guest. Signing in keeps this conversation and lifts the guest limits.'),
    ).toBeVisible();
  });

  test('a notice about the visitor before goes when that visitor signs out', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await fillSignIn(page, 'ana@example.com', 'pw');
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Attach a file' }).click();
    await (await chooser).setFiles({ name: 'evil.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') });
    await expect(page.locator('.t2v-notice-text')).toHaveText('Attach a PNG, JPEG, WebP, GIF or PDF file.');
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.locator('.t2v-notice')).toHaveCount(0); // the thread never unmounts
  });

  test('a signed-out pick of a wrong and a good file keeps the refusal once the good one starts a guest (M-B)', async ({
    page,
    backend,
  }) => {
    // Signed out to guest is the same visitor carrying on: the good file's upload starts
    // the guest, and the notice about the other file in the same pick must stay.
    await page.goto(backend.url);
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Attach a file' }).click();
    await (await chooser).setFiles([
      { name: 'evil.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') },
      { name: 'scan.png', mimeType: 'image/png', buffer: PNG },
    ]);
    const chip = page.locator('.t2v-composer .t2v-chip');
    await expect(chip).toContainText('scan.png');
    await expect(chip).not.toContainText('Uploading'); // uploaded: the guest has started
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.getByText('You’re chatting as a guest.', { exact: false })).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click(); // back to the chat
    await expect(page.locator('.t2v-notice-text')).toHaveText('Attach a PNG, JPEG, WebP, GIF or PDF file.');
  });
});

test.describe('a sign-up that signs in at once', () => {
  // A partner that does not require email confirmation: the engine answers the sign-up
  // with a session, and the SDK signs the end-user straight in.
  test.use({ backendArgs: ['--mode', 'account', '--signup-auto-confirm'] });

  test('lands on the chat, signed in, with no "Check your email"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Create an account' }).click();
    await page.getByLabel('Email').fill('new@example.com');
    await page.getByLabel('Password').fill('long enough');
    await page.getByRole('button', { name: 'Create account' }).click();
    await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeVisible();
    await expect(page.locator('.t2v-dot')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Check your email' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByText('new@example.com')).toBeVisible();
  });
});

test.describe('a refused guest (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--anonymous-refusal', 'captcha_required'] });

  test('gates with the reason, and a sign-in sends the stopped message again', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.locator('.t2v-reason')).toHaveText('Guest chat isn’t available right now. Sign in to keep going.');
    await fillSignIn(page, 'ana@example.com', 'pw');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('hello');
    await expect(lastReply(page)).toHaveText(/^ok/);
  });
});

test.describe('gated with no way to chat (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--no-anonymous'] });

  test('leaving "Check your email" while gated returns to sign-in, not a dead end', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Create an account' }).click();
    await page.getByLabel('Email').fill('new@example.com');
    await page.getByLabel('Password').fill('long enough');
    await page.getByRole('button', { name: 'Create account' }).click();
    await expect(page.getByRole('heading', { name: 'Check your email' })).toBeVisible();
    await expect(page.getByText('You can keep chatting in the meantime')).toHaveCount(0);
    await page.getByRole('button', { name: 'Back to sign in' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.getByLabel('Email')).toHaveValue('new@example.com');
    // Usable, not a dead end: a real sign-in from here reaches the chat.
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
  });

  test('the gate with no reason: says why the chat is gone', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.locator('.t2v-reason')).toHaveText('This app asks you to sign in before you chat.');
    expect(await computed(page, '.t2v-reason', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-reason', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
  });
});

test.describe('the Account view (account mode)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--desktop-sign-in'] });

  test('18px title, 13px labels, 36px inputs and block buttons, the error under its field, an identity row when signed in', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    expect(await computed(page, '.t2v-view-title', 'font-size')).toBe('18px');
    expect(await computed(page, '.t2v-field label', 'font-size')).toBe('13px');
    expect((await page.getByLabel('Email').boundingBox())!.height).toBe(36);
    for (const name of ['Continue with Apple', 'Continue with Google', 'Sign in']) {
      expect((await page.getByRole('button', { name, exact: true }).boundingBox())!.height).toBe(36);
    }
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('wrong');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    const err = page.getByRole('alert');
    await expect(err).toHaveText('Invalid email or password');
    expect(await computed(page, '.t2v-form-error', 'font-size')).toBe('13px');
    const pw = (await page.getByLabel('Password').boundingBox())!;
    const eb = (await err.boundingBox())!;
    expect(eb.y).toBeGreaterThan(pw.y + pw.height);
    expect(eb.y).toBeLessThan((await page.getByRole('button', { name: 'Sign in', exact: true }).boundingBox())!.y);
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Account', exact: true }).locator('.t2v-dot')).toBeVisible();
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await expect(page.locator('.t2v-avatar')).toHaveText('A');
    expect(await computed(page, '.t2v-avatar', 'border-radius')).toBe('50%');
    expect(await computed(page, '.t2v-who', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect((await page.getByRole('button', { name: 'Sign out' }).boundingBox())!.height).toBe(36);
  });
});
