// The source stylesheets against caniuse's Chromium 83: `doiuse --browsers
// "chrome 83"`, which reports but always exits 0, so any line it prints fails here.
//
//   node scripts/check-css.mjs src/styles/app.css
//
// Three caniuse features are "partial" on 83 and ignored, each because the part 83 lacks
// is never used and stylelint (.stylelintrc.json) bans it outright:
// - css-sticky: missing only on table parts; a css:sticky probe measured the
//   composer footer sticking on 83;
// - css-overflow: missing `overflow: clip` (banned) and the two-value shorthand;
// - text-decoration: missing text-decoration-thickness (banned); text-decoration-color
//   works on 83.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const IGNORED = ['css-sticky', 'css-overflow', 'text-decoration'];

const files = process.argv.slice(2);
if (files.length === 0) {
  console.error('usage: node scripts/check-css.mjs <file.css> ...');
  process.exit(2);
}
// Its package `exports` hides the CLI, so it is found by path.
const cli = fileURLToPath(new URL('../node_modules/doiuse/bin/cli.js', import.meta.url));
const run = spawnSync(process.execPath, [cli, '--browsers', 'chrome 83', '--ignore', IGNORED.join(','), ...files], {
  encoding: 'utf8',
});
const found = (run.stdout || '').split('\n').filter((line) => line.trim() !== '');
if (run.status !== 0 || found.length > 0) {
  console.error('check-css FAILED (doiuse, chrome 83):\n  ' + (found.join('\n  ') || run.stderr));
  process.exit(1);
}
console.log('check-css OK: ' + files.join(', ') + ' uses nothing Chromium 83 lacks (ignored: ' + IGNORED.join(', ') + ')');
