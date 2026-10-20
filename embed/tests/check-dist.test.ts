import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { checkDist, countForbiddenApi } from '../scripts/check-dist.mjs';

const GOOD_HTML =
  '<!DOCTYPE html><html><head><meta name="talk2view-page" content="sdk-embed" />' +
  '<script type="module" crossorigin src="./assets/index-a.js"></script>' +
  '<link rel="stylesheet" crossorigin href="./assets/style-a.css"></head><body><div id="root"></div></body></html>';

function dist(files: Record<string, string | null>): string {
  const dir = mkdtempSync(join(tmpdir(), 't2v-dist-'));
  mkdirSync(join(dir, 'assets'));
  const all: Record<string, string | null> = {
    'index.html': GOOD_HTML,
    'assets/index-a.js': 'console.log(1)',
    'assets/style-a.css': '.a{margin:0}',
    'assets/font-a.woff2': 'woff2',
    ...files,
  };
  for (const [name, text] of Object.entries(all)) if (text !== null) writeFileSync(join(dir, name), text);
  return dir;
}

describe('check-dist', () => {
  it('passes a page Chromium 83 can run', () => {
    expect(checkDist(dist({}))).toEqual([]);
  });

  it('fails an index.html without the built page\'s marker', () => {
    const unmarked = GOOD_HTML.replace('<meta name="talk2view-page" content="sdk-embed" />', '');
    expect(checkDist(dist({ 'index.html': unmarked }))).toEqual([
      'index.html lacks <meta name="talk2view-page">, the built page\'s marker',
    ]);
  });

  it.each([
    ['flexbox/grid gap', '.a{display:flex;gap:4px}'],
    [':focus-visible', '.a:focus-visible{color:red}'],
    [':is()', ':is(.a,.b){color:red}'],
    ['inset', '.a{inset:0}'],
    ['@layer', '@layer base{.a{color:red}}'],
    ['a remote url()', '@font-face{src:url(https://fonts.example/x.woff2)}'],
  ])('fails built CSS with %s', (label, css) => {
    const problems = checkDist(dist({ 'assets/style-a.css': css }));
    expect(problems.join('\n')).toContain(label);
  });

  it("counts a real WeakRef caller, but not the guard probe's own mentions", () => {
    const probe = 'x("weakref","typeof WeakRef / FinalizationRegistry",typeof globalThis.WeakRef+" / "+typeof globalThis.FinalizationRegistry)';
    expect(countForbiddenApi(probe, 'WeakRef')).toBe(0);
    expect(countForbiddenApi(probe + ';new WeakRef(y)', 'WeakRef')).toBe(1);
    expect(checkDist(dist({ 'assets/index-a.js': 'new WeakRef(y)' })).join('\n')).toContain('WeakRef appears 1x');
  });

  it('fails an inline script, an absolute asset URL, a missing font, a second chunk and a source map', () => {
    expect(checkDist(dist({ 'index.html': GOOD_HTML.replace('</body>', '<script>alert(1)</script></body>') })).join('\n'))
      .toContain('inline <script>');
    expect(checkDist(dist({ 'index.html': GOOD_HTML.replace('./assets/index-a.js', '/assets/index-a.js') })).join('\n'))
      .toContain('absolute asset URL');
    expect(checkDist(dist({ 'assets/index-b.js': '1' })).join('\n')).toContain('exactly 1 JS chunk');
    expect(checkDist(dist({ 'assets/index-a.js.map': '{}' })).join('\n')).toContain('source map');
    expect(checkDist(dist({ 'assets/font-a.woff2': null })).join('\n')).toContain('the font was not bundled');
  });
});
