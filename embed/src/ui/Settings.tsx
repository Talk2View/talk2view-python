/**
 * The Settings view: the AI model (with the partner's Default, TS chat/views/settings.tsx),
 * the text size, and the skills panel. Every choice is a command; what the page shows is
 * what the server's slices say, so a refused pick simply does not stick. Opening the view
 * asks for the skills list, but only once signed in (and again the moment sign-in
 * happens): a guest with no account at all gets a 401 from the engine for it, which the
 * store's own refusal handling would otherwise surface as a stray "Session expired"
 * notice. Signed out, the model picker and the skills section are hidden
 * outright, not merely disabled — text size is a local preference and stays available.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { defaultModelLabel, modelLabel, modelPick, skillsSection } from '../selectors';
import { resultOf, WATCH_MS, type PageModel, type PageStore } from '../store';
import type { FontSize, LocalSkill, PlatformSkill } from '../wire';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import { Notice } from './Thread';

const SIZES: Array<[FontSize, string]> = [
  ['small', 'Small'],
  ['medium', 'Medium'],
  ['large', 'Large'],
];

interface Pending<T> {
  value: T;
  id: string;
}

/**
 * One optimistic value: shown at once, until ITS OWN command's result is known — settled
 * on any result, ok or not, never on the slice merely happening to
 * agree, which a slow server can do by coincidence before either of two competing
 * commands has actually landed (a flicker). A newer pick always replaces an
 * older, unsettled one outright, so only the LATEST command's outcome is ever awaited.
 * A result that never comes at all — the queued command's op lost, an account change
 * mid-pick emptying `results` — is bounded by `WATCH_MS`, the same
 * window `Account.tsx` uses for its own busy state: past it, the control falls back to
 * the server's own value and the notice reads "That took too long. Try again."
 */
function usePending<T>(model: PageModel, store: PageStore): [Pending<T> | null, (value: T, id: string) => void] {
  const [pending, setPending] = useState<Pending<T> | null>(null);
  useEffect(() => {
    if (pending && resultOf(model, pending.id) !== null) setPending(null);
  }, [model, pending]);
  // Kept to ONLY `[pending, store]` — not `model` — so a change elsewhere in the page's
  // state does not restart this clock; it starts once, when `pending` is first set, and
  // is cancelled by the effect above the moment a result (or this timeout) clears it.
  useEffect(() => {
    if (!pending) return;
    const id = window.setTimeout(() => {
      setPending(null);
      store.showNotice('That took too long. Try again.');
    }, WATCH_MS);
    return () => window.clearTimeout(id);
  }, [pending, store]);
  return [pending, (value, id) => setPending({ value, id })];
}

/**
 * Same as `usePending`, keyed by name: every local skill's own toggle. The `WATCH_MS`
 * clock is kept in a ref, per key, and managed imperatively in `set()` rather than in a
 * reactive effect over the whole map — an effect keyed on `pending` (the map) would
 * restart EVERY key's timer the moment any ONE of them settles (the settle effect below
 * always produces a new map object), extending every sibling's window each time.
 */
function usePendingMap<T>(
  model: PageModel,
  store: PageStore,
): [Record<string, Pending<T>>, (key: string, value: T, id: string) => void] {
  const [pending, setPending] = useState<Record<string, Pending<T>>>({});
  // What the timeout below reads. A flag set inside a `setPending` updater is not
  // readable on the next line: React runs the updater then only when nothing else is
  // queued for this component, and otherwise defers it to the render, which skipped the
  // notice. The last render's map, kept here, is always current by the time a timer fires.
  const pendingRef = useRef(pending);
  pendingRef.current = pending;
  const timers = useRef<Record<string, number>>({});

  const clearTimer = (key: string) => {
    const t = timers.current[key];
    if (t !== undefined) {
      window.clearTimeout(t);
      delete timers.current[key];
    }
  };

  useEffect(() => {
    const settled = Object.keys(pending).filter((key) => resultOf(model, pending[key]!.id) !== null);
    if (settled.length === 0) return;
    settled.forEach(clearTimer);
    setPending((current) => {
      const next = { ...current };
      for (const key of settled) delete next[key];
      return next;
    });
  }, [model, pending]);

  // Every ref currently running, cleared when Settings itself unmounts.
  useEffect(() => () => Object.keys(timers.current).forEach(clearTimer), []);

  const set = (key: string, value: T, id: string) => {
    clearTimer(key);
    timers.current[key] = window.setTimeout(() => {
      delete timers.current[key];
      // `clearTimer` already keeps this from firing for a key that settled or was
      // superseded first, so `key` should always still be pending here — checked anyway
      // rather than trust that unreachable-today stays unreachable.
      if (!(key in pendingRef.current)) return;
      setPending((current) => {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      });
      store.showNotice('That took too long. Try again.');
    }, WATCH_MS);
    setPending((current) => ({ ...current, [key]: { value, id } }));
  };

  return [pending, set];
}

