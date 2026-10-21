// The page's theme tokens (src/styles/app.css) against the public TypeScript chat's
// (github.com/Talk2View/talk2view-typescript, src/chat/styles/chat.src.css). Run in CI
// (the embed job) next to check-vendored-sdk.mjs; no token, the repository is public:
//
//   node scripts/check-token-parity.mjs
//
// The TS sheet declares shadcn-style semantic tokens on `:root` (`--background`, …) and
// redeclares some on `:root.dark, .dark`; the page declares the same tokens as
// `--t2v-<name>` on `:root` and `html[data-theme='dark']`. Brand hexes are `--t2v-*` on
// both. This check demands that every TS token has a page twin with the same value
// (hexes compared case-insensitively, `var()` references verbatim), and that the page
// redeclares in dark mode exactly what TS redeclares. Page-only tokens (the font stack,
// the rgba opacity variants) are ignored: TS has no name for them.
//
// - A difference FAILS, naming each token.
// - A definite "not there" from GitHub (any 4xx but 429) FAILS: the file moved.
// - A network failure WARNS and never fails: the output says nothing was compared.
import { readFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { fetchCanonical, REPOSITORY } from './check-vendored-sdk.mjs';

export const TS_PATH = 'src/chat/styles/chat.src.css';
export const TS_LIGHT = ':root';
export const TS_DARK = ':root.dark,\n.dark';
export const PAGE_LIGHT = ':root';
export const PAGE_DARK = "html[data-theme='dark']";
/** The real TS sheet has 32 light / 18 dark tokens after SKIPPED. An upstream
 * restructuring that keeps both selectors but moves the declarations elsewhere (a
 * `@layer`, a second block, a rename) must not silently compare zero tokens and report a
 * match, so these are a floor well below the real count, not the count itself. */
export const MIN_LIGHT = 30;
export const MIN_DARK = 15;
/** TS tokens with no page twin: the font is the page's own (a web view inherits none), and
 * the private mirrors are an artefact of the TS build (chat.src.css:65-70). */
const SKIPPED = new Set(['--t2v-font', '--t2v-composer-radius', '--t2v-composer-bg', '--t2v-composer-padding']);

/** `--name: value;` declarations of each named block, as `{name: value}`, in order. Only
 * the first block whose selector text (whitespace-normalised) equals the given one is read;
 * a missing block throws, so a moved upstream file cannot pass by matching nothing. The
 * head never crosses a `;`: chat.src.css opens with semicolon-terminated at-rules with no
 * `{}` of their own (`@import "tailwindcss" source(none);`, `@custom-variant dark (...);`),
 * and without this a block-less prelude would be swallowed into the first block's selector.
 * @param {string} css
 * @param {string[]} selectors
 * @returns {Record<string, string>[]} */
export function parseTokenBlocks(css, selectors) {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, '');
  return selectors.map((selector) => {
    const wanted = selector.replace(/\s+/g, ' ').trim();
    const re = /([^{};]+)\{([^{}]*)\}/g;
    let match;
    while ((match = re.exec(stripped)) !== null) {
      const head = match[1].replace(/\s+/g, ' ').trim();
      if (head !== wanted) continue;
      const out = {};
      for (const line of match[2].split(';')) {
        const decl = /^\s*(--[\w-]+)\s*:\s*(.+?)\s*$/.exec(line);
        if (decl) out[decl[1]] = decl[2];
      }
      return out;
    }
    throw new Error('no "' + wanted + ' {" block');
  });
}

/** A value as compared: hex colours lowercased, other text as written. */
function norm(value) {
  return /^#[0-9a-f]{3,8}$/i.test(value) ? value.toLowerCase() : value;
}

/** The TS sheet's tokens as the page must spell them.
 * @param {string} tsCss
 * @returns {{ light: Record<string, string>, dark: Record<string, string> }} */
export function expectedFromTs(tsCss) {
  const [light, dark] = parseTokenBlocks(tsCss, [TS_LIGHT, TS_DARK]);
  const rename = (block) => {
    const out = {};
    for (const [name, value] of Object.entries(block)) {
      if (SKIPPED.has(name)) continue;
      const page = name.startsWith('--t2v-') ? name : '--t2v-' + name.slice(2);
      out[page] = norm(value);
    }
    return out;
  };
  return { light: rename(light), dark: rename(dark) };
}

