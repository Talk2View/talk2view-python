import type { Page, Route } from '@playwright/test';
import { expect, lastReply, sendMessage, test } from './fixtures';

const PNG = Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c40000000049454e44ae426082', 'hex');

async function attach(page: Page, name: string, mimeType: string, buffer: Buffer): Promise<void> {
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Attach a file' }).click();
  await (await chooser).setFiles({ name, mimeType, buffer });
}

async function attachMany(page: Page, files: Array<{ name: string; mimeType: string; buffer: Buffer }>): Promise<void> {
  const chooser = page.waitForEvent('filechooser');
  await page.getByRole('button', { name: 'Attach a file' }).click();
  await (await chooser).setFiles(files);
}

const png = (name: string) => ({ name, mimeType: 'image/png', buffer: PNG });

async function signIn(page: Page, email = 'ana@example.com'): Promise<void> {
  await page.getByRole('button', { name: 'Account' }).click();
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill('pw');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
}

/**
 * Hold `POST /upload` until `release()` is called, then answer with `answer` (default:
 * pass the real request through). Used to make the transient "uploading" state, and what
 * happens while it is still pending, reliably observable instead of racing the real
 * server.
 */
async function holdUpload(page: Page, answer?: { status: number; body: string }): Promise<() => void> {
  let release!: () => void;
  const gate = new Promise<void>((r) => (release = r));
  await page.route('**/upload', async (route: Route) => {
    await gate;
    try {
      if (answer) await route.fulfill({ status: answer.status, contentType: 'application/json', body: answer.body });
      else await route.continue();
    } catch {
      // The page may already have aborted this request (Remove, an identity change): the
      // route settling after that is expected, not a test failure.
    }
  });
  return release;
}

test.describe('attachments (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('a file uploads when chosen and goes with the message', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await attach(page, 'scan.png', 'image/png', PNG);
    const chip = page.locator('.t2v-composer .t2v-chip');
    await expect(chip).toContainText('scan.png');
    await expect(chip).not.toContainText('Uploading');
    await sendMessage(page, 'look at this');
    await expect(page.locator('.t2v-msg-user .t2v-chip')).toContainText('scan.png');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
  });

  test('a signed-out attach starts a guest first, then uploads, and the file goes with the message', async ({
    page,
    backend,
  }) => {
    // Attach is enabled signed out only when guests are allowed (the gate disables it
    // otherwise). The upload needs an identity to be filed under, so the page starts the
    // guest first, as a send does, instead of letting the upload be refused.
    await page.goto(backend.url);
    await attach(page, 'scan.png', 'image/png', PNG);
    const chip = page.locator('.t2v-composer .t2v-chip');
    await expect(chip).toContainText('scan.png');
    await expect(chip).not.toContainText('Uploading');
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
    await sendMessage(page, 'look at this');
    await expect(page.locator('.t2v-msg-user .t2v-chip')).toContainText('scan.png');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.getByText('You’re chatting as a guest.', { exact: false })).toBeVisible();
  });

  test('a refused upload is dropped, with the reason, and announced as not attached', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.route('**/upload', (route) =>
      route.fulfill({
        status: 413,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'too_large', message: 'x' } }),
      }),
    );
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-notice-text')).toHaveText('That file is too large to attach.');
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    // Never "Attached scan.png.": a refusal reads as one (a regression once).
    await expect(page.locator('.t2v-composer > .t2v-sr-only')).toHaveText('scan.png could not be attached.');
  });
});

