/**
 * Every tool call in the thread, as TS chat/tool-fallback.tsx draws it, in three
 * states:
 * - running: what the host says the agent is doing (`tools[name].activity`, default
 *   "Working…") and for how long;
 * - waiting on the end-user: the approval card;
 * - finished: a "Used"/"Denied" line that opens to the arguments and the result.
 */
import { useEffect, useId, useRef, useState } from 'react';
import { ChevronRight, LoaderCircle, ShieldAlert } from 'lucide-react';
import { useToolCallElapsed, type ToolCallMessagePartComponent, type ToolCallMessagePartProps } from '@assistant-ui/react';
import type { ApprovalArtifact } from '@sdk/assistant-ui/convert';
import { displayText, escapeForJson } from './displayText';
import { canShowArguments, deepEqual, hasUnsafeInteger, mayAlwaysAllow, parseArguments, prettyArguments } from '../selectors';
import { resultOf, WATCH_MS } from '../store';
import type { ToolPresentation } from '../wire';
import { useModel, useStore } from './context';

const awaitingDecision = (p: ToolCallMessagePartProps) =>
  p.approval != null && p.approval.approved === undefined && p.approval.resolution === undefined;

export const ToolCall: ToolCallMessagePartComponent = (props) => {
  if (awaitingDecision(props)) return <ApprovalCard {...props} />;
  return props.result === undefined ? <RunningCall {...props} /> : <FinishedCall {...props} />;
};

/** Seconds this call has been running: assistant-ui's clock when it has one, else a local tick. */
function useElapsedSeconds(): number {
  const fromPart = useToolCallElapsed();
  const [ticked, setTicked] = useState(0);
  const tracked = fromPart !== undefined;
  useEffect(() => {
    if (tracked) return;
    const id = window.setInterval(() => setTicked((s) => s + 1), 1000);
    return () => window.clearInterval(id);
  }, [tracked]);
  return fromPart !== undefined ? Math.floor(fromPart / 1000) : ticked;
}

function usePresentation(toolName: string): ToolPresentation | undefined {
  return useModel().state.tools[toolName];
}

/** `activity` is the host's own copy (`ChatOptions.tools`), shown as given. */
function RunningCall({ toolName }: ToolCallMessagePartProps) {
  const presentation = usePresentation(toolName);
  const seconds = useElapsedSeconds();
  return (
    <div className="t2v-tool t2v-tool-running" data-state="running">
      <LoaderCircle className="t2v-spin" aria-hidden="true" size={14} />
      <span className="t2v-tool-activity">{presentation?.activity ?? 'Working…'}</span>
      <span className="t2v-tool-elapsed">{String(seconds)}s</span>
    </div>
  );
}

/** A generated image's `display`: the bytes need a server route (a later plan), so say what it was. */
function displayLine(artifact: unknown): string | null {
  const display = (artifact as { display?: Record<string, unknown> } | undefined)?.display;
  if (!display) return null;
  if (display.kind === 'image') return 'Generated image: ' + displayText(String(display.filename ?? 'image'));
  if (display.kind === 'image_failure')
    return 'The image could not be generated (' + displayText(String(display.reason ?? 'error')) + ').';
  return null;
}

