import { describe, expect, it } from 'vitest';
import {
  descriptionProblem,
  skillOutcome,
  firstPerson,
  formFields,
  formMarkdown,
  freeName,
  nameProblem,
  ODD_LINE_BREAK_PROBLEM,
  savedName,
  splitProblems,
  suggestName,
} from '../src/ui/skillRules';

describe('skillRules (mirrors talk2view.skill_format)', () => {
  it.each([
    ['Report volumes in mL', 'report-volumes-in-ml'],
    ['  CT -- window__presets!! ', 'ct-window-presets'],
    ['Claude helper', 'helper'],
    ['Ärzte Übersicht', 'arzte-ubersicht'],
    ['', 'skill'],
    ['x'.repeat(80), 'x'.repeat(64)],
  ])('suggestName(%j) = %j', (text, name) => expect(suggestName(text)).toBe(name));

  it.each(['PDF-Processing', '-pdf', 'pdf--processing', '', 'x'.repeat(65), 'claude-tools'])('rejects %j', (n) =>
    expect(nameProblem(n)).not.toBeNull());
  it.each(['pdf-processing', 'a', 'x'.repeat(64)])('accepts %j', (n) => expect(nameProblem(n)).toBeNull());

  it('description rules', () => {
    expect(descriptionProblem('')).not.toBeNull();
    expect(descriptionProblem('x'.repeat(1025))).not.toBeNull();
    expect(descriptionProblem('Has <b>tags</b>')).not.toBeNull();
    expect(descriptionProblem('Reports mL. Use when measuring.')).toBeNull();
  });

  it('flags first and second person openings', () => {
    expect(firstPerson('I can help with PDFs')).toBe(true);
    expect(firstPerson('You can use this for PDFs')).toBe(true);
    expect(firstPerson('Processes PDFs. Use when…')).toBe(false);
  });
});

describe('the form <-> SKILL.md helpers', () => {
  it('freeName tries -2, -3 against the names taken', () => {
    expect(freeName('a-b', new Set())).toBe('a-b');
    expect(freeName('a-b', new Set(['a-b']))).toBe('a-b-2');
    expect(freeName('a-b', new Set(['a-b', 'a-b-2']))).toBe('a-b-3');
    expect(freeName('x'.repeat(64), new Set(['x'.repeat(64)]))).toBe('x'.repeat(62) + '-2');
  });

  it('splitProblems splits invalid_skill by line, then at the first ": "', () => {
    expect(splitProblems('name: Lowercase: only\ndescription: Required')).toEqual([
      { field: 'name', message: 'Lowercase: only' },
      { field: 'description', message: 'Required' },
    ]);
  });

  it('formFields reads the two keys only; anything richer is null (markdown mode)', () => {
    expect(formFields('---\nname: a-b\ndescription: "Says \\"hi\\". Use it."\n---\nBody\n')).toEqual({
      name: 'a-b',
      description: 'Says "hi". Use it.',
      instructions: 'Body',
    });
    expect(formFields("---\nname: a-b\ndescription: 'It''s. Use it.'\n---\nB\n")?.description).toBe("It's. Use it.");
    expect(formFields('---\nname: a-b\ndescription: D.\nlicense: MIT\n---\nB\n')).toBeNull();
    expect(formFields('---\nname: a-b\ndescription: |\n  two\n  lines\n---\nB\n')).toBeNull();
    expect(formFields('no frontmatter')).toBeNull();
  });

  it('formMarkdown round-trips through formFields', () => {
    const md = formMarkdown('a-b', 'Has: "quotes". Use it.', 'Do X.');
    expect(formFields(md)).toEqual({ name: 'a-b', description: 'Has: "quotes". Use it.', instructions: 'Do X.' });
  });
});

// The page writes and reads frontmatter the way the SDK's
// strict reader (talk2view.skill_format._unquote / _scalar) does.
describe('frontmatter quoting matches the SDK reader', () => {
  it('formMarkdown escapes only \\\\, ", \\n and \\t, and drops other control characters', () => {
    const md = formMarkdown('a-b', 'Tab\there, "q", back\\slash, cr\r, bell\u0007. Use it.', 'Body');
    expect(md).toBe('---\nname: a-b\ndescription: "Tab\\there, \\"q\\", back\\\\slash, cr, bell. Use it."\n---\nBody');
    expect(md).not.toMatch(/\\r|\\u/);
    expect(formFields(md)?.description).toBe('Tab\there, "q", back\\slash, cr, bell. Use it.');
  });

  it('formFields cuts a plain scalar at " #", as the SDK does', () => {
    expect(formFields('---\nname: a-b\ndescription: Uses #1 preset\n---\nB\n')?.description).toBe('Uses');
    expect(formFields('---\nname: a-b\ndescription: "Uses #1 preset"\n---\nB\n')?.description).toBe('Uses #1 preset');
    expect(formFields('---\nname: a-b\ndescription: C#-style. Use it.\n---\nB\n')?.description).toBe('C#-style. Use it.');
  });
});

describe('savedName (the clash shown live)', () => {
  it('is the slug, or the next free name when it is taken', () => {
    expect(savedName('a-b', new Set())).toBe('a-b');
    expect(savedName('a-b', new Set(['a-b']))).toBe('a-b-2');
  });
  it("an edit's own name is never a clash", () => {
    expect(savedName('a-b', new Set(['a-b']), 'a-b')).toBe('a-b');
    expect(savedName('c-d', new Set(['a-b', 'c-d']), 'a-b')).toBe('c-d-2');
  });
});

describe('skillOutcome (the save_skill settled line)', () => {
  it('takes the name the SDK says it saved under', () => {
    expect(skillOutcome({ name: 'asked' }, "Saved skill 'saved-as'. It applies from the user's next message.", false)).toEqual({
      saved: true,
      name: 'saved-as',
    });
  });
  it("falls back to the call's own name when the sentence names none", () => {
    expect(skillOutcome({ name: 'asked' }, 'Saved skill.', false)).toEqual({ saved: true, name: 'asked' });
  });
  it('an empty result (an older server records none) is no outcome: the generic line', () => {
    expect(skillOutcome({ name: 'asked' }, '', false)).toBeNull();
    expect(skillOutcome({ name: 'asked' }, undefined, false)).toBeNull();
  });
  it('a refusal or a denial is not saved', () => {
    expect(skillOutcome({ name: 'a' }, "Not saved. The user already has a skill named 'a'.", false)).toEqual({ saved: false, name: '' });
    expect(skillOutcome({ name: 'a' }, 'Denied by the user', true)).toEqual({ saved: false, name: '' });
  });
});

// The characters Python's str.splitlines() breaks at, other
// than "\n", would split a name or description when the SDK reads the file back.
describe('odd line breaks', () => {
  const breaks = ['\r', '\u000b', '\u000c', '\u001c', '\u001d', '\u001e', '\u0085', ' ', ' '];
  it.each(breaks)('refuses %j in a description, saying why', (brk) => {
    expect(descriptionProblem(`Does a${brk}b. Use when c.`)).toBe(ODD_LINE_BREAK_PROBLEM);
  });
  it.each(breaks)('refuses %j in a name', (brk) => {
    expect(nameProblem(`ok${brk}name`)).toBe(ODD_LINE_BREAK_PROBLEM);
  });
  it('says what to do', () => {
    expect(ODD_LINE_BREAK_PROBLEM).toMatch(/line-break character that isn't a normal newline.*remove it/);
  });
  it('still allows a normal newline and a tab in a description', () => {
    expect(descriptionProblem('Does a\nb.\tUse when c.')).toBeNull();
  });
});
