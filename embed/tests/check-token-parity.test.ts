import { describe, expect, it } from 'vitest';
import { checkTokenFloor, diffTokens, expectedFromTs, MIN_DARK, MIN_LIGHT, pageTokens, parseTokenBlocks } from '../scripts/check-token-parity.mjs';

const TS = `
:root {
  --t2v-teal: #26C8B8;
  --t2v-smoke: #021C25;
  --t2v-font: inherit;
  --background: #ffffff;
  --foreground: var(--t2v-smoke);
  --primary: var(--t2v-teal);
  --destructive: #BB524E;
  --radius: 0rem;
  --composer-padding: 0.5rem;
  /* private mirrors are ignored */
  --t2v-composer-radius: var(--composer-radius);
}
:root.dark,
.dark {
  --background: var(--t2v-smoke);
  --ring: var(--t2v-highlight);
}
@theme inline { --color-background: var(--background); }
`;

const PAGE = `
:root {
  --t2v-teal: #26C8B8;
  --t2v-smoke: #021C25;
  --t2v-background: #ffffff;
  --t2v-foreground: var(--t2v-smoke);
  --t2v-primary: var(--t2v-teal);
  --t2v-destructive: #bb524e;
  --t2v-radius: 0rem;
  --t2v-composer-padding: 0.5rem;
  --t2v-font: 'Space Grotesk', sans-serif;
  --t2v-hairline: rgba(2, 28, 37, 0.1);
}
html[data-theme='dark'] {
  --t2v-background: var(--t2v-smoke);
  --t2v-ring: var(--t2v-highlight);
}
`;

describe('parseTokenBlocks', () => {
  it('reads the declarations of the named blocks, in order, and nothing outside them', () => {
    const blocks = parseTokenBlocks(TS, [':root', ':root.dark,\n.dark']);
    expect(blocks[0]!['--background']).toBe('#ffffff');
    expect(blocks[0]!['--t2v-composer-radius']).toBe('var(--composer-radius)');
    expect(blocks[1]).toEqual({ '--background': 'var(--t2v-smoke)', '--ring': 'var(--t2v-highlight)' });
    expect(blocks[0]!['--color-background']).toBeUndefined();
  });

  it('throws when a block is missing, so a moved upstream file fails rather than passes', () => {
    expect(() => parseTokenBlocks(TS, [':root.light'])).toThrow(/:root\.light/);
  });
});

describe('expectedFromTs and pageTokens', () => {
  it('renames TS semantic tokens under --t2v-, keeps the brand names, drops --t2v-font and the mirrors', () => {
    const expected = expectedFromTs(TS);
    expect(expected.light).toEqual({
      '--t2v-teal': '#26c8b8', '--t2v-smoke': '#021c25',
      '--t2v-background': '#ffffff', '--t2v-foreground': 'var(--t2v-smoke)', '--t2v-primary': 'var(--t2v-teal)',
      '--t2v-destructive': '#bb524e', '--t2v-radius': '0rem', '--t2v-composer-padding': '0.5rem',
    });
    expect(expected.dark).toEqual({ '--t2v-background': 'var(--t2v-smoke)', '--t2v-ring': 'var(--t2v-highlight)' });
  });

  it('reads the page the same way and lowercases hexes', () => {
    const page = pageTokens(PAGE);
    expect(page.light['--t2v-destructive']).toBe('#bb524e');
    expect(page.light['--t2v-hairline']).toBe('rgba(2, 28, 37, 0.1)');
    expect(page.dark).toEqual({ '--t2v-background': 'var(--t2v-smoke)', '--t2v-ring': 'var(--t2v-highlight)' });
  });
});

describe('diffTokens', () => {
  it('is empty when the page carries every expected token with the same value', () => {
    expect(diffTokens(expectedFromTs(TS), pageTokens(PAGE))).toEqual([]);
  });

  it('names a missing token, a different value, and a dark token the page redeclares that TS does not', () => {
    const page = pageTokens(PAGE.replace('--t2v-primary: var(--t2v-teal);', '--t2v-primary: #000000;').replace("--t2v-radius: 0rem;", ''));
    page.dark['--t2v-primary'] = 'var(--t2v-teal)';
    expect(diffTokens(expectedFromTs(TS), page)).toEqual([
      'light --t2v-primary: page has #000000, TS has var(--t2v-teal)',
      'light --t2v-radius: missing from the page, TS has 0rem',
      'dark --t2v-primary: the page redeclares it in dark mode, TS does not',
    ]);
  });

  it('ignores page-only tokens (opacity variants, the font stack)', () => {
    expect(diffTokens(expectedFromTs(TS), pageTokens(PAGE + ':root { --t2v-extra: 1px; }'))).toEqual([]);
  });
});

describe('checkTokenFloor', () => {
  it('throws when a block exists but yields (near-)nothing, so a moved upstream file cannot pass by comparing zero tokens', () => {
    const moved = expectedFromTs(':root {\n  color: red;\n}\n:root.dark,\n.dark {\n}\n');
    expect(() => checkTokenFloor(moved)).toThrow(/read only 0 light \/ 0 dark tokens/);
  });

  it('does not throw once both palettes reach the floor', () => {
    const light: Record<string, string> = {};
    for (let i = 0; i < MIN_LIGHT; i += 1) light['--t2v-x' + String(i)] = '#000000';
    const dark: Record<string, string> = {};
    for (let i = 0; i < MIN_DARK; i += 1) dark['--t2v-y' + String(i)] = '#000000';
    expect(() => checkTokenFloor({ light, dark })).not.toThrow();
  });

  it('throws when only one palette is short', () => {
    const light: Record<string, string> = {};
    for (let i = 0; i < MIN_LIGHT; i += 1) light['--t2v-x' + String(i)] = '#000000';
    expect(() => checkTokenFloor({ light, dark: {} })).toThrow(/0 dark tokens/);
  });
});
