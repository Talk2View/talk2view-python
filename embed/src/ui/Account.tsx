/**
 * The Account view (TS chat/views/account.tsx): sign in, create an account, the "check
 * your email" screen, sign out. The view waits for its own command's result (matched by
 * id) only to show an error, the confirmation screen, or to go back to the chat; the auth
 * slice is what signs the end-user in, so nothing blocks on a result. The result is
 * watched here, not in the form: the form is replaced the moment the auth slice shows the
 * account, which is before the result arrives. Apple and Google sign in through the
 * end-user's browser (`ProviderSignIn`); they show only when the engine
 * offers them.
 */
import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { authIdentityKey, failureText, REASON_COPY, TOO_LONG, type GateReason } from '../selectors';
import { resultOf, WATCH_MS } from '../store';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import { ProviderSignIn } from './ProviderSignIn';

type Mode = 'login' | 'signup';

interface Sent {
  id: string;
  email: string;
  timedOut: boolean;
  identityMoved: boolean;
}


export function AccountView({
  reason,
  gated,
  visible,
  onDone,
}: {
  reason: GateReason;
  gated: boolean;
  /** Whether the Account pane is the one currently shown: the pane
   * itself never unmounts (App.tsx), so this is what a hidden pane's own cleanup —
   * clearing a typed password — has to key on instead. */
  visible: boolean;
  onDone: () => void;
}) {
  const store = useStore();
  const model = useModel();
  const auth = model.state.auth;
  // The sign-in or sign-up this view is waiting on. `timedOut`: its result did not come
  // within WATCH_MS. `identityMoved`: the identity changed while it was in flight.
  const [sent, setSent] = useState<Sent | null>(null);
  const [confirmSentTo, setConfirmSentTo] = useState<string | null>(null);
  // What "Check your email" hands back to the sign-in form when there is nowhere else to
  // go: the mode and email to open it with, instead of its own usual defaults.
  const [retry, setRetry] = useState<{ mode: Mode; email: string } | null>(null);

  const result = resultOf(model, sent?.id ?? null);
  useEffect(() => {
    if (!result || !result.ok || !sent) return;
    setSent(null);
    if (result.type === 'signup' && result.data?.confirmationRequired === true) setConfirmSentTo(sent.email);
    else onDone();
  }, [result, sent, onDone]);

  // The pane staying mounted (App.tsx) means none of this resets itself
  // on a later, UNRELATED identity change — becoming a guest by another route, or the
  // host signing someone in directly. "Check your email" must never outrank a signed-in
  // state, and a stale sign-in error from a wrong password must not still be showing
  // "much later" once the identity has moved on. An in-flight `sent` is kept, only marked
  // `identityMoved`, unless THIS identity change's own command has SETTLED as a failure
  // (`result && !result.ok`), or had already timed out: its "That took too long" was
  // about the identity before, and must not greet a later, fresh form. A correct
  // sign-in changes the auth slice (this effect's own trigger) before its command's result
  // is necessarily recorded (the module header: the slice arrives before the result) —
  // `result` can still be null, or the PREVIOUS command's result, in the very render this
  // fires. Clearing `sent` on anything but a settled failure risks nulling it while that
  // result is still in flight: the existing effect above needs BOTH `result.ok` and `sent` to
  // call `onDone()`, and would then never see them together — stranding the view on
  // Account with "Signed in as…" showing, "Chat" never reached.
  const authIdentity = authIdentityKey(auth);
  const lastAuthIdentity = useRef(authIdentity);
  useEffect(() => {
    if (authIdentity === lastAuthIdentity.current) return;
    lastAuthIdentity.current = authIdentity;
    setConfirmSentTo(null);
    const settledFailure = result !== null && !result.ok;
    setSent((current) =>
      current === null || settledFailure || current.timedOut ? null : { ...current, identityMoved: true },
    );
  }, [authIdentity, result]);

  // A provider sign-in this pane showed, ending with a real account, goes back to the chat
  // (the password path does it from its result; this one has none).
  const oauthPending = auth.oauthPending ?? null;
  const sawPending = useRef(false);
  useEffect(() => {
    if (oauthPending) {
      sawPending.current = true;
      return;
    }
    if (!sawPending.current) return;
    sawPending.current = false;
    if (visible && auth.isAuthenticated && !auth.isAnonymous) onDone();
  }, [oauthPending, visible, auth.isAuthenticated, auth.isAnonymous, onDone]);

  // A lost result (queued, but its op never arrives) would otherwise leave a gated form
  // busy — and the gate itself closed — for good. The same window the store gives up on a
  // watched command (`WATCH_MS`) frees this one too, rather than a second clock of its own.
  // The timed-out command is kept, so that a failure arriving late still shows its real
  // reason, until the identity changes or the form is sent again. One whose
  // identity already moved on is simply dropped: the form it belonged to is gone.
  const waiting = sent !== null && !sent.timedOut && result === null;
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(
      () => setSent((current) => (current === null ? null : current.identityMoved ? null : { ...current, timedOut: true })),
      WATCH_MS,
    );
    return () => window.clearTimeout(id);
  }, [waiting, sent?.id]);

  const submit = (mode: Mode, email: string, password: string) => {
    setSent({ id: store.command({ type: mode, email, password }), email, timedOut: false, identityMoved: false });
  };

  // Leaving "Check your email": gated, there is nowhere else to go — the sign-in
  // form, in login mode, with the email just used kept — so the screen never becomes a
  // dead end. Not gated, the usual "back to the chat" applies.
  const leaveConfirmation = () => {
    const email = confirmSentTo;
    setConfirmSentTo(null);
    if (gated) setRetry({ mode: 'login', email: email ?? '' });
    else onDone();
  };

  let body;
  if (confirmSentTo) {
    body = <CheckYourEmail email={confirmSentTo} gated={gated} onDone={leaveConfirmation} />;
  } else if (auth.isAuthenticated && !auth.isAnonymous) {
    body = <SignedIn email={auth.user?.email ?? ''} />;
  } else {
    body = (
      <SignInForm
        guest={auth.isAuthenticated && auth.isAnonymous}
        reason={reason}
        busy={waiting}
        error={
          // A real reason, even a late one, says more than the timeout.
          result && !result.ok ? failureText(result) : sent?.timedOut ? TOO_LONG : null
        }
        initialMode={retry?.mode}
        initialEmail={retry?.email}
        visible={visible}
        authIdentity={authIdentity}
        onSubmit={submit}
      />
    );
  }
  return (
    <div className="t2v-view t2v-account" role="region" aria-label="Account">
      {body}
    </div>
  );
}

