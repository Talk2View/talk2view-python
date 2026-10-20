/**
 * scripts/check-vendored-sdk.mjs's comparison logic, with no network: the canonical files
 * are strings here, and a fetch that failed is `null`.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  applyUnifiedDiff,
  checkFiles,
  fetchCanonical,
  HEADER_END,
  judgeVendored,
  parseVendored,
  REPOSITORY,
} from '../scripts/check-vendored-sdk.mjs';

const COMMIT = 'a'.repeat(40);

/** judgeVendored's argument (the script is plain JavaScript, so it declares no type). */
interface Judged {
  name: string;
  text: string;
  deltaText: string | null;
  atCommit: string | { missing: number } | null;
  atMain: string | { missing: number } | null;
}

function vendored(body: string, delta = 'none', path = 'src/ui/safeMarkdown.ts', repository = REPOSITORY): string {
  return [
    '// vendored-from: ' + repository + ' ' + path,
    '// vendored-commit: ' + COMMIT,
    '// local-delta: ' + delta,
    '//',
    '// Prose about the file.',
    HEADER_END,
    '',
  ].join('\n') + body;
}

const BASE = 'one\ntwo\nthree\nfour\nfive\n';
const BODY = 'one\ntwo\nTHREE\nfour\nfive\nsix\n';
const DELTA = [
  '--- a/src/x.ts',
  '+++ b/src/x.ts',
  '@@ -1,5 +1,6 @@',
  ' one',
  ' two',
  '-three',
  '+THREE',
  ' four',
  ' five',
  '+six',
  '',
].join('\n');

describe('parseVendored', () => {
  it('reads the header fields and returns the body after the header byte for byte', () => {
    const body = 'import x from "y";\n\n// a comment\n';
    expect(parseVendored(vendored(body))).toEqual({
      repository: REPOSITORY,
      path: 'src/ui/safeMarkdown.ts',
      commit: COMMIT,
      delta: 'none',
      body,
    });
  });

  it.each([
    ['no end line', vendored('x\n').replace(HEADER_END, '// the end')],
    ['no commit line', vendored('x\n').replace('// vendored-commit: ', '// commit: ')],
    ['a short commit', vendored('x\n').replace(COMMIT, 'abc1234')],
    ['a delta that is a path', vendored('x\n', '../evil.delta.diff')],
    ['a delta that is not a diff', vendored('x\n', 'patch.txt')],
    ['a source with no path', vendored('x\n').replace(' src/ui/safeMarkdown.ts', '')],
  ])('throws on a malformed header: %s', (_label, text) => {
    expect(() => parseVendored(text)).toThrow();
  });
});

describe('applyUnifiedDiff', () => {
  it('applies a change and an addition at the end', () => {
    expect(applyUnifiedDiff(BASE, DELTA)).toBe(BODY);
  });

  it('applies several hunks and keeps the lines between them', () => {
    const base = Array.from({ length: 20 }, (_, i) => 'l' + String(i + 1)).join('\n') + '\n';
    const diff = ['@@ -1,3 +1,3 @@', '-l1', '+L1', ' l2', ' l3', '@@ -18,3 +18,2 @@', ' l18', '-l19', ' l20', ''].join('\n');
    const expected = base.replace('l1\n', 'L1\n').replace('l19\n', '');
    expect(applyUnifiedDiff(base, diff)).toBe(expected);
  });

  it('refuses a hunk whose context does not match, rather than guess', () => {
    expect(() => applyUnifiedDiff(BASE.replace('two', 'TWO'), DELTA)).toThrow(/does not apply at line 2/);
  });

  it('refuses a diff with no hunks', () => {
    expect(() => applyUnifiedDiff(BASE, '--- a/x\n+++ b/x\n')).toThrow(/no hunks/);
  });

  it('honours "No newline at end of file" on either side', () => {
    const dropNewline = ['@@ -1,2 +1,2 @@', ' a', '-b', '+b', '\\ No newline at end of file', ''].join('\n');
    expect(applyUnifiedDiff('a\nb\n', dropNewline)).toBe('a\nb');
    const addNewline = ['@@ -1,2 +1,2 @@', ' a', '-b', '\\ No newline at end of file', '+b', ''].join('\n');
    expect(applyUnifiedDiff('a\nb', addNewline)).toBe('a\nb\n');
  });
});

