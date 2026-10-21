/**
 * The render checks for how the UI looks: every visual rule is asserted on computed
 * styles, in both palettes, against the real UI server (e2e/backend.py). A screenshot
 * compared by eye is never enough on its own here.
 */
import type { Page } from '@playwright/test';
import { expect, lastReply, sendMessage, signIn, test } from './fixtures';

/** Every `--t2v-*` custom property on <html>, resolved as Chromium reports it. */
export async function tokens(page: Page, names: string[]): Promise<Record<string, string>> {
  return page.evaluate((wanted) => {
    const style = getComputedStyle(document.documentElement);
    const out: Record<string, string> = {};
    for (const name of wanted) out[name] = style.getPropertyValue(name).trim();
    return out;
  }, names);
}

/** A computed property of the first element matching `selector`. */
export async function computed(page: Page, selector: string, property: string): Promise<string> {
  return page.evaluate(
    ([sel, prop]) => {
      const el = document.querySelector(sel!);
      if (!el) throw new Error('no element matches ' + sel);
      return getComputedStyle(el).getPropertyValue(prop!);
    },
    [selector, property],
  );
}

const SEMANTIC = [
  '--t2v-background', '--t2v-foreground', '--t2v-card', '--t2v-card-foreground', '--t2v-popover',
  '--t2v-popover-foreground', '--t2v-primary', '--t2v-primary-foreground', '--t2v-secondary',
  '--t2v-secondary-foreground', '--t2v-muted', '--t2v-muted-foreground', '--t2v-accent',
  '--t2v-accent-foreground', '--t2v-destructive', '--t2v-border', '--t2v-input', '--t2v-ring',
  '--t2v-radius', '--t2v-composer-radius', '--t2v-composer-bg', '--t2v-composer-padding',
];
const BRAND = ['--t2v-teal', '--t2v-smoke', '--t2v-ivory', '--t2v-mist', '--t2v-stone', '--t2v-slate', '--t2v-ash', '--t2v-highlight', '--t2v-harbor', '--t2v-amber'];

// This installed Chromium (bundled with @playwright/test 1.60.0) substitutes var()
// references when it serialises a custom property's computed value, and reserialises a
// sub-1 decimal without its leading zero (0.5rem -> .5rem); it does not quote the
// declaration verbatim. The expectations below are the RESOLVED values, read off this
// browser's actual getComputedStyle output, not the source text of app.css.
const LIGHT: Record<string, string> = {
  '--t2v-background': '#ffffff', '--t2v-foreground': '#021C25', '--t2v-card': '#ffffff',
  '--t2v-card-foreground': '#021C25', '--t2v-popover': '#ffffff', '--t2v-popover-foreground': '#021C25',
  '--t2v-primary': '#26C8B8', '--t2v-primary-foreground': '#021C25', '--t2v-secondary': '#F2F0EC',
  '--t2v-secondary-foreground': '#021C25', '--t2v-muted': '#CAEBEB', '--t2v-muted-foreground': '#67777C',
  '--t2v-accent': '#CAEBEB', '--t2v-accent-foreground': '#021C25', '--t2v-destructive': '#BB524E',
  '--t2v-border': '#9AA4A8', '--t2v-input': '#9AA4A8', '--t2v-ring': '#26C8B8',
  '--t2v-radius': '0rem', '--t2v-composer-radius': '0rem', '--t2v-composer-bg': '#ffffff', '--t2v-composer-padding': '.5rem',
};
const DARK: Record<string, string> = {
  ...LIGHT,
  '--t2v-background': '#021C25', '--t2v-foreground': '#F2F0EC', '--t2v-card': '#0b2731',
  '--t2v-card-foreground': '#F2F0EC', '--t2v-popover': '#0b2731', '--t2v-popover-foreground': '#F2F0EC',
  '--t2v-secondary': '#354951', '--t2v-secondary-foreground': '#F2F0EC', '--t2v-muted': '#354951',
  '--t2v-muted-foreground': '#9AA4A8', '--t2v-accent': '#354951', '--t2v-accent-foreground': '#F2F0EC',
  '--t2v-border': '#354951', '--t2v-input': '#354951', '--t2v-ring': '#73FFFF',
  '--t2v-composer-bg': '#021C25',
};
const BRAND_VALUES: Record<string, string> = {
  '--t2v-teal': '#26C8B8', '--t2v-smoke': '#021C25', '--t2v-ivory': '#F2F0EC', '--t2v-mist': '#CAEBEB', '--t2v-stone': '#9AA4A8',
  '--t2v-slate': '#67777C', '--t2v-ash': '#354951', '--t2v-highlight': '#73FFFF', '--t2v-harbor': '#608B8D', '--t2v-amber': '#F0A74A',
};

// The opacity variants: page-only tokens (TS writes them as a
// Tailwind `x/NN` opacity utility, resolved here as rgba since there is no color-mix on
// Chromium 83), so check-token-parity.mjs does not compare them and only these specs do.
// Seven page-only names were retired (--t2v-hover-wash, --t2v-muted-50/40/30,
// --t2v-border-50, --t2v-primary-60, --t2v-foreground-90): every use in src/ and e2e/ was
// grepped first and moved onto --t2v-wash/--t2v-hairline/--t2v-hairline-strong instead.
const VARIANTS = [
  '--t2v-hairline', '--t2v-hairline-strong', '--t2v-muted-foreground-60',
  '--t2v-muted-foreground-30', '--t2v-muted-foreground-20', '--t2v-amber-50', '--t2v-amber-15',
  '--t2v-destructive-10', '--t2v-destructive-20', '--t2v-error-foreground',
  '--t2v-wash', '--t2v-wash-2', '--t2v-shadow', '--t2v-destructive-30', '--t2v-destructive-8',
  '--t2v-ring-soft',
];
const VARIANTS_LIGHT: Record<string, string> = {
  '--t2v-hairline': 'rgba(2, 28, 37, .1)', '--t2v-hairline-strong': 'rgba(2, 28, 37, .22)',
  '--t2v-muted-foreground-60': 'rgba(103, 119, 124, .6)',
  '--t2v-muted-foreground-30': 'rgba(103, 119, 124, .3)', '--t2v-muted-foreground-20': 'rgba(103, 119, 124, .2)',
  '--t2v-amber-50': 'rgba(240, 167, 74, .5)',
  '--t2v-amber-15': 'rgba(240, 167, 74, .15)', '--t2v-destructive-10': 'rgba(187, 82, 78, .1)',
  '--t2v-destructive-20': 'rgba(187, 82, 78, .2)',
  '--t2v-error-foreground': '#BB524E',
  '--t2v-wash': 'rgba(2, 28, 37, .04)', '--t2v-wash-2': 'rgba(2, 28, 37, .07)',
  '--t2v-shadow': '0 2px 8px rgba(2, 28, 37, .14)', '--t2v-destructive-30': 'rgba(187, 82, 78, .35)',
  '--t2v-destructive-8': 'rgba(187, 82, 78, .08)', '--t2v-ring-soft': 'rgba(38, 200, 184, .35)',
};
const VARIANTS_DARK: Record<string, string> = {
  ...VARIANTS_LIGHT,
  '--t2v-hairline': 'rgba(242, 240, 236, .1)', '--t2v-hairline-strong': 'rgba(242, 240, 236, .22)',
  '--t2v-wash': 'rgba(242, 240, 236, .05)', '--t2v-wash-2': 'rgba(242, 240, 236, .09)',
  '--t2v-shadow': '0 2px 10px rgba(0, 0, 0, .45)', '--t2v-destructive-30': 'rgba(187, 82, 78, .45)',
  '--t2v-destructive-8': 'rgba(187, 82, 78, .14)', '--t2v-ring-soft': 'rgba(115, 255, 255, .35)',
  '--t2v-muted-foreground-60': 'rgba(154, 164, 168, .6)',
  '--t2v-muted-foreground-30': 'rgba(154, 164, 168, .3)', '--t2v-muted-foreground-20': 'rgba(154, 164, 168, .2)',
  '--t2v-destructive-10': 'rgba(187, 82, 78, .05)',
  '--t2v-error-foreground': '#ffc9c9',
};

test.describe('the TS token set, light', () => {
  test('every semantic and brand token carries the TS value', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    expect(await tokens(page, SEMANTIC)).toEqual(LIGHT);
    expect(await tokens(page, BRAND)).toEqual(BRAND_VALUES);
    expect(await tokens(page, VARIANTS)).toEqual(VARIANTS_LIGHT);
    // ...and they resolve: the ground is white, the text Smoke, the primary Teal.
    expect(await computed(page, 'body', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, 'body', 'color')).toBe('rgb(2, 28, 37)');
    // The empty composer's Send starts disabled (wash-2) — its own
    // caret-color is unconditionally Teal instead, the same resolved-token check.
    expect(await computed(page, '.t2v-input', 'caret-color')).toBe('rgb(38, 200, 184)');
  });
});

