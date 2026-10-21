import { expect, lastReply, sendMessage, test } from './fixtures';

test.describe('turn timings (real Talk2View + ChatController)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });

  test('the page reports its part of a turn once, and nothing it shows', async ({ page, backend }) => {
    const commands: Array<Record<string, unknown>> = [];
    page.on('request', (request) => {
      if (request.url().endsWith('/command')) commands.push(request.postDataJSON() as Record<string, unknown>);
    });
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();

    await sendMessage(page, 'hello there');
    await expect(lastReply(page)).toHaveText(/^ok/);
    await expect.poll(() => backend.turns.length, { timeout: 10000 }).toBe(1);

    const turn = backend.turns[0]!;
    const ui = turn.ui as Record<string, unknown>;
    expect(turn.outcome).toBe('done');
    expect(ui.reported).toBe(true);
    expect(typeof ui.send_to_post_ms).toBe('number');
    expect(typeof ui.first_op_to_paint_ms).toBe('number');
    expect(typeof ui.queue_ms).toBe('number');
    expect(typeof ui.delivery_ms).toBe('number');
    const reports = commands.filter((c) => c.type === 'metrics');
    expect(reports).toHaveLength(1);
    expect(Object.keys(reports[0]!).sort()).toEqual(
      ['firstOpAtMs', 'firstOpToPaintMs', 'id', 'messageId', 'sendToPostMs', 'type'],
    );
    expect(JSON.stringify(turn)).not.toContain('hello there');
    await expect(page.locator('.t2v-notice')).toHaveCount(0);
  });
});