function Field({ title, help, children }: { title: string; help: ReactNode; children: (id: string) => ReactNode }) {
  const id = useId();
  return (
    <div className="t2v-field">
      <label htmlFor={id} className="t2v-strong">
        {title}
      </label>
      {children(id)}
      <p className="t2v-hint">{help}</p>
    </div>
  );
}

/**
 * The prompt/description, clamped to three lines with a toggle when it does not fit
 * (ToolCall.tsx's ApprovalPrompt: `scrollHeight > clientHeight` after paint). Untrusted —
 * a partner's or a host folder's skill text — so every character reaches the DOM only
 * through `displayText`. The "Show more"/"Show less" control sits inside a clickable
 * `<label>` for a local skill, so its own click must never also toggle the checkbox.
 */
function SkillDescription({ id, text }: { id: string; text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (el) setClamped(el.scrollHeight > el.clientHeight + 1);
  }, [text]);
  return (
    <>
      <p id={id} ref={ref} className={expanded ? 't2v-skill-desc t2v-skill-desc-expanded' : 't2v-skill-desc'}>
        {displayText(text)}
      </p>
      {clamped || expanded ? (
        <button
          type="button"
          className="t2v-link t2v-small"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
        >
          {expanded ? 'Show less' : 'Show more'}
        </button>
      ) : null}
    </>
  );
}

/**
 * One local skill: a checkbox whose accessible name is the skill's name ALONE (an
 * `aria-label`, not the label's full text content, which would otherwise run the
 * description — up to the SDK's own size cap — into every screen reader announcement);
 * the description is reached instead through `aria-describedby`. `aria-busy` is the
 * optimistic-vs-settled state made observable: true for as long
 * as this box shows a pick the server has not yet confirmed either way, cleared the
 * moment its own command's result — ok or refused — is known.
 */
function LocalSkillRow({
  skill,
  checked,
  pending,
  onToggle,
}: {
  skill: LocalSkill;
  checked: boolean;
  pending: boolean;
  onToggle: (enabled: boolean) => void;
}) {
  const descId = useId();
  return (
    <label className="t2v-skill">
      <input
        type="checkbox"
        checked={checked}
        aria-label={displayText(skill.name)}
        aria-describedby={descId}
        aria-busy={pending}
        onChange={(e) => onToggle(e.target.checked)}
      />
      <span className="t2v-skill-text">
        <span className="t2v-strong">{displayText(skill.name)}</span>
        <span className="t2v-skill-label">{displayText(skill.label)}</span>
        <SkillDescription id={descId} text={skill.description} />
      </span>
    </label>
  );
}

/** A platform skill: read-only, so no `cursor: pointer` (`.t2v-skill-static`) and no control to describe. */
function PlatformSkillRow({ skill }: { skill: PlatformSkill }) {
  const descId = useId();
  return (
    <div className="t2v-skill t2v-skill-static">
      <span className="t2v-skill-text">
        <span className="t2v-strong">{displayText(skill.name)}</span>
        <SkillDescription id={descId} text={skill.description} />
      </span>
    </div>
  );
}

