/**
 * "Continue with Apple" / "Continue with Google". The sign-in runs in
 * the end-user's own browser, so this only starts it (`oauthStart`), shows that it is
 * waiting (`auth.oauthPending`), and offers Cancel (`oauthCancel`). The account arrives as
 * any sign-in does, through the auth slice. A failure is `auth.oauthError`, shown here
 * until the next attempt or until the end-user dismisses it (`oauthDismissError`), with or
 * without the buttons (the engine may stop offering them mid-attempt).
 */
import { useEffect, useRef, useState, type RefObject } from 'react';
import { LoaderCircle } from 'lucide-react';
import { authIdentityKey, failureText, PROVIDER_NAMES, providerButtons, TOO_LONG } from '../selectors';
import { resultOf, WATCH_MS } from '../store';
import type { Command, CommandResult, OAuthProvider } from '../wire';
import { useModel, useStore } from './context';
import { displayText } from './displayText';

const BUSY_ELSEWHERE = 'A sign-in is already waiting in your browser.';

function refusalText(result: CommandResult): string {
  return result.error?.type === 'busy' ? BUSY_ELSEWHERE : failureText(result);
}

/** Has focus gone nowhere (the element that held it was just removed)? The page's rule: only
 * then does this component move it. */
function focusLost(): boolean {
  const active = document.activeElement;
  return active === null || active === document.body;
}

/**
 * One command per action: a `useRef` latch (a second click in the same task sees it before
 * any re-render), released when the result is in or after `WATCH_MS`, as every latch is.
 * An identity change forgets the command: its refusal was about the
 * identity before and must not greet the next one; so does `forget()`.
 */
function useCommand() {
  const store = useStore();
  const model = useModel();
  const [sent, setSent] = useState<{ id: string; timedOut: boolean } | null>(null);
  const latch = useRef(false);
  const result = resultOf(model, sent?.id ?? null);
  const waiting = sent !== null && !sent.timedOut && result === null;
  // Keyed on `sent` too: a command the store answers for at once (the chat is over) is
  // never `waiting` at all, and must still let go of the latch.
  useEffect(() => {
    if (!waiting) latch.current = false;
  }, [waiting, sent]);
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(() => setSent((c) => (c ? { ...c, timedOut: true } : c)), WATCH_MS);
    return () => window.clearTimeout(id);
  }, [waiting, sent?.id]);
  const identity = authIdentityKey(model.state.auth);
  const lastIdentity = useRef(identity);
  useEffect(() => {
    if (identity === lastIdentity.current) return;
    lastIdentity.current = identity;
    setSent(null);
  }, [identity]);
  const send = (command: Command): void => {
    if (latch.current || waiting) return;
    latch.current = true;
    setSent({ id: store.command(command), timedOut: false });
  };
  const forget = (): void => setSent(null);
  // Lost: no result within WATCH_MS (a late one still shows, and wins).
  const timedOut = sent !== null && sent.timedOut && result === null;
  return { send, forget, result, waiting, timedOut };
}

export function ProviderSignIn({ busy }: { busy: boolean }) {
  const s = useModel().state;
  const providers = providerButtons(s);
  const pending = s.auth.oauthPending ?? null;
  const oauthError = s.auth.oauthError ?? null;
  const start = useCommand();
  const dismiss = useCommand();
  const section = useRef<HTMLDivElement>(null);
  const waiting = useRef<HTMLDivElement>(null);

  // Once a sign-in has been waiting, this page's start has had its answer: whatever it was
  // (a `busy` that lost the race to another start, say), it is about an attempt that has
  // since run, and must not outlive it or hide that attempt's own outcome.
  const isPending = pending !== null;
  const { forget } = start;
  // Keyed on the change alone: `forget` is a new function each render (and only sets state).
  useEffect(() => {
    if (isPending) forget();
  }, [isPending]);

  // Focus: the control that held it goes away on Continue (the waiting view replaces
  // the buttons), on Cancel or when the attempt ends (the waiting view goes), and on Dismiss.
  // Moved only when it fell to <body>, and only on a change seen here, never on first mount.
  // It lands on something Enter does nothing to: the waiting view itself (Tab reaches
  // Cancel next), else this section, else the form's heading. Landing on Cancel, or on a
  // Continue button, let a second or held Enter undo what the first one did.
  const phase = isPending ? 'pending' : oauthError ? 'error' : 'idle';
  const lastPhase = useRef(phase);
  useEffect(() => {
    const before = lastPhase.current;
    lastPhase.current = phase;
    if (before === phase || !focusLost()) return;
    const target =
      (phase === 'pending' ? waiting.current : section.current) ??
      document.querySelector<HTMLElement>('.t2v-account .t2v-view-title');
    target?.focus();
  }, [phase]);

  if (pending) return <Pending provider={pending.provider} containerRef={waiting} />;
  const refusal = start.result && !start.result.ok ? refusalText(start.result) : start.timedOut ? TOO_LONG : null;
  // The failure shows whether or not the buttons do: a switch turned off mid-attempt takes
  // the buttons away and leaves its message.
  if (providers.length === 0 && !refusal && !oauthError) return null;
  return (
    <div
      ref={section}
      className="t2v-providers"
      role="group"
      aria-label={providers.length > 0 ? 'Sign in with ' + providers.map((p) => PROVIDER_NAMES[p]).join(' or ') : 'Sign in with Apple or Google'}
      tabIndex={-1}
    >
      {providers.map((p) => (
        <button
          key={p}
          type="button"
          className={'t2v-btn t2v-btn-block t2v-provider t2v-provider-' + p}
          disabled={busy || start.waiting}
          aria-busy={start.waiting}
          onClick={() => {
            if (!busy) start.send({ type: 'oauthStart', provider: p });
          }}
        >
          <ProviderMark provider={p} />
          <span>Continue with {PROVIDER_NAMES[p]}</span>
        </button>
      ))}
      {refusal ? (
        <p className="t2v-form-error" role="alert">
          {displayText(refusal)}
        </p>
      ) : oauthError ? (
        <div className="t2v-form-error t2v-oauth-error" role="alert">
          <span>{displayText(oauthError.message)}</span>
          <button
            type="button"
            className="t2v-link t2v-small"
            disabled={dismiss.waiting}
            onClick={() => dismiss.send({ type: 'oauthDismissError' })}
          >
            Dismiss
          </button>
        </div>
      ) : null}
      {providers.length > 0 ? (
        <p className="t2v-divider" aria-hidden="true">
          <span>or</span>
        </p>
      ) : null}
    </div>
  );
}