/** The page's tokens, `--t2v-*` only.
 * @param {string} pageCss
 * @returns {{ light: Record<string, string>, dark: Record<string, string> }} */
export function pageTokens(pageCss) {
  const [light, dark] = parseTokenBlocks(pageCss, [PAGE_LIGHT, PAGE_DARK]);
  const keep = (block) => {
    const out = {};
    for (const [name, value] of Object.entries(block)) if (name.startsWith('--t2v-')) out[name] = norm(value);
    return out;
  };
  return { light: keep(light), dark: keep(dark) };
}

/** Refuses a suspiciously small parsed set instead of letting `diffTokens` compare zero
 * tokens and report a match: an earlier version's regex silently returned `{}` for the
 * light block against the live file, and the check printed "OK: 0 tokens match" with
 * exit 0. Throws (never returns false) so `main()`'s existing
 * try/catch-and-FAIL pattern around `expectedFromTs` covers this too.
 * @param {{ light: Record<string, string>, dark: Record<string, string> }} expected
 * @returns {void} */
export function checkTokenFloor(expected) {
  const light = Object.keys(expected.light).length;
  const dark = Object.keys(expected.dark).length;
  if (light < MIN_LIGHT || dark < MIN_DARK) {
    throw new Error(
      'read only ' + light + ' light / ' + dark + ' dark tokens from ' + TS_PATH + ' (expected at least ' +
        MIN_LIGHT + '/' + MIN_DARK + '): the upstream token blocks changed shape.',
    );
  }
}

/** Every difference, one line each; empty when the page matches.
 * @param {{ light: Record<string, string>, dark: Record<string, string> }} expected
 * @param {{ light: Record<string, string>, dark: Record<string, string> }} page
 * @returns {string[]} */
export function diffTokens(expected, page) {
  const out = [];
  for (const [name, value] of Object.entries(expected.light)) {
    const have = page.light[name];
    if (have === undefined) out.push('light ' + name + ': missing from the page, TS has ' + value);
    else if (have !== value) out.push('light ' + name + ': page has ' + have + ', TS has ' + value);
  }
  for (const [name, value] of Object.entries(expected.dark)) {
    const have = page.dark[name];
    if (have === undefined) out.push('dark ' + name + ': missing from the page, TS has ' + value);
    else if (have !== value) out.push('dark ' + name + ': page has ' + have + ', TS has ' + value);
  }
  for (const name of Object.keys(page.dark)) {
    if (name in expected.light && !(name in expected.dark)) {
      out.push('dark ' + name + ': the page redeclares it in dark mode, TS does not');
    }
  }
  return out;
}

async function main() {
  const pageCss = readFileSync(fileURLToPath(new URL('../src/styles/app.css', import.meta.url)), 'utf8');
  const page = pageTokens(pageCss);
  const fetched = await fetchCanonical(fetch, REPOSITORY, 'main', TS_PATH);
  if ('unreachable' in fetched) {
    console.warn('WARNING: ' + fetched.unreachable + '. A network failure is a warning, not a failure: the tokens were NOT compared this run.');
    return;
  }
  if ('missing' in fetched) {
    console.error('check-token-parity FAILED: ' + REPOSITORY + ' ' + TS_PATH + ' is not on canonical main (HTTP ' + fetched.missing + '): the stylesheet moved. Update TS_PATH.');
    process.exit(1);
  }
  let expected;
  try {
    expected = expectedFromTs(fetched.text);
  } catch (err) {
    console.error('check-token-parity FAILED: ' + TS_PATH + ' no longer has the token blocks this check reads (' + err.message + ').');
    process.exit(1);
  }
  try {
    checkTokenFloor(expected);
  } catch (err) {
    console.error('check-token-parity FAILED: ' + err.message);
    process.exit(1);
  }
  const diffs = diffTokens(expected, page);
  if (diffs.length > 0) {
    console.error('check-token-parity FAILED (src/styles/app.css vs ' + REPOSITORY + ' ' + TS_PATH + '):\n  - ' + diffs.join('\n  - '));
    process.exit(1);
  }
  const count = Object.keys(expected.light).length + Object.keys(expected.dark).length;
  console.log('check-token-parity OK: ' + count + ' tokens match ' + REPOSITORY + ' ' + TS_PATH);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) await main();
