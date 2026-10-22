/**
 * The Settings view: the AI model (with the partner's Default, TS chat/views/settings.tsx),
 * and the text size. Skills have their own view (Skills.tsx, from the header).
 * Every choice is a command; what the page shows is what the server's slices say, so a
 * refused pick simply does not stick. Signed out, the model picker is hidden outright, not
 * merely disabled — text size is a local preference and stays available.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, UserRound } from 'lucide-react';
import { defaultModelLabel, FALLBACK_STT_MODEL, languageName, languagesFor, modelLabel, modelPick } from '../selectors';
import { resultOf, WATCH_MS, type PageModel, type PageStore } from '../store';
import type { FontSize } from '../wire';
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
 * Same as `usePending`, keyed by name: every local skill's own toggle (Skills.tsx). The
 * `WATCH_MS` clock is kept in a ref, per key, and managed imperatively in `set()` rather
 * than in a reactive effect over the whole map — an effect keyed on `pending` (the map)
 * would restart EVERY key's timer the moment any ONE of them settles (the settle effect
 * below always produces a new map object), extending every sibling's window each time.
 * Exported for `Skills.tsx`, which owns the skill toggles now that Settings only links to it.
 */
export function usePendingMap<T>(
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

  // Every ref currently running, cleared when the calling view itself unmounts.
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
      <label htmlFor={id}>{title}</label>
      {children(id)}
      <p className="t2v-hint">{help}</p>
    </div>
  );
}

/** A labelled group of fields (Assistant / Appearance / Skills). */
function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="t2v-section" aria-label={title}>
      <h2 className="t2v-section-label">{title}</h2>
      {children}
    </section>
  );
}

/* SkillDescription, LocalSkillRow and PlatformSkillRow moved to Skills.tsx:
 * skills live in their own view, opened from the header; Settings has no skills section. */

