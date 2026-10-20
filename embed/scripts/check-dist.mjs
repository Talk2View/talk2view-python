// Fails the build when a built page could not work on Chromium 83 or under the UI
// server's CSP. Run by `npm run build` / `npm run build:guard`:
//
//   node scripts/check-dist.mjs <dist dir>
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

// The runtime guard's own `weakref` probe (src/guard/probes.ts) names both APIs in its
// label and reads them with `typeof globalThis.X`. Those are not callers, so they, and
// only they, verbatim, are stripped before counting.
export const PROBE_MENTIONS = [
  'typeof WeakRef / FinalizationRegistry',
  'typeof globalThis.WeakRef',
  'typeof globalThis.FinalizationRegistry',
];

export function countForbiddenApi(text, api) {
  let stripped = text;
  for (const literal of PROBE_MENTIONS) stripped = stripped.split(literal).join('');
  return stripped.split(api).length - 1;
}

export const FORBIDDEN_CSS = [
  ['flexbox/grid gap', /(^|[;{\s])(row-|column-)?gap\s*:/],
  ['inset', /(^|[;{\s])inset(-inline|-block)?(-start|-end)?\s*:/],
  [':is()', /:is\(/],
  [':where()', /:where\(/],
  [':has()', /:has\(/],
  ['@layer', /@layer\b/],
  ['container queries', /@container\b|container-type\s*:/],
  ['aspect-ratio', /aspect-ratio\s*:/],
  ['dvh/svh/lvh units', /\d(dvh|svh|lvh|dvw|svw|lvw)\b/],
  ['oklch/oklab', /okl(ch|ab)\(/],
  ['color-mix', /color-mix\(/],
  [':focus-visible', /:focus-visible/],
  ['a remote url()', /url\(\s*['"]?(https?:)?\/\//],
];

/** The built page's own marker (index.html): scripts/check_wheel_assets.py demands it too,
 * so a wheel carrying the Python tests' stub page cannot pass for the real one. */
export const PAGE_MARKER = /<meta\s[^>]*name=["']talk2view-page["']/i;

/** Every problem with the built page in `dist`; empty when it is fine. */
export function checkDist(dist) {
  const problems = [];
  const assets = join(dist, 'assets');
  let files = [];
  try {
    files = readdirSync(assets);
  } catch {
    return [`${assets} is missing: run vite build first`];
  }
  const css = files.filter((f) => f.endsWith('.css'));
  const js = files.filter((f) => f.endsWith('.js'));
  if (css.length !== 1) problems.push(`expected exactly 1 CSS file, found ${css.length}`);
  if (js.length !== 1) problems.push(`expected exactly 1 JS chunk, found ${js.length}: ${js.join(', ')}`);
  if (!files.some((f) => f.endsWith('.woff2'))) problems.push('no .woff2 in assets/: the font was not bundled');
  if (files.some((f) => f.endsWith('.map'))) problems.push('a source map is in assets/: the wheel ships none');

  for (const name of css) {
    const text = readFileSync(join(assets, name), 'utf8');
    for (const [label, pattern] of FORBIDDEN_CSS) {
      const hit = pattern.exec(text);
      if (hit) problems.push(`${name}: forbidden CSS (${label}) near "${text.slice(Math.max(0, hit.index - 30), hit.index + 40)}"`);
    }
  }
  for (const name of js) {
    const text = readFileSync(join(assets, name), 'utf8');
    for (const api of ['WeakRef', 'FinalizationRegistry']) {
      const count = countForbiddenApi(text, api);
      // Chromium 84+, and not polyfillable.
      if (count > 0) problems.push(`${name}: ${api} appears ${count}x; find the caller before shipping to Chromium 83`);
    }
  }
  let html = '';
  try {
    html = readFileSync(join(dist, 'index.html'), 'utf8');
  } catch {
    problems.push('index.html is missing');
  }
  if (/<script(?![^>]*\bsrc=)[^>]*>/i.test(html)) problems.push('index.html has an inline <script>; the CSP blocks it');
  if (/(src|href)="\//.test(html)) problems.push('index.html has an absolute asset URL; base must be "./"');
  if (/(src|href)="https?:/.test(html)) problems.push('index.html loads from another origin; the CSP blocks it');
  if (html && !PAGE_MARKER.test(html)) problems.push('index.html lacks <meta name="talk2view-page">, the built page\'s marker');
  return problems;
}

function main() {
  const target = process.argv[2];
  if (!target) {
    console.error('usage: node scripts/check-dist.mjs <dist dir>');
    process.exit(2);
  }
  const dist = resolve(target);
  const problems = checkDist(dist);
  if (problems.length) {
    console.error('check-dist FAILED for ' + dist + ':\n  - ' + problems.join('\n  - '));
    process.exit(1);
  }
  const js = readdirSync(join(dist, 'assets')).find((f) => f.endsWith('.js'));
  const size = readFileSync(join(dist, 'assets', js)).length;
  console.log(`check-dist OK: ${dist} (1 JS chunk ${(size / 1024).toFixed(0)} KiB, 1 CSS file, font bundled, no forbidden CSS, no WeakRef, no inline script)`);
}

// pathToFileURL, not a file:// template: on Windows the two spellings differ.
if (import.meta.url === pathToFileURL(process.argv[1]).href) main();