test.describe('the TS token set, dark', () => {
  test.use({ backendArgs: ['--dark'] });

  test('the dark palette redeclares what chat.src.css redeclares, and nothing else', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    expect(await tokens(page, SEMANTIC)).toEqual(DARK);
    expect(await tokens(page, BRAND)).toEqual(BRAND_VALUES);
    expect(await tokens(page, VARIANTS)).toEqual(VARIANTS_DARK);
    expect(await computed(page, 'body', 'background-color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, 'body', 'color')).toBe('rgb(242, 240, 236)');
    expect(await computed(page, '.t2v-input', 'caret-color')).toBe('rgb(38, 200, 184)');
  });
});

test.describe('the header and the frame', () => {
  test('44 px, TS classes, "New chat", 28 px buttons with 16 px icons, the footer line under the body', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    const header = page.locator('.t2v-chat-header.aui-modal-header');
    await expect(header).toBeVisible();
    expect((await header.boundingBox())!.height).toBe(44);
    await expect(page.locator('h1.aui-modal-title')).toHaveText('New chat');
    expect(await computed(page, 'h1.aui-modal-title', 'font-size')).toBe('13px');
    expect(await computed(page, 'h1.aui-modal-title', 'font-weight')).toBe('500');
    expect(await computed(page, '.t2v-chat-header', 'padding-left')).toBe('16px');
    expect(await computed(page, '.t2v-chat-header', 'padding-right')).toBe('8px');
    expect(await computed(page, '.t2v-chat-header', 'border-bottom-color')).toBe('rgba(2, 28, 37, 0.1)');
    expect(await computed(page, '.t2v-chat-header', 'background-color')).toBe('rgb(255, 255, 255)');
    const settings = page.getByRole('button', { name: 'Settings' });
    const box = (await settings.boundingBox())!;
    expect([box.width, box.height]).toEqual([28, 28]);
    expect(await settings.locator('svg').evaluate((el) => el.getBoundingClientRect().width)).toBe(16);
    // Spacing: 8px title-to-group (TS gap-2), 2px between buttons in
    // the group (TS gap-0.5) — not 10px on the first button too.
    const title = (await page.locator('h1.aui-modal-title').boundingBox())!;
    const account = (await page.getByRole('button', { name: 'Account', exact: true }).boundingBox())!;
    expect(Math.round(account.x - (title.x + title.width))).toBe(8);
    // Skills sits right after Account now (as TS's), then Settings: 2px apart each.
    const skills = (await page.getByRole('button', { name: 'Skills', exact: true }).boundingBox())!;
    expect(Math.round(skills.x - (account.x + account.width))).toBe(2);
    expect(Math.round(box.x - (skills.x + skills.width))).toBe(2);
    await settings.click();
    expect(await computed(page, 'button[aria-label="Settings"]', 'background-color')).toBe('rgba(2, 28, 37, 0.07)'); // aria-pressed: wash-2
    await settings.click();
    // The caption sits inside the composer's own footer now —
    // no separate strip, no border of its own; the panel's bottom is composer + pads.
    const caption = page.locator('.t2v-footer .t2v-caption');
    await expect(caption).toHaveText('Answers can be wrong. Check them before you act.');
    expect(await computed(page, '.t2v-caption', 'font-size')).toBe('11px');
    expect(await page.locator('.t2v-chat-footer').count()).toBe(0);
    // Scrollbars: thin Stone thumb on nothing.
    expect(await computed(page, '.t2v-viewport', 'overflow-y')).toBe('scroll');
  });
});

test.describe('the header, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('one Smoke surface for header, thread and views; wash-2 on the pressed button', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    // One surface: header, thread and views are all Smoke; #0b2731 is now the raised card colour.
    expect(await computed(page, '.t2v-chat-header', 'background-color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, '.t2v-viewport', 'background-color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, '.t2v-chat-header', 'border-bottom-color')).toBe('rgba(242, 240, 236, 0.1)');
    await page.getByRole('button', { name: 'Settings' }).click();
    expect(await computed(page, '.t2v-view', 'background-color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, 'button[aria-label="Settings"]', 'background-color')).toBe('rgba(242, 240, 236, 0.09)');
  });
});

test.describe('the banner, stopped', () => {
  test.use({ backendArgs: ['--mode', 'account'] });
  test('12/16 centred, on the gutter, not a bare 14px', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Account' }).locator('.t2v-dot')).toBeVisible();
    // An /upload answer of "stopped" ends the chat the same as a refused
    // command would — the simplest existing way to reach conn.phase 'over' for this check.
    await page.route('**/upload', (route) =>
      route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ ok: false, error: { type: 'stopped', message: 'no' } }) }),
    );
    const chooser = page.waitForEvent('filechooser');
    await page.getByRole('button', { name: 'Attach a file' }).click();
    await (await chooser).setFiles({ name: 'a.png', mimeType: 'image/png', buffer: Buffer.from('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c40000000049454e44ae426082', 'hex') });
    const banner = page.getByText('This chat has stopped. Open it again from the app.');
    await expect(banner).toBeVisible();
    expect(await computed(page, '.t2v-banner', 'font-size')).toBe('12px');
    expect(await computed(page, '.t2v-banner', 'line-height')).toBe('16px');
    expect(await computed(page, '.t2v-banner', 'text-align')).toBe('center');
    expect(await computed(page, '.t2v-banner', 'padding-left')).toBe('16px');
  });
});

test.describe('the welcome and the openers', () => {
  test('a 24 px medium heading, openers as full-width rows under the composer, hidden once typing starts', async ({ page, backend }) => {
    await page.goto(backend.url);
    const heading = page.locator('h2.aui-thread-welcome-message-inner');
    await expect(heading).toHaveText('Ask about your scene');
    expect(await computed(page, 'h2.aui-thread-welcome-message-inner', 'font-size')).toBe('24px');
    expect(await computed(page, 'h2.aui-thread-welcome-message-inner', 'font-weight')).toBe('500');
    expect(await computed(page, 'h2.aui-thread-welcome-message-inner', 'letter-spacing')).toBe('-0.6px');
    const sub = page.locator('.t2v-welcome-sub');
    await expect(sub).toHaveText('Load data, change views and run tools by talking to Slicer.');
    expect(await computed(page, '.t2v-welcome-sub', 'font-size')).toBe('14px');
    expect(await computed(page, '.t2v-welcome-sub', 'color')).toBe('rgb(103, 119, 124)');
    expect(await computed(page, '.t2v-welcome-sub', 'margin-top')).toBe('6px');
    const opener = page.getByRole('button', { name: 'Load MRHead' });
    await expect(opener).toHaveClass(/aui-thread-welcome-suggestion/);
    // Under the composer: the opener's top is below the composer's bottom.
    const composer = (await page.locator('.t2v-composer').boundingBox())!;
    const row = (await opener.boundingBox())!;
    expect(row.y).toBeGreaterThan(composer.y + composer.height);
    expect(row.width).toBe(composer.width);
    expect(await computed(page, '.aui-thread-welcome-suggestion', 'font-size')).toBe('14px');
    expect(await computed(page, '.aui-thread-welcome-suggestion', 'padding-left')).toBe('10px');
    await expect(opener.locator('.t2v-suggestion-mark')).toHaveText('>');
    // A brand call (found via a built-vs-mockup screenshot pass): Teal — was gray
    // (--t2v-muted-foreground-60) here before, never actually applied.
    expect(await computed(page, '.t2v-suggestion-mark', 'color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-suggestion-mark', 'font-weight')).toBe('500');
    expect(await computed(page, '.t2v-suggestion-mark', 'width')).toBe('14px');
    // Typing hides the openers (TS: `s.composer.isEmpty`), and clearing brings them back.
    await page.getByRole('textbox', { name: 'Message' }).fill('x');
    await expect(opener).toBeHidden();
    await page.getByRole('textbox', { name: 'Message' }).fill('');
    await expect(opener).toBeVisible();
    // The entry animation is transform/opacity only, 200 ms, and honours reduced motion.
    expect(await computed(page, '.aui-thread-welcome-message-inner', 'animation-duration')).toBe('0.2s');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    expect(await computed(page, '.aui-thread-welcome-message-inner', 'animation-name')).toBe('none');
  });

  for (const [w, h] of [
    [480, 800],
    [320, 480],
  ] as const) {
    test(`an empty thread centres the welcome and the openers as one group at ${w}x${h}`, async ({ page, backend }) => {
      // thread.aui.tsx: `justify-center` while empty, so the space above the welcome and
      // the space below the openers (the group's own top and bottom) agree — not the
      // welcome floating alone above a composer already pinned to the bottom by its own
      // margin (the bug: 234.5px above the welcome, 16px below the openers, at 480x800).
      await page.setViewportSize({ width: w, height: h });
      await page.goto(backend.url);
      await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
      const gap = await page.evaluate(() => {
        const viewport = document.querySelector('.t2v-viewport')!.getBoundingClientRect();
        const welcome = document.querySelector('.t2v-welcome')!.getBoundingClientRect();
        const openers = document.querySelector('.t2v-suggestions')!.getBoundingClientRect();
        return { above: welcome.top - viewport.top, below: viewport.bottom - openers.bottom };
      });
      // The group's two auto margins (.t2v-welcome's top, .t2v-footer's bottom,
      // while empty) still split the leftover space evenly, but the FIXED offsets on each
      // side of that split are no longer equal — the viewport's own top padding (16px)
      // and the footer's own bottom padding (12px, from "composer + 8 + 16 + 12"),
      // each the right value for its own convention. The gap is exactly that 4px
      // difference now, not near zero.
      expect(gap.above - gap.below, 'space above the welcome vs below the openers: ' + JSON.stringify(gap)).toBeGreaterThanOrEqual(2);
      expect(gap.above - gap.below, 'space above the welcome vs below the openers: ' + JSON.stringify(gap)).toBeLessThanOrEqual(6);
      // The footer's own position: sticky is dormant while centred, not overridden — once
      // there is something to scroll it resumes pinning to the bottom (guard/autorun.ts's
      // auto:sticky step, moved out of the probes for the same reason).
      expect(await computed(page, '.t2v-footer', 'position')).toBe('sticky');
    });
  }

  for (const [w, h] of [
    [320, 330],
    [320, 260],
  ] as const) {
    test(`the welcome heading is reachable, not clipped by centring, on a dock too short for the group at ${w}x${h}`, async ({
      page,
      backend,
    }) => {
      // `justify-content: center` on the scroll container itself split the overflow
      // evenly above and below, leaving the top half — the welcome heading, the first
      // thing on screen — permanently scrolled above scrollTop 0 with nothing able to
      // reach it (measured: -8.5px at 320x330, -43.5px at 320x260, before this fix).
      // Auto margins collapse to 0 on overflow instead, falling back to plain top
      // alignment.
      await page.setViewportSize({ width: w, height: h });
      await page.goto(backend.url);
      await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
      const welcomeTop = await page.evaluate(() => {
        const viewport = document.querySelector('.t2v-viewport')!;
        viewport.scrollTop = 0;
        return viewport.querySelector('.t2v-welcome')!.getBoundingClientRect().top - viewport.getBoundingClientRect().top;
      });
      expect(welcomeTop, 'welcome heading top relative to the viewport, at scrollTop 0').toBeGreaterThanOrEqual(0);
    });
  }
});

