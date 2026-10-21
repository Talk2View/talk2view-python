/**
 * A stored step this malformed no longer
 * reaches a page at all — `conversations.py` drops it on read, and
 * `restore_conversation`'s validator rejects the whole snapshot before it is applied — but
 * a LIVE turn's steps are built directly by whatever is behind the chat (a real engine, or
 * here, `backend.py`'s `/corruptstep`), with no validator in front of them at all.
 *
 * The primary defence is `runtime.ts`'s `convertMessage`: one message's conversion is
 * caught on its own, so a message assistant-ui's vendored `convert.ts` cannot render
 * (`stepPart` reads a step's `status` unguarded) becomes its own row — "This message could
 * not be shown." — instead of throwing. That throw happens inside
 * `useExternalStoreRuntime`'s own bookkeeping, called from `useEmbedRuntime` above
 * `<Shell>` in `ui/App.tsx` (`Connected`) — an error boundary around `<Thread>` alone,
 * a DESCENDANT of that call, can never see it (a boundary only catches a descendant's
 * throw, never an ancestor's). `ui/ThreadErrorBoundary.tsx` is kept around `<Thread>`
 * regardless, as a backstop for a crash that originates inside the thread view's own
 * render instead (a bug in `ToolCall.tsx`/`Markdown.tsx`, say) — this spec does not
 * reach that path, since the primary defence already catches everything `/corruptstep`
 * can throw, but the full suite exercises normal rendering through it on every other spec
 * without it ever firing, which is the property that matters for a backstop.
 */
import { expect, sendMessage, test } from './fixtures';

test.describe('a message the thread cannot render (runtime.ts convertMessage)', () => {
  test('shows its own row instead of crashing the page, and the rest of the chat still works', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    await expect(page.locator('.t2v-welcome-heading')).toBeVisible();

    await sendMessage(page, '/corruptstep');
    // Not blank, and not a raised page error: the user's own message renders normally,
    // and the unshowable reply becomes its own row.
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('/corruptstep');
    await expect(page.locator('.t2v-msg-assistant').last()).toHaveText('This message could not be shown.');
    // The rest of the page is unaffected: header, composer and the thread list all work.
    await expect(page.locator('h1.aui-modal-title')).toHaveText('/corruptstep');
    const chats = page.getByRole('button', { name: 'Chats' });
    await expect(chats).toBeEnabled();

    await page.getByRole('button', { name: 'New chat' }).click();
    await expect(page.locator('.t2v-welcome-heading')).toBeVisible();
    await sendMessage(page, 'a fine second chat');
    await expect(page.locator('.t2v-msg-assistant').last()).not.toHaveText('This message could not be shown.');

    // Switching back to the corrupted chat shows the same row again, not a crash: the
    // fallback is recomputed on every render, not a one-off patch to stored state.
    await chats.click();
    const rows = page.locator('.t2v-chat-thread-list [data-slot="aui_thread-list-item"]');
    await expect(rows).toHaveCount(2);
    await rows.nth(1).locator('[data-slot="aui_thread-list-item-trigger"]').click(); // newest first
    await expect(page.locator('.t2v-msg-assistant').last()).toHaveText('This message could not be shown.');
  });

  test('a live, non-array steps does not blank the page either', async ({ page, backend }) => {
    // Not a bad element in the list (`/corruptstep`'s [None]) -- `steps` itself is truthy
    // but not a list at all, which `toDisplayMessage`'s own `m.steps.map(toStep)` throws
    // on (selectors.ts). Before R4's fix, `toDisplayMessage` ran outside `convertMessage`'s
    // try, so this specific shape crashed the page even with the try/catch in place.
    await page.goto(backend.url);
    await sendMessage(page, '/nonarraysteps');
    await expect(page.locator('.t2v-msg-user .t2v-bubble')).toHaveText('/nonarraysteps');
    await expect(page.locator('.t2v-msg-assistant').last()).toHaveText('This message could not be shown.');
    await expect(page.locator('h1.aui-modal-title')).toHaveText('/nonarraysteps');
    await expect(page.getByRole('button', { name: 'Chats' })).toBeEnabled();
  });
});