test.describe('refusals that land apart', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('add up to one notice instead of the later replacing the earlier; a dismissed notice starts afresh', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await signIn(page);
    const answers = [
      { type: 'too_large', status: 413 },
      { type: 'upload_failed', status: 502, message: 'The engine refused it.' },
      { type: 'upload_failed', status: 502, message: 'The engine refused it.' },
    ];
    await page.route('**/upload', (route) => {
      const answer = answers.shift()!;
      return route.fulfill({
        status: answer.status,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: answer.type, message: answer.message ?? 'x' } }),
      });
    });
    const notice = page.locator('.t2v-notice-text');
    await attach(page, 'big.png', 'image/png', PNG);
    await expect(notice).toHaveText('That file is too large to attach.');
    await attach(page, 'other.png', 'image/png', PNG);
    await expect(notice).toHaveText('2 files could not be attached.');
    await page.locator('.t2v-notice').getByRole('button', { name: 'Dismiss' }).click();
    await attach(page, 'third.png', 'image/png', PNG);
    await expect(notice).toHaveText('The engine refused it.');
  });
});

test.describe('the host turns files off mid-session', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('a chip already in the composer is dropped, and the next message carries no file', async ({ page, backend }) => {
    const sends: Array<{ attachmentIds?: string[] }> = [];
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const body = request.postDataJSON() as { type?: string; attachmentIds?: string[] } | null;
      if (body?.type === 'send') sends.push(body);
    });
    await page.goto(backend.url);
    await signIn(page);
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    backend.control('attachments-off');
    await expect(page.getByRole('button', { name: 'Attach a file' })).toHaveCount(0);
    await sendMessage(page, 'no file now');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await expect(page.locator('.t2v-msg-user .t2v-chip')).toHaveCount(0);
    expect(sends).toEqual([expect.objectContaining({ attachmentIds: [] })]);
  });
});

test.describe('when the integrator turns files off', () => {
  test.use({ backendArgs: ['--no-attachments'] });

  test('there is no attach button', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Attach a file' })).toHaveCount(0);
  });
});

test.describe('the wire: what actually travels', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  // Filenames survive the round trip verbatim (multipart.py's safe_filename strips C0/
  // C1/bidi controls and line-break separators, but nothing else): a zero-width space is
  // not one of them, so this is a real client-side defence, not the server doing the
  // work. Escaped, not stripped: the name is still legible, just never invisible.
  test('a zero-width character in a file name is never rendered raw, in the composer or the sent message', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await signIn(page);
    const hostile = 'inv' + String.fromCharCode(0x200b) + 'oice.png';
    await attach(page, hostile, 'image/png', PNG);
    const composerChip = page.locator('.t2v-composer .t2v-chip');
    await expect(composerChip).toContainText('inv\\u200Boice.png');
    await sendMessage(page, 'look at this');
    await expect(page.locator('.t2v-msg-user .t2v-chip')).toContainText('inv\\u200Boice.png');
  });

  // A 300-character name is shown in full while uploading and while waiting to be sent
  // (the composer's own chip carries the raw `File.name`, not the server's truncated-at-
  // 255 copy): `.t2v-chip-name`'s ellipsis truncation (app.css, shared with the message-
  // side chip) must not let the box itself force the composer wider than its own bound.
  test('a 300-character file name does not widen the composer at 320px', async ({ page, backend }) => {
    await page.setViewportSize({ width: 320, height: 480 });
    await page.goto(backend.url);
    await signIn(page);
    const longName = 'a'.repeat(296) + '.png';
    await attach(page, longName, 'image/png', PNG);
    const composer = page.locator('.t2v-composer');
    await expect(composer.locator('.t2v-chip')).toBeVisible();
    const [scrollWidth, clientWidth] = await composer.evaluate((el) => [el.scrollWidth, el.clientWidth]);
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
  });

  // Routes/listeners are installed only after sign-in has settled the page (a route
  // installed early caught an earlier command under load).
  test('the send command carries the server’s attachment id, never the composer’s own placeholder', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await signIn(page);
    let uploadedId: string | null = null;
    const sentAttachmentIds: unknown[] = [];
    page.on('response', (response) => {
      if (!response.url().endsWith('/upload') || !response.ok()) return;
      void response
        .json()
        .then((body: { id?: string }) => {
          if (typeof body.id === 'string') uploadedId = body.id;
        })
        .catch(() => undefined);
    });
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const data = request.postDataJSON() as { type?: string; attachmentIds?: unknown };
      if (data.type === 'send') sentAttachmentIds.push(data.attachmentIds);
    });
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    await sendMessage(page, 'look at this');
    await expect(lastReply(page)).toHaveText(/^ok/);
    expect(uploadedId).not.toBeNull();
    expect(uploadedId).not.toMatch(/^upload-/); // the composer's own placeholder id, never sent
    expect(sentAttachmentIds).toEqual([[uploadedId]]);
  });
});

