/**
 * The Skills view: the end-user's own skills, then the app's. Skills are local
 * (a folder the host names), so guests get the whole view, and every visit asks
 * `skillsList` -- guests too, so a skill dropped into the folder is picked up; the SDK
 * reads the app's catalogue only with a session, and answers a guest's listing ok. Its own
 * `<Notice />` (Thread.tsx) is what Settings' skills section used for the same reason:
 * the thread pane, which carries the page's other Notice, is hidden behind this view, so
 * a refused toggle needs its own copy to ever be seen.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { FolderOpen, Pencil, Plus, Trash2, Upload } from 'lucide-react';
import { resultOf } from '../store';
import { failureText, skillsSection } from '../selectors';
import type { LocalSkill, PlatformSkill } from '../wire';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import { usePendingMap } from './Settings';
import { DescriptionField, Problems, SkillForm, useCommandResult, type SkillFormInitial } from './SkillForm';
import { formFields, splitProblems, type FieldProblem } from './skillRules';
import { Notice } from './Thread';

/**
 * One skill, as a bordered card (the TS SDK's Skills view): a checkbox when it can be
 * turned on or off, the name and its description on one line each (truncated; the
 * checkbox is described by the whole description, and the title shows it on hover), the
 * problems the SDK found with it, and on the right either its actions (always visible,
 * as TS's) or a muted tag for a read-only row. Every string from a skill reaches the DOM
 * through `displayText` -- a partner's or a host folder's skill text is untrusted.
 */
function Row({
  skill,
  checked,
  pending,
  onToggle,
  actions,
  tag,
}: {
  skill: LocalSkill | PlatformSkill;
  checked?: boolean;
  pending?: boolean;
  onToggle?: (enabled: boolean) => void;
  actions?: React.ReactNode;
  tag?: string;
}) {
  const descId = useId();
  const problems = 'problems' in skill ? skill.problems ?? [] : [];
  const name = displayText(skill.name);
  const description = skill.description ? displayText(skill.description) : '';
  const text = (
    <span className="t2v-skill-text">
      <span className="t2v-skill-name" title={name}>
        {name}
      </span>
      {description ? (
        <span id={descId} className="t2v-skill-desc" title={description}>
          {description}
        </span>
      ) : null}
      {problems.map((p, i) => (
        <span key={i} className="t2v-skill-problem">
          {displayText(`${p.field}: ${p.message}`)}
        </span>
      ))}
      {'hasExtraFiles' in skill && skill.hasExtraFiles ? (
        <span className="t2v-skill-note">Only the instructions are used; the extra files aren't supported yet.</span>
      ) : null}
    </span>
  );
  return (
    <div className="t2v-skill t2v-skill-row">
      {onToggle ? (
        <label className="t2v-skill-main">
          <input
            type="checkbox"
            checked={checked}
            aria-label={name}
            aria-describedby={description ? descId : undefined}
            aria-busy={pending}
            onChange={(e) => onToggle(e.target.checked)}
          />
          {text}
        </label>
      ) : (
        <span className="t2v-skill-main">{text}</span>
      )}
      {actions ? <span className="t2v-skill-actions">{actions}</span> : null}
      {tag ? <span className="t2v-skill-note t2v-skill-tag">{tag}</span> : null}
    </div>
  );
}

type Mode =
  | { kind: 'list' }
  | { kind: 'form'; initial: SkillFormInitial; replaces?: { key: string; name: string }; key: number };

/** What the SDK addresses a local skill by: its on-disk key (an older server sends none). */
const keyOf = (s: LocalSkill) => s.key ?? s.name;

