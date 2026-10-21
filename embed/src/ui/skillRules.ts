/** Live checks for the skill form: a port of talk2view.skill_format's rules for the
 * fields the form edits. The SDK re-checks on save and is the authority. Pure: no React,
 * no store. */
export const NAME_MAX = 64;
export const DESCRIPTION_MAX = 1024;
/** Past this many lines of instructions the form suggests trimming. */
export const INSTRUCTIONS_SOFT_MAX_LINES = 500;
const RESERVED = ['anthropic', 'claude'];
const XML_TAG = /<[^<>\n]+>/;
/** Every character Python's `str.splitlines()` breaks at other than "\n": the SDK reads
 * frontmatter line by line with it, so one of these would split a name or description
 * on the way back in (talk2view.skill_format refuses them too). */
const ODD_LINE_BREAK = /[\r\u000b\u000c\u001c\u001d\u001e\u0085\u2028\u2029]/;
export const ODD_LINE_BREAK_PROBLEM =
  "Contains a line-break character that isn't a normal newline (e.g. from a pasted document); remove it.";

export function suggestName(text: string): string {
  const ascii = text.normalize('NFKD').replace(/[̀-ͯ]/g, '');
  const words = (ascii.toLowerCase().match(/[a-z0-9]+/g) ?? []).filter((w) => !RESERVED.includes(w));
  const name = words.join('-').slice(0, NAME_MAX).replace(/^-+|-+$/g, '');
  return name.replace(/-{2,}/g, '-') || 'skill';
}

export function nameProblem(name: string): string | null {
  if (!name) return 'A skill needs a name.';
  if (ODD_LINE_BREAK.test(name)) return ODD_LINE_BREAK_PROBLEM;
  if (name.length > NAME_MAX) return `At most ${NAME_MAX} characters.`;
  if (XML_TAG.test(name) || !/^[a-z0-9]+(-[a-z0-9]+)*$/.test(name)) return 'Lowercase letters, numbers and single hyphens.';
  if (RESERVED.some((w) => name.includes(w))) return "Must not contain 'anthropic' or 'claude'.";
  return null;
}

export function descriptionProblem(d: string): string | null {
  if (!d.trim()) return 'Say what the skill does and when to use it.';
  if (ODD_LINE_BREAK.test(d)) return ODD_LINE_BREAK_PROBLEM;
  if (d.length > DESCRIPTION_MAX) return `At most ${DESCRIPTION_MAX} characters.`;
  if (XML_TAG.test(d)) return 'No XML tags.';
  return null;
}