function CheckYourEmail({ email, gated, onDone }: { email: string; gated: boolean; onDone: () => void }) {
  return (
    <div className="t2v-stack">
      <h2 className="t2v-view-title">Check your email</h2>
      <p className="t2v-muted">
        We sent a confirmation link to <span className="t2v-strong">{displayText(email)}</span>.{' '}
        {gated
          ? 'Confirm it, then sign in to continue this chat.'
          : 'You can keep chatting in the meantime — this conversation moves to your account once you confirm.'}
      </p>
      <button type="button" className="t2v-btn t2v-btn-block" onClick={onDone}>
        {gated ? 'Back to sign in' : 'Back to the chat'}
      </button>
    </div>
  );
}

function SignedIn({ email }: { email: string }) {
  const store = useStore();
  const model = useModel();
  const [sent, setSent] = useState<{ id: string; timedOut: boolean } | null>(null);
  // `logout` is a quiet command: a refusal raises no notice, so this view has to watch its
  // own result. Without it a failed sign-out would leave the button disabled and
  // "Signing out…" forever, with nothing to click and nothing said. A result that never
  // comes at all is bounded by `WATCH_MS`, as the sign-in form's is.
  const result = resultOf(model, sent?.id ?? null);
  const waiting = sent !== null && !sent.timedOut && result === null;
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(() => setSent((current) => (current ? { ...current, timedOut: true } : current)), WATCH_MS);
    return () => window.clearTimeout(id);
  }, [waiting, sent?.id]);
  const busy = sent !== null && !sent.timedOut && (result === null || result.ok);
  const failed = result !== null && !result.ok;
  const error = failed ? failureText(result) : sent?.timedOut && result === null ? TOO_LONG : null;
  return (
    <>
      <div className="t2v-stack">
        <p className="t2v-muted t2v-small">Signed in as</p>
        <p className="t2v-strong t2v-truncate">{displayText(email)}</p>
      </div>
      <p className="t2v-muted t2v-small">Your conversations are saved to this account, and it works in every Talk2View app.</p>
      <button
        type="button"
        className="t2v-btn t2v-btn-block"
        disabled={busy}
        onClick={() => setSent({ id: store.command({ type: 'logout' }), timedOut: false })}
      >
        {busy ? 'Signing out…' : 'Sign out'}
      </button>
      {error ? (
        <p className="t2v-error" role="alert">
          {displayText(error)}
        </p>
      ) : null}
    </>
  );
}