test.describe('a picked-but-unsent attachment does not survive a fresh identity', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('New chat and a sign-out both clear it', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);

    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(1);
    await page.getByRole('button', { name: 'New chat' }).click();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);

    await attach(page, 'scan2.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(1);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
  });
});

// ────────────────────────────────────────────────────────────────────────────
// What a review of uploads found, plus the tests it named
// ────────────────────────────────────────────────────────────────────────────

test.describe('Send and Enter are inert while an upload is still running', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('nothing is posted, the typed text is kept, and Send works once the upload lands', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
    const sentCommands: string[] = [];
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const data = request.postDataJSON() as { type?: string };
      if (data.type) sentCommands.push(data.type);
    });
    const release = await holdUpload(page);
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).toContainText('Uploading');
    const send = page.getByRole('button', { name: 'Send' });
    await expect(send).toBeDisabled();
    const input = page.getByRole('textbox', { name: 'Message' });
    await input.fill('hello');
    await input.press('Enter'); // goes through form.requestSubmit(); must not reach composer.send()
    await page.waitForTimeout(300);
    expect(sentCommands).not.toContain('send');
    expect(errors).toEqual([]);
    await expect(input).toHaveValue('hello'); // never cleared: nothing was sent
    release();
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    await expect(send).toBeEnabled();
    await input.press('Enter');
    await expect(lastReply(page)).toHaveText(/^ok/);
    expect(sentCommands).toContain('send');
  });
});

test.describe('more than 10 files', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('the first 10 stay, the rest are refused up front, and the send carries exactly 10 ids with the text', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await signIn(page);
    const sentAttachmentIds: unknown[] = [];
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const data = request.postDataJSON() as { type?: string; attachmentIds?: unknown };
      if (data.type === 'send') sentAttachmentIds.push(data.attachmentIds);
    });
    await attachMany(
      page,
      Array.from({ length: 11 }, (_, i) => ({ name: 'f' + String(i) + '.png', mimeType: 'image/png', buffer: PNG })),
    );
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(10);
    await expect(page.locator('.t2v-notice-text')).toHaveText('You can attach up to 10 files.');
    await expect(page.getByRole('button', { name: 'Attach a file' })).toBeDisabled();
    await expect(page.locator('.t2v-composer .t2v-chip-status')).toHaveCount(0, { timeout: 15000 });
    await sendMessage(page, 'eleven files');
    await expect(lastReply(page)).toHaveText(/^ok/);
    expect(sentAttachmentIds).toHaveLength(1);
    expect((sentAttachmentIds[0] as unknown[]).length).toBe(10);
    await expect(page.locator('.t2v-msg-user')).toHaveCount(1); // one message: the server never refused it
  });
});

test.describe('a wrong file type', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('shows the page’s own copy, not assistant-ui’s developer string', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Attach a file' }).click();
    await (await chooser).setFiles({ name: 'evil.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') });
    await expect(page.locator('.t2v-notice-text')).toHaveText('Attach a PNG, JPEG, WebP, GIF or PDF file.');
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
  });

  test('a good file in the same pick still uploads, and can still be sent', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await attachMany(page, [
      { name: 'evil.exe', mimeType: 'application/x-msdownload', buffer: Buffer.from('MZ') },
      { name: 'good.png', mimeType: 'image/png', buffer: PNG },
    ]);
    await expect(page.locator('.t2v-notice-text')).toHaveText('Attach a PNG, JPEG, WebP, GIF or PDF file.');
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(1);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    await sendMessage(page, 'rest');
    await expect(lastReply(page)).toHaveText(/^ok/);
  });
});