test.describe('the user bubble and the composer', () => {
  test('a Mist bubble with Smoke text, a 16 px composer input, round 28 px send', async ({ page, backend }) => {
    await page.goto(backend.url);
    const shell = page.locator('[data-slot="aui_composer-shell"]');
    await expect(shell).toBeVisible();
    // The composer auto-focuses on load (autoFocus, load-bearing for sendMessage() across
    // every other spec), so the shell starts in the FOCUSED state, not the unfocused one:
    // blur it first so this reads the real unfocused border, and poll rather than read
    // once, since the border-color transition (0.15s) has not necessarily settled yet.
    // The unfocused border is hairline-strong always now, not a
    // lighter hairline that only strengthened on focus.
    await page.getByRole('textbox', { name: 'Message' }).evaluate((el) => (el as HTMLElement).blur());
    await expect.poll(() => computed(page, '[data-slot="aui_composer-shell"]', 'border-top-color')).toBe('rgba(2, 28, 37, 0.22)');
    expect(await computed(page, '.t2v-input', 'min-height')).toBe('24px');
    expect(await computed(page, '.t2v-input', 'padding-top')).toBe('10px');
    expect((await page.locator('.t2v-composer-shell').boundingBox())!.height).toBe(76);
    expect(await computed(page, '[data-slot="aui_composer-shell"]', 'background-color')).toBe('rgb(255, 255, 255)');
    const input = page.getByRole('textbox', { name: 'Message' });
    await expect(input).toHaveAttribute('placeholder', 'Send a message...');
    expect(await computed(page, '.aui-composer-input', 'font-size')).toBe('16px');
    expect(await computed(page, '.aui-composer-input', 'line-height')).toBe('24px');
    expect(await computed(page, '.aui-composer-input', 'padding-left')).toBe('12px'); // 10px 12px 2px
    await page.getByRole('textbox', { name: 'Message' }).focus();
    // Polled, not read once: the border-color transition (0.15s) has not necessarily
    // settled to the focused Teal yet.
    await expect.poll(() => computed(page, '[data-slot="aui_composer-shell"]', 'border-top-color')).toBe('rgb(38, 200, 184)');
    expect(await page.getByRole('button', { name: 'Attach a file' }).locator('svg').getAttribute('class')).toContain('lucide-paperclip');
    expect(await computed(page, '.t2v-send:disabled', 'background-color')).toBe('rgba(2, 28, 37, 0.07)');
    // The rest mic's own colours (muted/transparent, one Teal send) are dictation.spec.ts's
    // own — this backend has no --audio, so .aui-composer-dictate does not exist here.
    const send = page.getByRole('button', { name: 'Send' });
    const box = (await send.boundingBox())!;
    expect([box.width, box.height]).toEqual([28, 28]);
    expect(await computed(page, '.aui-composer-send', 'border-top-left-radius')).toBe('9999px');
    // Typed first: the empty composer's own Send starts disabled — wash-2, not a
    // dimmed Teal — so its enabled Teal fill needs actual text to check.
    await input.fill('x');
    expect(await computed(page, '.aui-composer-send', 'background-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.aui-composer-send', 'color')).toBe('rgb(2, 28, 37)');
    await input.fill('');
    await sendMessage(page, 'hello');
    const bubble = page.locator('.aui-user-message-content');
    await expect(bubble).toHaveText('hello');
    expect(await computed(page, '.aui-user-message-content', 'background-color')).toBe('rgb(202, 235, 235)');
    expect(await computed(page, '.aui-user-message-content', 'color')).toBe('rgb(2, 28, 37)');
    // 14px, not 16.
    expect(await computed(page, '.aui-user-message-content', 'padding-left')).toBe('14px');
    expect(await computed(page, '.aui-user-message-content', 'padding-top')).toBe('8px');
    // Reply text inherits the page's own --t2v-font-size body base (Medium is TS's
    // 16px, matching thread.aui.tsx:539's un-classed
    // text, which inherits the browser's 16px default) rather than a literal that used
    // to skip the Text size setting entirely: leading-relaxed (1.625, unitless) at 16px
    // is 26px. The size-follows-the-setting test below proves this is inherited, not
    // coincidence.
    expect(await computed(page, '.aui-assistant-message-content', 'font-size')).toBe('16px');
    expect(await computed(page, '.aui-assistant-message-content', 'line-height')).toBe('26px');
  });

  test('the scroll-to-bottom button is a 32px round raised button with a shadow, 12px above the composer', async ({
    page,
    backend,
  }) => {
    await page.setViewportSize({ width: 320, height: 480 }); // small enough that the reply overflows
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    // The turn fully ENDED, not merely started: while still streaming, assistant-ui keeps
    // re-scrolling to the bottom itself on every chunk, which would race a manual scroll
    // attempt and win.
    await expect(lastReply(page).locator('.t2v-md pre code')).toContainText("load_sample_data('MRHead')");
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    // Scrolled away from the bottom: only then does assistant-ui enable the button
    // (`.t2v-scroll:disabled { visibility: hidden }` otherwise hides it). A real `scroll`
    // event, not just the property write: assistant-ui's own listener needs one to notice.
    await page.locator('.t2v-viewport').evaluate((el) => {
      el.scrollTop = 0;
      el.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    const button = page.getByRole('button', { name: 'Scroll to the latest' });
    await expect(button).toBeEnabled();
    const box = (await button.boundingBox())!;
    expect([box.width, box.height]).toEqual([32, 32]);
    expect(await computed(page, '.t2v-scroll', 'border-top-left-radius')).toBe('9999px');
    expect(await computed(page, '.t2v-scroll', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, '.t2v-scroll', 'border-top-color')).toBe('rgba(2, 28, 37, 0.22)');
    expect(await computed(page, '.t2v-scroll', 'box-shadow')).toBe('rgba(2, 28, 37, 0.14) 0px 2px 8px 0px');
    const composer = (await page.locator('.t2v-composer').boundingBox())!;
    expect(Math.round(composer.y - (box.y + box.height))).toBe(12);
  });
});

test.describe('Text size and the reply', () => {
  // setFontSize needs an account to apply to (router.py's _account(), account_controller
  // fixed at 0.0; account mode, not signed in — same "works signed out" shape as
  // settings.spec.ts's own text-size test, just against a real reply instead of an empty
  // body).
  test.use({ backendArgs: ['--mode', 'account'] });

  test('Large moves the reply and the user bubble together', async ({ page, backend }) => {
    // Before this fix, .aui-assistant-message-content fixed font-size at a literal 16px
    // regardless of --t2v-font-size, so Large only ever moved the user bubble (which
    // inherits body) and left the reply — the one text the setting exists for on a
    // docked panel — exactly where it started. Proves both now move, and move together.
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.locator('.aui-assistant-message-content').first()).toBeVisible();
    expect(await computed(page, '.aui-user-message-content', 'font-size')).toBe('16px');
    expect(await computed(page, '.aui-assistant-message-content', 'font-size')).toBe('16px');
    await page.getByRole('button', { name: 'Settings' }).click();
    await page.getByRole('group', { name: 'Text size' }).getByRole('button', { name: 'Large' }).click();
    await expect.poll(() => computed(page, '.aui-user-message-content', 'font-size')).toBe('18px');
    expect(await computed(page, '.aui-assistant-message-content', 'font-size')).toBe('18px');
  });
});

test.describe('Settings', () => {
  test('guest: an Appearance section with a segmented Text size and an Assistant card with a Sign in button', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    const labels = page.locator('.t2v-section-label');
    // The Skills link shows for a guest too — local skills are a folder the host
    // names, not gated on a session (Skills.tsx's own doc comment).
    await expect(labels).toHaveText(['Appearance', 'Assistant']);
    expect(await computed(page, '.t2v-section-label', 'font-size')).toBe('11px');
    expect(await computed(page, '.t2v-section-label', 'text-transform')).toBe('uppercase');
    const group = page.getByRole('group', { name: 'Text size' });
    await expect(group.getByRole('button')).toHaveText(['Small', 'Medium', 'Large']);
    expect(await computed(page, '.t2v-seg button[aria-pressed="true"]', 'background-color')).toBe('rgba(2, 28, 37, 0.07)');
    expect((await group.boundingBox())!.height).toBe(32);
    expect(await computed(page, '.t2v-guest-card', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    await page.locator('.t2v-guest-card').getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('heading', { name: 'Welcome to Talk2View' })).toBeVisible();
  });
});
test.describe('Settings, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('guest: the guest card is an Ivory wash, the pressed segment is the dark wash-2', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Settings' }).click();
    expect(await computed(page, '.t2v-guest-card', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
    expect(await computed(page, '.t2v-seg button[aria-pressed="true"]', 'background-color')).toBe('rgba(242, 240, 236, 0.09)');
  });
});
test.describe('Settings, signed in (account mode)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });
  test('Assistant / Appearance sections, 32px selects, no skills section', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Account', exact: true }).locator('.t2v-dot')).toBeVisible();
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.locator('.t2v-section-label')).toHaveText(['Assistant', 'Appearance']);
    expect((await page.getByLabel('AI model').boundingBox())!.height).toBe(32);
    expect(await computed(page, '.t2v-select', 'font-size')).toBe('13px');
    expect(await computed(page, '.t2v-field label', 'font-size')).toBe('13px');
    // Skills have their own view (the header button); Settings shows nothing about them.
    await expect(page.getByRole('button', { name: /^Skills ·/ })).toHaveCount(0);
    await expect(page.locator('.t2v-skill')).toHaveCount(0);
  });
});

