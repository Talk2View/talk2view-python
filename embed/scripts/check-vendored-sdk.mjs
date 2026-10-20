// The page's vendored TS SDK files (src/vendor/sdk/) against their source of truth, the
// public github.com/Talk2View/talk2view-typescript repository. Run in CI (the sdk-embed
// job); it needs no token, because the repository is public:
//
//   node scripts/check-vendored-sdk.mjs
//
// Every vendored file opens with a header (`parseVendored`):
//
//   // vendored-from: Talk2View/talk2view-typescript src/ui/safeMarkdown.ts
//   // vendored-commit: <the 40-character commit it was copied from>
//   // local-delta: none                     (or the name of a .delta.diff beside it)
//   // ...what it is, and what the delta adds and why...
//   // end of vendoring header
//
// and everything after that last line is the body. What the check demands:
//
// - Local delta "none" (the reply sanitiser among them): the body must be the file on
//   canonical main, byte for byte, and the file at the recorded commit too, so the header
//   tells the truth. Any difference FAILS, naming the file to re-vendor.
// - A declared delta: the body must be the file at the recorded commit with the declared
//   unified diff applied. A difference FAILS: the copy has an undeclared local change.
//   When canonical main's file differs from the recorded commit's, it WARNS only:
//   canonical has moved on, and the file should be re-vendored on top of it.
// - A definite "not there" from GitHub (any 4xx but 429: the file was moved, renamed or
//   removed upstream, the commit is wrong, or the repository stopped being public) FAILS.
// - A network failure (a thrown fetch, a timeout, 429 or a 5xx) WARNS and never fails. The
//   output says the files were not compared.
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const REPOSITORY = 'Talk2View/talk2view-typescript';
export const HEADER_END = '// end of vendoring header';
const RAW = 'https://raw.githubusercontent.com/';
const FETCH_MS = 20000;

/**
 * A vendored file's header fields and its body. Throws when the header is malformed:
 * a vendored file without one is itself a failure.
 */
export function parseVendored(text) {
  const end = text.indexOf(HEADER_END + '\n');
  if (end < 0) throw new Error('no "' + HEADER_END + '" line');
  const header = text.slice(0, end).split('\n');
  const field = (name) => {
    const line = header.find((l) => l.startsWith('// ' + name + ': '));
    if (!line) throw new Error('no "// ' + name + ':" line in the header');
    return line.slice(('// ' + name + ': ').length).trim();
  };
  const [repository, path, ...rest] = field('vendored-from').split(/\s+/);
  if (!repository || !path || rest.length > 0) throw new Error('"vendored-from" must be "<owner>/<repo> <path>"');
  const commit = field('vendored-commit');
  if (!/^[0-9a-f]{40}$/.test(commit)) throw new Error('"vendored-commit" must be a full 40-character commit');
  const delta = field('local-delta');
  if (delta !== 'none' && !/^[\w.-]+\.delta\.diff$/.test(delta)) {
    throw new Error('"local-delta" must be "none" or the name of a .delta.diff file beside it');
  }
  return { repository, path, commit, delta, body: text.slice(end + HEADER_END.length + 1) };
}

/** `text` as lines without their newlines, and whether its last line ended with one. */
function toLines(text) {
  const endsWithNewline = text.endsWith('\n');
  const body = endsWithNewline ? text.slice(0, -1) : text;
  return { lines: body === '' && !endsWithNewline ? [] : body.split('\n'), endsWithNewline };
}

/**
 * `base` with a unified diff applied. Every context and removed line must match `base`
 * exactly where its hunk says; anything else throws, so a delta that no longer applies is
 * reported, never guessed at.
 */
