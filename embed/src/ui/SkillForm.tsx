/**
 * The New / Edit skill form and its markdown editor, shown in place of the
 * Skills view's list. Split out of Skills.tsx to keep that file to the list.
 *
 * Checks run live (skillRules.ts, a port of talk2view.skill_format's rules), but the SDK
 * is the authority: `skillSave` re-validates, and its `invalid_skill` problems are shown
 * under their fields (or under the editor, in markdown mode). Every string that came from
 * a file or the host — a problem, a skill name — reaches the DOM through `displayText`.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { resultOf, WATCH_MS } from '../store';
import { failureText } from '../selectors';
import type { Command, CommandResult } from '../wire';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import {
  DESCRIPTION_MAX,
  descriptionProblem,
  firstPerson,
  formFields,
  formMarkdown,
  INSTRUCTIONS_SOFT_MAX_LINES,
  nameProblem,
  savedName,
  splitProblems,
  suggestName,
  type FieldProblem,
  type SkillFields,
} from './skillRules';

/**
 * Send a command and hear its outcome once: `onResult` gets the result (or HTTP refusal)
 * when it lands, or null past WATCH_MS when none came (the caller says it took too long).
 * The skill commands whose outcome a view shows are QUIET in store.ts, so the caller is
 * the only one who reports them.
 */
export function useCommandResult(): (command: Command, onResult: (result: CommandResult | null) => void) => void {
  const store = useStore();
  const model = useModel();
  const waiting = useRef(new Map<string, { onResult: (result: CommandResult | null) => void; timer: number }>());
  useEffect(() => {
    for (const [id, wait] of waiting.current) {
      const result = resultOf(model, id);
      if (!result) continue;
      waiting.current.delete(id);
      window.clearTimeout(wait.timer);
      wait.onResult(result);
    }
  }, [model]);
  useEffect(() => {
    const all = waiting.current;
    return () => {
      all.forEach((wait) => window.clearTimeout(wait.timer));
      all.clear();
    };
  }, []);
  return useCallback(
    (command, onResult) => {
      const id = store.command(command);
      const timer = window.setTimeout(() => {
        if (waiting.current.delete(id)) onResult(null);
      }, WATCH_MS);
      waiting.current.set(id, { onResult, timer });
    },
    [store],
  );
}

/** What the form opens with: fields, or the whole file when it holds more than they can. */
export type SkillFormInitial = { kind: 'form'; fields: SkillFields } | { kind: 'markdown'; text: string };

const EMPTY: SkillFields = { name: '', description: '', instructions: '' };

export function Problems({ items }: { items: FieldProblem[] }) {
  return (
    <>
      {items.map((p, i) => (
        <p key={i} className="t2v-field-error">
          {displayText(p.field ? `${p.field}: ${p.message}` : p.message)}
        </p>
      ))}
    </>
  );
}

/**
 * "Description" with the TS SDK's hint, its counter, the first-person hint, the live
 * check and the SDK's own problems. Shared by the form, by an import that stopped for a
 * missing description, and by the chat's `save_skill` card in Edit mode.
 */
export function DescriptionField({
  value,
  onChange,
  problems,
}: {
  value: string;
  onChange: (value: string) => void;
  problems: FieldProblem[];
}) {
  const id = useId();
  const hint = useId();
  const counter = useId();
  const issue = value ? descriptionProblem(value) : null;
  return (
    <div className="t2v-field">
      <label htmlFor={id}>Description</label>
      <textarea
        id={id}
        className={'t2v-textarea t2v-skill-input' + (issue || problems.length ? ' t2v-invalid' : '')}
        rows={3}
        value={value}
        aria-describedby={`${hint} ${counter}`}
        placeholder="Reports segment volumes in millilitres, rounded to one decimal. Use when measuring or reporting volumes."
        onChange={(e) => onChange(e.target.value)}
      />
      <div className="t2v-skill-field-foot">
        <p id={hint} className="t2v-hint">
          One line. This is what the assistant reads when deciding whether to load it.
        </p>
        <span id={counter} className="t2v-skill-counter">
          {value.length} / {DESCRIPTION_MAX}
        </span>
      </div>
      {firstPerson(value) ? (
        <p className="t2v-hint t2v-hint-person">Write it as “Reports … Use when …”, not “I …” or “You …”.</p>
      ) : null}
      {issue ? <p className="t2v-hint t2v-hint-error">{issue}</p> : null}
      <Problems items={problems} />
    </div>
  );
}

