/**
 * Untrusted text — a tool name, an argument, a prompt, a detail value: anything a model or
 * a compromised tool can set — is rendered as plain text only, never HTML (the wire
 * contract's own rule). But plain text still LOOKS however its writer wants: a right-to-
 * left override placed inside a file path can make the extension read backwards, so a
 * "*.exe" ends up displayed as though it were a "*.png"; a zero-width space slipped into
 * "paypal" still reads as "paypal"; a wall of stacked combining marks on one base
 * character can grow a single glyph tall enough to blot out everything around it. Every
 * character this module treats as unsafe is replaced with a visible escape, so what is on
 * screen is what is in the string:
 *
 * - every Unicode format character (category Cf: bidi controls, zero-width joiners/
 *   spaces, the byte order mark, the Arabic letter mark, language tag characters…) and
 *   every C0/C1 control character other than a newline or a tab;
 * - a short, fixed set of characters that are not Cf/Cc but still render as nothing or as
 *   a placeholder box: the three Hangul filler characters (U+115F, U+1160, U+3164), the
 *   halfwidth Hangul filler (U+FFA0), and the blank Braille cell (U+2800);
 * - a combining mark (category Mn/Me) past the 4th in an unbroken run stacked on one base
 *   character — a normal accented letter (one, two, sometimes three marks) stays exactly
 *   as written; a "zalgo"-style pile does not.
 *
 * Left alone on purpose: a variation selector (U+FE00–FE0F, U+E0100–E01EF — legitimate,
 * e.g. emoji presentation), NBSP, and the line/paragraph separators U+2028/U+2029 (none of
 * these hide or misrepresent anything by themselves).
 *
 * Two renderers share one scan (`mapUnsafe`), so they agree on exactly which characters
 * that is: `displayText` (a human-readable `\uXXXX` escape, `\u{XXXXX}` past the BMP — see
 * ToolCall.tsx, Thread.tsx) and `escapeForJson` (a JSON-*valid* `\uXXXX` escape, a
 * surrogate pair past the BMP since JSON has no `\u{...}` form — see ToolCall.tsx's
 * edit-arguments draft, seeded with this instead of the raw text so the textarea shows
 * the same visible escapes the read-only view does, rather than the spoof again).
 */

// Chromium 83's V8 has supported \p{...} Unicode property escapes with the `u` flag since
// Chrome 64 (property escapes shipped in V8 6.4, years before 83): the regex's own syntax
// is not something the build target lowers the way newer statement-level JS syntax is, and
// `npm run build` (target chrome83) compiles this file with no warning or error.

/** Any character `mapUnsafe` might need to look at — the fast path. Ordinary text between
 * matches is copied with `slice()`, not walked one code point at a time, so a long mostly-
 * plain string (a 1 MB argument value) stays close to a single native regex pass. */
const CANDIDATE = /[\p{Cf}\p{Cc}\p{Mn}\p{Me}\u{115F}\u{1160}\u{2800}\u{3164}\u{FFA0}]/gu;
const CF_CC = /^[\p{Cf}\p{Cc}]$/u;
const COMBINING = /^[\p{Mn}\p{Me}]$/u;
const EXTRA_INVISIBLE = new Set([0x115f, 0x1160, 0x2800, 0x3164, 0xffa0]);
/** A base character may carry this many combining marks before the rest are escaped. */
const MAX_COMBINING_RUN = 4;

function isVariationSelector(code: number): boolean {
  return (code >= 0xfe00 && code <= 0xfe0f) || (code >= 0xe0100 && code <= 0xe01ef);
}

/**
 * Every code point `CANDIDATE` can match, classified and — for the ones this module
 * treats as unsafe — replaced through `escape`. Ordinary text is untouched. The one piece
 * of state carried across the scan is the combining-run length, which only a mark
 * immediately following another counts toward (an intervening ordinary character, or a
 * skipped variation selector, starts a fresh run).
 */
function mapUnsafe(s: string, escape: (ch: string) => string): string {
  let out = '';
  let last = 0;
  let combiningRun = 0;
  let runEndsAt = -1;
  for (const m of s.matchAll(CANDIDATE)) {
    const ch = m[0];
    const idx = m.index;
    const code = ch.codePointAt(0)!;
    if (isVariationSelector(code)) continue; // left alone entirely, never breaks a run either
    if (ch === '\n' || ch === '\t') continue; // the only Cc characters this module keeps
    if (CF_CC.test(ch) || EXTRA_INVISIBLE.has(code)) {
      combiningRun = 0;
      runEndsAt = -1;
      out += s.slice(last, idx) + escape(ch);
      last = idx + ch.length;
      continue;
    }
    // A combining mark (COMBINING.test is true for everything CANDIDATE can still match
    // here): count it toward the run only if it directly follows the previous one.
    combiningRun = idx === runEndsAt ? combiningRun + 1 : 1;
    runEndsAt = idx + ch.length;
    if (combiningRun > MAX_COMBINING_RUN) {
      out += s.slice(last, idx) + escape(ch);
      last = idx + ch.length;
    }
  }
  out += s.slice(last);
  return out;
}

function displayEscape(ch: string): string {
  // `ch` is one matched code point (the `u` flag matches a surrogate pair as a whole), so
  // this is always the character's own code point, not one half of a pair.
  const code = ch.codePointAt(0)!;
  return code > 0xffff
    ? `\\u{${code.toString(16).toUpperCase()}}`
    : `\\u${code.toString(16).toUpperCase().padStart(4, '0')}`;
}

/** Every unsafe character becomes a visible `\uXXXX` (or `\u{XXXXX}` past the BMP) escape. */
export function displayText(s: string): string {
  return mapUnsafe(s, displayEscape);
}

function jsonEscape(ch: string): string {
  const code = ch.codePointAt(0)!;
  if (code <= 0xffff) return '\\u' + code.toString(16).toUpperCase().padStart(4, '0');
  // JSON has no \u{...} form: a surrogate pair, same as JSON.stringify would emit.
  const c = code - 0x10000;
  const high = 0xd800 + (c >> 10);
  const low = 0xdc00 + (c & 0x3ff);
  return '\\u' + high.toString(16).toUpperCase().padStart(4, '0') + '\\u' + low.toString(16).toUpperCase().padStart(4, '0');
}

/**
 * The same characters `displayText` would escape, but as a JSON-*valid* `\uXXXX` escape
 * (a surrogate pair past the BMP, since JSON has no `\u{...}` form) rather than the
 * human-readable one — for seeding a JSON text editor with untrusted text so what shows
 * up there is already the visible escape, not the raw spoof, while `JSON.parse` on the
 * unedited text still yields the exact original string.
 */
export function escapeForJson(s: string): string {
  return mapUnsafe(s, jsonEscape);
}