describe('judgeVendored', () => {
  const none = (over: Partial<Judged>) =>
    judgeVendored({ name: 'ui/safeMarkdown.ts', text: vendored(BASE), deltaText: null, atCommit: BASE, atMain: BASE, ...over });
  const delta = (over: Partial<Judged>) =>
    judgeVendored({
      name: 'types.ts',
      text: vendored(BODY, 'types.ts.delta.diff', 'src/types.ts'),
      deltaText: DELTA,
      atCommit: BASE,
      atMain: BASE,
      ...over,
    });

  it('passes a delta-free copy equal to canonical main and to its commit', () => {
    expect(none({})).toEqual({ failures: [], warnings: [] });
  });

  it('FAILS a delta-free copy that differs from canonical main, naming the file to re-vendor', () => {
    const verdict = none({ atMain: BASE.replace('five', 'FIVE') });
    expect(verdict.warnings).toEqual([]);
    expect(verdict.failures).toHaveLength(1);
    expect(verdict.failures[0]).toMatch(/^ui\/safeMarkdown\.ts: differs from canonical main .*Re-vendor it/);
  });

  it('FAILS a delta-free copy edited locally', () => {
    expect(none({ text: vendored(BASE + '// a local tweak\n') }).failures).toHaveLength(1);
  });

  it('FAILS a delta-free copy whose header names a commit it does not match', () => {
    expect(none({ atCommit: 'older\n' }).failures[0]).toMatch(/not the commit its header names/);
  });

  it('passes a copy equal to its commit plus its declared delta', () => {
    expect(delta({})).toEqual({ failures: [], warnings: [] });
  });

  it('only WARNS when canonical main has changed a file with a delta since its commit', () => {
    expect(delta({ atMain: BASE + 'seven\n' })).toEqual({
      failures: [],
      warnings: [expect.stringMatching(/^types\.ts: canonical main has changed .* Re-vendor it on top of main/)],
    });
  });

  it('FAILS a copy with a change its delta does not declare', () => {
    expect(delta({ text: vendored(BODY + 'seven\n', 'types.ts.delta.diff', 'src/types.ts') }).failures[0]).toMatch(
      /undeclared local change/,
    );
  });

  it('FAILS a delta that no longer applies to its commit', () => {
    expect(delta({ atCommit: BASE.replace('three', 'drei') }).failures[0]).toMatch(/does not apply/);
  });

  it('FAILS a declared delta that is missing', () => {
    expect(delta({ deltaText: null }).failures[0]).toMatch(/not beside it/);
  });

  it('FAILS a copy from another repository', () => {
    expect(none({ text: vendored(BASE, 'none', 'src/ui/safeMarkdown.ts', 'someone/fork') }).failures[0]).toMatch(
      /vendored from someone\/fork/,
    );
  });

  it('FAILS a malformed header', () => {
    expect(none({ text: BASE }).failures[0]).toMatch(/vendoring header is malformed/);
  });

  it('only WARNS, and says the file was not compared, when canonical could not be fetched', () => {
    for (const verdict of [none({ atMain: null }), none({ atCommit: null }), delta({ atMain: null, atCommit: null })]) {
      expect(verdict.failures).toEqual([]);
      expect(verdict.warnings).toEqual([expect.stringMatching(/could not be fetched, so this file was NOT compared/)]);
    }
  });
});