function FinishedCall({ toolName, args, result, isError, artifact }: ToolCallMessagePartProps) {
  const [open, setOpen] = useState(false);
  const note = displayLine(artifact);
  const state = isError ? 'denied' : 'used';
  return (
    <div className="t2v-tool" data-state={state}>
      <button type="button" className="t2v-tool-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <ChevronRight className={open ? 't2v-chevron t2v-chevron-open' : 't2v-chevron'} aria-hidden="true" size={14} />
        <span className="t2v-tool-verb">{isError ? 'Denied' : 'Used'}</span>{' '}
        <code className="t2v-tool-name">{displayText(toolName)}</code>
      </button>
      {note ? <p className="t2v-tool-note">{note}</p> : null}
      {open ? (
        <div className="t2v-tool-body">
          <pre className="t2v-tool-args">{displayText(prettyArguments(args))}</pre>
          {result !== undefined && result !== '' ? <pre className="t2v-tool-result">{displayText(String(result))}</pre> : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * The host's warning for a destructive tool, shown as given (its own copy), or the page's
 * own, naming what it does: the host's activity, shown as given too, else the tool's own
 * name, escaped as tool names are everywhere.
 */
function DestructiveWarning({ toolName, presentation }: { toolName: string; presentation: ToolPresentation }) {
  return (
    <div className="t2v-warning" role="alert">
      {presentation.warning ? (
        presentation.warning
      ) : (
        <span>
          This will modify your work: <strong>{presentation.activity ?? displayText(toolName)}</strong>. Review before approving.
        </span>
      )}
    </div>
  );
}

/**
 * The prompt/description, clamped to three lines with a toggle when it does not fit
 * (`scrollHeight > clientHeight` after paint — no ResizeObserver needed, since the text
 * itself never changes after this card mounts). A card left permanently truncated with no
 * way to read the rest is unreviewable, and `title` alone is a tooltip: not touch-
 * reachable, and not always exposed to a screen reader either.
 */
function ApprovalPrompt({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (el) setClamped(el.scrollHeight > el.clientHeight + 1);
  }, [text]);
  const shown = displayText(text);
  return (
    <>
      <p ref={ref} className={expanded ? 't2v-approval-prompt t2v-approval-prompt-expanded' : 't2v-approval-prompt'}>
        {shown}
      </p>
      {clamped || expanded ? (
        <button type="button" className="t2v-link t2v-small" onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Show less' : 'Show more'}
        </button>
      ) : null}
    </>
  );
}

/**
 * The approval card. Rules (TS tool-fallback.tsx):
 * - edited arguments apply to THIS call only, so "Always allow" is off while they are;
 * - a destructive tool is never "Always allow"ed: one click would let it run unasked for
 *   the rest of the conversation (`mayAlwaysAllow`, tested on its own);
 * - a reason is sent only with a denial;
 * - an engine approval (anything but a client one: fail closed) is allowed or denied
 *   per call — no "Always allow", no edits, no reason (Python ignores them there);
 * - only the approval the server holds is answered: a card left over answers nothing;
 * - arguments this page cannot faithfully show (not a plain object: `canShowArguments`)
 *   are never rendered as an empty, harmless-looking "{}" with Allow enabled — only Deny;
 * - one command per decision, even across several clicks inside one JS task, where a
 *   `disabled` attribute has not reached the DOM yet (`latchRef`, synchronous, unlike the
 *   reactive `busy`);
 * - an answer whose result never comes frees the buttons after `WATCH_MS`, with the
 *   notice "That took too long. Try again.";
 * - arguments holding an integer too large for a JavaScript number are shown rounded
 *   (`hasUnsafeInteger`), so the card says so and offers no "Edit arguments": an edit
 *   would send the rounded value, where the tool unedited gets the exact one.
 */
function ApprovalCard({ toolName, args, approval, artifact }: ToolCallMessagePartProps) {
  const store = useStore();
  const model = useModel();
  const presentation = model.state.tools[toolName];
  const extra = (artifact as { approval?: ApprovalArtifact } | undefined)?.approval;
  const engine = extra?.kind === 'engine';
  const mayAlways = mayAlwaysAllow(extra?.kind, presentation);
  const argsOk = canShowArguments(args);
  const roundedNumbers = argsOk && hasUnsafeInteger(args);
  const original = prettyArguments(args);
  // Seeded with the ESCAPED form: the raw original would put a bidi override or a
  // zero-width character back in front of the end-user the instant they open the
  // editor, the same spoof the read-only view already guards against. escapeForJson
  // produces valid JSON that parses back to the exact original — `deepEqual` below,
  // not a string comparison, is what tells "untouched" from "edited".
  const originalParsed = parseArguments(original);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => escapeForJson(original));
  const [denying, setDenying] = useState(false);
  const [reason, setReason] = useState('');
  const [sent, setSent] = useState<{ id: string; timedOut: boolean } | null>(null);
  const argsId = useId();
  const reasonId = useId();
  const titleId = useId();
  const rootRef = useRef<HTMLDivElement>(null);
  const denyBtnRef = useRef<HTMLButtonElement>(null);
  const wasDenying = useRef(false);
  const latchRef = useRef(false);

  // Busy until the answer is in: the card goes when pendingApproval changes; a refused
  // answer (its result) gives the buttons back, and so does one that never comes, after
  // the same WATCH_MS the store and the other views wait.
  const answered = resultOf(model, sent?.id ?? null);
  const waiting = sent !== null && !sent.timedOut && answered === null;
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(() => {
      setSent((current) => (current ? { ...current, timedOut: true } : current));
      store.showNotice('That took too long. Try again.');
    }, WATCH_MS);
    return () => window.clearTimeout(id);
  }, [waiting, sent?.id, store]);
  const busy = sent !== null && !sent.timedOut && (answered === null || answered.ok);

  // The latch resets once `busy` clears (a refusal, or nothing sent yet); it does NOT
  // reset on every render, so it still blocks a second `decide()` call arriving before
  // React has re-rendered `busy` at all (see the module doc: several `el.click()`s inside
  // one JS task never see an updated `disabled` attribute either).
  useEffect(() => {
    if (!busy) latchRef.current = false;
  }, [busy]);

  // Bring the card into view: the thread can be scrolled such that a newly-drawn card's
  // title sits above the visible area (the reply text before it, or a sticky header/
  // footer), leaving only its tail on screen. `nearest` does nothing if it is already
  // visible. A macrotask (not an effect, not a rAF) so this runs AFTER assistant-ui's own
  // scroll-to-bottom effect on the viewport — that effect fires in the same commit, as a
  // parent of this card, so anything scheduled inside React's own effect phase or a single
  // requestAnimationFrame still loses the race and gets scrolled straight back to the
  // bottom underneath it.
  useEffect(() => {
    const id = window.setTimeout(() => rootRef.current?.scrollIntoView({ block: 'nearest' }), 0);
    return () => window.clearTimeout(id);
  }, []);

  // When this card goes — answered, replaced by a fresh one, or denied by Stop — its own
  // buttons go with it. Land focus back on the composer rather than let the browser drop
  // it to <body>. Thread never unmounts (App.tsx keeps it alive behind Account/Settings,
  // hidden with `visibility:hidden`), so this card can resolve — and unmount — while the
  // end-user is on a DIFFERENT view entirely, mid-something-else (a half-typed sign-in
  // field, say): only steal focus when it is nowhere useful already. Removing a focused
  // element moves the browser's own focus to <body> (there is nothing else to give it
  // to), so "focus is on <body> right now" already covers "focus was on something inside
  // the card that just went" — a focus genuinely elsewhere (Account's form) never passes
  // through <body> at all and is left alone.
  useEffect(() => {
    return () => {
      const active = document.activeElement;
      if (active === null || active === document.body) {
        document.querySelector<HTMLTextAreaElement>('.t2v-input')?.focus();
      }
    };
  }, []);

  // "Back" out of the deny-reason view returns to buttons that already existed before it
  // (same reasoning as the unmount case): send focus to the one that led here rather than
  // let it drop.
  useEffect(() => {
    if (wasDenying.current && !denying) denyBtnRef.current?.focus();
    wasDenying.current = denying;
  }, [denying]);

  // "Changed" is a comparison of the PARSED values, never of the strings: `draft` starts
  // as the escaped form of `original`, which is never textually equal to it even when the
  // end-user has not touched a thing — comparing raw text here would send `updatedInput`
  // (and turn off "Always allow") on every edit-arguments open, edited or not.
  const parsedDraft = editing ? parseArguments(draft) : null;
  const draftValid = !editing || parsedDraft !== null;
  const draftChanged = editing && parsedDraft !== null && originalParsed !== null && !deepEqual(parsedDraft, originalParsed);
  const edited = draftChanged ? parsedDraft : null;

  const decide = (action: 'once' | 'always' | 'deny') => {
    if (latchRef.current) return; // synchronous: guards a burst of clicks a re-render has not caught up with yet
    const pending = store.latest().state.pendingApproval;
    if (busy || !approval || pending?.toolCallId !== approval.id) return;
    if (action === 'always' && !mayAlways) return; // the button is not drawn; fail closed anyway
    if (!argsOk && action !== 'deny') return; // fail closed: undisplayable arguments, Deny only
    latchRef.current = true;
    const id = store.command({
      type: 'approve',
      toolCallId: approval.id,
      action,
      ...(action === 'deny' && !engine && reason.trim() ? { feedback: reason.trim() } : {}),
      ...(action === 'once' && !engine && edited ? { updatedInput: edited } : {}),
    });
    setSent({ id, timedOut: false });
  };

  return (
    <div ref={rootRef} className="t2v-approval" data-state="waiting" data-kind={engine ? 'engine' : 'client'} role="group" aria-labelledby={titleId}>
      <div className="t2v-approval-head">
        <ShieldAlert className="t2v-approval-icon" aria-hidden="true" size={16} />
        <div className="t2v-approval-text">
          <p className="t2v-approval-title" id={titleId}>
            The assistant wants to run <code>{displayText(toolName)}</code>
          </p>
          {approval?.prompt ? <ApprovalPrompt text={approval.prompt} /> : null}
        </div>
      </div>

      {extra && extra.details.length > 0 ? (
        <dl className="t2v-approval-details">
          {extra.details.map((detail, index) => (
            <div key={index} className="t2v-approval-detail">
              <dt>{displayText(detail.label)}</dt>
              <dd>{displayText(detail.value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}

      {!argsOk ? (
        <p className="t2v-warning" role="alert">
          These arguments could not be displayed.
        </p>
      ) : editing ? (
        <div className="t2v-field">
          <label htmlFor={argsId}>Arguments (JSON)</label>
          <textarea
            id={argsId}
            className={'t2v-textarea t2v-mono' + (draftValid ? '' : ' t2v-invalid')}
            value={draft}
            spellCheck={false}
            rows={Math.min(12, draft.split('\n').length + 1)}
            onChange={(e) => setDraft(e.target.value)}
          />
          {!draftValid ? <span className="t2v-hint t2v-hint-error">Not valid JSON — fix it or deny.</span> : null}
          {draftChanged && draftValid && mayAlways ? (
            <span className="t2v-hint">Your changes apply to this call only, so “Always allow” is off while they’re in place.</span>
          ) : null}
        </div>
      ) : (
        <pre className="t2v-tool-args">{displayText(original)}</pre>
      )}
      {roundedNumbers ? <p className="t2v-hint">Some numbers are too large to show exactly.</p> : null}

      {presentation?.destructive ? <DestructiveWarning toolName={toolName} presentation={presentation} /> : null}

      {denying ? (
        <div className="t2v-field">
          <label htmlFor={reasonId}>Reason for denying (optional, sent to the assistant)</label>
          <textarea
            id={reasonId}
            className="t2v-textarea"
            rows={2}
            value={reason}
            placeholder="e.g. wrong volume"
            autoFocus
            onChange={(e) => setReason(e.target.value)}
          />
        </div>
      ) : null}

      <div className="t2v-actions">
        {denying ? (
          <>
            <button type="button" className="t2v-btn t2v-btn-danger" data-action="confirm-deny" disabled={busy} onClick={() => decide('deny')}>
              Confirm deny
            </button>
            <button type="button" className="t2v-btn t2v-btn-ghost" disabled={busy} onClick={() => setDenying(false)}>
              Back
            </button>
          </>
        ) : (
          <>
            {argsOk ? (
              <button type="button" className="t2v-btn t2v-btn-primary" data-action="once" disabled={busy || !draftValid} onClick={() => decide('once')}>
                {engine ? 'Allow' : 'Allow once'}
              </button>
            ) : null}
            {argsOk && mayAlways ? (
              <button type="button" className="t2v-btn" data-action="always" disabled={busy || draftChanged} onClick={() => decide('always')}>
                Always allow
              </button>
            ) : null}
            <button
              type="button"
              className="t2v-btn t2v-btn-ghost"
              data-action="deny"
              ref={denyBtnRef}
              disabled={busy}
              onClick={() => (engine ? decide('deny') : setDenying(true))}
            >
              Deny
            </button>
            {!engine && argsOk && !roundedNumbers ? (
              <button
                type="button"
                className="t2v-btn t2v-btn-ghost t2v-push-right"
                data-action="edit"
                disabled={busy}
                onClick={() => {
                  if (editing) setDraft(escapeForJson(original));
                  setEditing(!editing);
                }}
              >
                {editing ? 'Discard edits' : 'Edit arguments'}
              </button>
            ) : null}
          </>
        )}
      </div>
      {argsOk && mayAlways && !denying ? (
        <p className="t2v-hint">
          “Always allow” skips this card for <code>{displayText(toolName)}</code> in this app, for the rest of this conversation.
        </p>
      ) : null}
    </div>
  );
}