test.describe('the Skills view, signed in (account mode)', () => {
  test.use({ backendArgs: ['--mode', 'account', '--skills-dir', 'auto', '--bundled-skills'] });
  test('skill rows: TS-style hairline cards, 16px checkbox, a tag on a read-only row, a styled app heading', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await page.getByRole('button', { name: 'Skills', exact: true }).click();
    // The app group's heading ("App skills" / "From …") styled like Settings' own
    // section labels (.t2v-section-label), not a bare browser h2.
    expect(await computed(page, '.t2v-section-title', 'font-size')).toBe('11px');
    expect(await computed(page, '.t2v-section-title', 'font-weight')).toBe('600');
    expect(await computed(page, '.t2v-section-title', 'text-transform')).toBe('uppercase');
    // scan-report (origin "code") is read-only here: no checkbox, an "Always on" note.
    const row = page.locator('.t2v-skill-row', { hasText: 'scan-report' });
    await expect(row.locator('.t2v-skill-note')).toHaveText('Always on');
    // A bordered card, as the TS SDK's (`border p-3`, `gap-2` between cards).
    expect(await computed(page, '.t2v-skill-row', 'border-top-color')).toBe('rgba(2, 28, 37, 0.1)');
    expect(await computed(page, '.t2v-skill-row', 'border-left-width')).toBe('1px');
    expect(await computed(page, '.t2v-skill-row', 'border-bottom-color')).toBe('rgba(2, 28, 37, 0.1)');
    expect(await computed(page, '.t2v-skill-row', 'padding-top')).toBe('12px');
    expect(await computed(page, '.t2v-skill-row', 'padding-left')).toBe('12px');
    expect(await computed(page, '.t2v-skill-row + .t2v-skill-row', 'margin-top')).toBe('8px');
    // The tag sits on the right of the row, 12px muted.
    const rowBox = (await row.boundingBox())!;
    const tagBox = (await row.locator('.t2v-skill-tag').boundingBox())!;
    expect(rowBox.x + rowBox.width - (tagBox.x + tagBox.width)).toBeLessThanOrEqual(14);
    // team-brief (a platform skill) is read-only too, merged into the same list.
    await expect(page.getByText('team-brief')).toBeVisible();
    await expect(page.getByRole('checkbox', { name: /team-brief/ })).toHaveCount(0);
  });

  test('row actions are always visible, as the TS SDK\'s (no hover or focus reveal)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Skills', exact: true }).click();
    const bundledRow = page.locator('.t2v-skill-row', { hasText: 'ct-window-presets' });
    const actions = bundledRow.locator('.t2v-skill-actions');
    const customize = bundledRow.getByRole('button', { name: /^Customize/ });
    await page.mouse.move(0, 0); // nowhere near the row, and nothing in it focused
    await expect(customize).toBeVisible();
    expect(await actions.evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
    // One of yours: Edit and Delete, 28px icon buttons, visible at rest too.
    await page.getByRole('button', { name: 'New skill' }).click();
    await page.getByLabel('Name').fill('at-rest');
    await page.getByLabel('Description', { exact: true }).fill('Checks the actions. Use to test.');
    await page.getByLabel('Content', { exact: true }).fill('x');
    await page.getByRole('button', { name: 'Save skill' }).click();
    const mine = page.locator('.t2v-skill-row', { hasText: 'at-rest' });
    await page.mouse.move(0, 0);
    for (const name of ['Edit at-rest', 'Delete at-rest']) {
      const button = mine.getByRole('button', { name });
      await expect(button).toBeVisible();
      const box = (await button.boundingBox())!;
      expect([box.width, box.height]).toEqual([28, 28]);
    }
    expect(await mine.locator('.t2v-skill-actions').evaluate((el) => getComputedStyle(el).opacity)).toBe('1');
  });
});

test.describe('the Skills view, looks', () => {
  test.use({ backendArgs: ['--mode', 'account', '--skills-dir', 'auto', '--bundled-skills'] });
  test('a 28px header button; 12px tags and intro; the empty dashed box; the form laid out as TS', async ({
    page,
    backend,
  }) => {
    await page.goto(backend.url);
    const button = page.getByRole('button', { name: 'Skills', exact: true });
    expect((await button.boundingBox())!.height).toBe(28);
    await button.click();
    // The hairline cards and the always-visible actions are asserted, against this same
    // backend, by "skill rows: TS-style hairline cards, …" and "row actions are always
    // visible" just above — not duplicated here.
    const note = page.locator('.t2v-skill-note').first();
    await expect(note).toHaveText('Always on'); // scan-report (origin "code") is first
    expect(await note.evaluate((el) => getComputedStyle(el).fontSize)).toBe('12px');
    expect(await computed(page, '.t2v-skills-intro', 'font-size')).toBe('12px');
    expect(await computed(page, '.t2v-skills-intro', 'color')).toBe(await computed(page, '.t2v-skill-note', 'color'));
    expect(await computed(page, '.t2v-skills-empty', 'border-top-style')).toBe('dashed');
    expect(await computed(page, '.t2v-skills-empty', 'text-align')).toBe('center');
    expect(await computed(page, '.t2v-skills-empty', 'padding-top')).toBe('16px');
    expect(await computed(page, '.t2v-skills-empty', 'font-size')).toBe('12px');
    // The form: no "New skill" title, a 192px monospace Content box, Save skill then
    // Cancel on the left.
    await page.getByRole('button', { name: 'New skill' }).click();
    await expect(page.locator('.t2v-skill-form .t2v-section-title')).toHaveCount(0);
    const content = page.getByLabel('Content', { exact: true });
    expect((await content.boundingBox())!.height).toBeGreaterThanOrEqual(192);
    expect(await content.evaluate((el) => getComputedStyle(el).fontSize)).toBe('12px');
    expect(await content.evaluate((el) => getComputedStyle(el).resize)).toBe('vertical');
    const save = (await page.getByRole('button', { name: 'Save skill' }).boundingBox())!;
    const cancel = (await page.getByRole('button', { name: 'Cancel' }).boundingBox())!;
    const form = (await page.locator('.t2v-skill-form').boundingBox())!;
    expect(save.x).toBe(form.x);
    expect(cancel.x).toBeGreaterThan(save.x + save.width);
  });
});

test.describe('the save_skill card, looks', () => {
  test.use({ backendArgs: ['--mode', 'account', '--skills-dir', 'auto'] });
  test('the same raised surface and Teal left rule as the approval card, and 10px above the action row', async ({ page, backend }) => {
    await page.goto(backend.url);
    await signIn(page);
    await sendMessage(page, '/skill');
    const card = page.locator('.t2v-skill-card');
    await expect(card).toBeVisible();
    // `.t2v-skill-card` sits on the same `.t2v-approval` base class as the approval card
    // (ToolCall.tsx/SkillCard.tsx), so it is the identical raised surface, hairline and
    // 3px Teal left rule checked in "the approval card" describe below — same selectors,
    // scoped to this card.
    expect(await computed(page, '.t2v-skill-card', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, '.t2v-skill-card', 'border-top-color')).toBe('rgba(2, 28, 37, 0.22)');
    expect(await computed(page, '.t2v-skill-card', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-skill-card', 'border-left-width')).toBe('3px');
    // gapAboveActions queries `.t2v-approval .t2v-actions`, which this card matches too.
    expect(await gapAboveActions(page)).toBe(10);
  });
});

test.describe('the user bubble, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('Ash bubble, Ivory text, Smoke composer', async ({ page, backend }) => {
    await page.goto(backend.url);
    // Wait for the page (and applyTheme's data-theme="dark") to land,
    // the same way every sibling dark test already does — a bare computed() read right
    // after goto can beat both, throwing "no element matches" or reading the light
    // background for one frame (1/22 under load).
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    // The shell moved off --t2v-composer-bg (Smoke) onto --t2v-card (the raised
    // surface, one step lighter than Smoke) — the same token every other card now uses.
    expect(await computed(page, '.t2v-composer-shell', 'background-color')).toBe('rgb(11, 39, 49)');
    expect(await computed(page, '.t2v-caption', 'color')).toBe('rgb(154, 164, 168)');
    await sendMessage(page, 'hello');
    await expect(page.locator('.aui-user-message-content')).toHaveText('hello');
    expect(await computed(page, '.aui-user-message-content', 'background-color')).toBe('rgb(53, 73, 81)');
    expect(await computed(page, '.aui-user-message-content', 'color')).toBe('rgb(242, 240, 236)');
  });
});

/**
 * Nothing asserted these views' colours before, which is how a
 * `.t2v-btn` base change reached production painting Google, Sign out and Back
 * solid Teal — TS draws all three on `secondaryClass` (outlined), reserving solid Teal for
 * `primaryClass` (the submit button only). Checked in both palettes, at rest and on
 * hover; `expect.poll` throughout, not a one-shot read (`.t2v-btn`'s own `background-color
 * 0.15s` transition can still be resolving when a check lands right after a click or a
 * view change, the same race the confirm-deny button's own check already guards against).
 */