test.describe('a draft and a picked file never cross identities', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  // Also pins that the identity key must include the user id, not merely
  // isAuthenticated/isAnonymous — a sign-out-then-sign-in test alone cannot tell that
  // apart from a plain "signed in or not" comparison, so this goes ana straight to bob.
  test('signing in as a different person clears both the draft and the chip', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page, 'ana@example.com');
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    await page.getByRole('textbox', { name: 'Message' }).fill('secret draft');
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    // Already on the Account view (sign-out lands there, not gated): fill the form
    // directly, rather than signIn()'s own first click on the header's Account toggle,
    // which would now match TWO buttons (it, and "Create an account") and toggle away.
    await page.getByLabel('Email').fill('bob@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('');
  });
});

test.describe('removing a chip stops the upload, not just the display', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  const LATE = JSON.stringify({ id: 'att-LATE', filename: 'late.png', mime_type: 'image/png', size_bytes: 4 });

  test('the request itself fails, and a late server answer never comes back as a chip', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const failed: string[] = [];
    page.on('requestfailed', (r) => {
      if (r.url().endsWith('/upload')) failed.push(r.failure()?.errorText ?? '');
    });
    const release = await holdUpload(page, { status: 200, body: LATE });
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).toContainText('Uploading');
    await page.getByRole('button', { name: /Remove/ }).click();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    release();
    await page.waitForTimeout(300);
    expect(failed.length).toBeGreaterThan(0);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0); // the late completion added nothing back
    await sendMessage(page, 'plain');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await expect(page.locator('.t2v-msg-user .t2v-chip')).toHaveCount(0);
  });
});

test.describe('an /upload answer that means the chat is over', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  for (const [label, status, type] of [
    ['401', 401, 'unauthorized'],
    ['403 forbidden', 403, 'forbidden'],
    ['503 stopped', 503, 'stopped'],
  ] as const) {
    test('ends the chat, the same as a command refused the same way: ' + label, async ({ page, backend }) => {
      await page.goto(backend.url);
      await signIn(page);
      await page.route('**/upload', (route) =>
        route.fulfill({ status, contentType: 'application/json', body: JSON.stringify({ ok: false, error: { type, message: 'no' } }) }),
      );
      await attach(page, 'a.png', 'image/png', PNG);
      await expect(page.getByText('This chat has stopped. Open it again from the app.')).toBeVisible();
      await expect(page.getByRole('textbox', { name: 'Message' })).toBeDisabled();
    });
  }

  test('403 attachments_disabled stays a plain notice: the host turned off files, not the whole chat', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.route('**/upload', (route) =>
      route.fulfill({
        status: 403,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'attachments_disabled', message: 'Attachments are turned off in this app.' } }),
      }),
    );
    await attach(page, 'a.png', 'image/png', PNG);
    await expect(page.locator('.t2v-notice-text')).toHaveText('This app does not take attachments.');
    await sendMessage(page, 'still works');
    await expect(lastReply(page)).toHaveText(/^ok/);
  });
});

test.describe('accessible names, and an upload announced without a second role=status', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('two chips get two different Remove names', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await attachMany(page, [
      { name: 'one.png', mimeType: 'image/png', buffer: PNG },
      { name: 'two.png', mimeType: 'image/png', buffer: PNG },
    ]);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(2);
    await expect(page.getByRole('button', { name: 'Remove one.png' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Remove two.png' })).toBeVisible();
  });

  test('"Uploading …" then "Attached …", in its own aria-live region — never role="status"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const live = page.locator('.t2v-composer > .t2v-sr-only');
    await expect(live).toHaveAttribute('aria-live', 'polite');
    await expect(live).not.toHaveAttribute('role', 'status');
    await expect(live).toHaveText(''); // mounts empty: a region that starts full announces nothing
    const release = await holdUpload(page);
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(live).toHaveText('Uploading scan.png…');
    release();
    await expect(live).toHaveText('Attached scan.png.');
  });
});

