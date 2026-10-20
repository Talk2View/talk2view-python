import { commandAnswered, expect, holdCommands, lastReply, sendMessage, test } from './fixtures';

test.describe('the thread and the composer (FakeController)', () => {
  test('the welcome, a streamed Markdown reply, and the token kept out of the URL', async ({ page, backend }) => {
    const errors: string[] = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.getByText('Answers can be wrong. Check them before you act.')).toBeVisible();
    expect(page.url()).not.toContain('#t=');
    await expect(page.locator('h1')).toHaveText('Chat'); // the frame is the h1…
    await expect(page.locator('h2.t2v-welcome-heading')).toBeVisible(); // …the welcome the h2 under it

    await sendMessage(page, 'hello');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('hello');
    const reply = lastReply(page);
    await expect(reply.locator('.t2v-md h2')).toHaveText('Segmentation plan');
    await expect(reply.locator('.t2v-md table td').first()).toHaveText('Load');
    await expect(reply.locator('.t2v-md pre code')).toContainText("load_sample_data('MRHead')");
    await expect(reply).toContainText('日本語');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    expect(errors).toEqual([]);
  });

  test('a suggestion sends itself', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Load MRHead' }).click();
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('Load MRHead');
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeHidden();
  });

  test('a double click on a suggestion sends it once, and says nothing about being busy', async ({ page, backend }) => {
    await page.goto(backend.url);
    // Posts are serialised, so a second send would reach a controller the first one has
    // made busy — and its refusal would be the end-user's first sight of the chat.
    await page.getByRole('button', { name: 'Load MRHead' }).dblclick();
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('Load MRHead');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    expect(backend.controllerCalls).toEqual(['send']);
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
  });

  test('a refused opener leaves the suggestions usable again', async ({ page, backend }) => {
    // Refuse only the opener's own `send`, at the HTTP layer — as a busy router would —
    // so the welcome never leaves the screen (no message was ever appended) and the only
    // question is whether the `sent` latch that disabled both suggestions lets go.
    let refused = false;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (!refused && body?.type === 'send') {
        refused = true;
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ error: { type: 'busy', message: 'Refused by the test' } }),
        });
        return;
      }
      await route.continue();
    });
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Load MRHead' }).click();
    await expect(page.locator('.t2v-notice')).toContainText('The assistant is still working on the last message.');
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Load MRHead' })).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Show the axial view' })).toBeEnabled();
    await page.getByRole('button', { name: 'Show the axial view' }).click();
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('Show the axial view');
  });

  test('a suggestion clicked while a stale notice is up stays disabled until its own send answers', async ({
    page,
    backend,
  }) => {
    // The first send is refused (leaving a notice up); the second is slow. If the latch
    // let go on any change to the notice line, rather than only this click's own send, it
    // would free itself the instant it was set — before the slow send even reaches the
    // server — and a second click could post it again.
    let sends = 0;
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type === 'send') {
        sends++;
        if (sends === 1) {
          await route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({ error: { type: 'busy', message: 'Refused by the test' } }),
          });
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      await route.continue();
    });
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Load MRHead' }).click();
    await expect(page.locator('.t2v-notice')).toContainText('The assistant is still working on the last message.');
    const button = page.getByRole('button', { name: 'Show the axial view' });
    await expect(button).toBeEnabled();
    await button.click();
    await page.waitForTimeout(300);
    await expect(button).toBeDisabled();
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('Show the axial view', { timeout: 10000 });
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveCount(1);
    expect(sends).toBe(2);
    expect(backend.controllerCalls).toEqual(['send']);
  });

  test('a suggestion whose send never answers frees the suggestions after WATCH_MS', async ({ page, backend }) => {
    await page.goto(backend.url);
    const button = page.getByRole('button', { name: 'Load MRHead' });
    await expect(button).toBeEnabled(); // settled
    await holdCommands(page, 'send');
    await page.clock.install();
    const answered = commandAnswered(page, 'send');
    await button.click();
    await expect(button).toBeDisabled();
    await answered;
    await page.clock.fastForward(59000);
    await expect(button).toBeDisabled();
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
    await page.clock.fastForward(2000);
    await expect(button).toBeEnabled();
    await expect(page.getByRole('button', { name: 'Show the axial view' })).toBeEnabled();
    await expect(page.locator('.t2v-notice-text')).toHaveText('That took too long. Try again.');
  });

  test('a reload restores the conversation from the snapshot', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page).locator('.t2v-md h2')).toBeVisible();
    await page.reload();
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('first');
    await expect(lastReply(page).locator('.t2v-md h2')).toHaveText('Segmentation plan');
  });

  test('a failed turn shows the error card; Try again sends it again, Dismiss puts it away', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/fail now');
    const card = page.locator('.t2v-error');
    await expect(card).toContainText('Scripted failure');
    await card.getByRole('button', { name: 'Try again' }).click();
    // The card and the message count are the same whether or not the click did anything,
    // so read what reached the controller.
    await expect.poll(() => backend.controllerCalls).toEqual(['send', 'retry']);
    await expect(page.locator('.t2v-msg-user')).toHaveCount(1);
    await expect(card).toContainText('Scripted failure'); // the scripted turn fails again
    await card.getByRole('button', { name: 'Dismiss' }).click();
    await expect(card).toBeHidden();
  });

  test('a link in a reply opens in the host, and Copy uses the host clipboard', async ({ page, backend }) => {
    const popups: string[] = [];
    page.context().on('page', (opened) => popups.push(opened.url()));
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page).locator('.t2v-md h2')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await lastReply(page).getByRole('link', { name: 'the Slicer docs' }).click();
    await lastReply(page).getByRole('button', { name: 'Copy' }).click();
    await expect.poll(() => backend.hostCalls).toEqual([
      { host: 'open_external', value: 'https://slicer.readthedocs.io/en/latest/' },
      { host: 'clipboard_set', value: expect.stringContaining('## Segmentation plan') },
    ]);
    await page.waitForTimeout(1000); // a window the link opened would have arrived by now
    expect(popups).toEqual([]); // the web view opened no window of its own
    expect(page.url()).toBe(backend.url.split('#')[0]); // and never navigated
  });

  test('a middle click on a link goes to the host too, and opens no window', async ({ page, backend }) => {
    const popups: string[] = [];
    page.context().on('page', (opened) => popups.push(opened.url()));
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page).locator('.t2v-md h2')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    // The middle button fires `auxclick`, never `click`, and a web view whose
    // createWindow reuses the view would take the chat with it.
    await lastReply(page).getByRole('link', { name: 'the Slicer docs' }).click({ button: 'middle' });
    await expect.poll(() => backend.hostCalls).toEqual([
      { host: 'open_external', value: 'https://slicer.readthedocs.io/en/latest/' },
    ]);
    await page.waitForTimeout(1000);
    expect(popups).toEqual([]);
    expect(page.url()).toBe(backend.url.split('#')[0]);
  });

  test('a tool call becomes the approval card, then shows how it ended, its arguments and its result', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve load it'); // the scripted turn ends waiting on a tool
    // A tool call waiting on the end-user is the approval card (its own spec,
    // e2e/approval.spec.ts), not a running `.t2v-tool` row — the gate replaces it
    // (convert.ts withApprovalGate), so ChatOptions tools[].activity is not shown here.
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await expect(page.locator('.t2v-tool')).toHaveCount(0);
    await page.getByRole('button', { name: 'Stop' }).click(); // denies the waiting approval
    const tool = lastReply(page).locator('.t2v-tool');
    await expect(tool).toHaveAttribute('data-state', 'denied');
    await expect(tool.locator('.t2v-tool-verb')).toHaveText('Denied');
    await expect(tool.locator('.t2v-tool-name')).toHaveText('load_sample_data');
    await tool.locator('.t2v-tool-head').click();
    await expect(tool.locator('.t2v-tool-args')).toHaveText('{\n  "name": "MRHead"\n}');
    await expect(tool.locator('.t2v-tool-result')).toHaveText('Cancelled by the user');
  });

  test('New chat empties the thread', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await page.getByRole('button', { name: 'New chat' }).click();
    await expect(page.locator('.t2v-msg')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
  });

  test('the thread stays mounted behind Account, and New chat still reaches it', async ({ page, backend }) => {
    // The frame's headline claim (ui/App.tsx): the thread never unmounts behind a view.
    // assistant-ui's composer text lives in the runtime, not in Thread's own component
    // tree, so a half-typed message would survive even if Thread unmounted and remounted
    // — it is not, on its own, evidence either way. A DOM node's identity is: tag one
    // inside Thread, then check that visiting Account did not destroy and recreate it
    // (React never restores an attribute it did not itself set).
    const account = page.getByRole('button', { name: 'Account', exact: true });
    await page.goto(backend.url);
    await page.getByRole('textbox', { name: 'Message' }).fill('a draft never sent');
    await page.evaluate(() => document.querySelector('.t2v-viewport')!.setAttribute('data-kept', 'yes'));
    await account.click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await account.click(); // toggles back to the thread
    await expect(page.getByRole('textbox', { name: 'Message' })).toHaveValue('a draft never sent');
    expect(await page.evaluate(() => document.querySelector('.t2v-viewport')?.getAttribute('data-kept'))).toBe('yes');
    // New chat reaches the thread from a view too, not only the toggle above.
    await account.click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await page.getByRole('button', { name: 'New chat' }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
  });

  test('without a token the page says where to open it', async ({ page, backend }) => {
    await page.goto(backend.url.split('#')[0]!);
    await expect(page.getByText('Open this chat from the app it belongs to.')).toBeVisible();
  });
});

