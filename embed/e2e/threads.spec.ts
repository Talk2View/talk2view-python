import type { Page } from '@playwright/test';
import { expect, holdCommands, lastReply, sendMessage, test } from './fixtures';

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

test.describe('the Chats view (FakeController with a thread list)', () => {
  test('New chat keeps the old one, the list switches back, the title follows', async ({ page, backend }) => {
    await page.goto(backend.url);
    const chats = page.getByRole('button', { name: 'Chats' });
    // Chats stays enabled even with nothing behind it yet — an
    // empty list has its own "No chats yet" state (looks.spec.ts's own spec) rather than
    // being unreachable.
    await expect(chats).toBeEnabled();
    await expect(page.locator('h1.aui-modal-title')).toHaveText('New chat');
    await sendMessage(page, 'Segment the liver');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await expect(page.locator('h1.aui-modal-title')).toHaveText('Segment the liver');
    await expect(chats).toBeEnabled();
    await page.getByRole('button', { name: 'New chat' }).click();
    await expect(page.locator('h1.aui-modal-title')).toHaveText('New chat');
    await expect(page.locator('.t2v-msg-user')).toHaveCount(0);
    expect(backend.controllerCalls).not.toContain('clear'); // detached, not deleted
    await chats.click();
    const list = page.locator('.t2v-chat-thread-list');
    // The ARIA HTML mapping gives <input type="search"> role "searchbox", not "textbox"
    // (confirmed empirically against this page's real accessibility tree; the same input
    // TS's own ThreadListSearch also renders as type="search").
    await expect(list.getByRole('searchbox', { name: 'Search chats' })).toBeVisible();
    await expect(list.locator('[data-slot="aui_thread-list-group-label"]')).toHaveText(['Today']);
    const row = list.locator('[data-slot="aui_thread-list-item"]');
    await expect(row).toHaveCount(1);
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('Segment the liver');
    expect((await row.boundingBox())!.height).toBe(36);
    await row.locator('[data-slot="aui_thread-list-item-trigger"]').click();
    await expect(list).toBeHidden(); // picking closes the view
    await expect(page.locator('h1.aui-modal-title')).toHaveText('Segment the liver');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('Segment the liver');
  });

  test('search, rename and delete', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'second');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const list = page.locator('.t2v-chat-thread-list');
    const rows = list.locator('[data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(2);
    await expect(rows.first().locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('second'); // newest first
    await list.getByRole('searchbox', { name: 'Search chats' }).fill('FIR');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('first');
    await list.getByRole('searchbox', { name: 'Search chats' }).fill('zzz');
    await expect(list.getByText(/^No chats match/)).toBeVisible();
    await list.getByRole('searchbox', { name: 'Search chats' }).fill('');
    // Rename the first row: the input opens selected; Enter commits.
    await rows.first().hover();
    await rows.first().getByRole('button', { name: 'Rename' }).click();
    const input = rows.first().getByRole('textbox', { name: 'Rename thread' });
    await expect(input).toBeFocused();
    await input.fill('Renamed second');
    await input.press('Enter');
    await expect(rows.first().locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('Renamed second');
    // Escape cancels.
    await rows.nth(1).hover();
    await rows.nth(1).getByRole('button', { name: 'Rename' }).click();
    await rows.nth(1).getByRole('textbox', { name: 'Rename thread' }).fill('nope');
    await rows.nth(1).getByRole('textbox', { name: 'Rename thread' }).press('Escape');
    await expect(rows.nth(1).locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('first');
    // Delete the current one: the list loses it and the thread is blank.
    await rows.first().hover();
    await rows.first().getByRole('button', { name: 'Delete' }).click();
    await expect(rows).toHaveCount(1);
    await page.getByRole('button', { name: 'Chats' }).click(); // toggles back to the thread
    await expect(page.locator('h1.aui-modal-title')).toHaveText('New chat');
    await expect(page.locator('.t2v-msg-user')).toHaveCount(0);
  });

  test('a hostile first message is shown escaped in the list, the title, and the rename input', async ({ page, backend }) => {
    await page.goto(backend.url);
    const rlo = String.fromCharCode(0x202e);
    await sendMessage(page, 'report' + rlo + 'gnp.exe');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await expect(page.locator('h1.aui-modal-title')).toHaveText('report\\u202Egnp.exe');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('report\\u202Egnp.exe');
    // The rename input opens seeded with the SAME escaped form: what the end-user edits is
    // what they see, not a live bidi override reversing the text inside the field.
    await row.hover();
    await row.getByRole('button', { name: 'Rename' }).click();
    const input = row.getByRole('textbox', { name: 'Rename thread' });
    await expect(input).toBeFocused();
    await expect(input).toHaveValue('report\\u202Egnp.exe');
  });
});

test.describe('the Chats view with the list off', () => {
  test.use({ backendArgs: ['--no-thread-list'] });
  test('no button, New chat deletes the session as before', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('button', { name: 'Chats' })).toHaveCount(0);
    await sendMessage(page, 'hello');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await expect.poll(() => backend.controllerCalls).toContain('clear');
  });
});