async function accountButtonColours(
  page: Page,
  vals: { border: string; bg: string; color: string; hoverBg: string; appleBg: string; appleColor: string },
) {
  await expect(page.getByRole('button', { name: 'Continue with Apple' })).toBeEnabled();
  await expect.poll(() => computed(page, '.t2v-provider-google', 'border-top-color')).toBe(vals.border);
  await expect.poll(() => computed(page, '.t2v-provider-google', 'background-color')).toBe(vals.bg);
  await expect.poll(() => computed(page, '.t2v-provider-google', 'color')).toBe(vals.color);
  const google = (await page.locator('.t2v-provider-google').boundingBox())!;
  expect(google.height).toBe(36); // every block button is 36, not TS's own 40
  // Apple keeps its own black/white override in both themes (Apple's guideline): unaffected.
  await expect.poll(() => computed(page, '.t2v-provider-apple', 'background-color')).toBe(vals.appleBg);
  await expect.poll(() => computed(page, '.t2v-provider-apple', 'color')).toBe(vals.appleColor);
  await page.locator('.t2v-provider-google').hover();
  await expect.poll(() => computed(page, '.t2v-provider-google', 'background-color')).toBe(vals.hoverBg);
  await page.mouse.move(0, 0);

  await page.getByLabel('Email').fill('ana@example.com');
  await page.getByLabel('Password').fill('pw');
  await expect.poll(() => computed(page, 'button[type="submit"]', 'background-color')).toBe('rgb(38, 200, 184)'); // primaryClass: solid Teal
  await expect.poll(() => computed(page, 'button[type="submit"]', 'color')).toBe('rgb(2, 28, 37)');
  const submit = (await page.locator('button[type="submit"]').boundingBox())!;
  expect(submit.height).toBe(36);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Chat', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'Account', exact: true }).click();
  const signOut = page.getByRole('button', { name: 'Sign out' });
  await expect.poll(() => computed(page, '.t2v-btn-outline.t2v-btn-block', 'border-top-color')).toBe(vals.border);
  await expect.poll(() => computed(page, '.t2v-btn-outline.t2v-btn-block', 'background-color')).toBe(vals.bg);
  await expect.poll(() => computed(page, '.t2v-btn-outline.t2v-btn-block', 'color')).toBe(vals.color);
  expect((await signOut.boundingBox())!.height).toBe(36);
  await signOut.hover();
  await expect.poll(() => computed(page, '.t2v-btn-outline.t2v-btn-block', 'background-color')).toBe(vals.hoverBg);
}

test.describe('the account view buttons', () => {
  test.use({ backendArgs: ['--mode', 'account', '--desktop-sign-in'] });
  test('Google, Sign out and Back are outlined; the submit button alone is solid Teal', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await accountButtonColours(page, {
      border: 'rgba(2, 28, 37, 0.22)', // --t2v-hairline-strong
      bg: 'rgb(255, 255, 255)', // --t2v-card (raised)
      color: 'rgb(2, 28, 37)', // text-foreground: Smoke
      hoverBg: 'rgba(2, 28, 37, 0.04)', // --t2v-wash
      appleBg: 'rgb(2, 28, 37)',
      appleColor: 'rgb(255, 255, 255)',
    });
  });
});

test.describe('the account view buttons, dark', () => {
  test.use({ backendArgs: ['--mode', 'account', '--desktop-sign-in', '--dark'] });
  test('Google, Sign out and Back are outlined; the submit button alone is solid Teal', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account', exact: true }).click();
    await accountButtonColours(page, {
      border: 'rgba(242, 240, 236, 0.22)', // --t2v-hairline-strong
      bg: 'rgb(11, 39, 49)', // --t2v-card (raised)
      color: 'rgb(242, 240, 236)', // text-foreground: Ivory
      hoverBg: 'rgba(242, 240, 236, 0.05)', // --t2v-wash
      appleBg: 'rgb(242, 240, 236)',
      appleColor: 'rgb(2, 28, 37)',
    });
  });
});

test.describe('the focus ring uses --t2v-ring, not --t2v-primary', () => {
  // TS: focus-visible:ring-ring/50, and --ring is Highlight in the dark palette while
  // --primary stays Teal — a mouse click leaving a Teal outline in dark mode was the
  // token gap this closes. .t2v-view-title (Account.tsx:330) is tabIndex={-1} and
  // programmatically focused on view entry, so it always carries the outline to check.
  test('light: Teal (== --t2v-ring == --t2v-primary here, so this also guards against a bad token, not just a bad property)', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    // Account.tsx:330 only actually moves focus here when a provider section it was in
    // goes away — not on every view entry — so force it directly, the way a keyboard
    // user tabbing to it would land on it either way. The Account button click just
    // above was a real, dispatched click (Playwright's own), which focusVisible.ts reads
    // the same way a mouse user's click would — so a lone keypress first (any key; a
    // keyboard user driving this far would have pressed one already) clears that back to
    // the keyboard state this test means to check, without moving focus off .t2v-view-title.
    await page.keyboard.press('Shift');
    await page.locator('.t2v-view-title').evaluate((el) => (el as HTMLElement).focus());
    expect(await computed(page, '.t2v-view-title', 'outline-color')).toBe('rgb(38, 200, 184)'); // Teal
  });
});

test.describe('the focus ring uses --t2v-ring, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('Highlight, not Teal', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.keyboard.press('Shift'); // see the light-mode test above
    await page.locator('.t2v-view-title').evaluate((el) => (el as HTMLElement).focus());
    expect(await computed(page, '.t2v-view-title', 'outline-color')).toBe('rgb(115, 255, 255)'); // Highlight, not rgb(38, 200, 184) Teal
  });
});

test.describe('focus-visible: a click suppresses the ring, a keypress restores it', () => {
  // Chromium 83 has no :focus-visible (86+, banned throughout this project), so TS's
  // focus-visible:ring-ring/50 has no direct equivalent: focusVisible.ts (src/ui/) tracks
  // the last input at the document level instead, the way the pre-native-support WICG
  // polyfill did, and app.css's `html.t2v-mouse *:focus { outline: none }` reads it.
  test('a clicked tool row keeps no outline; a Tab away and back brings it back', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/toolsonly');
    const trigger = page.locator('.aui-tool-fallback-trigger').first();
    await expect(trigger).toBeVisible();
    await trigger.click(); // a real, dispatched click: pointerdown before the resulting focus
    expect(await computed(page, '.aui-tool-fallback-trigger', 'outline-style')).toBe('none');
    // Tab then Shift+Tab: two real keydowns, landing focus back on the very same element
    // that was just clicked — proving the ring is keyed off the input method, not the
    // element (a click, then a keyboard user tabbing through and back onto that same
    // row, sees it same as any other stop). Deliberately not just any next tab stop:
    // .t2v-input (the composer) sets its own `outline: none` regardless of this
    // mechanism, which a naive "Tab once and check" would have wrongly read as a bug here.
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await expect(trigger).toBeFocused();
    expect(await computed(page, '.aui-tool-fallback-trigger', 'outline-style')).toBe('solid');
  });

  test('a mouse user copying with Ctrl+C keeps no ring on the button they clicked', async ({ page, backend }) => {
    // The WICG polyfill's own onKeyDown returns early on a held Meta/Alt/Ctrl (and native
    // Chromium ignores a modifier-only or modifier-held press the same way): a mouse user
    // who clicks a button, then presses Ctrl+C to copy selected reply text, is not "now
    // driving by keyboard" in the sense this heuristic means. Before this fix, any keydown
    // — Ctrl+C included — cleared the mouse state and put a ring on the clicked button.
    await page.goto(backend.url);
    await sendMessage(page, '/toolsonly');
    const trigger = page.locator('.aui-tool-fallback-trigger').first();
    await expect(trigger).toBeVisible();
    await trigger.click();
    expect(await computed(page, '.aui-tool-fallback-trigger', 'outline-style')).toBe('none');
    await page.keyboard.press('Control+c');
    expect(await computed(page, '.aui-tool-fallback-trigger', 'outline-style')).toBe('none');
  });

  test('a text field still rings on a mouse click, unlike a button', async ({ page, backend }) => {
    // Native :focus-visible (and the WICG polyfill) always match a focused text-entry
    // field, click or not: a click there still means "about to type", unlike a click on a
    // button. TS's own Input carries focus-visible:ring-3 ring-ring/50 unconditionally for
    // the same reason. Before this fix, the blanket `html.t2v-mouse *:focus { outline:
    // none }` suppressed this too.
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    const email = page.getByLabel('Email');
    await email.click();
    await expect(email).toBeFocused();
    // Fields ring INSIDE their box now — a Teal border plus a
    // 3px soft ring — never the outset `outline` buttons still use for keyboard focus.
    expect(await computed(page, '.t2v-text', 'border-top-color')).toBe('rgb(38, 200, 184)'); // light; dark: rgb(115, 255, 255)
    expect(await computed(page, '.t2v-text', 'box-shadow')).toBe('rgba(38, 200, 184, 0.35) 0px 0px 0px 3px'); // dark: rgba(115, 255, 255, 0.35)
    expect(await computed(page, '.t2v-text', 'outline-style')).toBe('none');
  });
});