test.describe('the attach button follows the same gate as the rest of the composer', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('disabled once the chat is over', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.route('**/upload', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'unauthorized', message: 'no' } }),
      }),
    );
    await attach(page, 'a.png', 'image/png', PNG);
    await expect(page.getByText('This chat has stopped. Open it again from the app.')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Attach a file' })).toBeDisabled();
  });
});

test.describe('paste with the feature off uploads nothing, on the wire', () => {
  test.use({ backendArgs: ['--mode', 'account', '--no-attachments'] });

  test('no /upload request, no notice', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    let uploads = 0;
    page.on('request', (r) => {
      if (r.url().endsWith('/upload')) uploads++;
    });
    await page.getByRole('textbox', { name: 'Message' }).focus();
    await page.evaluate(() => {
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'p.png', { type: 'image/png' }));
      const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
      document.querySelector('textarea')!.dispatchEvent(ev);
    });
    await page.waitForTimeout(500);
    expect(uploads).toBe(0);
    await expect(page.locator('.t2v-notice-text')).toHaveCount(0);
  });
});

test.describe('the file picker only offers the real types', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('the hidden file input’s accept attribute is the real list, not "*"', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const chooserPromise = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Attach a file' }).click();
    const chooser = await chooserPromise;
    const accept = await chooser.element().evaluate((el) => (el as HTMLInputElement).accept);
    expect(accept).toBe('image/png,image/jpeg,image/webp,image/gif,application/pdf');
    await chooser.setFiles([]);
  });
});

test.describe('a hostile server refusal reason is shown exactly, and escaped', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  // Kills "the notice does not go through displayText" and "the reason is replaced by
  // fixed text" in one assertion: `toHaveText`, not `toContainText` or `not.toBeEmpty()`,
  // pins the EXACT string — raw, the bidi override would make the DOM text unequal to
  // this literal escape, and any substituted copy would not equal it either.
  test('the notice matches the server’s own text exactly, through displayText', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const hostileReason = 'Bad file report' + String.fromCharCode(0x202e) + 'gnp.exe';
    await page.route('**/upload', (route) =>
      route.fulfill({
        status: 502,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'upload_failed', message: hostileReason } }),
      }),
    );
    await attach(page, 'a.png', 'image/png', PNG);
    await expect(page.locator('.t2v-notice-text')).toHaveText('Bad file report\\u202Egnp.exe');
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Regressions: each test below failed against an earlier version of the page
// ────────────────────────────────────────────────────────────────────────────

test.describe('reset() must not wipe a draft the end-user is typing', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('New chat held slow, typing right after the click keeps the draft', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await page.route('**/command', async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.continue();
    });
    await page.getByRole('button', { name: 'New chat' }).click();
    await page.getByRole('textbox', { name: 'Message' }).fill('typed right after New chat');
    await page.waitForTimeout(2000); // the held newSession result lands well within this
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('typed right after New chat');
  });

  // The identity effect fires on signed-out → guest too (store.ts's own
  // needsGuestStart), and a real guest start is an engine round trip: held here to
  // reproduce that window without a real network delay.
  test('the first message as a new guest, a follow-up typed at once, keeps the draft', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.route('**/command', async (route) => {
      await new Promise((r) => setTimeout(r, 600));
      await route.continue();
    });
    await sendMessage(page, 'first as guest');
    await page.getByRole('textbox', { name: 'Message' }).fill('follow-up typed immediately');
    await page.waitForTimeout(1500);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('follow-up typed immediately');
  });

  // Kept green on purpose: the "previous had a user id" gate must not stop protecting
  // identities that DO have one — only signed-out (no id) is exempted.
  test('ana → bob still clears both the draft and the chip (the gate does not weaken this)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page, 'ana@example.com');
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    await page.getByRole('textbox', { name: 'Message' }).fill('secret draft');
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByRole('button', { name: 'Sign out' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    await page.getByLabel('Email').fill('bob@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('');
  });
});

