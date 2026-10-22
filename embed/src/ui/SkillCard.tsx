/**
 * The `save_skill` card: what the assistant proposes as a skill, in plain words,
 * with Save / Edit / Cancel. No "Always allow": a skill is standing instructions, so every
 * save is the end-user's own click (the SDK refuses "always" for this tool as well, so the
 * card never sends it). Edit turns the card, in place, into the same fields as the Skills
 * view's form (SkillForm.tsx: `NameField`, `DescriptionField`, `InstructionsField`), with
 * the same live checks; the edited fields go back as `updatedInput`. Cancel is a denial,
 * with an optional reason for the assistant.
 *
 * How it answers (the latch, busy, the WATCH_MS timeout, answering only the approval the
 * server holds) and where it sits are `ApprovalCard`'s own, shared through
 * `useApprovalAnswer` and `useCardPlacement` (ToolCall.tsx). Every string the assistant
 * wrote reaches the DOM through `displayText`.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { Sparkles } from 'lucide-react';
import type { ToolCallMessagePartProps } from '@assistant-ui/react';
import { useModel } from './context';
import { displayText } from './displayText';
import { DescriptionField, InstructionsField, NameField } from './SkillForm';
import { descriptionProblem, nameProblem, savedName, suggestName, type FieldProblem } from './skillRules';
import { useApprovalAnswer, useCardPlacement } from './ToolCall';

type Args = { name?: unknown; description?: unknown; instructions?: unknown; replaces?: unknown };
const text = (v: unknown) => (typeof v === 'string' ? v : '');

export function SkillCard({ args, approval }: ToolCallMessagePartProps) {
  const a = (args ?? {}) as Args;
  const proposed = { name: text(a.name), description: text(a.description), instructions: text(a.instructions) };
  const replaces = text(a.replaces) || undefined;
  const { state } = useModel();
  const { busy, decide } = useApprovalAnswer(approval);
  const [editing, setEditing] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [reason, setReason] = useState('');
  const [name, setName] = useState(proposed.name);
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState(proposed.description);
  const [instructions, setInstructions] = useState(proposed.instructions);
  const [showAll, setShowAll] = useState(false);
  const titleId = useId();
  const reasonId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const cancelBtnRef = useRef<HTMLButtonElement>(null);
  const wasCancelling = useRef(false);

  useCardPlacement(rootRef);

  // "Back" out of the reason view: focus returns to the Cancel that led there.
  useEffect(() => {
    if (wasCancelling.current && !cancelling) cancelBtnRef.current?.focus();
    wasCancelling.current = cancelling;
  }, [cancelling]);

  // The name as the Skills view's form works it out: a new skill's slug of what was
  // typed, an update's own name as it is until changed, then the next free name when one
  // of the end-user's skills already has it.
  const mine = state.skills.local.filter((s) => s.origin === 'user');
  const taken = new Set(mine.map((s) => s.name));
  // The skill `replaces` names, as the SDK finds it (save_doc): by its on-disk key, else
  // by its name. Only one of the end-user's own skills can be replaced; anything else --
  // a bundled or app skill, a typo -- the SDK refuses.
  const replaced = replaces === undefined ? undefined : (mine.find((s) => s.key === replaces) ?? mine.find((s) => s.name === replaces));
  const typed = replaces !== undefined && !nameTouched ? name : suggestName(name);
  const slug = savedName(typed, taken, replaced?.name ?? replaces);
  const shownName = editing ? slug : proposed.name;

  const overridesApp =
    state.skills.local.some((s) => s.name === shownName && s.origin !== 'user' && !s.overridden) ||
    (state.skills.platform ?? []).some((p) => p.name === shownName);
  // The SDK refuses a new skill whose name one of the end-user's
  // own skills already has (no `replaces`); say so before Save, not only after. In Edit
  // mode the name moves to the next free one instead (NameField's clash line).
  const ownClash = !editing && !replaces && taken.has(proposed.name);
  const replacesNothing = replaces !== undefined && replaced === undefined;
  const note =
    replacesNothing
      ? `This asks to replace ${replaces}, which isn't one of your skills; it won't be saved.`
      : replaced !== undefined && replaced.name !== shownName
        ? `Renames your skill ${replaced.name} → ${shownName}`
        : replaced !== undefined
          ? `Updates your skill ${replaced.name}`
          : ownClash
            ? `You already have a skill named ${proposed.name}. Edit to rename it, or it won't be saved.`
            : overridesApp
              ? `Replaces the app's ${shownName} for you`
              : null;

  // An empty name blocks Save, and says why.
  const nameEmpty: FieldProblem[] = editing && !name.trim() ? [{ field: '', message: nameProblem('')! }] : [];
  const descriptionEmpty: FieldProblem[] = editing && !description.trim() ? [{ field: '', message: descriptionProblem('')! }] : [];
  const instructionsEmpty: FieldProblem[] =
    editing && !instructions.trim() ? [{ field: '', message: 'Say what the assistant should do.' }] : [];
  const blocked =
    editing && (nameEmpty.length > 0 || nameProblem(slug) !== null || descriptionProblem(description) !== null || instructionsEmpty.length > 0);

  const save = () => {
    if (blocked) return;
    decide(
      'once',
      editing ? { updatedInput: { name: slug, description, instructions, ...(replaces ? { replaces } : {}) } } : {},
    );
  };

  // Folded to six lines by height (CSS), so a single very long line folds too. "Show
  // all" appears whenever the fold actually hides something (ToolCall.tsx's ApprovalPrompt
  // measures the same way).
  const bodyRef = useRef<HTMLPreElement>(null);
  const [folds, setFolds] = useState(false);
  useEffect(() => {
    const el = bodyRef.current;
    if (el && !showAll) setFolds(el.scrollHeight > el.clientHeight + 1);
  }, [proposed.instructions, showAll, editing]);

  return (
    <div ref={rootRef} className="t2v-approval t2v-skill-card" data-state="waiting" role="group" aria-labelledby={titleId}>
      <div className="t2v-approval-head">
        <Sparkles className="t2v-approval-icon" aria-hidden="true" size={16} />
        <div className="t2v-approval-text">
          <p className="t2v-approval-title t2v-skill-card-title" id={titleId}>
            Save skill?
          </p>
          {!editing ? <code className="t2v-skill-card-name">{displayText(proposed.name)}</code> : null}
        </div>
      </div>

      {editing ? (
        <div className="t2v-skill-form t2v-skill-card-form">
          <NameField
            value={name}
            slug={slug}
            clash={slug !== typed}
            problems={nameEmpty}
            onChange={(value) => {
              setName(value);
              setNameTouched(true);
            }}
          />
          <DescriptionField value={description} onChange={setDescription} problems={descriptionEmpty} />
          <InstructionsField value={instructions} onChange={setInstructions} problems={instructionsEmpty} rows={6} />
        </div>
      ) : (
        <>
          <p className="t2v-skill-card-desc">{displayText(proposed.description)}</p>
          {/* Plain text, never markdown: markdown drops a link reference
              definition and hides a link's title and URL behind a hover, and every word
              of a standing instruction must be visible before Save. */}
          <pre ref={bodyRef} className={showAll ? 't2v-skill-card-body' : 't2v-skill-card-body t2v-skill-card-folded'}>
            {displayText(proposed.instructions)}
          </pre>
          {folds || showAll ? (
            <button type="button" className="t2v-link t2v-small t2v-skill-card-more" aria-expanded={showAll} onClick={() => setShowAll(!showAll)}>
              {showAll ? 'Show less' : 'Show all'}
            </button>
          ) : null}
        </>
      )}

      {note ? (
        <p className={'t2v-hint t2v-hint-bounded' + (replacesNothing ? ' t2v-hint-error t2v-skill-card-warning' : '')}>
          {displayText(note)}
        </p>
      ) : null}

      {cancelling ? (
        <div className="t2v-field">
          <label htmlFor={reasonId}>Reason (optional, sent to the assistant)</label>
          <textarea id={reasonId} className="t2v-textarea" rows={2} value={reason} autoFocus onChange={(e) => setReason(e.target.value)} />
        </div>
      ) : null}

      <div className="t2v-actions">
        {cancelling ? (
          <>
            <button type="button" className="t2v-btn t2v-btn-danger" disabled={busy} onClick={() => decide('deny', { feedback: reason })}>
              Confirm cancel
            </button>
            <button type="button" className="t2v-btn t2v-btn-ghost" disabled={busy} onClick={() => setCancelling(false)}>
              Back
            </button>
          </>
        ) : (
          <>
            <button type="button" className="t2v-btn t2v-btn-primary" disabled={busy || blocked} onClick={save}>
              Save
            </button>
            {!editing ? (
              <button type="button" className="t2v-btn t2v-btn-outline" disabled={busy} onClick={() => setEditing(true)}>
                Edit
              </button>
            ) : null}
            <button type="button" className="t2v-btn t2v-btn-ghost" ref={cancelBtnRef} disabled={busy} onClick={() => setCancelling(true)}>
              Cancel
            </button>
          </>
        )}
      </div>
    </div>
  );
}
