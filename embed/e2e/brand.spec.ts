import { expect, sendMessage, test } from './fixtures';

// The page is co-branded: the Talk2View mark is the favicon and the
// sign-in view's lockup, and the chat's own header keeps the host's title.

test.describe('the sign-in view (FakeController, no guests)', () => {
  test.use({ backendArgs: ['--no-anonymous'] });

  test('welcomes the end-user to Talk2View under the lockup', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
    const lockup = page.getByRole('img', { name: 'Talk2View' });
    await expect(lockup).toBeVisible();
    // Above the heading.
    const [mark, heading] = await Promise.all([
      lockup.boundingBox(),
      page.getByRole('heading', { name: 'Welcome to Talk2View' }).boundingBox(),
    ]);
    expect(mark!.y + mark!.height).toBeLessThanOrEqual(heading!.y);
  });
});

test('sign-up keeps its own heading, under the same lockup', async ({ page, backend }) => {
  await page.goto(backend.url);
  await sendMessage(page, '/limit');
  await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Talk2View' })).toBeVisible();
});

test('the favicon is the Teal logomark', async ({ page, backend }) => {
  await page.goto(backend.url);
  const href = await page.locator('link[rel="icon"]').getAttribute('href');
  expect(href).toMatch(/^data:image\/svg\+xml,/);
  expect(decodeURIComponent(href!)).toContain('#26C8B8');
});

test('the chat header keeps the host title, with no Talk2View mark', async ({ page, backend }) => {
  await page.goto(backend.url);
  await expect(page.locator('.t2v-header .t2v-lockup')).toHaveCount(0);
});