export function firstPerson(d: string): boolean {
  return /^\s*(i|i'm|i am|you|your|we)\b/i.test(d);
}

/** `name`, or the first of `name-2`, `name-3`, … not in `taken` (kept within NAME_MAX). */
export function freeName(name: string, taken: ReadonlySet<string>): string {
  if (!taken.has(name)) return name;
  for (let n = 2; ; n++) {
    const suffix = '-' + String(n);
    const candidate = name.slice(0, NAME_MAX - suffix.length).replace(/-+$/, '') + suffix;
    if (!taken.has(candidate)) return candidate;
  }
}

/** One problem from an `invalid_skill` message: its `"<field>: <message>"` line. */
export interface FieldProblem {
  field: string;
  message: string;
}

/** `invalid_skill`'s message, split by line and then at the first `": "`. */
export function splitProblems(message: string): FieldProblem[] {
  return message
    .split('\n')
    .filter((line) => line.trim() !== '')
    .map((line) => {
      const at = line.indexOf(': ');
      return at < 0 ? { field: '', message: line } : { field: line.slice(0, at), message: line.slice(at + 2) };
    });
}

/** The same frontmatter regex as talk2view.skills.FRONTMATTER_RE. */
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---\r?\n([\s\S]*)$/;
const KEY_LINE = /^(name|description):(?:[ \t]+(.*)|[ \t]*)$/;

/** A one-line YAML scalar as skill_format._unquote reads it; null for one it would not. */
function unquote(raw: string): string | null {
  const v = raw.trim();
  if (v.length >= 2 && v[0] === "'" && v[v.length - 1] === "'") return v.slice(1, -1).replace(/''/g, "'");
  if (v.length >= 2 && v[0] === '"' && v[v.length - 1] === '"') {
    const escapes: Record<string, string> = { n: '\n', t: '\t', '"': '"', '\\': '\\', '/': '/' };
    return v.slice(1, -1).replace(/\\([nt"\\/])/g, (_m, c: string) => escapes[c]!);
  }
  if (v.startsWith("'") || v.startsWith('"')) return null;
  if (v === '|' || v === '>' || /^[|>][-+]$/.test(v)) return null;
  // A plain scalar ends at a comment, as the SDK reads it.
  const comment = v.indexOf(' #');
  return comment < 0 ? v : v.slice(0, comment).trimEnd();
}

/** `value` as a double-quoted scalar the SDK's reader gives back exactly: only `\\`,
 * `"`, newline and tab are escaped (the escapes it knows), and every other control
 * character is dropped, since it has no escape the SDK would read back. */
export function quoted(value: string): string {
  const clean = value.replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '');
  return '"' + clean.replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/\n/g, '\\n').replace(/\t/g, '\\t') + '"';
}

export interface SkillFields {
  name: string;
  description: string;
  instructions: string;
}

/**
 * The form's fields from a SKILL.md, or null when the file holds more than the form can
 * show (another frontmatter key, a multi-line value, a comment): that opens in markdown
 * mode, so nothing in it is lost on save. Only the two keys, one line each.
 */
export function formFields(markdown: string): SkillFields | null {
  const match = FRONTMATTER.exec(markdown);
  if (!match) return null;
  const fields: Partial<SkillFields> = {};
  for (const line of match[1]!.split(/\r?\n/)) {
    const key = KEY_LINE.exec(line);
    if (!key) return null;
    const k = key[1] as 'name' | 'description';
    if (k in fields) return null;
    const value = unquote(key[2] ?? '');
    if (value === null) return null;
    fields[k] = value;
  }
  // The SDK writes the body with one trailing newline; the form shows it without.
  const body = match[2]!.replace(/\r?\n$/, '');
  return { name: fields.name ?? '', description: fields.description ?? '', instructions: body };
}

/** The SKILL.md a new skill's form would save, for "Edit as markdown". */
export function formMarkdown(name: string, description: string, instructions: string): string {
  return `---\nname: ${name}\ndescription: ${quoted(description)}\n---\n${instructions}`;
}

/**
 * The name a new skill will be saved under, worked out live from what was typed: its
 * slug, or the next free `-2`, `-3`… when one of the user's skills already has it. An
 * edit keeps its own name free (`replaces`), so renaming back to it is no clash.
 */
export function savedName(slug: string, taken: ReadonlySet<string>, replaces?: string): string {
  if (slug === replaces) return slug;
  const others = new Set(taken);
  if (replaces !== undefined) others.delete(replaces);
  return freeName(slug, others);
}

/**
 * `save_skill`'s finished line: "Saved skill <name>" (the name the SDK says it saved
 * under, else the call's own), "Not saved" when it was cancelled or refused, else null
 * (the generic line). The SDK's handler starts every result with one or the other.
 */
export function skillOutcome(args: unknown, result: unknown, isError: boolean | undefined): { saved: boolean; name: string } | null {
  const text = typeof result === 'string' ? result : '';
  if (isError || text.startsWith('Not saved')) return { saved: false, name: '' };
  if (!text.startsWith('Saved skill')) return null;
  const named = /^Saved skill (['"])(.*?)\1/.exec(text);
  const fromArgs = (args as { name?: unknown } | undefined)?.name;
  return { saved: true, name: named ? named[2]! : typeof fromArgs === 'string' ? fromArgs : '' };
}