export function SkillsView({ onAccount: _onAccount }: { onAccount: () => void }) {
  const store = useStore();
  const model = useModel();
  const { state } = model;
  const { skills, auth } = state;
  const session = auth.isAuthenticated;

  // Every visit, guests too: the SDK rescans the skill
  // folders first, so a skill dropped in while signed out is listed; it reads the app's
  // catalogue only with a session and answers a guest's listing ok, so there is no stray
  // "Session expired". Sent again when the session changes. Its id is kept:
  // the view says skills are off only once this listing has answered, and only with a
  // session (`skillsSection`) -- a guest's listing proves nothing about the app's skills,
  // so a guest never sees "off".
  const [listId, setListId] = useState<string | null>(null);
  useEffect(() => {
    setListId(store.command({ type: 'skillsList' }));
  }, [store, session]);
  const listed = resultOf(model, listId)?.ok === true;
  const { off, awaitingRegister } = skillsSection(skills, listed && session);

  // Every local skill's own toggle, the same optimistic/settled pattern Settings used:
  // shown at once, `aria-busy` until its own command's result —
  // ok or refused — is known, and rolled back to the server's value past WATCH_MS.
  const [toggled, setToggled] = usePendingMap<boolean>(model, store);
  const toggle = (name: string, enabled: boolean) => {
    const id = store.command({ type: enabled ? 'skillEnable' : 'skillDisable', name });
    setToggled(name, enabled, id);
  };

  const run = useCommandResult();
  const [mode, setMode] = useState<Mode>({ kind: 'list' });
  const opened = useRef(0);
  const writable = skills.store?.writable === true;
  // An import's `skill_exists` (the name it clashed on) or `invalid_skill` (its problems).
  const [importClash, setImportClash] = useState<string | null>(null);
  const [importProblems, setImportProblems] = useState<FieldProblem[]>([]);
  // An import that stopped for a missing description: what is typed so far,
  // and the SDK's problems with the last one sent; null when none is being asked for.
  const [importDescription, setImportDescription] = useState<string | null>(null);
  const [importDescriptionProblems, setImportDescriptionProblems] = useState<FieldProblem[]>([]);

  // Every New, Edit, or return to the list moves this on, so a `skillRead` answering an
  // Edit the end-user has already moved on from is dropped.
  const intent = useRef(0);
  const toList = () => {
    intent.current++;
    setMode({ kind: 'list' });
  };
  const openNew = () => {
    intent.current++;
    setMode({ kind: 'form', initial: { kind: 'form', fields: { name: '', description: '', instructions: '' } }, key: ++opened.current });
  };
  /** Edit (and Customize's copy): the file read back, in the form when it fits there.
   * `key` is what the SDK reads and later replaces; `name` is the one shown. */
  const openEdit = (key: string, name: string) => {
    const mine = ++intent.current;
    run({ type: 'skillRead', name: key }, (result) => {
      if (mine !== intent.current) return;
      if (result === null) return store.showNotice('That took too long. Try again.');
      if (!result.ok) return store.showNotice(failureText(result));
      const text = typeof result.data?.markdown === 'string' ? result.data.markdown : '';
      const fields = formFields(text);
      const initial: SkillFormInitial = fields ? { kind: 'form', fields } : { kind: 'markdown', text };
      setMode({ kind: 'form', initial, replaces: { key, name }, key: ++opened.current });
    });
  };
  const customize = (skill: LocalSkill) =>
    run({ type: 'skillCustomize', name: keyOf(skill) }, (result) => {
      if (result === null) return store.showNotice('That took too long. Try again.');
      // A refusal is already the notice (skillCustomize is not QUIET in store.ts). The
      // copy's name is also its key: the SDK writes it into a folder of that name.
      if (result.ok && typeof result.data?.name === 'string') openEdit(result.data.name, result.data.name);
    });
  /** No confirm step: the row goes, and the notice offers Undo for 6 s. */
  const remove = (skill: LocalSkill) => {
    const key = keyOf(skill);
    run({ type: 'skillDelete', name: key }, (result) => {
      if (!result?.ok) return; // a refusal is already the notice (skillDelete is not QUIET)
      store.showNotice(`Deleted ${skill.name}.`, {
        label: 'Undo',
        // A `skill_exists` here (the name taken again meanwhile) is the notice selectors.ts
        // words for skillRestore; skillRestore is not QUIET.
        onClick: () => void store.command({ type: 'skillRestore', name: key }),
      });
    });
  };
  const clearImport = () => {
    setImportClash(null);
    setImportProblems([]);
    setImportDescription(null);
    setImportDescriptionProblems([]);
  };
  /**
   * A fresh import (no answer), or an answer to the one waiting in the SDK: `resolve` for
   * a name clash, `description` for a file that had none. The SDK keeps the picked path
   * (and a description already given) while it waits; the page never names a path.
   */
  const importSkill = (answer: { resolve?: 'replace' | 'keepBoth'; description?: string } = {}) => {
    const asking = answer.description !== undefined;
    if (!asking) clearImport();
    run({ type: 'skillImport', ...answer }, (result) => {
      if (result === null) return store.showNotice('That took too long. Try again.');
      if (result.ok) return clearImport(); // imported (the list shows it), or the picker was cancelled
      const error = result.error;
      const problems = error?.type === 'invalid_skill' ? splitProblems(error.message) : [];
      if (problems.length && problems.every((p) => p.field === 'description')) {
        // Only the description is missing or wrong: ask for it here.
        setImportDescription((typed) => typed ?? '');
        setImportDescriptionProblems(asking ? problems : []);
        return;
      }
      clearImport();
      if (error?.type === 'skill_exists') setImportClash(error.message);
      else if (problems.length) setImportProblems(problems);
      else store.showNotice(failureText(result));
    });
  };
  const canImport = writable && state.capabilities.skillImport === true;
  // Opening the folder only needs one: a read-only store's folder
  // is still worth showing.
  const canOpenFolder = skills.store?.canOpenFolder === true;

  const yours = skills.local.filter((s) => s.origin === 'user');
  const appLocal = skills.local.filter((s) => s.origin !== 'user');
  const overridden = new Set(yours.map((s) => s.name));
  // The form's clash check: every other skill of yours (an edit keeps its own name free).
  const takenBesides = (editing?: string) => new Set(yours.map((s) => s.name).filter((n) => n !== editing));
  // The host names its app (`ChatOptions.app_name`); without one, a plain "App skills".
  const appName = state.options.appName;

  if (off) {
    return (
      <div className="t2v-view t2v-skills-view" role="region" aria-label="Skills">
        <Notice />
        <p className="t2v-hint">Skills are off for this app.</p>
      </div>
    );
  }
  if (mode.kind === 'form') {
    return (
      <div className="t2v-view t2v-skills-view" role="region" aria-label="Skills">
        <Notice />
        <SkillForm
          key={mode.key}
          initial={mode.initial}
          replaces={mode.replaces}
          taken={takenBesides(mode.replaces?.name)}
          onDone={toList}
        />
      </div>
    );
  }
  return (
    <div className="t2v-view t2v-skills-view" role="region" aria-label="Skills">
      <Notice />
      <p className="t2v-hint t2v-skills-intro">
        Markdown the assistant can read when it needs what you know. Kept on this device, sent with your chats.
      </p>
      {writable || canImport || canOpenFolder ? (
        <div className="t2v-skills-toolbar">
          {writable ? (
            <button type="button" className="t2v-btn t2v-btn-outline" onClick={openNew}>
              <Plus aria-hidden="true" size={14} />
              New skill
            </button>
          ) : null}
          {canImport ? (
            <button type="button" className="t2v-btn t2v-btn-outline" onClick={() => importSkill()}>
              <Upload aria-hidden="true" size={14} />
              Import .md
            </button>
          ) : null}
          {canOpenFolder ? (
            <button
              type="button"
              className="t2v-icon-btn t2v-skills-folder"
              aria-label="Open skills folder"
              title="Open skills folder"
              onClick={() => void store.command({ type: 'skillOpenFolder' })}
            >
              <FolderOpen aria-hidden="true" size={16} />
            </button>
          ) : null}
        </div>
      ) : null}
      {importClash !== null ? (
        <div className="t2v-skill-import-prompt" role="group" aria-label="Import">
          <p className="t2v-hint">{displayText(`A skill named ${importClash} exists.`)}</p>
          <span className="t2v-skill-form-buttons">
            <button type="button" className="t2v-btn t2v-btn-outline t2v-btn-sm" onClick={() => importSkill({ resolve: 'replace' })}>
              Replace
            </button>
            <button type="button" className="t2v-btn t2v-btn-outline t2v-btn-sm" onClick={() => importSkill({ resolve: 'keepBoth' })}>
              Keep both
            </button>
          </span>
        </div>
      ) : null}
      {importDescription !== null ? (
        <form
          className="t2v-skill-import-prompt"
          aria-label="Import"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            importSkill({ description: importDescription });
          }}
        >
          <p className="t2v-hint">This skill has no description. Add one to import it.</p>
          <DescriptionField value={importDescription} onChange={setImportDescription} problems={importDescriptionProblems} />
          <span className="t2v-skill-form-buttons">
            <button type="submit" className="t2v-btn t2v-btn-primary t2v-btn-sm">
              Import
            </button>
            <button type="button" className="t2v-btn t2v-btn-ghost t2v-btn-sm" onClick={clearImport}>
              Cancel
            </button>
          </span>
        </form>
      ) : null}
      {importProblems.length ? (
        <div className="t2v-skill-import-prompt">
          <Problems items={importProblems} />
          <p className="t2v-hint">Fix the file and import it again.</p>
        </div>
      ) : null}
      <section className="t2v-skills-group" aria-label="Your skills">
        {yours.length === 0 ? (
          <p className="t2v-skills-empty">
            No skills yet. Write one, import a markdown file, or ask the assistant to “save this as a skill”.
          </p>
        ) : (
          yours.map((s) => (
            <Row
              key={keyOf(s)}
              skill={s}
              checked={toggled[s.name]?.value ?? s.enabled}
              pending={s.name in toggled}
              onToggle={(on) => toggle(s.name, on)}
              actions={
                writable ? (
                  <>
                    <button
                      type="button"
                      className="t2v-icon-btn t2v-skill-action"
                      aria-label={`Edit ${displayText(s.name)}`}
                      title="Edit"
                      onClick={() => openEdit(keyOf(s), s.name)}
                    >
                      <Pencil aria-hidden="true" size={14} />
                    </button>
                    <button
                      type="button"
                      className="t2v-icon-btn t2v-skill-action t2v-skill-delete"
                      aria-label={`Delete ${displayText(s.name)}`}
                      title="Delete"
                      onClick={() => remove(s)}
                    >
                      <Trash2 aria-hidden="true" size={14} />
                    </button>
                  </>
                ) : undefined
              }
            />
          ))
        )}
        {awaitingRegister ? <p className="t2v-hint">Changes reach the assistant with your next message.</p> : null}
      </section>
      <section className="t2v-skills-group t2v-skills-app" aria-labelledby="t2v-app">
        <div className="t2v-skills-group-head">
          <h2 id="t2v-app" className="t2v-section-title">
            {appName ? displayText(`From ${appName}`) : 'App skills'}
          </h2>
        </div>
        {appLocal.map((s) =>
          s.origin === 'bundled' && s.overridden ? (
            // One of yours has its name: listed only, never toggled or customized again.
            <Row key={'replaced:' + keyOf(s)} skill={s} tag="Replaced by yours" />
          ) : s.origin === 'bundled' ? (
            <Row
              key={keyOf(s)}
              skill={s}
              checked={toggled[s.name]?.value ?? s.enabled}
              pending={s.name in toggled}
              onToggle={(on) => toggle(s.name, on)}
              actions={
                writable ? (
                  <button
                    type="button"
                    className="t2v-btn t2v-btn-ghost t2v-btn-sm"
                    aria-label={`Customize ${displayText(s.name)}`}
                    onClick={() => customize(s)}
                  >
                    Customize
                  </button>
                ) : undefined
              }
            />
          ) : (
            <Row key={s.name} skill={s} tag="Always on" />
          ),
        )}
        {skills.platform?.map((p) => (
          <Row key={p.name} skill={p} tag={overridden.has(p.name) ? 'Replaced by yours' : p.source === 'builtin' ? 'Built in' : 'Always on'} />
        ))}
        {session && listed && skills.platform === null ? <p className="t2v-hint">The app's other skills can't be listed yet.</p> : null}
      </section>
      {skills.needsSessionReset ? (
        <p className="t2v-hint">The assistant forgets a skill you turned off when this chat is next idle.</p>
      ) : null}
    </div>
  );
}