export function applyUnifiedDiff(base, diff) {
  const old = toLines(base);
  const out = [];
  let cursor = 0; // the next line of `base` not yet consumed, 0-based
  let hunks = 0;
  // "\ No newline at end of file" after an added or a context line: the new file's last
  // line has no newline. Only the hunk that reaches the end of the file can carry it.
  let newSideUnterminated = false;
  const lines = diff.split('\n');
  if (lines[lines.length - 1] === '') lines.pop();
  let i = 0;
  while (i < lines.length) {
    const head = /^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@/.exec(lines[i]);
    i += 1;
    if (!head) continue; // the ---/+++ lines, or anything else before a hunk
    hunks += 1;
    const oldCount = head[2] === undefined ? 1 : Number(head[2]);
    // A hunk that takes nothing from the old file names the line BEFORE its insertion.
    const start = oldCount === 0 ? Number(head[1]) : Number(head[1]) - 1;
    if (start < cursor) throw new Error('hunk ' + hunks + ' overlaps the one before it');
    if (start > old.lines.length) throw new Error('hunk ' + hunks + ' starts past the end of the file');
    while (cursor < start) out.push(old.lines[cursor++]);
    let previous = '';
    for (; i < lines.length && !lines[i].startsWith('@@'); i += 1) {
      const line = lines[i];
      const kind = line === '' ? ' ' : line[0]; // a blank context line may have lost its space
      const content = line.slice(1);
      if (kind === '\\') {
        if (previous === '+' || previous === ' ') newSideUnterminated = true;
        continue;
      }
      if (kind === ' ' || kind === '-') {
        if (cursor >= old.lines.length || old.lines[cursor] !== content) {
          throw new Error('hunk ' + hunks + ' does not apply at line ' + (cursor + 1) + ': expected ' + JSON.stringify(content));
        }
        cursor += 1;
        if (kind === ' ') out.push(content);
      } else if (kind === '+') {
        out.push(content);
      } else {
        throw new Error('hunk ' + hunks + ' has a line that is neither context, removal nor addition');
      }
      previous = kind;
    }
  }
  if (hunks === 0) throw new Error('the delta has no hunks');
  const reachedEnd = cursor === old.lines.length;
  while (cursor < old.lines.length) out.push(old.lines[cursor++]);
  const endsWithNewline = reachedEnd ? !newSideUnterminated : old.endsWithNewline;
  return out.join('\n') + (out.length > 0 && endsWithNewline ? '\n' : '');
}

const isMissing = (fetched) => fetched !== null && typeof fetched === 'object' && typeof fetched.missing === 'number';

/**
 * The verdict on one vendored file, from its text, its declared delta (or null), and the
 * canonical file at the recorded commit and on main: its text; `{ missing: status }` when
 * GitHub answered that it is not there; or null when it could not be fetched. No network
 * here, so it is unit-tested on its own.
 */
export function judgeVendored({ name, text, deltaText, atCommit, atMain }) {
  const failures = [];
  const warnings = [];
  let parsed;
  try {
    parsed = parseVendored(text);
  } catch (err) {
    return { failures: [name + ': its vendoring header is malformed (' + err.message + ')'], warnings };
  }
  const where = parsed.repository + ' ' + parsed.path;
  const redo = ' Re-vendor it from ' + where + ' (README.md, "Where the TS SDK code comes from").';
  if (parsed.repository !== REPOSITORY) failures.push(name + ': vendored from ' + parsed.repository + ', not ' + REPOSITORY + '.');
  if (isMissing(atCommit) || isMissing(atMain)) {
    // A definite answer, so never a warning: the check could otherwise stay green for good.
    if (isMissing(atCommit) && isMissing(atMain)) {
      failures.push(name + ': ' + where + ' is found neither at ' + parsed.commit + ' nor on main (HTTP ' + atMain.missing + '): the repository moved, was renamed, or is no longer public.');
    } else if (isMissing(atMain)) {
      failures.push(name + ': ' + where + ' is gone from canonical main (HTTP ' + atMain.missing + '): moved, renamed or removed upstream. Re-vendor it from wherever it went and update vendored-from (README.md, "Where the TS SDK code comes from").');
    } else {
      failures.push(name + ': ' + where + ' is not at the recorded commit ' + parsed.commit + ' (HTTP ' + atCommit.missing + '). Set vendored-commit and vendored-from to where it was copied from.');
    }
    return { failures, warnings };
  }
  if (atCommit === null || atMain === null) {
    warnings.push(name + ': canonical ' + where + ' could not be fetched, so this file was NOT compared this run.');
    return { failures, warnings };
  }
  if (parsed.delta === 'none') {
    if (parsed.body !== atMain) failures.push(name + ': differs from canonical main (' + where + ').' + redo);
    else if (parsed.body !== atCommit) {
      failures.push(name + ': matches canonical main, but not the commit its header names (' + parsed.commit + '). Set vendored-commit to the commit it was copied from.');
    }
    return { failures, warnings };
  }
  if (deltaText === null) {
    failures.push(name + ': its header declares ' + parsed.delta + ', which is not beside it.');
    return { failures, warnings };
  }
  let expected;
  try {
    expected = applyUnifiedDiff(atCommit, deltaText);
  } catch (err) {
    failures.push(name + ': ' + parsed.delta + ' does not apply to ' + where + ' at ' + parsed.commit + ' (' + err.message + ').');
    return { failures, warnings };
  }
  if (parsed.body !== expected) {
    failures.push(name + ': is not ' + where + ' at ' + parsed.commit + ' plus ' + parsed.delta + '. An undeclared local change: move it into the delta, or drop it.');
  }
  if (atMain !== atCommit) {
    warnings.push(name + ': canonical main has changed ' + where + ' since ' + parsed.commit + '. Re-vendor it on top of main and re-apply ' + parsed.delta + '.');
  }
  return { failures, warnings };
}