describe('a definite "not there" from GitHub FAILS; only the network warns', () => {
  const none = (over: Partial<Judged>) =>
    judgeVendored({ name: 'ui/safeMarkdown.ts', text: vendored(BASE), deltaText: null, atCommit: BASE, atMain: BASE, ...over });

  it('FAILS a file gone from canonical main while the recorded commit still has it', () => {
    expect(none({ atMain: { missing: 404 } })).toEqual({
      failures: [expect.stringMatching(/^ui\/safeMarkdown\.ts: .* is gone from canonical main \(HTTP 404\): moved, renamed or removed upstream\. Re-vendor/)],
      warnings: [],
    });
  });

  it('FAILS a file missing at its recorded commit, and one missing everywhere', () => {
    expect(none({ atCommit: { missing: 404 } }).failures[0]).toMatch(/is not at the recorded commit/);
    expect(none({ atCommit: { missing: 404 }, atMain: { missing: 404 } }).failures[0]).toMatch(/is no longer public/);
  });

  // A tiny fetch stub: the answer for each URL, a status, or a thrown error.
  const stub = (answer: (url: string) => number | Error) => async (url: string) => {
    const got = answer(url);
    if (got instanceof Error) throw got;
    return new Response(got === 200 ? BASE : 'nope', { status: got });
  };

  it('fetchCanonical tells a 4xx apart from a network failure, a 429 and a 5xx', async () => {
    const at = (status: number | Error) => fetchCanonical(stub(() => status), REPOSITORY, 'main', 'src/x.ts');
    expect(await at(200)).toEqual({ text: BASE });
    expect(await at(404)).toEqual({ missing: 404 });
    expect(await at(410)).toEqual({ missing: 410 });
    expect(await at(429)).toEqual({ unreachable: 'HTTP 429 for main:src/x.ts' });
    expect(await at(503)).toEqual({ unreachable: 'HTTP 503 for main:src/x.ts' });
    expect(await at(new Error('getaddrinfo ENOTFOUND'))).toEqual({ unreachable: 'getaddrinfo ENOTFOUND for main:src/x.ts' });
  });

  it('checkFiles FAILS a 404 on main while the commit fetches, and only warns on a network failure', async () => {
    const files = [{ name: 'ui/safeMarkdown.ts', text: vendored(BASE), deltaText: null }];
    const moved = await checkFiles(files, stub((url) => (url.includes('/main/') ? 404 : 200)));
    expect(moved.failures).toEqual([expect.stringMatching(/gone from canonical main \(HTTP 404\)/)]);
    expect(moved.unreached).toBe(0);
    for (const down of [503, new Error('timed out')]) {
      const result = await checkFiles(files, stub((url) => (url.includes('/main/') ? down : 200)));
      expect(result.failures).toEqual([]);
      expect(result.unreached).toBe(1);
      expect(result.warnings).toEqual([expect.stringMatching(/was NOT compared/)]);
    }
    expect(await checkFiles(files, stub(() => 200))).toEqual({ failures: [], warnings: [], unreached: 0, networkError: '' });
  });
});

describe('the checked-in vendored files', () => {
  const dir = fileURLToPath(new URL('../src/vendor/sdk', import.meta.url));
  const files = (readdirSync(dir, { recursive: true }) as string[]).filter((f) => f.endsWith('.ts')).sort();

  it('are the four the page imports', () => {
    expect(files).toEqual(['assistant-ui/convert.ts', 'constants.ts', 'types.ts', 'ui/safeMarkdown.ts']);
  });

  it.each(files)('%s has a well-formed header, and any delta it declares is beside it', (file) => {
    const parsed = parseVendored(readFileSync(join(dir, file), 'utf8'));
    expect(parsed.repository).toBe(REPOSITORY);
    expect(parsed.path).toBe('src/' + file);
    if (parsed.delta !== 'none') expect(readFileSync(join(dir, file, '..', parsed.delta), 'utf8')).toMatch(/^@@ /m);
  });

  it('keeps the sanitiser delta-free: CI fails it on any difference from canonical main', () => {
    expect(parseVendored(readFileSync(join(dir, 'ui/safeMarkdown.ts'), 'utf8')).delta).toBe('none');
  });
});