function SignInForm({
  guest,
  reason,
  busy,
  error,
  initialMode,
  initialEmail,
  visible,
  authIdentity,
  onSubmit,
}: {
  guest: boolean;
  reason: GateReason;
  busy: boolean;
  error: string | null;
  /** Set only when this mount replaces "Check your email" while still gated. */
  initialMode?: Mode;
  initialEmail?: string;
  /** Whether the Account pane is the one currently shown. */
  visible: boolean;
  /** `user id : isAuthenticated : isAnonymous`: a typed password is
   * cleared the moment this changes, even while the form stays mounted and visible (a
   * guest signing in another route, for instance) — the email is kept. */
  authIdentity: string;
  onSubmit: (mode: Mode, email: string, password: string) => void;
}) {
  const store = useStore();
  const { options, auth } = useModel().state;
  const { resetPasswordUrl } = options;
  // A provider sign-in waiting in the browser: the email form would race it, so it goes
  // (unmounting it also drops any typed password) until that attempt ends.
  const pending = (auth.oauthPending ?? null) !== null;
  // Someone who ran out of guest allowance needs an account, not a password.
  const [mode, setMode] = useState<Mode>(initialMode ?? (reason === 'demo-limit' ? 'signup' : 'login'));
  const [email, setEmail] = useState(initialEmail ?? '');
  // App.tsx keeps Account mounted across view
  // switches, so this form no longer remounts each time it becomes visible — the mode
  // computed above only ever ran once, at whatever `reason` happened to be at the FIRST
  // mount. `lastReason` captures that same baseline (already accounted for by the
  // initializer above, or by `initialMode` on the "Check your email" round-trip's own
  // fresh mount) and the mode is re-picked only on an ACTUAL later transition — becoming
  // gated while already open, or the reason changing — never on mount itself, so a
  // manual switch between "Sign in" and "Create an account" is never stomped on.
  const lastReason = useRef(reason);
  useEffect(() => {
    if (reason === lastReason.current) return;
    lastReason.current = reason;
    setMode(reason === 'demo-limit' ? 'signup' : 'login');
  }, [reason]);
  // The password is never React state: a controlled input mirrors every keystroke into
  // the DOM node's `value` attribute (not just its live property), so it would sit in
  // `outerHTML` — what devtools or `QWebEnginePage.toHtml()` see — for as long as it is
  // being typed. An uncontrolled input, read and cleared through this ref, never touches
  // the attribute at all.
  const passwordRef = useRef<HTMLInputElement>(null);
  // A typed, unsubmitted password is the live DOM property, not React
  // state or the `value` attribute — but it survives a trip through Settings (the pane
  // only hides, App.tsx never unmounts it) and an identity change that leaves this form
  // mounted (becoming a guest by another route). Both clear it; the email is kept.
  useEffect(() => {
    if (!visible && passwordRef.current) passwordRef.current.value = '';
  }, [visible]);
  const lastAuthIdentity = useRef(authIdentity);
  useEffect(() => {
    if (authIdentity === lastAuthIdentity.current) return;
    lastAuthIdentity.current = authIdentity;
    if (passwordRef.current) passwordRef.current.value = '';
  }, [authIdentity]);
  const [shownError, setShownError] = useState(true);
  // What the reason banner announces to a screen reader: starts empty so the first (or
  // any later) value is an actual change, not a live region that mounts already full.
  const [announced, setAnnounced] = useState('');
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();
  const visibleError = shownError ? error : null;

  useEffect(() => {
    setAnnounced(reason ? REASON_COPY[reason] : '');
  }, [reason]);

  // Fields are `readOnly`, not `disabled`, while busy, so focus is never dropped to
  // <body> by the field itself — but the submit button, which does disable, can hold
  // focus when busy starts. Once an error lands, put focus back on the password field.
  useEffect(() => {
    if (visibleError) passwordRef.current?.focus();
  }, [visibleError]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setShownError(true);
    const password = passwordRef.current?.value ?? '';
    onSubmit(mode, email.trim(), password);
    // Never lingers past the moment it is sent: not on a failed attempt's re-render, not
    // in React state while the request is in flight, not in the DOM's `value` attribute
    // (cleared on the node itself, an uncontrolled input).
    if (passwordRef.current) passwordRef.current.value = '';
  };

  const signup = mode === 'signup';
  return (
    <>
      {reason ? <p className="t2v-reason">{REASON_COPY[reason]}</p> : null}
      <div className="t2v-sr-only" aria-live="polite" aria-atomic="true">
        {announced}
      </div>
      <div className="t2v-stack">
        {/* tabIndex -1: where focus goes when the provider section it was in goes away. */}
        <h2 className="t2v-view-title" tabIndex={-1}>
          {signup ? 'Create your account' : 'Sign in'}
        </h2>
        <p className="t2v-muted t2v-small">
          {!guest
            ? 'One account for every Talk2View app.'
            : signup
              ? 'You’re chatting as a guest. Creating an account keeps this conversation and lifts the guest limits.'
              : // The controller keeps a guest's conversation when that guest signs in
                // (controller.py `_follow_identity`); the store's post-limit reload needs it.
                'You’re chatting as a guest. Signing in keeps this conversation and lifts the guest limits.'}
        </p>
      </div>
      <ProviderSignIn busy={busy} />
      {pending ? null : (
        <>
          <form className="t2v-form" onSubmit={submit}>
            <div className="t2v-field">
              <label htmlFor={emailId}>Email</label>
              <input
                id={emailId}
                className="t2v-text"
                type="email"
                required
                autoComplete="email"
                value={email}
                readOnly={busy}
                aria-busy={busy}
                aria-describedby={visibleError ? errorId : undefined}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="t2v-field">
              <div className="t2v-label-row">
                <label htmlFor={passwordId}>Password</label>
                {!signup && resetPasswordUrl ? (
                  <button
                    type="button"
                    className="t2v-link t2v-small"
                    onClick={() => store.command({ type: 'openLink', url: resetPasswordUrl })}
                  >
                    Forgot it?
                  </button>
                ) : null}
              </div>
              <input
                id={passwordId}
                ref={passwordRef}
                className="t2v-text"
                type="password"
                required
                minLength={signup ? 8 : undefined}
                autoComplete={signup ? 'new-password' : 'current-password'}
                readOnly={busy}
                aria-busy={busy}
                aria-describedby={visibleError ? errorId : undefined}
              />
              {signup ? <p className="t2v-hint">At least 8 characters.</p> : null}
            </div>
            {visibleError ? (
              <p id={errorId} role="alert" className="t2v-form-error">
                {displayText(visibleError)}
              </p>
            ) : null}
            <button type="submit" className="t2v-btn t2v-btn-primary t2v-btn-block" disabled={busy}>
              {busy ? (signup ? 'Creating account…' : 'Signing in…') : signup ? 'Create account' : 'Sign in'}
            </button>
          </form>
          <p className="t2v-muted t2v-small">
            {signup ? 'Already have an account? ' : 'New to Talk2View? '}
            <button
              type="button"
              className="t2v-link"
              disabled={busy}
              onClick={() => {
                setShownError(false);
                setMode(signup ? 'login' : 'signup');
              }}
            >
              {signup ? 'Sign in' : 'Create an account'}
            </button>
          </p>
        </>
      )}
    </>
  );
}
