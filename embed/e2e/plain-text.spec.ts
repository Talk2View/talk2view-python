/**
 * The plain-text rule, checked: every surface that shows untrusted text (from the engine, a model, a
 * tool, a file or a skill) as plain text goes through `displayText`; Markdown (a reply, the
 * plan) does not, and neither does the host's own copy (`ChatOptions`), which is shown as
 * given. backend.py's --hostile puts one string with a right-to-left override into every
 * untrusted field it can reach: the agent's status, a tool's name (finished, and on the
 * approval card), arguments, result, image name and image-failure reason, the error, a
 * refused sign-in's reason, the signed-in email, the model names, the skills, a provider
 * sign-in's failure and a refused provider start. The page
 * may show it only as the visible escape: never the raw character, in any text node,
 * attribute or field value. A surface added later that draws one of these fields without
 * `displayText` fails here. Notices, the approval prompt and details, and attachment names
 * have hostile-string specs of their own (the attachments, approval, account and chat
 * specs); the "Check your email" address is the visitor's own typing, which the email
 * field's validation keeps free of such characters.
 * The same run gives the host's copy a ZWNJ, which must reach the page as it was written.
 */
import type { Page } from '@playwright/test';
import { expect, sendMessage, test } from './fixtures';

const RLO = String.fromCodePoint(0x202e);
/** How the page shows backend.py's HOSTILE: the override as a visible escape. */
const SHOWN = 'report\\u202Egnp.exe';
/** backend.py's HOST_WORD: Persian, with the ZWNJ (U+200C) the host's copy needs. */
const HOST_WORD = String.fromCodePoint(0x0645, 0x06cc, 0x200c, 0x062e, 0x0648, 0x0627, 0x0646, 0x0645);

/** Where the raw override appears on the page: text nodes, attributes, field values. */
async function rawOverrides(page: Page): Promise<string[]> {
  return page.evaluate((rlo) => {
    const found: string[] = [];
    const where = (el: Element | null) => '<' + (el?.tagName.toLowerCase() ?? '?') + ' class="' + String(el?.getAttribute('class') ?? '') + '">';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.nodeValue?.includes(rlo)) found.push('text in ' + where(node.parentElement));
    }
    for (const el of Array.from(document.body.querySelectorAll('*'))) {
      for (const attr of Array.from(el.attributes)) if (attr.value.includes(rlo)) found.push(attr.name + ' of ' + where(el));
      if ((el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) && el.value.includes(rlo)) found.push('value of ' + where(el));
    }
    return found;
  }, RLO);
}