/**
 * "Name" with the TS SDK's hint -- or, when the name it will be saved under (`slug`) is
 * not what was typed, "Saved as <slug>", and when that is because one of the end-user's
 * skills has the name, the clash line instead -- the live check and the SDK's own
 * problems. Shared by the form and by the chat's `save_skill` card in Edit mode.
 */
export function NameField({
  value,
  onChange,
  slug,
  clash,
  problems,
}: {
  value: string;
  onChange: (value: string) => void;
  slug: string;
  clash: boolean;
  problems: FieldProblem[];
}) {
  const id = useId();
  const hint = useId();
  const issue = value.trim() ? nameProblem(slug) : null;
  return (
    <div className="t2v-field">
      <label htmlFor={id}>Name</label>
      <input
        id={id}
        className={'t2v-text t2v-skill-input' + (issue || problems.length ? ' t2v-invalid' : '')}
        value={value}
        autoComplete="off"
        spellCheck={false}
        placeholder="reporting-style"
        aria-describedby={hint}
        onChange={(e) => onChange(e.target.value)}
      />
      {clash ? (
        <p id={hint} className="t2v-hint t2v-skill-clash">
          {displayText(`A skill with that name exists; this one will be saved as ${slug}.`)}
        </p>
      ) : value.trim() && slug !== value ? (
        <p id={hint} className="t2v-hint t2v-skill-slug">
          {displayText(`Saved as ${slug}`)}
        </p>
      ) : (
        <p id={hint} className="t2v-hint">
          How the assistant refers to it. Short and specific.
        </p>
      )}
      {issue ? <p className="t2v-hint t2v-hint-error">{issue}</p> : null}
      <Problems items={problems} />
    </div>
  );
}

/** "Content": the skill's markdown body, with the TS SDK's hint and the long-skill hint.
 * Shared like `NameField`. */
export function InstructionsField({
  value,
  onChange,
  problems,
  rows = 8,
}: {
  value: string;
  onChange: (value: string) => void;
  problems: FieldProblem[];
  rows?: number;
}) {
  const id = useId();
  const hint = useId();
  const lines = value ? value.split('\n').length : 0;
  return (
    <div className="t2v-field">
      <label htmlFor={id}>Content</label>
      <textarea
        id={id}
        className={'t2v-textarea t2v-skill-content' + (problems.length ? ' t2v-invalid' : '')}
        rows={rows}
        value={value}
        aria-describedby={hint}
        placeholder={'# Reporting style\n\nAlways lead with the finding, then the measurement.'}
        onChange={(e) => onChange(e.target.value)}
      />
      <p id={hint} className="t2v-hint">
        Markdown. Everything here is sent with your chats, so keep it to what the assistant needs.
      </p>
      {lines > INSTRUCTIONS_SOFT_MAX_LINES ? (
        <p className="t2v-hint">Long skills cost the assistant context; consider trimming.</p>
      ) : null}
      <Problems items={problems} />
    </div>
  );
}