export function SettingsView({ onAccount }: { onAccount: () => void }) {
  const store = useStore();
  const model = useModel();
  const { state } = model;
  const { auth, preferences, audioModels, config } = state;
  const signedIn = auth.isAuthenticated;
  const features = state.options.features;

  const [pendingModel, setPendingModel] = usePending<string | null>(model, store);
  const [pendingSize, setPendingSize] = usePending<FontSize>(model, store);
  const [pendingVoice, setPendingVoice] = usePending<string | null>(model, store);
  const [pendingLanguage, setPendingLanguage] = usePending<string | null>(model, store);

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

  // TS settings.tsx's Picker: "loading" / "ready" / "error", not a bare list.length check —
  // an empty-but-ready catalogue and a failed read must
  // never both read as "Loading…" forever, the way `list.length > 0` alone would have them.
  const voiceState = audioModels.state;
  const voicesLoaded = voiceState === 'ready';
  const defaultSttId = config?.default_stt_model || FALLBACK_STT_MODEL;
  const voiceValue = preferences.sttModel ?? '';
  const shownVoice = pendingVoice ? pendingVoice.value ?? '' : voiceValue;
  const voiceOrphan = voicesLoaded && shownVoice && !audioModels.list.some((m) => m.id === shownVoice) ? shownVoice : null;
  const defaultVoice = audioModels.list.find((m) => m.id === config?.default_stt_model);
  const defaultVoiceLabel = defaultVoice ? 'Default — ' + (defaultVoice.name ?? defaultVoice.id) : 'Default';
  const activeVoice = audioModels.list.find((m) => m.id === (shownVoice || defaultSttId));
  const languages = languagesFor(activeVoice);
  const language = preferences.sttLanguage ?? '';
  const shownLanguage = pendingLanguage ? pendingLanguage.value ?? '' : language;
  const languageOrphan = shownLanguage !== '' && !languages.some(([code]) => code === shownLanguage);
  const voiceName = activeVoice ? activeVoice.name ?? activeVoice.id : 'the voice model';
  const languageHelp = activeVoice?.supported_languages?.length
    ? languages.length === 1
      ? displayText(voiceName) + ' transcribes ' + languages[0]![1] + ' only.'
      : 'The ' + String(languages.length) + ' languages ' + displayText(voiceName) + ' can transcribe.'
    : 'A hint for the voice model.';
  const sizeLabelId = useId();

  const textSize = (
    <div className="t2v-field">
      <label id={sizeLabelId}>Text size</label>
      <div className="t2v-seg" role="group" aria-labelledby={sizeLabelId} aria-busy={pendingSize !== null}>
        {SIZES.map(([size, label]) => (
          <button
            key={size}
            type="button"
            aria-pressed={shownSize === size}
            onClick={() => {
              const cmdId = store.command({ type: 'setFontSize', fontSize: size });
              setPendingSize(size, cmdId);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <p className="t2v-hint">How large the chat’s text is.</p>
    </div>
  );

  const assistantSection = signedIn ? (
    <Section title="Assistant">
      <Field title="AI model" help="Answers your next message. Switching keeps this conversation.">
        {(id) => (
          <div className="t2v-select-wrap">
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
            <ChevronDown className="t2v-select-chevron" aria-hidden="true" size={16} />
          </div>
        )}
      </Field>

      {features.dictation ? (
        <>
          <Field
            title="Voice model"
            help={
              voiceState === 'error'
                ? 'Couldn’t load the list — the default will be used.'
                : 'Transcribes what you say when you use the mic.'
            }
          >
            {(id) => (
              <div className="t2v-select-wrap">
                <select
                  id={id}
                  className="t2v-select"
                  value={shownVoice}
                  disabled={!voicesLoaded}
                  aria-busy={pendingVoice !== null}
                  onChange={(e) => {
                    const modelId = e.target.value || null;
                    const cmdId = store.command({ type: 'selectSttModel', modelId });
                    setPendingVoice(modelId, cmdId);
                    // A language the new model cannot transcribe goes back to auto-detect
                    // (TS settings.tsx:106-114).
                    const next = audioModels.list.find((m) => m.id === (modelId || defaultSttId));
                    if (language && !languagesFor(next).some(([code]) => code === language)) {
                      setPendingLanguage(null, store.command({ type: 'selectSttLanguage', language: null }));
                    }
                  }}
                >
                  <option value="">{voiceState === 'loading' ? 'Loading…' : displayText(defaultVoiceLabel)}</option>
                  {voiceOrphan ? <option value={voiceOrphan}>{displayText(voiceOrphan)} (no longer offered)</option> : null}
                  {audioModels.list.map((m) => (
                    <option key={m.id} value={m.id}>
                      {displayText(m.name ?? m.id)}
                    </option>
                  ))}
                </select>
                <ChevronDown className="t2v-select-chevron" aria-hidden="true" size={16} />
              </div>
            )}
          </Field>
          <Field title="Spoken language" help={languageHelp}>
            {(id) => (
              <div className="t2v-select-wrap">
                <select
                  id={id}
                  className="t2v-select"
                  value={shownLanguage}
                  aria-busy={pendingLanguage !== null}
                  onChange={(e) => {
                    const next = e.target.value || null;
                    setPendingLanguage(next, store.command({ type: 'selectSttLanguage', language: next }));
                  }}
                >
                  <option value="">Detect automatically</option>
                  {languageOrphan ? (
                    <option value={shownLanguage}>
                      {displayText(languageName(shownLanguage))} (not supported by {displayText(voiceName)})
                    </option>
                  ) : null}
                  {languages.map(([code, name]) => (
                    <option key={code} value={code}>
                      {displayText(name)}
                    </option>
                  ))}
                </select>
                <ChevronDown className="t2v-select-chevron" aria-hidden="true" size={16} />
              </div>
            )}
          </Field>
        </>
      ) : null}
    </Section>
  ) : (
    <Section title="Assistant">
      <div className="t2v-guest-card">
        <p>Sign in to choose a model, pick a voice for dictation and add skills. Your chats are saved to your account.</p>
        <button type="button" className="t2v-btn t2v-btn-outline" onClick={onAccount}>
          <UserRound aria-hidden="true" size={14} />
          Sign in
        </button>
      </div>
    </Section>
  );


  return (
    <div className="t2v-view t2v-settings" role="region" aria-label="Settings">
      <Notice />
      {signedIn ? (
        <>
          {assistantSection}
          <Section title="Appearance">{textSize}</Section>
        </>
      ) : (
        <>
          <Section title="Appearance">{textSize}</Section>
          {assistantSection}
        </>
      )}
    </div>
  );
}
