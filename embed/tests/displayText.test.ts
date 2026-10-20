import { describe, expect, it } from 'vitest';
import { displayText, escapeForJson } from '../src/ui/displayText';

// Every invisible/bidi character below is built with `String.fromCodePoint`, never pasted
// or escaped as a literal character in this SOURCE FILE: a literal one here would itself
// be an invisible character in a tracked file, which the pre-commit Cf/Cc scan (0
// tolerance) would catch. `String.fromCodePoint` gets there without needing a JS `\u`
// source escape at all, so nothing in this file's own bytes is a raw Cf/Cc character.
const c = (codePoint: number): string => String.fromCodePoint(codePoint);

describe('displayText (untrusted text made visible)', () => {
  it('leaves ordinary text untouched', () => {
    expect(displayText('load_sample_data')).toBe('load_sample_data');
    expect(displayText('{"name": "MRHead"}')).toBe('{"name": "MRHead"}');
    expect(displayText('café — 日本語 — ✓')).toBe('café — 日本語 — ✓'); // real script, not Cf/Cc
  });

  it('keeps newlines and tabs as themselves', () => {
    expect(displayText('a\nb\tc')).toBe('a\nb\tc');
  });

  it('escapes a right-to-left override so a spoofed extension reads literally', () => {
    // The classic attack: a path ending "report" + RLO + "gnp.exe" is meant
    // to display as though it ends in .png (the RLO reverses "exe.gnp" on screen).
    const spoofed = '/tmp/report' + c(0x202e) + 'gnp.exe';
    expect(displayText(spoofed)).toBe('/tmp/report\\u202Egnp.exe');
  });

  it('escapes every bidi control the review named', () => {
    expect(displayText('a' + c(0x202a) + 'b')).toBe('a\\u202Ab'); // LRE
    expect(displayText('a' + c(0x202b) + 'b')).toBe('a\\u202Bb'); // RLE
    expect(displayText('a' + c(0x202c) + 'b')).toBe('a\\u202Cb'); // PDF
    expect(displayText('a' + c(0x202d) + 'b')).toBe('a\\u202Db'); // LRO
    expect(displayText('a' + c(0x202e) + 'b')).toBe('a\\u202Eb'); // RLO
    expect(displayText('a' + c(0x2066) + 'b')).toBe('a\\u2066b'); // LRI
    expect(displayText('a' + c(0x2067) + 'b')).toBe('a\\u2067b'); // RLI
    expect(displayText('a' + c(0x2068) + 'b')).toBe('a\\u2068b'); // FSI
    expect(displayText('a' + c(0x2069) + 'b')).toBe('a\\u2069b'); // PDI
    expect(displayText('a' + c(0x200e) + 'b')).toBe('a\\u200Eb'); // LRM
    expect(displayText('a' + c(0x200f) + 'b')).toBe('a\\u200Fb'); // RLM
    expect(displayText('a' + c(0x061c) + 'b')).toBe('a\\u061Cb'); // Arabic letter mark
  });

  it('escapes zero-width characters, so a lookalike brand name cannot hide inside them', () => {
    expect(displayText('pay' + c(0x200b) + 'pal')).toBe('pay\\u200Bpal'); // ZWSP
    expect(displayText('pay' + c(0x200c) + 'pal')).toBe('pay\\u200Cpal'); // ZWNJ
    expect(displayText('pay' + c(0x200d) + 'pal')).toBe('pay\\u200Dpal'); // ZWJ
    expect(displayText('pay' + c(0x2060) + 'pal')).toBe('pay\\u2060pal'); // word joiner
    expect(displayText('pay' + c(0xfeff) + 'pal')).toBe('pay\\uFEFFpal'); // BOM / ZWNBSP
  });

  it('escapes the soft hyphen (category Cf, easy to miss)', () => {
    expect(displayText('extra' + c(0x00ad) + 'ordinary')).toBe('extra\\u00ADordinary');
  });

  it('escapes C0 and C1 controls other than newline and tab', () => {
    expect(displayText('a' + c(0x0000) + 'b')).toBe('a\\u0000b'); // NUL
    expect(displayText('a' + c(0x0007) + 'b')).toBe('a\\u0007b'); // BEL
    expect(displayText('a' + c(0x001b) + 'b')).toBe('a\\u001Bb'); // ESC
    expect(displayText('a' + c(0x007f) + 'b')).toBe('a\\u007Fb'); // DEL
    expect(displayText('a' + c(0x0085) + 'b')).toBe('a\\u0085b'); // NEL (C1)
    expect(displayText('a' + c(0x009f) + 'b')).toBe('a\\u009Fb'); // APC (C1)
  });

  it('escapes a carriage return: only \\n and \\t are exempt', () => {
    expect(displayText('a\rb')).toBe('a\\u000Db');
  });

  it('escapes a language tag character past the BMP with the \\u{XXXXX} form', () => {
    // U+E0001 LANGUAGE TAG: a supplementary-plane Cf character.
    expect(displayText('a' + c(0xe0001) + 'b')).toBe('a\\u{E0001}b');
  });

  it('escapes several invisible characters in the same string, each on its own', () => {
    const s = 'a' + c(0x200b) + 'b' + c(0x202e) + 'c' + c(0x0000) + 'd';
    expect(displayText(s)).toBe('a\\u200Bb\\u202Ec\\u0000d');
  });

  it('does not touch a real astral character (an emoji is not Cf/Cc)', () => {
    const emoji = c(0x1f600);
    expect(displayText(emoji)).toBe(emoji);
  });

  it('escapes the fixed set of invisible/filler characters that are not Cf/Cc', () => {
    expect(displayText('a' + c(0x115f) + 'b')).toBe('a\\u115Fb'); // Hangul choseong filler
    expect(displayText('a' + c(0x1160) + 'b')).toBe('a\\u1160b'); // Hangul jungseong filler
    expect(displayText('a' + c(0x3164) + 'b')).toBe('a\\u3164b'); // Hangul filler
    expect(displayText('a' + c(0x2800) + 'b')).toBe('a\\u2800b'); // Braille pattern blank
    expect(displayText('a' + c(0xffa0) + 'b')).toBe('a\\uFFA0b'); // halfwidth Hangul filler
  });

  it('leaves a normal accented letter (1–4 stacked combining marks) exactly as written', () => {
    const acute = c(0x0301); // COMBINING ACUTE ACCENT
    expect(displayText('e' + acute)).toBe('e' + acute);
    expect(displayText('e' + acute.repeat(4))).toBe('e' + acute.repeat(4));
  });

  it('escapes a combining mark past the 4th stacked on one base character ("zalgo" text)', () => {
    const acute = c(0x0301);
    const zalgo = 'e' + acute.repeat(6);
    expect(displayText(zalgo)).toBe('e' + acute.repeat(4) + '\\u0301\\u0301');
  });

  it('a combining run resets after an ordinary character breaks it', () => {
    const acute = c(0x0301);
    // 4 + break + 4: neither run alone is long enough to escape anything.
    const s = 'e' + acute.repeat(4) + 'f' + acute.repeat(4);
    expect(displayText(s)).toBe(s);
  });

  it('leaves a variation selector alone, even stacked with combining marks', () => {
    const acute = c(0x0301);
    const vs16 = c(0xfe0f); // VARIATION SELECTOR-16 (emoji presentation)
    expect(displayText('a' + vs16 + 'b')).toBe('a' + vs16 + 'b');
    // A variation selector inside an otherwise over-long combining run: it is skipped
    // entirely (never counted, never escaped), and does not glue the two halves of the
    // run into one continuous run either.
    const s = 'e' + acute.repeat(4) + vs16 + acute.repeat(4);
    expect(displayText(s)).toBe(s);
  });

  it('leaves NBSP and the line/paragraph separators alone', () => {
    const nbsp = c(0x00a0);
    const ls = c(0x2028); // LINE SEPARATOR
    const ps = c(0x2029); // PARAGRAPH SEPARATOR
    expect(displayText('a' + nbsp + 'b')).toBe('a' + nbsp + 'b');
    expect(displayText('a' + ls + 'b')).toBe('a' + ls + 'b');
    expect(displayText('a' + ps + 'b')).toBe('a' + ps + 'b');
  });

  it('scans a 1 MB mostly-ordinary string close to linearly, not character by character', () => {
    // Representative of a genuinely huge tool argument: long plain runs with occasional
    // unsafe characters scattered through, the shape the fast path (slice between regex
    // matches) is meant for.
    const chunk = 'the quick brown fox jumps over the lazy dog ' + c(0x200b);
    const big = chunk.repeat(Math.ceil((1024 * 1024) / chunk.length));
    const t0 = performance.now();
    const out = displayText(big);
    const ms = performance.now() - t0;
    expect(out).toContain('\\u200B');
    expect(out).not.toContain(c(0x200b));
    // eslint-disable-next-line no-console
    console.log(`displayText on a ${(big.length / 1024 / 1024).toFixed(2)} MB string: ${ms.toFixed(1)} ms`);
    expect(ms).toBeLessThan(1000); // generous: a UI thread must not hang on this
  });
});