function Pending({ provider, containerRef }: { provider: string; containerRef: RefObject<HTMLDivElement | null> }) {
  const cancel = useCommand();
  const name = provider === 'apple' || provider === 'google' ? PROVIDER_NAMES[provider] : 'your account';
  // A live region that mounts already filled is not announced (Account.tsx's reason
  // banner): it mounts empty and the line is put in once it is there.
  const text = 'Finish signing in with ' + name + ' in your browser';
  const [line, setLine] = useState('');
  useEffect(() => {
    setLine(text);
  }, [text]);
  // The group is what focus lands on when this view replaces the button that held it:
  // named at once (the live line inside it is still empty at that moment), inert to Enter.
  return (
    <div ref={containerRef} className="t2v-stack t2v-oauth-pending" role="group" aria-label={text} tabIndex={-1}>
      <p className="t2v-strong t2v-oauth-line">
        {/* Animated under Reduce Motion too: a frozen loader reads as a hang. */}
        <LoaderCircle className="t2v-spin" aria-hidden="true" size={16} />
        <span role="status" aria-live="polite" aria-atomic="true">
          {line}
        </span>
      </p>
      {/* The wire does not say when the host has opened the browser, so this never
          claims it has. */}
      <p className="t2v-muted t2v-small">
        A sign-in page opens in your browser. Confirm there, then come back: this chat updates
        by itself. Nothing opened? Cancel and try again.
      </p>
      <button
        type="button"
        className="t2v-btn t2v-btn-block"
        disabled={cancel.waiting}
        aria-busy={cancel.waiting}
        onClick={() => cancel.send({ type: 'oauthCancel' })}
      >
        Cancel
      </button>
    </div>
  );
}

function ProviderMark({ provider }: { provider: OAuthProvider }) {
  if (provider === 'apple') {
    return (
      <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
        <path d="M16.365 1.43c0 1.14-.493 2.27-1.177 3.08-.744.9-1.99 1.57-2.987 1.57-.12 0-.23-.02-.3-.03-.01-.06-.04-.22-.04-.39 0-1.15.572-2.27 1.206-2.98.804-.94 2.142-1.64 3.248-1.68.03.13.05.28.05.43zm4.565 15.71c-.03.07-.463 1.58-1.518 3.12-.945 1.34-1.94 2.71-3.43 2.71-1.517 0-1.9-.88-3.63-.88-1.698 0-2.302.91-3.67.91-1.377 0-2.332-1.26-3.428-2.8-1.287-1.82-2.323-4.63-2.323-7.28 0-4.28 2.797-6.55 5.552-6.55 1.448 0 2.675.95 3.6.95.865 0 2.222-1.01 3.902-1.01.613 0 2.886.06 4.374 2.19-.13.09-2.383 1.37-2.383 4.19 0 3.26 2.854 4.42 2.955 4.45z" />
      </svg>
    );
  }
  return (
    <svg width="16" height="16" viewBox="0 0 48 48" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z" />
      <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z" />
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z" />
    </svg>
  );
}