test.describe('the empty Chats state, signed in', () => {
  test.use({ backendArgs: ['--mode', 'account'] });
  test('the sign-in invitation is dropped once there is no one left to invite', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Account', exact: true }).locator('.t2v-dot')).toBeVisible();
    await page.getByRole('button', { name: 'Chats' }).click();
    await expect(page.locator('.t2v-thread-list-empty h3')).toHaveText('No chats yet');
    await expect(page.locator('.t2v-thread-list-empty p')).toHaveText('Conversations you start show up here.');
  });
});

test.describe('the Chats view, computed styles, light', () => {
  test('32 px search input, 11 px Slate group label, a wash active row, Smoke text, destructive Delete', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const list = page.locator('.t2v-chat-thread-list');
    const row = list.locator('[data-slot="aui_thread-list-item"]').first();
    await expect(row).toBeVisible();
    expect(await computed(page, '.t2v-thread-search-input', 'height')).toBe('32px');
    expect(await computed(page, '.t2v-thread-search-input', 'padding-left')).toBe('32px');
    expect(await computed(page, '.t2v-thread-search-input', 'font-size')).toBe('14px');
    expect(await computed(page, '.t2v-thread-group', 'color')).toBe('rgb(103, 119, 124)'); // Slate
    expect(await computed(page, '.t2v-thread-group', 'font-size')).toBe('11px');
    expect((await row.boundingBox())!.height).toBe(36);
    // The current thread's own row carries data-active, so its background is the
    // hover/active wash regardless of the mouse (thread-row[data-active]) — a
    // neutral wash, not Mist (the "wash instead of Mist" theme extended here too).
    expect(await computed(page, '.t2v-thread-row', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.t2v-thread-row', 'border-left-color')).toBe('rgb(38, 200, 184)'); // Teal
    expect(await computed(page, '.t2v-thread-trigger', 'font-size')).toBe('14px');
    expect(await computed(page, '.t2v-thread-trigger', 'color')).toBe('rgb(2, 28, 37)'); // Smoke
    await row.hover();
    await expect.poll(() => computed(page, '.t2v-thread-more', 'opacity')).toBe('1');
    // Slate while the row is merely hovered — only hovering the Delete button
    // itself switches it to the destructive tint (the row-hover reveal and the button's
    // own hover state are two different rules, matched independently).
    expect(await computed(page, '.t2v-thread-delete', 'color')).toBe('rgb(103, 119, 124)'); // Slate
    await row.getByRole('button', { name: 'Delete' }).hover();
    expect(await computed(page, '.t2v-thread-delete', 'color')).toBe('rgb(187, 82, 78)'); // destructive
    expect(await computed(page, '.t2v-thread-delete', 'background-color')).toBe('rgba(187, 82, 78, 0.1)');
  });
});

test.describe('the Chats view, computed styles, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('the same rows in the dark palette: an Ivory wash active row, Ivory text, Stone label, destructive Delete unchanged', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const list = page.locator('.t2v-chat-thread-list');
    const row = list.locator('[data-slot="aui_thread-list-item"]').first();
    await expect(row).toBeVisible();
    expect(await computed(page, '.t2v-thread-group', 'color')).toBe('rgb(154, 164, 168)'); // Stone
    expect(await computed(page, '.t2v-thread-row', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
    expect(await computed(page, '.t2v-thread-row', 'border-left-color')).toBe('rgb(38, 200, 184)'); // Teal
    expect(await computed(page, '.t2v-thread-trigger', 'color')).toBe('rgb(242, 240, 236)'); // Ivory
    await row.hover();
    expect(await computed(page, '.t2v-thread-delete', 'color')).toBe('rgb(154, 164, 168)'); // Stone
    await row.getByRole('button', { name: 'Delete' }).hover();
    expect(await computed(page, '.t2v-thread-delete', 'color')).toBe('rgb(187, 82, 78)'); // destructive, unchanged
    expect(await computed(page, '.t2v-thread-delete', 'background-color')).toBe('rgba(187, 82, 78, 0.05)'); // dark's own destructive-10
  });
});