/** Every vendored .ts file under `dir`, as [path relative to `dir`, absolute path]. */
function vendoredFiles(dir) {
  const found = [];
  const walk = (at) => {
    for (const entry of readdirSync(at, { withFileTypes: true })) {
      const full = join(at, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith('.ts')) found.push([relative(dir, full), full]);
    }
  };
  walk(dir);
  return found.sort();
}

/**
 * One canonical file at one ref: `{ text }`; `{ missing: status }` when GitHub answered that
 * it is not there (any 4xx but 429); or `{ unreachable: reason }` for a network failure: a
 * thrown fetch, a timeout, 429 (rate limited) or a 5xx. `fetchImpl` is `fetch`, or a stub.
 */
export async function fetchCanonical(fetchImpl, repository, ref, path) {
  const where = ref + ':' + path;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_MS);
  try {
    const res = await fetchImpl(RAW + repository + '/' + ref + '/' + path, { signal: controller.signal });
    if (res.ok) return { text: await res.text() };
    if (res.status === 429 || res.status >= 500) return { unreachable: 'HTTP ' + res.status + ' for ' + where };
    return { missing: res.status };
  } catch (err) {
    return { unreachable: (err && err.message ? err.message : String(err)) + ' for ' + where };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Every vendored file (`{ name, text, deltaText }`) against canonical, fetched with
 * `fetchImpl`: the failures, the warnings, and how many files the network kept from being
 * compared (with the first network error, for the output).
 */
export async function checkFiles(files, fetchImpl) {
  const failures = [];
  const warnings = [];
  let unreached = 0;
  let networkError = '';
  for (const { name, text, deltaText } of files) {
    let parsed = null;
    try {
      parsed = parseVendored(text);
    } catch {
      // judgeVendored reports it
    }
    let atCommit = null;
    let atMain = null;
    if (parsed) {
      const [commit, main] = await Promise.all([
        fetchCanonical(fetchImpl, parsed.repository, parsed.commit, parsed.path),
        fetchCanonical(fetchImpl, parsed.repository, 'main', parsed.path),
      ]);
      const settle = (fetched) => ('text' in fetched ? fetched.text : 'missing' in fetched ? fetched : null);
      atCommit = settle(commit);
      atMain = settle(main);
      const unreachable = [commit, main].find((fetched) => 'unreachable' in fetched);
      if (unreachable && !isMissing(atCommit) && !isMissing(atMain)) {
        unreached += 1;
        networkError = networkError || unreachable.unreachable;
      }
    }
    const verdict = judgeVendored({ name, text, deltaText, atCommit, atMain });
    failures.push(...verdict.failures);
    warnings.push(...verdict.warnings);
  }
  return { failures, warnings, unreached, networkError };
}

async function main() {
  const dir = resolve(fileURLToPath(new URL('../src/vendor/sdk', import.meta.url)));
  const files = vendoredFiles(dir).map(([name, full]) => {
    const text = readFileSync(full, 'utf8');
    let deltaText = null;
    try {
      const parsed = parseVendored(text);
      if (parsed.delta !== 'none') deltaText = readFileSync(join(full, '..', parsed.delta), 'utf8');
    } catch {
      deltaText = null; // judgeVendored reports a bad header or a missing delta
    }
    return { name, text, deltaText };
  });
  const { failures, warnings, unreached, networkError } = await checkFiles(files, fetch);
  for (const warning of warnings) console.warn('WARNING: ' + warning);
  if (unreached > 0) {
    console.warn('WARNING: GitHub could not be reached for ' + unreached + ' of ' + files.length + ' files (' + networkError + '). A network failure is a warning, not a failure: those files were NOT checked this run.');
  }
  if (failures.length > 0) {
    console.error('check-vendored-sdk FAILED:\n  - ' + failures.join('\n  - '));
    process.exit(1);
  }
  const compared = files.length - unreached;
  const verdict = compared === files.length ? 'OK' : compared === 0 ? 'NOT CHECKED (network)' : 'PARTLY CHECKED (network)';
  console.log('check-vendored-sdk ' + verdict + ': ' + compared + ' of ' + files.length + ' vendored files compared against ' + REPOSITORY + (warnings.length ? ', ' + warnings.length + ' warning(s)' : ''));
}

// pathToFileURL, not a file:// template: on Windows the two spellings differ.
if (import.meta.url === pathToFileURL(process.argv[1]).href) await main();