test.describe('tool rows', () => {
  test('three finished tools fold into "3 tool calls"; each row says "Used name" with a Check', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/toolsonly');
    const group = page.locator('.aui-tool-group-root');
    await expect(group.locator('.aui-tool-group-trigger')).toHaveText('3 tool calls');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible(); // the turn is over
    // The group opened itself while live and stays open once settled.
    await expect(group.locator('.aui-tool-fallback-trigger')).toHaveCount(3);
    const first = group.locator('.aui-tool-fallback-trigger').first();
    await expect(first).toHaveText(/^Used load_sample_data/);
    await expect(first.locator('.t2v-tool-name')).toHaveText('load_sample_data');
    expect(await first.locator('svg').first().getAttribute('class')).toContain('lucide-check');
    expect(await computed(page, '.aui-tool-fallback-trigger', 'font-size')).toBe('13px');
    expect(await computed(page, '.aui-tool-fallback-trigger', 'color')).toBe('rgb(103, 119, 124)');
    expect((await first.boundingBox())!.height).toBe(28);
    expect(await computed(page, '.t2v-tool[data-state="used"] .aui-tool-fallback-trigger-icon', 'color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-tool-name', 'font-size')).toBe('12px');
    expect(await computed(page, '.t2v-tool-name', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.aui-tool-group-trigger', 'font-weight')).toBe('500');
    expect((await group.locator('.aui-tool-group-trigger').boundingBox())!.height).toBe(24);
    await first.click();
    const args = group.locator('.aui-tool-fallback-args pre').first();
    await expect(args).toContainText('"name": "MRHead"');
    // Wash, not Mist-50 — Mist is the user bubble's own fill.
    expect(await computed(page, '.aui-tool-fallback-args pre', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.aui-tool-fallback-args pre', 'font-size')).toBe('12px');
    await expect(group.locator('.aui-tool-fallback-result-header').first()).toHaveText('Result');
    expect(await computed(page, '.aui-tool-fallback-content', 'padding-left')).toBe('22px');
    // Folding the group hides the rows.
    await group.locator('.aui-tool-group-trigger').click();
    await expect(group.locator('.aui-tool-fallback-trigger')).toHaveCount(0);
  });

  test('a lone tool call still gets its own "1 tool call" group, matching TS', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve');
    const group = page.locator('.aui-tool-group-root');
    // Opened by the pending approval (requires-action is a real, non-instant live state,
    // unlike backend.py's atomic tool calls elsewhere in this file) before it is answered;
    // TS's own trigger spells the singular too (tool-group.aui.tsx:112).
    await expect(group.locator('.aui-tool-group-trigger')).toHaveText('1 tool call');
    await page.getByRole('button', { name: 'Allow once' }).click();
    await expect(group.locator('.aui-tool-fallback-trigger')).toHaveText(/^Used load_sample_data/);
  });
});

test.describe('tool rows, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('the name chip is an Ivory wash, the check is Teal', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/toolsonly');
    const group = page.locator('.aui-tool-group-root');
    await expect(group.locator('.aui-tool-fallback-trigger')).toHaveCount(3);
    expect(await computed(page, '.t2v-tool-name', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
    expect(await computed(page, '.t2v-tool[data-state="used"] .aui-tool-fallback-trigger-icon', 'color')).toBe('rgb(38, 200, 184)');
  });
});

test.describe('the running row (account mode, a platform tool)', () => {
  test.use({ backendArgs: ['--mode', 'account'] });
  test('shimmer text, a spinner and a live count', async ({ page, backend }) => {
    await page.goto(backend.url);
    await page.getByRole('button', { name: 'Account' }).click();
    await page.getByLabel('Email').fill('ana@example.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await expect(page.getByRole('button', { name: 'Account' }).locator('.t2v-dot')).toBeVisible();
    await sendMessage(page, 'search the web for presets');
    const row = page.locator('.aui-tool-fallback-root[data-state="running"] .aui-tool-fallback-trigger');
    await expect(row).toContainText('Searching the web');
    await expect(row.locator('.t2v-shimmer')).toHaveText('Searching the web');
    expect(await computed(page, '.aui-tool-fallback-root[data-state="running"] .t2v-shimmer', 'animation-name')).toBe('t2v-shimmer');
    expect(await computed(page, '.aui-tool-fallback-root[data-state="running"] .t2v-shimmer', '-webkit-background-clip')).toBe('text');
    await expect(row.locator('.aui-tool-fallback-duration')).toHaveText(/^[2-9]s$/, { timeout: 4000 });
    // Not `row`: once the call finishes, data-state flips to "used" and that locator
    // (scoped to `[data-state="running"]`) stops matching anything at all — there is
    // only ever the one tool call here, so the trigger alone is unambiguous.
    await expect(page.locator('.aui-tool-fallback-trigger')).toHaveText(/^Used web_search/, { timeout: 10000 });
  });
});

/** The space between the approval card's action row and whatever sits above it. */
async function gapAboveActions(page: Page): Promise<number> {
  return page.evaluate(() => {
    const row = document.querySelector('.t2v-approval .t2v-actions')!;
    const above = row.previousElementSibling!;
    return Math.round(row.getBoundingClientRect().top - above.getBoundingClientRect().bottom);
  });
}

test.describe('the approval card', () => {
  test('a raised card with a hairline, a 3px Teal left rule, wash args, one 28px button row, Edit in the corner, a 12px hint', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve');
    const card = page.locator('.t2v-approval');
    await expect(card).toBeVisible();
    expect(await computed(page, '.t2v-approval', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, '.t2v-approval', 'border-top-color')).toBe('rgba(2, 28, 37, 0.22)');
    expect(await computed(page, '.t2v-approval', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-approval', 'border-left-width')).toBe('3px');
    await expect(card.locator('.t2v-approval-title')).toHaveText('Run load_sample_data?');
    expect(await computed(page, '.t2v-approval-title', 'font-size')).toBe('14px');
    expect(await computed(page, '.t2v-approval-prompt', 'font-size')).toBe('13px');
    expect(await computed(page, '.t2v-approval .t2v-tool-args', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    const edit = card.getByRole('button', { name: 'Edit arguments' });
    await expect(edit).toHaveText('Edit');
    expect((await edit.boundingBox())!.height).toBe(24);
    const head = (await card.locator('.t2v-approval-head').boundingBox())!;
    expect((await edit.boundingBox())!.y).toBeLessThan(head.y + head.height);
    for (const name of ['Allow once', 'Always allow', 'Deny']) expect((await card.getByRole('button', { name }).boundingBox())!.height).toBe(28);
    const once = (await card.getByRole('button', { name: 'Allow once' }).boundingBox())!;
    const deny = (await card.getByRole('button', { name: 'Deny' }).boundingBox())!;
    expect(Math.round(deny.y)).toBe(Math.round(once.y)); // one row
    await expect(card.locator('.t2v-hint-bounded')).toHaveText('Always allow: no more prompts for load_sample_data in this chat.');
    expect(await computed(page, '.t2v-hint-bounded', 'font-size')).toBe('12px');
    expect(await gapAboveActions(page)).toBe(10); // "Children + 10px": the row too
    expect(await page.evaluate(() => {
      const args = document.querySelector('.t2v-approval .t2v-tool-args')!;
      return Math.round(args.getBoundingClientRect().top - args.previousElementSibling!.getBoundingClientRect().bottom);
    })).toBe(10); // and the args under the head
    await page.getByRole('button', { name: 'Deny' }).click();
    // The reason's label is 12/16 muted, and the row keeps its 10px under the
    // textarea, so the focus ring (3px) never touches Confirm deny.
    await expect(card.locator('.t2v-field label')).toHaveText('Reason (optional, sent to the assistant)');
    expect(await computed(page, '.t2v-approval .t2v-field label', 'font-size')).toBe('12px');
    expect(await computed(page, '.t2v-approval .t2v-field label', 'font-weight')).toBe('400');
    expect(await computed(page, '.t2v-approval .t2v-field label', 'color')).toBe('rgb(103, 119, 124)');
    expect(await gapAboveActions(page)).toBe(10);
    // Polled, not read once: "Deny" and "Confirm deny" share the same DOM node (React
    // reconciles the action row positionally, with no key of its own to tell "Allow
    // once"/"Confirm deny" apart at slot 0) — the button's classes change in place, from
    // .t2v-btn-primary's teal to .t2v-btn-danger's wash, and .t2v-btn's own `transition:
    // background-color 0.15s` (TS parity) animates between them rather than
    // swapping instantly.
    await expect.poll(() => computed(page, 'button[data-action="confirm-deny"]', 'background-color')).toBe('rgba(187, 82, 78, 0.1)');
    await expect.poll(() => computed(page, 'button[data-action="confirm-deny"]', 'color')).toBe('rgb(187, 82, 78)');
  });
  test('the row stays one row at 320 wide', async ({ page, backend }) => {
    await page.setViewportSize({ width: 320, height: 600 });
    await page.goto(backend.url);
    await sendMessage(page, '/approve');
    const card = page.locator('.t2v-approval');
    const once = (await card.getByRole('button', { name: 'Allow once' }).boundingBox())!;
    const deny = (await card.getByRole('button', { name: 'Deny' }).boundingBox())!;
    expect(Math.round(deny.y)).toBe(Math.round(once.y));
  });
});

test.describe('the approval card, a destructive tool', () => {
  // Brief deviation: --destructive-tool removes "Always allow" (mayAlwaysAllow), so the
  // main test above (which checks Always allow and its hint) uses the plain scripted
  // approval instead; this one is only the amber warning's own colours.
  test.use({ backendArgs: ['--destructive-tool'] });
  test('Amber warning, TS small buttons, no Always allow', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve');
    const card = page.locator('.t2v-approval');
    await expect(card).toBeVisible();
    expect(await computed(page, '.t2v-warning', 'border-top-color')).toBe('rgba(240, 167, 74, 0.5)');
    expect(await computed(page, '.t2v-warning', 'background-color')).toBe('rgba(240, 167, 74, 0.15)');
    await expect(card.getByRole('button')).toHaveText(['Edit', 'Allow once', 'Deny']);
    await expect(page.getByRole('button', { name: 'Always allow' })).toHaveCount(0);
    expect(await gapAboveActions(page)).toBe(10); // Allow once never sits on the warning
  });
});

test.describe('the approval card, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('the raised card, the hairline and the Teal left rule in the dark palette', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/approve');
    const card = page.locator('.t2v-approval');
    await expect(card).toBeVisible();
    expect(await computed(page, '.t2v-approval', 'background-color')).toBe('rgb(11, 39, 49)');
    expect(await computed(page, '.t2v-approval', 'border-top-color')).toBe('rgba(242, 240, 236, 0.22)');
    expect(await computed(page, '.t2v-approval', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-approval .t2v-tool-args', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
  });
});

test.describe('the error card', () => {
  test('a 35% red rule, an 8% wash, a red icon, the message in the foreground colour, secondary Try again', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/fail');
    const card = page.locator('.t2v-error');
    await expect(card).toBeVisible();
    expect(await computed(page, '.t2v-error', 'border-top-color')).toBe('rgba(187, 82, 78, 0.35)');
    expect(await computed(page, '.t2v-error', 'background-color')).toBe('rgba(187, 82, 78, 0.08)');
    expect(await computed(page, '.t2v-error-icon', 'color')).toBe('rgb(187, 82, 78)');
    expect(await computed(page, '.t2v-error-text', 'color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, '.t2v-error-text', 'font-size')).toBe('13px');
    const retry = page.getByRole('button', { name: 'Try again' });
    expect(await retry.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(255, 255, 255)');
    expect((await retry.boundingBox())!.height).toBe(24);
  });
});

test.describe('the error card, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('a 45% red rule, a 14% wash, red-200 no longer needed but kept for TS parity elsewhere', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/fail');
    const card = page.locator('.t2v-error');
    await expect(card).toBeVisible();
    expect(await computed(page, '.t2v-error', 'border-top-color')).toBe('rgba(187, 82, 78, 0.45)');
    expect(await computed(page, '.t2v-error', 'background-color')).toBe('rgba(187, 82, 78, 0.14)');
    expect(await computed(page, '.t2v-error-icon', 'color')).toBe('rgb(187, 82, 78)');
    // fg on the tint reads better than the old dark:text-red-200 — the token
    // itself stays declared for TS parity, just unused here.
    expect(await computed(page, '.t2v-error-text', 'color')).toBe('rgb(242, 240, 236)');
    const retry = page.getByRole('button', { name: 'Try again' });
    expect(await retry.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe('rgb(11, 39, 49)');
    expect((await retry.boundingBox())!.height).toBe(24);
  });
});