test.describe('the cap counter does not drift', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('three sends of four files each all succeed, four ids each time', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const sentAttachmentIds: unknown[] = [];
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const data = request.postDataJSON() as { type?: string; attachmentIds?: unknown };
      if (data.type === 'send') sentAttachmentIds.push(data.attachmentIds);
    });
    for (let round = 0; round < 3; round++) {
      await attachMany(
        page,
        Array.from({ length: 4 }, (_, i) => png('r' + String(round) + 'f' + String(i) + '.png')),
      );
      await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(4);
      await expect(page.locator('.t2v-composer .t2v-chip-status')).toHaveCount(0);
      await sendMessage(page, 'round ' + String(round));
      await expect(page.locator('.t2v-msg-assistant')).toHaveCount(round + 1);
      await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    }
    expect(sentAttachmentIds).toHaveLength(3);
    for (const ids of sentAttachmentIds) expect((ids as unknown[]).length).toBe(4);
  });

  test('removing a chip frees its slot in the cap too, independent of sending', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await attachMany(
      page,
      Array.from({ length: 10 }, (_, i) => png('c' + String(i) + '.png')),
    );
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(10);
    await expect(page.getByRole('button', { name: 'Attach a file' })).toBeDisabled();
    await page.getByRole('button', { name: 'Remove c0.png' }).click();
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(9);
    await expect(page.getByRole('button', { name: 'Attach a file' })).toBeEnabled();
    await attach(page, 'new.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(10);
  });
});

test.describe('several refusals together never leave a zombie chip', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('six picks of four files, all refused: no chip stays, Send still works afterward', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    const sends: unknown[] = [];
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('request', (request) => {
      if (request.method() !== 'POST' || !request.url().endsWith('/command')) return;
      const data = request.postDataJSON() as { type?: string; attachmentIds?: unknown };
      if (data.type === 'send') sends.push(data.attachmentIds);
    });
    await page.route('**/upload', (route) =>
      route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ ok: false, error: { type: 'upload_failed', message: 'no' } }) }),
    );
    for (let i = 0; i < 6; i++) {
      await attachMany(page, [png('r1.png'), png('r2.png'), png('r3.png'), png('r4.png')]);
      await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
    }
    await page.unroute('**/upload');
    await sendMessage(page, 'with no zombie chip');
    await expect(lastReply(page)).toHaveText(/^ok/);
    expect(sends).toEqual([[]]); // no attachments: none of the 24 ever succeeded
    expect(errors).toEqual([]);
  });
});

test.describe('a fatal /upload refusal matches a fatal command refusal', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('401 on /upload: the banner only — no notice, no stale "Uploading…" in the live region', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await page.route('**/upload', (route) =>
      route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'unauthorized', message: 'Missing or wrong token.' } }),
      }),
    );
    await attach(page, 'a.png', 'image/png', PNG);
    await expect(page.getByText('This chat has stopped. Open it again from the app.')).toBeVisible();
    await expect(page.locator('.t2v-notice-text')).toHaveCount(0);
    await expect(page.locator('.t2v-composer > .t2v-sr-only')).not.toHaveText(/Uploading/);
  });
});

test.describe('a new session that did not come from the button still clears an unsent file', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  // Bypasses the New chat button entirely (posts the exact command it sends, straight to
  // the wire) so only the messages-emptied WATCHER (Composer.tsx) can be what clears the
  // chip — a real stand-in for a host-driven new session (Slicer's own Python calling
  // ChatController.clear() directly), which has no click for the button's own handler to
  // run at all.
  test('posting newSession directly still clears the chip', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await attach(page, 'scan.png', 'image/png', PNG);
    await expect(page.locator('.t2v-composer .t2v-chip')).not.toContainText('Uploading');
    const token = await page.evaluate(() => sessionStorage.getItem('t2v_ui_token'));
    await page.evaluate(async (tok) => {
      await fetch('/command', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + String(tok), 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: 'host-clear-1', type: 'newSession' }),
      });
    }, token);
    await expect(page.locator('.t2v-composer .t2v-chip')).toHaveCount(0);
  });
});