test.describe('without a host clipboard', () => {
  test.use({ backendArgs: ['--no-clipboard'] });

  test('there is no Copy button', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await expect(lastReply(page).getByRole('button', { name: 'Retry' })).toBeVisible();
    await expect(lastReply(page).getByRole('button', { name: 'Copy' })).toHaveCount(0);
  });
});

test.describe('with the account feature turned off', () => {
  test.use({ backendArgs: ['--no-account'] });

  test('there is no Account button, and the chat still works', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('button', { name: 'Account' })).toHaveCount(0);
    await sendMessage(page, 'hello');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('hello');
  });
});

test.describe('a slow stream', () => {
  test.use({ backendArgs: ['--chunk-delay', '0.1'] });

  test('Stop ends a streaming reply and keeps what streamed', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page).locator('.t2v-md h2')).toBeVisible();
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await expect(lastReply(page).locator('.t2v-md h2')).toContainText('Segmentation');
    await expect(lastReply(page)).not.toContainText('Unicode check');
  });

  test('the thread announces what the agent is doing, and then what it said', async ({ page, backend }) => {
    await page.goto(backend.url);
    // Scoped to Thread's own top-level region, a DIRECT child of .t2v-thread: Account.tsx's
    // sign-in form has one too (App.tsx keeps Account mounted, hidden, for a
    // half-typed sign-in), unrelated to the thread's own announcements, and Composer.tsx's
    // own attachment-upload region is nested inside .t2v-thread as well.
    const live = page.locator('.t2v-thread > .t2v-sr-only');
    await expect(live).toHaveAttribute('aria-live', 'polite');
    await expect(live).toHaveText(''); // a live region that mounts with its words in it announces nothing
    await sendMessage(page, 'hello');
    await expect(live).toHaveText('Thinking'); // the agent status, while the turn runs
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    // The reply once the turn has ended, and as its text: never "hash hash Segmentation plan".
    await expect(live).toContainText('Segmentation plan');
    await expect(live).not.toContainText('##');
  });

  test('New chat is out of reach while a turn runs', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible();
    // `clear` holds isLoading until the engine session is deleted, so a second New chat
    // would abandon the first one's run and delete a second session.
    await expect(page.getByRole('button', { name: 'New chat' })).toBeDisabled();
    await page.getByRole('button', { name: 'Stop' }).click();
    await expect(page.getByRole('button', { name: 'New chat' })).toBeEnabled();
    expect(backend.controllerCalls).toEqual(['send', 'cancel']);
  });
});

test.describe('the host theme', () => {
  test.use({ backendArgs: ['--dark'] });

  test('dark mode reaches the computed colours', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    const colours = await page.evaluate(() => {
      const body = getComputedStyle(document.body);
      const composer = getComputedStyle(document.querySelector('.t2v-composer')!);
      return { bg: body.backgroundColor, fg: body.color, composer: composer.backgroundColor };
    });
    expect(colours).toEqual({ bg: 'rgb(2, 28, 37)', fg: 'rgb(242, 240, 236)', composer: 'rgb(10, 42, 53)' });
  });
});