test.describe('code blocks', () => {
  test('a language label, a per-block copy that goes to the host, and a 3 s tick', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    // Waits for the fence's own final text, not just "Send visible": the code block (and
    // so its header) does not exist in the DOM at all until the closing ``` has streamed
    // in, whether or not the turn has otherwise ended server-side.
    await expect(lastReply(page).locator('.aui-md-pre code')).toContainText("volume = load_sample_data('MRHead')");
    const header = lastReply(page).locator('.aui-code-header-root');
    await expect(header).toHaveCount(1);
    await expect(header.locator('.aui-code-header-language')).toHaveText('python');
    expect(await computed(page, '.aui-code-header-language', 'text-transform')).toBe('lowercase');
    expect(await computed(page, '.aui-code-header-root', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.aui-code-header-root', 'border-bottom-width')).toBe('0px');
    // The header's own border-bottom stays 0, but the pre below it now carries a
    // full hairline border (not just sides+bottom with an explicit border-top: 0) — one
    // line between them either way, just attributed to the pre's own top edge now.
    expect(await computed(page, '.aui-md-pre', 'border-top-width')).toBe('1px');
    expect(await computed(page, '.aui-md-pre', 'font-size')).toBe('13px');
    expect(await computed(page, '.aui-md-pre', 'background-color')).toBe('rgb(255, 255, 255)');
    const copy = header.getByRole('button', { name: 'Copy code' });
    // An earlier fix removed the ONE race the old comment here named (the header
    // missing outright between a text update wiping it and a separate effect re-adding
    // it — confirmed gone: a plain page.evaluate() querySelector finds the button, with
    // its correct markup, on every one of 5 straight checks right after the wait above).
    // A different one is still real and unrelated to that fix: this reply keeps streaming
    // AFTER the fence closes (a blockquote, then a link), so dangerouslySetInnerHTML goes
    // on replacing the whole container — button included — on every further chunk, and
    // Locator.boundingBox()'s own actionability wait can time out against that ongoing
    // churn even though the button is never actually absent for an outside observer
    // (verified: a plain, non-polling boundingBox() read here now fails consistently,
    // 15/15, not the rare race the old code's version was; `.getByRole` vs a plain CSS
    // class locator makes no difference — confirmed both). expect.poll absorbs it exactly
    // the way it already did for the confirm-deny button's own CSS-transition race.
    await expect.poll(async () => (await copy.boundingBox())?.width ?? null).toBe(24);
    await copy.click();
    await expect.poll(() => backend.hostCalls.filter((c) => c.host === 'clipboard_set').map((c) => c.value)).toEqual(["volume = load_sample_data('MRHead')\n"]);
    await expect(copy.locator('svg.lucide-check')).toBeVisible();
    await page.clock.install();
    await page.clock.runFor(3100);
    await expect(copy.locator('svg.lucide-copy')).toBeVisible();
    // Inline code keeps the neutral wash (Mist is the user bubble's own fill now) and the mono stack.
    expect(await computed(page, '.aui-md-inline-code', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
  });

  test('a capitalised fence still shows its language, lowercase in the rendering', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/jsonfence');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    const header = lastReply(page).locator('.aui-code-header-root');
    // The DOM keeps the fence's own case (as TS does — markdown-text.tsx never lowercases
    // the text itself, only the rendering, with CSS `lowercase`, already proven generic —
    // not scoped to any one instance — by the sibling test above). A `.innerText` read
    // here to prove the PIXELS are lowercase turned out to be its own flake (3/8 at
    // `--repeat-each 8`): this reply's fence closes near the very end of the streamed
    // text, so `.innerText`'s required layout pass can land mid-replacement of the whole
    // container on the next chunk — exactly the class of race an earlier fix removed from the
    // header's own existence, just for a different read. The computed CSS property is the
    // stable half of that same proof and needs no poll.
    await expect(header.locator('.aui-code-header-language')).toHaveText('JSON');
    expect(await computed(page, '.aui-code-header-language', 'text-transform')).toBe('lowercase');
  });
});

test.describe('code blocks with no host clipboard', () => {
  test.use({ backendArgs: ['--no-clipboard'] });
  test('the language label stays, the copy button goes', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    const header = lastReply(page).locator('.aui-code-header-root');
    await expect(header.locator('.aui-code-header-language')).toHaveText('python');
    await expect(header.getByRole('button')).toHaveCount(0);
  });
});

test.describe('markdown rhythm and neutral washes', () => {
  test('light: 10px paragraphs, 2px list rows, 17px h2, wash table head and inline code, hairline code block', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    const r = lastReply(page);
    // Waits for the reply's own LAST markdown element (backend.py's REPLY appends a
    // blockquote and a link after DEFAULT_REPLY's table/fence), not just "Send visible":
    // a one-shot computed() below has no retry of its own, unlike toHaveText/toBeVisible,
    // so reading it before the tail has streamed in would catch the intro paragraph as
    // .t2v-md's transient LAST child instead (the same race the code-blocks test's own
    // comment names).
    await expect(r.locator('.t2v-md a[href]')).toBeVisible();
    await expect(r.locator('h2')).toHaveText('Segmentation plan');
    expect(await computed(page, '.t2v-md h2', 'font-size')).toBe('17px');
    expect(await computed(page, '.t2v-md h2', 'margin-bottom')).toBe('6px');
    expect(await computed(page, '.t2v-md p', 'margin-bottom')).toBe('10px');
    expect(await computed(page, '.t2v-md ol', 'margin-left')).toBe('22px');
    expect(await computed(page, '.t2v-md li + li', 'margin-top')).toBe('2px');
    expect(await computed(page, '.t2v-md th', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.t2v-md th', 'border-bottom-color')).toBe('rgba(2, 28, 37, 0.22)');
    expect(await computed(page, '.t2v-md table', 'border-collapse')).toBe('collapse');
    expect(await computed(page, '.t2v-md td code', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect(await computed(page, '.aui-code-header-root', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    expect((await page.locator('.aui-code-header-root').boundingBox())!.height).toBe(28);
    expect(await computed(page, '.aui-md-pre', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, '.aui-md-pre', 'border-left-color')).toBe('rgba(2, 28, 37, 0.1)');
    expect(await computed(page, '.aui-md-pre', 'padding-left')).toBe('12px');
    expect(await computed(page, '.t2v-md blockquote', 'border-left-color')).toBe('rgba(2, 28, 37, 0.22)');
    // Mist stays on the bubble only: the user bubble's own font-size is left
    // alone here, not pinned to a literal — it deliberately tracks --t2v-font-size (the
    // Text size setting), per "Large moves the reply and the user bubble together" above;
    // a hardcoded size here would be the exact regression that test exists to catch.
    expect(await computed(page, '.t2v-bubble', 'background-color')).toBe('rgb(202, 235, 235)');
  });
});
test.describe('markdown rhythm and neutral washes, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('Ivory washes on Smoke, the code body on the raised surface, Ash only on the bubble', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page).locator('.t2v-md a[href]')).toBeVisible();
    expect(await computed(page, '.t2v-md th', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
    expect(await computed(page, '.aui-md-pre', 'background-color')).toBe('rgb(11, 39, 49)');
    expect(await computed(page, '.t2v-bubble', 'background-color')).toBe('rgb(53, 73, 81)');
  });
});

test.describe('the reply actions', () => {
  test('Copy turns into a tick for 3 s and the bar is 24 px ghost buttons', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(page.getByRole('button', { name: 'Send' })).toBeVisible();
    // exact: the per-code-block "Copy code" button (this reply has one) also
    // matches an un-exact 'Copy'.
    const copy = lastReply(page).getByRole('button', { name: 'Copy', exact: true });
    expect((await copy.boundingBox())!.width).toBe(24);
    await expect(copy.locator('svg.lucide-copy')).toBeVisible();
    await copy.click();
    await expect(copy.locator('svg.lucide-check')).toBeVisible();
    await expect.poll(() => backend.hostCalls.some((c) => c.host === 'clipboard_set' && c.value.startsWith('## Segmentation plan'))).toBe(true);
    await page.clock.install();
    await page.clock.runFor(3100);
    await expect(copy.locator('svg.lucide-copy')).toBeVisible();
    await expect(lastReply(page).getByRole('button', { name: 'Refresh' })).toBeVisible();
    expect(await computed(page, '.t2v-msg-actions', 'color')).toBe('rgb(103, 119, 124)');
  });
});

test.describe('an empty thread inherits nothing from the Chats view (class collision)', () => {
  test('light: the welcome heading is Smoke, typed text is Smoke, and the thread root has no padding', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    expect(await computed(page, 'h2.aui-thread-welcome-message-inner', 'color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, '.t2v-thread', 'font-size')).toBe('16px');
    expect(await computed(page, '.t2v-thread', 'padding-left')).toBe('0px');
    await page.getByRole('textbox', { name: 'Message' }).fill('Segment the liver');
    expect(await computed(page, '.t2v-input', 'color')).toBe('rgb(2, 28, 37)');
  });
});

test.describe('an empty thread inherits nothing from the Chats view, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('the welcome heading is Ivory and the viewport is flush with the panel', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    expect(await computed(page, 'h2.aui-thread-welcome-message-inner', 'color')).toBe('rgb(242, 240, 236)');
    // The subheading's own dark colour.
    expect(await computed(page, '.t2v-welcome-sub', 'color')).toBe('rgb(154, 164, 168)');
    const root = (await page.locator('.t2v-thread').boundingBox())!;
    const viewport = (await page.locator('.t2v-viewport').boundingBox())!;
    expect(viewport.x).toBe(root.x);
    expect(viewport.width).toBe(root.width);
  });
});

test.describe('one gutter and a capped column', () => {
  test('title, heading, composer and suggestion mark share a 16 px left edge; 12 px under 360', async ({ page, backend }) => {
    await page.goto(backend.url);
    await expect(page.getByRole('heading', { name: 'Ask about your scene' })).toBeVisible();
    const x = async (sel: string) => (await page.locator(sel).first().boundingBox())!.x;
    expect(await x('h1.aui-modal-title')).toBe(16);
    expect(await x('h2.aui-thread-welcome-message-inner')).toBe(16);
    expect(await x('.t2v-composer')).toBe(16);
    expect(await x('.aui-thread-welcome-suggestion')).toBe(16);
    await sendMessage(page, 'hello');
    expect(await x('.aui-assistant-message-content')).toBe(16);
    // The reply's Copy/Refresh glyph must line up with the text above it.
    expect(await x('.t2v-msg-actions svg')).toBe(16);
    await page.setViewportSize({ width: 320, height: 600 });
    expect(await x('h1.aui-modal-title')).toBe(12);
    expect(await x('.t2v-composer')).toBe(12);
  });
  test('above 560 px the thread column stops at 640 and centres', async ({ page, backend }) => {
    await page.setViewportSize({ width: 900, height: 700 });
    await page.goto(backend.url);
    const box = (await page.locator('.t2v-composer').boundingBox())!;
    expect(Math.round(box.width)).toBe(640);
    expect(Math.round(box.x)).toBe(130);
    // The views (Chats/Settings/Account) cap at 560.
    await page.getByRole('button', { name: 'Settings' }).click();
    const view = (await page.locator('.t2v-view > *').first().boundingBox())!;
    expect(Math.round(view.width)).toBe(560);
    expect(Math.round(view.x)).toBe(170);
  });
});

test.describe('the Chats view', () => {
  test('36px rows, a Teal left rule on the active row, slate actions on hover, a time at the right, an empty state', async ({ page, backend }) => {
    await page.goto(backend.url);
    const chats = page.getByRole('button', { name: 'Chats' });
    await expect(chats).toBeEnabled();
    await chats.click();
    await expect(page.locator('.t2v-thread-list-empty h3')).toHaveText('No chats yet');
    await expect(page.locator('.t2v-thread-list-empty').getByRole('button', { name: 'New chat' })).toBeVisible();
    await chats.click();
    await sendMessage(page, 'Load MRHead');
    await page.getByRole('button', { name: 'New chat' }).click();
    await sendMessage(page, 'Show the axial view');
    await chats.click();
    const rows = page.locator('.t2v-thread-row');
    await expect(rows).toHaveCount(2);
    expect((await rows.first().boundingBox())!.height).toBe(36);
    expect(await computed(page, '.t2v-thread-row[data-active]', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-thread-row[data-active]', 'background-color')).toBe('rgba(2, 28, 37, 0.04)');
    await expect(rows.nth(1).locator('.t2v-thread-when')).toHaveText(/\d/);
    // The time sits in the row's own reserved slot, flush with its
    // right edge (10px, the row's own right padding) — not short of it by however wide
    // the hidden actions are.
    const rowBox = (await rows.nth(1).boundingBox())!;
    const whenBox = (await rows.nth(1).locator('.t2v-thread-when').boundingBox())!;
    expect(Math.round(rowBox.x + rowBox.width - (whenBox.x + whenBox.width))).toBe(10);
    await rows.nth(1).hover();
    expect(await computed(page, '.t2v-thread-when', 'opacity')).toBe('0');
    expect(await computed(page, '.t2v-thread-delete', 'opacity')).toBe('1');
    expect(await computed(page, '.t2v-thread-row:hover .t2v-thread-delete', 'color')).toBe('rgb(103, 119, 124)');
    expect(await computed(page, '.t2v-thread-group', 'font-size')).toBe('11px');
    expect(await computed(page, '.t2v-thread-group', 'text-transform')).toBe('uppercase');
    expect(await computed(page, '.t2v-thread-search-input', 'background-color')).toBe('rgb(255, 255, 255)');
    expect(await computed(page, '.t2v-thread-search-input', 'border-top-color')).toBe('rgba(2, 28, 37, 0.22)');
  });
});
test.describe('the Chats view, dark', () => {
  test.use({ backendArgs: ['--dark'] });
  test('the active row is an Ivory wash with the Teal rule', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await page.getByRole('button', { name: 'Chats' }).click();
    expect(await computed(page, '.t2v-thread-row[data-active]', 'background-color')).toBe('rgba(242, 240, 236, 0.05)');
    expect(await computed(page, '.t2v-thread-row[data-active]', 'border-left-color')).toBe('rgb(38, 200, 184)');
    expect(await computed(page, '.t2v-view', 'background-color')).toBe('rgb(2, 28, 37)');
  });
});

test.describe('the plan', () => {
  // The engine's write_todos checklist (the fake's `/plan`), drawn as steps the way
  // assistant-ui's AgentPlan element does: no box, a title and "N of M", a 3px bar, and a
  // check / a Teal dot / a faint dot per step. No spinner on the active step: a plan
  // outlives its run, and a spinner on a finished chat would say it is still working.
  test('a checklist becomes steps: "Plan · 1 of 3", a third of a Teal bar, done / next / to do', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/plan');
    const plan = lastReply(page).locator('.t2v-plan');
    await expect(plan).toBeVisible();
    expect(await computed(page, '.t2v-plan', 'border-top-width')).toBe('0px');
    expect(await computed(page, '.t2v-plan', 'background-color')).toBe('rgba(0, 0, 0, 0)');
    await expect(plan.locator('.t2v-plan-title')).toHaveText('Plan');
    expect(await computed(page, '.t2v-plan-title', 'font-size')).toBe('13px');
    await expect(plan.locator('.t2v-plan-count')).toHaveText('1 of 3');
    const bar = plan.getByRole('progressbar', { name: 'Plan progress' });
    await expect(bar).toHaveAttribute('aria-valuenow', '1');
    await expect(bar).toHaveAttribute('aria-valuemax', '3');
    expect((await bar.boundingBox())!.height).toBe(3);
    expect(await computed(page, '.t2v-plan-fill', 'background-color')).toBe('rgb(38, 200, 184)');
    const barBox = (await bar.boundingBox())!;
    const fillBox = (await plan.locator('.t2v-plan-fill').boundingBox())!;
    expect(Math.abs(fillBox.width - barBox.width / 3)).toBeLessThan(1);
    const steps = plan.locator('.t2v-plan-step');
    await expect(steps).toHaveText(['Done: Load the MRHead volume', 'Next: Threshold the brain', 'To do: Measure the segment volume']);
    expect(await steps.evaluateAll((els) => els.map((el) => el.getAttribute('data-state')))).toEqual(['done', 'active', 'pending']);
    expect(await computed(page, '.t2v-plan-step[data-state="done"]', 'color')).toBe('rgb(103, 119, 124)');
    expect(await computed(page, '.t2v-plan-step[data-state="active"]', 'color')).toBe('rgb(2, 28, 37)');
    expect(await computed(page, '.t2v-plan-step[data-state="active"] .t2v-plan-mark', 'background-color')).toBe('rgb(38, 200, 184)');
    await expect(plan.locator('.t2v-spin')).toHaveCount(0);
  });

  test('a plan that is not only a checklist shows as markdown under the title', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, '/plan-notes');
    const plan = lastReply(page).locator('.t2v-plan');
    await expect(plan.locator('.t2v-plan-title')).toHaveText('Plan');
    await expect(plan.locator('.t2v-plan-count')).toHaveCount(0);
    await expect(plan.getByRole('progressbar')).toHaveCount(0);
    await expect(plan.locator('.t2v-md strong')).toHaveText('window / level');
  });
});

test.describe('the thread', () => {
  // `.t2v-messages > * + * { margin-top: 20px }`. `.t2v-msg { margin: 0 }`
  // came later with the same specificity and cancelled it, so every message sat flush on
  // the one before (the reply's action row straight onto the next question).
  test('20 px between messages, user and assistant alike', async ({ page, backend }) => {
    await page.goto(backend.url);
    await sendMessage(page, 'hello');
    await expect(lastReply(page).locator('.t2v-msg-actions')).toBeVisible();
    await sendMessage(page, 'thanks');
    await expect(page.locator('.t2v-msg')).toHaveCount(4);
    await expect(lastReply(page).locator('.t2v-msg-actions')).toBeVisible();
    const gaps = await page.locator('.t2v-messages > .t2v-msg').evaluateAll((els) =>
      els.slice(1).map((el, i) => Math.round(el.getBoundingClientRect().top - els[i]!.getBoundingClientRect().bottom)),
    );
    expect(gaps).toEqual([20, 20, 20]);
  });
});