test.describe('keyboard focus after rename and delete', () => {
  test('Enter commits a rename and focus returns to the trigger', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await row.getByRole('button', { name: 'Rename' }).press('Enter');
    const input = row.getByRole('textbox', { name: 'Rename thread' });
    await expect(input).toBeFocused();
    await input.fill('Renamed');
    await input.press('Enter');
    await expect(row.locator('[data-slot="aui_thread-list-item-trigger"]')).toBeFocused();
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('Renamed');
  });

  test('Escape cancels a rename and focus returns to the trigger', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await row.getByRole('button', { name: 'Rename' }).press('Enter');
    const input = row.getByRole('textbox', { name: 'Rename thread' });
    await input.fill('nope');
    await input.press('Escape');
    await expect(row.locator('[data-slot="aui_thread-list-item-trigger"]')).toBeFocused();
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('first');
  });

  test('blur commits a rename, the same as Enter', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await row.hover();
    await row.getByRole('button', { name: 'Rename' }).click();
    const input = row.getByRole('textbox', { name: 'Rename thread' });
    await input.fill('Renamed by blur');
    await input.blur();
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('Renamed by blur');
  });

  test('deleting a middle row moves focus to the row that is now next', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'second');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'third');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const rows = page.locator('[data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(3); // third, second, first
    const second = rows.filter({ hasText: 'second' });
    const first = rows.filter({ hasText: 'first' });
    await second.getByRole('button', { name: 'Delete' }).press('Enter');
    await expect(first.locator('[data-slot="aui_thread-list-item-trigger"]')).toBeFocused();
    await expect(rows).toHaveCount(2);
  });

  test('deleting the last row moves focus to the row before it', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'second');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const rows = page.locator('[data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(2); // second, first — "first" is last
    const first = rows.filter({ hasText: 'first' });
    const second = rows.filter({ hasText: 'second' });
    await first.getByRole('button', { name: 'Delete' }).press('Enter');
    await expect(second.locator('[data-slot="aui_thread-list-item-trigger"]')).toBeFocused();
    await expect(rows).toHaveCount(1);
  });

  test('deleting the only thread moves focus to the search box', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const rows = page.locator('[data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(1);
    // The focus call itself is synchronous with the click (onDelete calls it right after
    // store.command(), no await between them), but the search box only stays mounted
    // while hasThreads is true — the server's own confirmation that this delete emptied
    // the list would unmount it, and on a fast local round trip that could race
    // Playwright's own first poll (measured: 3/8 failures at --repeat-each 8 without
    // this). Holding the command server-side removes the race outright rather than
    // trying to out-poll it: the list never actually empties in this test, so there is
    // nothing left to unmount the search box.
    await holdCommands(page, 'threadDelete');
    await rows.first().getByRole('button', { name: 'Delete' }).press('Enter');
    await expect(page.getByRole('searchbox', { name: 'Search chats' })).toBeFocused();
  });
});

test.describe('an unchanged or empty rename sends no command', () => {
  test.use({ backendArgs: ['--thread-command-error'] });
  test('proven by the knob: a command that was actually sent would fail loudly here, and none of it does', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await row.hover();
    await row.getByRole('button', { name: 'Rename' }).click();
    await row.getByRole('textbox', { name: 'Rename thread' }).press('Enter'); // unchanged
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('first');
    await row.hover();
    await row.getByRole('button', { name: 'Rename' }).click();
    await row.getByRole('textbox', { name: 'Rename thread' }).fill('   ');
    await row.getByRole('textbox', { name: 'Rename thread' }).press('Enter'); // trims to empty
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('first');
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
  });
});