export function SkillForm({
  initial,
  replaces,
  taken,
  onDone,
}: {
  initial: SkillFormInitial;
  /** The skill being edited: its on-disk `key` (what `skillSave.replaces` sends) and its
   * name before this save. Absent for a new one. */
  replaces?: { key: string; name: string };
  /** The names already in the user's store: a clash's free name avoids them. */
  taken: ReadonlySet<string>;
  onDone: () => void;
}) {
  const store = useStore();
  const run = useCommandResult();
  const start = initial.kind === 'form' ? initial.fields : EMPTY;
  const [kind, setKind] = useState<'form' | 'markdown'>(initial.kind);
  const [name, setName] = useState(start.name);
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState(start.description);
  const [instructions, setInstructions] = useState(start.instructions);
  const [markdown, setMarkdown] = useState(initial.kind === 'markdown' ? initial.text : '');
  const [problems, setProblems] = useState<FieldProblem[]>([]);
  const [stuck, setStuck] = useState(false);
  const [busy, setBusy] = useState(false);

  // An existing skill's name stays exactly as it is until the end-user changes it.
  const typed = replaces !== undefined && !nameTouched ? name : suggestName(name);
  // A clash is shown live: the slug is the free name Save will use.
  const slug = savedName(typed, taken, replaces?.name);
  const clash = slug !== typed;

  const save = () => {
    const command: Command =
      kind === 'markdown'
        ? { type: 'skillSave', markdown, replaces: replaces?.key }
        : { type: 'skillSave', name: slug, description, instructions, replaces: replaces?.key };
    setBusy(true);
    run(command, (result) => {
      setBusy(false);
      if (result === null) {
        store.showNotice('That took too long. Try again.');
        return;
      }
      if (result.ok) {
        onDone();
        return;
      }
      const error = result.error;
      if (error?.type === 'invalid_skill') {
        setProblems(splitProblems(error.message));
      } else if (error?.type === 'skill_exists') {
        // Markdown mode (its name is in the text), or a skill made elsewhere meanwhile.
        setProblems([{ field: 'name', message: `A skill named ${error.message} already exists.` }]);
      } else {
        store.showNotice(failureText(result));
      }
    });
  };

  const toMarkdown = () => {
    setMarkdown(formMarkdown(slug, description, instructions));
    setProblems([]);
    setKind('markdown');
  };
  const toForm = () => {
    const fields = formFields(markdown);
    if (!fields) {
      setStuck(true);
      return;
    }
    setName(fields.name);
    setNameTouched(replaces !== undefined && fields.name !== replaces.name);
    setDescription(fields.description);
    setInstructions(fields.instructions);
    setProblems([]);
    setStuck(false);
    setKind('form');
  };

  const on = (field: string) => problems.filter((p) => p.field === field);
  const formFieldsShown = ['name', 'description', 'body'];
  const elsewhere = kind === 'markdown' ? problems : problems.filter((p) => !formFieldsShown.includes(p.field));

  return (
    <form
      className="t2v-skill-form"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        if (!busy) save();
      }}
    >
      {kind === 'form' ? (
        <>
          <NameField
            value={name}
            slug={slug}
            clash={clash}
            problems={on('name')}
            onChange={(value) => {
              setName(value);
              setNameTouched(true);
            }}
          />
          <DescriptionField value={description} onChange={setDescription} problems={on('description')} />
          <InstructionsField value={instructions} onChange={setInstructions} problems={on('body')} />
        </>
      ) : (
        <div className="t2v-field">
          <textarea
            aria-label="SKILL.md"
            className={'t2v-textarea t2v-mono t2v-skill-markdown' + (problems.length ? ' t2v-invalid' : '')}
            rows={16}
            spellCheck={false}
            value={markdown}
            onChange={(e) => {
              setMarkdown(e.target.value);
              setStuck(false);
            }}
          />
          {stuck ? (
            <p className="t2v-hint">This file has more than a name, a description and instructions, so it stays in markdown.</p>
          ) : null}
        </div>
      )}
      <Problems items={elsewhere} />
      <div className="t2v-skill-form-actions">
        <button type="submit" className="t2v-btn t2v-btn-primary" disabled={busy}>
          Save skill
        </button>
        <button type="button" className="t2v-btn t2v-btn-outline" onClick={onDone}>
          Cancel
        </button>
        {kind === 'form' ? (
          <button type="button" className="t2v-link t2v-small t2v-skill-form-mode" onClick={toMarkdown}>
            Edit as markdown
          </button>
        ) : (
          <button type="button" className="t2v-link t2v-small t2v-skill-form-mode" onClick={toForm}>
            Edit as form
          </button>
        )}
      </div>
    </form>
  );
}