export function SettingsView() {
  const store = useStore();
  const model = useModel();
  const { state } = model;
  const { auth, preferences, skills } = state;
  const signedIn = auth.isAuthenticated;

  const [pendingModel, setPendingModel] = usePending<string | null>(model, store);
  const [pendingSize, setPendingSize] = usePending<FontSize>(model, store);
  const [toggled, setToggled] = usePendingMap<boolean>(model, store);

  // Only once signed in: a guest with no account gets a 401 for this, which the store's
  // own (correct) handling of a failed, non-quiet command would otherwise turn into a
  // stray "Session expired" notice on the (hidden) thread pane. Re-sent the moment
  // sign-in completes, in case Settings is still open when it does. Its id is kept: the
  // section says skills are off only once this listing has answered (`skillsSection`).
  const [listId, setListId] = useState<string | null>(null);
  useEffect(() => {
    if (signedIn) setListId(store.command({ type: 'skillsList' }));
  }, [store, signedIn]);
  const listed = resultOf(model, listId)?.ok === true;
  const { off: skillsOff, awaitingRegister } = skillsSection(skills, listed);

  const toggleSkill = (name: string, enabled: boolean) => {
    const id = store.command({ type: enabled ? 'skillEnable' : 'skillDisable', name });
    setToggled(name, enabled, id);
  };

  const { value: modelValue, orphan, loaded } = modelPick(state);
  const shownModel = pendingModel ? pendingModel.value ?? '' : modelValue;
  const shownSize = pendingSize?.value ?? (preferences.fontSize ?? 'medium');
  // A pending pick that has fallen out of the list mid-flight (the
  // partner's own list refreshing while a pick is still awaiting its result) needs an
  // <option> of its own too, or the <select> shows blank until it settles — the same
  // "still shown, not silently replaced" rule `orphan` already gives a SETTLED one.
  const pendingOrphan =
    pendingModel && pendingModel.value !== null && pendingModel.value !== orphan && !state.models.list.some((m) => m.id === pendingModel.value)
      ? pendingModel.value
      : null;

  return (
    <div className="t2v-view t2v-settings" role="region" aria-label="Settings">
      <Notice />

      {signedIn ? (
        <Field title="AI model" help="Answers your next message. Switching keeps this conversation.">
          {(id) => (
            <select
              id={id}
              className="t2v-select"
              value={shownModel}
              disabled={!loaded}
              aria-busy={pendingModel !== null}
              onChange={(e) => {
                const modelId = e.target.value || null;
                const cmdId = store.command({ type: 'selectModel', modelId });
                setPendingModel(modelId, cmdId);
              }}
            >
              <option value="">{displayText(defaultModelLabel(state))}</option>
              {orphan ? (
                <option value={orphan}>
                  {displayText(orphan)} (no longer offered)
                </option>
              ) : null}
              {!loaded && modelValue ? <option value={modelValue}>{displayText(modelValue)}</option> : null}
              {pendingOrphan ? <option value={pendingOrphan}>{displayText(pendingOrphan)}</option> : null}
              {state.models.list.map((m) => (
                <option key={m.id} value={m.id}>
                  {displayText(modelLabel(m))}
                </option>
              ))}
            </select>
          )}
        </Field>
      ) : null}

      <Field title="Text size" help="How large the chat's text is.">
        {(id) => (
          <select
            id={id}
            className="t2v-select"
            value={shownSize}
            aria-busy={pendingSize !== null}
            onChange={(e) => {
              const fontSize = e.target.value as FontSize;
              const cmdId = store.command({ type: 'setFontSize', fontSize });
              setPendingSize(fontSize, cmdId);
            }}
          >
            {SIZES.map(([size, label]) => (
              <option key={size} value={size}>
                {label}
              </option>
            ))}
          </select>
        )}
      </Field>

      {signedIn ? (
        <section className="t2v-skills" aria-label="Skills">
          <h2 className="t2v-view-title">Skills</h2>
          {skillsOff ? (
            <p className="t2v-hint">Skills are off for this app.</p>
          ) : (
            <>
              {skills.local.length === 0 && !skills.platform?.length ? <p className="t2v-hint">No skills yet.</p> : null}
              {skills.local.map((skill) => (
                <LocalSkillRow
                  key={skill.name}
                  skill={skill}
                  checked={toggled[skill.name]?.value ?? skill.enabled}
                  pending={toggled[skill.name] !== undefined}
                  onToggle={(enabled) => toggleSkill(skill.name, enabled)}
                />
              ))}
              {awaitingRegister ? <p className="t2v-hint">These skills reach the assistant with your next message.</p> : null}
              {skills.platform && skills.platform.length > 0 ? (
                <>
                  <h3 className="t2v-subtitle">From this app’s publisher</h3>
                  {skills.platform.map((skill) => (
                    <PlatformSkillRow key={skill.name} skill={skill} />
                  ))}
                </>
              ) : null}
              {skills.needsSessionReset ? (
                <p className="t2v-hint">The assistant forgets a skill you turned off when this chat is next idle.</p>
              ) : null}
            </>
          )}
        </section>
      ) : (
        <p className="t2v-hint">Sign in to choose a model and skills.</p>
      )}
    </div>
  );
}