test.describe('a thread command that fails shows the old thread and a notice', () => {
  test.use({ backendArgs: ['--thread-command-error'] });
  test('the old title stays and the notice explains why', async ({ page, backend }) => {
    // A single thread, not a second one made via New chat: --thread-command-error patches
    // all four thread methods, threadNew included, so a New chat here would fail too and
    // never produce a second thread to switch to — this proves the same thing (the old
    // state stays, the notice says why) through threadRename instead, needing only one.
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'Chats' }).click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await row.hover();
    await row.getByRole('button', { name: 'Rename' }).click();
    await row.getByRole('textbox', { name: 'Rename thread' }).fill('Renamed');
    await row.getByRole('textbox', { name: 'Rename thread' }).press('Enter');
    // The rename failed (router.py's own generic except Exception turns ANY exception into
    // internal_error): the title stays "first" — the server's own state never moved — and
    // the notice says why, the same as any other failed command.
    await expect(row.locator('[data-slot="aui_thread-list-item-title"]')).toHaveText('first');
    await expect(page.locator('.t2v-notice-text')).toHaveText('Something went wrong.');
  });
});

test.describe('an approval pulls the list back to the thread', () => {
  test.use({ backendArgs: ['--chunk-delay', '0.02'] });
  test('brings the thread back, the same as Settings', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve now');
    await expect(page.getByRole('button', { name: 'Chats' })).toBeEnabled();
    await page.getByRole('button', { name: 'Chats' }).click();
    await expect(page.locator('.t2v-approval')).toBeVisible();
    await expect(page.locator('.t2v-chat-thread-list')).toHaveCount(0);
  });
});

test.describe('the running row spinner', () => {
  // The scripted reply is 85 words; this test waits for the whole turn to finish, so the
  // delay has to stay well under Playwright's 15s default assertion timeout (0.2s/word,
  // used elsewhere in this file where nothing waits for completion, would be 17s here).
  test.use({ backendArgs: ['--chunk-delay', '0.1'] });
  test('the current thread row shows a spinner and Running while it streams, gone once settled', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    const chats = page.getByRole('button', { name: 'Chats' });
    await expect(chats).toBeEnabled();
    await chats.click();
    const row = page.locator('[data-slot="aui_thread-list-item"]').first();
    await expect(row.locator('.t2v-thread-running')).toBeVisible();
    await expect(row).toContainText('Running'); // sr-only, still in the DOM
    await chats.click(); // back to the thread; let the turn finish
    await expect(lastReply(page)).toContainText('Segmentation plan');
    // isLoading can still be true for a moment after the last chunk paints, before the
    // stream's own end settles it — wait for Send to reappear (the turn is genuinely
    // over, the same idiom looks.spec.ts uses), not just for the text to have arrived.
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    await chats.click();
    await expect(row.locator('.t2v-thread-running')).toHaveCount(0);
  });
});

test.describe('New chat and switching abandon a running turn, not cancel it', () => {
  test.use({ backendArgs: ['--chunk-delay', '0.2'] });

  test('New chat stays enabled mid-stream and detaches instead of cancelling', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    const newChat = page.getByRole('button', { name: 'New chat' });
    // TS never disables its own New chat; threadNew abandons a running turn the same way
    // a switch does, so it gets the same rule (newSession, the no-thread-list path, keeps
    // its own disabled-while-isLoading guard, unaffected by this).
    await expect(newChat).toBeEnabled();
    await newChat.click();
    await expect(page.locator('h1.aui-modal-title')).toHaveText('New chat');
    expect(backend.controllerCalls).not.toContain('cancel');
    expect(backend.controllerCalls).not.toContain('clear');
  });

  test('switching mid-stream restores the other thread, abandoning the run with no notice', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'first');
    await expect(lastReply(page)).toContainText('Segmentation plan');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'second'); // streaming, slowed by --chunk-delay
    const chats = page.getByRole('button', { name: 'Chats' });
    await expect(chats).toBeEnabled();
    await chats.click();
    const rows = page.locator('[data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(2);
    await rows.filter({ hasText: 'first' }).locator('[data-slot="aui_thread-list-item-trigger"]').click();
    await expect(page.locator('h1.aui-modal-title')).toHaveText('first');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('first');
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
    expect(backend.controllerCalls).not.toContain('cancel');
  });
});