test.describe('every plain-text surface is escaped (FakeController)', () => {
  test.use({ backendArgs: ['--hostile'] });

  test('the status line, a running and a finished tool, the error card, the approval card\'s tool name', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.locator('.t2v-welcome-heading')).toBeVisible();
    expect(await rawOverrides(page)).toEqual([]);

    await sendMessage(page, '/hostile');
    await expect(page.locator('.t2v-working')).toHaveText('Reading ' + SHOWN); // the status line
    expect(await rawOverrides(page)).toEqual([]);
    await expect(page.locator('.t2v-tool-activity')).toBeVisible(); // a running tool
    expect(await rawOverrides(page)).toEqual([]);
    await expect(page.locator('.t2v-error-text')).toHaveText('Could not open ' + SHOWN); // the error card
    await expect(page.locator('.t2v-tool-note')).toHaveText([
      'Generated image: ' + SHOWN,
      'The image could not be generated (' + SHOWN + ').', // the failure's reason (M-D)
    ]);
    await page.locator('.t2v-tool-head').first().click(); // open the first finished tool
    await expect(page.locator('.t2v-tool-result')).toHaveText(SHOWN);
    await expect(page.locator('.t2v-tool-args')).toContainText(SHOWN);
    expect(await rawOverrides(page)).toEqual([]);

    await page.locator('.t2v-error').getByRole('button', { name: 'Dismiss' }).click();
    await sendMessage(page, '/hostileapprove');
    await expect(page.locator('.t2v-approval-title')).toHaveText('The assistant wants to run ' + SHOWN);
    expect(await rawOverrides(page)).toEqual([]);
  });

  test('the host\'s own copy is shown as given, its ZWNJ unescaped', async ({ page, backend }) => {
    const exactly = async (selector: string, text: string) => {
      await expect(page.locator(selector)).toHaveText(text);
      await expect(page.locator(selector)).not.toContainText('\\u200C');
      expect(await page.locator(selector).evaluate((el) => el.textContent ?? '')).toContain(String.fromCodePoint(0x200c));
    };
    await page.goto(backend.url);
    await exactly('.t2v-welcome-heading', 'Ask about ' + HOST_WORD);
    await exactly('.t2v-suggestion', HOST_WORD);
    await exactly('.t2v-footnote', 'Check ' + HOST_WORD);

    await sendMessage(page, '/hostile');
    await exactly('.t2v-tool-activity', 'Loading ' + HOST_WORD); // a running tool's activity
    await page.locator('.t2v-error').getByRole('button', { name: 'Dismiss' }).click();
    await sendMessage(page, '/approve now');
    await exactly('.t2v-approval .t2v-warning', 'Deletes ' + HOST_WORD); // the host's warning
  });

  test('the page\'s own destructive warning names the host\'s activity as given (M-E)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/hostdestructive');
    const line = page.locator('.t2v-approval .t2v-warning');
    await expect(line).toHaveText('This will modify your work: Resetting ' + HOST_WORD + '. Review before approving.');
    await expect(line).not.toContainText('\\u200C');
    expect(await line.evaluate((el) => el.textContent ?? '')).toContain(String.fromCodePoint(0x200c));
  });
});

test.describe('every plain-text surface is escaped (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--hostile'] });

  test('a refused sign-in, the signed-in email, the model picker and the skills', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('wrong');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.locator('.t2v-form-error')).toHaveText('No account named ' + SHOWN); // Account's refusal
    expect(await rawOverrides(page)).toEqual([]);

    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeVisible();
    await page.getByRole('button', { name: 'Account' }).click();
    await expect(page.locator('.t2v-account .t2v-truncate')).toHaveText('ana.' + SHOWN + '@example.com');
    expect(await rawOverrides(page)).toEqual([]);

    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByText('From this app’s publisher')).toBeVisible();
    await expect(page.getByLabel('AI model').locator('option')).toHaveText([
      'Default — Fast ' + SHOWN,
      'Fast ' + SHOWN,
      'm-deep ' + SHOWN,
    ]);
    await expect(page.getByRole('checkbox', { name: 'skill-' + SHOWN, exact: true })).toBeVisible();
    await expect(page.locator('.t2v-skill-static')).toContainText('team-brief' + SHOWN);
    expect(await rawOverrides(page)).toEqual([]);
  });
});

test.describe('provider sign-in surfaces are escaped (account mode)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--hostile', '--desktop-sign-in'] });

  test('the failure of a sign-in and a refused start', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    const apple = page.getByRole('button', { name: 'Continue with Apple' });
    await expect(apple).toBeEnabled();
    // The engine's own /desktop/token refusal, which the SDK passes on as oauthError.
    await apple.click();
    await expect(page.getByRole('alert')).toContainText('Sign-in failed for ' + SHOWN);
    expect(await rawOverrides(page)).toEqual([]);
    // A refused start's reason (not `busy`): the server's message, shown as text.
    await page.route('**/command', async (route) => {
      const body = route.request().postDataJSON() as { type?: string } | null;
      if (body?.type !== 'oauthStart') return route.continue();
      const message = 'No sign-in with report' + RLO + 'gnp.exe';
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ ok: false, error: { type: 'oauth_unavailable', message } }),
      });
    });
    await page.getByRole('button', { name: 'Continue with Google' }).click();
    await expect(page.getByRole('alert')).toHaveText('No sign-in with ' + SHOWN);
    expect(await rawOverrides(page)).toEqual([]);
  });
});