describe('escapeForJson (the same escapes, as valid JSON text)', () => {
  it('escapes a BMP character as a JSON \\uXXXX escape that parses back to the original', () => {
    const spoofed = '/tmp/report' + c(0x202e) + 'gnp.exe';
    const escaped = escapeForJson(spoofed);
    expect(escaped).toBe('/tmp/report\\u202Egnp.exe');
    expect(JSON.parse('"' + escaped + '"')).toBe(spoofed);
  });

  it('escapes an astral character as a JSON surrogate pair, not \\u{XXXXX}', () => {
    const s = 'a' + c(0xe0001) + 'b';
    const escaped = escapeForJson(s);
    expect(escaped).toBe('a\\uDB40\\uDC01b'); // U+E0001 as a UTF-16 surrogate pair
    expect(JSON.parse('"' + escaped + '"')).toBe(s);
  });

  it('round-trips a whole JSON object through JSON.stringify -> escapeForJson -> JSON.parse', () => {
    const original = { path: '/tmp/report' + c(0x202e) + 'gnp.exe', note: 'pay' + c(0x200b) + 'pal' };
    const pretty = JSON.stringify(original, null, 2);
    const escaped = escapeForJson(pretty);
    expect(escaped).toContain('\\u202E');
    expect(escaped).not.toContain(c(0x202e));
    expect(JSON.parse(escaped)).toEqual(original);
  });

  it('leaves ordinary JSON text (and its structural whitespace) untouched', () => {
    const pretty = JSON.stringify({ name: 'MRHead', count: 2 }, null, 2);
    expect(escapeForJson(pretty)).toBe(pretty);
  });
});
