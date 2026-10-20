/**
 * The composer: assistant-ui's composer primitives. Enter sends, Shift+Enter adds a line;
 * Stop replaces Send while a turn runs (Stop on a waiting approval denies it, server
 * side). The first keystroke of each identity warms its key (`warmUp`). Files upload the
 * moment they are chosen (runtime.ts); a refused one leaves the composer with its reason
 * as the notice. The attach button is not there when the integrator turned files off.
 */
import { useEffect, useRef, useState, type FC } from 'react';
import { ArrowUp, Paperclip, Square, X } from 'lucide-react';
import { AttachmentPrimitive, AuiIf, ComposerPrimitive, useAui, useAuiEvent, useAuiState } from '@assistant-ui/react';
import { MAX_ATTACHMENTS } from '../runtime';
import { authIdentityKey, chipBlocksSend, gate, uploadFailureText } from '../selectors';
import { displayText } from './displayText';
import { useModel, useStore } from './context';

/**
 * A chip's own name is rendered from the attachment state directly, not
 * `<AttachmentPrimitive.Name />` (a fixed component with no children/asChild support for
 * this case): a file's name is chosen by whoever picked the file, so it is untrusted text
 * the same way a tool argument is (displayText.ts) — a bidi override in it must show as a
 * visible `\uXXXX` escape, never render raw and turn a "*.exe" into what looks like a
 * "*.png". The Remove button's own name carries the same escaped text, so two chips never
 * share one accessible name.
 */
const ComposerChip: FC = () => {
  const status = useAuiState((s) => s.attachment.status.type);
  const name = useAuiState((s) => s.attachment.name);
  const shown = displayText(name);
  return (
    <AttachmentPrimitive.Root className="t2v-chip" data-status={status}>
      <span className="t2v-chip-name">{shown}</span>
      {status === 'running' ? <span className="t2v-chip-status">Uploading…</span> : null}
      <AttachmentPrimitive.Remove className="t2v-chip-remove" aria-label={'Remove ' + shown} title={'Remove ' + shown}>
        <X aria-hidden="true" size={12} />
      </AttachmentPrimitive.Remove>
    </AttachmentPrimitive.Root>
  );
};

/**
 * What a screen reader hears about an upload starting and finishing: a chip's own status
 * change, not the thread's turn status (Thread.tsx's `LiveRegion`), and not a second
 * `role="status"` competing with the reconnect banner's (App.tsx) — an earlier review
 * flagged exactly that collision, fixed there by a plain `aria-live` region that starts
 * empty; this is the same pattern, scoped to the composer instead. Only the most recent
 * transition is kept: a multi-file pick announces its last file's state, which is a
 * reasonable simplification, not silence.
 *
 * Three states tracked, not two: a chip going 'running' → 'incomplete'
 * (refused) used to read the SAME as 'running' → 'requires-action' (a real "no longer
 * running" test on both), so a refusal was announced as "Attached X." — worse than
 * silence. A chip that vanishes while still 'running' (Remove clicked, mid-upload) is a
 * deliberate action the end-user just took: left unannounced on purpose, not stale text.
 */
function useAttachmentAnnouncement(): string {
  const attachments = useAuiState((s) => s.composer.attachments);
  const [text, setText] = useState('');
  const known = useRef(new Map<string, 'running' | 'ready' | 'incomplete'>());
  useEffect(() => {
    const seen = new Set<string>();
    let next: string | null = null;
    for (const a of attachments) {
      seen.add(a.id);
      const was = known.current.get(a.id);
      const now = a.status.type === 'running' ? 'running' : a.status.type === 'incomplete' ? 'incomplete' : 'ready';
      if (now !== was) {
        known.current.set(a.id, now);
        if (now === 'running') next = 'Uploading ' + displayText(a.name) + '…';
        else if (now === 'ready') next = 'Attached ' + displayText(a.name) + '.';
        else next = displayText(a.name) + ' could not be attached.';
      }
    }
    for (const id of known.current.keys()) if (!seen.has(id)) known.current.delete(id);
    if (next !== null) setText(next);
  }, [attachments]);
  return text;
}

/** How long refusals that land apart still add up to one notice. */
const REFUSAL_WINDOW_MS = 5000;

export const Composer: FC = () => {
  const store = useStore();
  const model = useModel();
  const aui = useAui();
  const attachmentsOn = model.state.options.features.attachments;
  const text = useAuiState((s) => s.composer.text);
  const attachments = useAuiState((s) => s.composer.attachments);
  const attachmentCount = attachments.length;
  // canSend does not itself check an attachment's own status (assistant-ui core), so
  // without this a chip that is still uploading — or one left over `incomplete` after a
  // refusal (see the cleanup effect below) — lets Send/Enter through: composer.send()
  // then throws inside its own attachment resolution ("Attach the file again.") with no
  // notice shown and nothing posted. Guarded twice: the button's own `disabled` (a
  // click), and preventing the form's submit event (Enter, which goes through
  // `requestSubmit()` — assistant-ui's own submit handler is only ever reached when the
  // event was not already prevented, `composeEventHandlers` in `ComposerRoot.tsx`).
  const notReady = attachments.some((a) => chipBlocksSend(a.status.type));
  const announcement = useAttachmentAnnouncement();

  // Refusals that land apart — a second pick while the first one's notice is still up —
  // add up instead of the later one replacing the earlier: within REFUSAL_WINDOW_MS,
  // and only while that notice is still the one on show (dismissed, replaced, or cleared
  // by an identity change, it starts afresh). One reason reads as itself; several read as
  // a count, the same copy one multi-file pick already uses.
  const refused = useRef<{ at: number; notice: string; reasons: string[] }>({ at: 0, notice: '', reasons: [] });
  const refuse = (reasons: string[]) => {
    const now = Date.now();
    const last = refused.current;
    const addUp = now - last.at <= REFUSAL_WINDOW_MS && store.latest().notice === last.notice;
    const all = addUp ? last.reasons.concat(reasons) : reasons;
    const notice = new Set(all).size === 1 ? all[0]! : String(all.length) + ' files could not be attached.';
    refused.current = { at: now, notice, reasons: all };
    store.showNotice(notice);
  };

  // The host turned files off mid-session: a chip still in the composer would go
  // out with the next message, which the host no longer takes. runtime.ts stops the
  // uploads and frees the slots; this drops the chips.
  const hadAttachmentsOn = useRef(attachmentsOn);
  useEffect(() => {
    if (hadAttachmentsOn.current && !attachmentsOn) void aui.composer().clearAttachments();
    hadAttachmentsOn.current = attachmentsOn;
  }, [attachmentsOn, aui]);

  useEffect(() => {
    if (text) store.typed();
  }, [text, store]);

  // A file picked by one identity must never be sent by the next, and neither should a
  // typed draft (ana types a message, signs out, bob signs in and reads it) — but
  // `reset()` (text, attachments and any add() still in flight, together) must fire only
  // when there is something to protect. Signed-out → guest (the very first message as a
  // new guest, `store.ts`'s own `needsGuestStart`) is an identity change too, and what
  // was typed or attached signed out belongs to that very guest: an attach while signed
  // out starts the guest first and waits for it (`store.ts`'s `beforeUpload`). So
  // gating on whether the PREVIOUS identity had a user id (a guest has one; signed-out
  // does not) keeps a fast-typed follow-up right after the guest starts, AND the chip
  // whose upload started that guest, instead of a reset() firing on the very transition
  // that made them legitimate. The very first render is not a "change" (nothing to
  // protect on mount either).
  const auth = model.state.auth;
  const identityKey = authIdentityKey(auth);
  const lastIdentityKey = useRef(identityKey);
  const lastUserId = useRef(auth.user?.id ?? null);
  useEffect(() => {
    if (identityKey === lastIdentityKey.current) return;
    const previousHadUserId = lastUserId.current !== null;
    lastIdentityKey.current = identityKey;
    lastUserId.current = auth.user?.id ?? null;
    if (previousHadUserId) void aui.composer().reset();
  }, [identityKey, auth.user?.id, aui]);

  // New chat clears a picked-but-unsent FILE (it belonged to the conversation that just
  // ended), but not a draft someone is already typing for the next one: `newSession`'s
  // own command can be slow, and a fast typist would otherwise see their new message
  // vanish the moment its result lands (a regression once — the New chat
  // BUTTON already resets text too, but that happens at CLICK time, before anything new
  // could have been typed). `clearAttachments()` only, not `reset()`. This state-based
  // watcher exists at all because a host-driven new session (Slicer's own Python calling
  // the ChatController directly) produces the exact same wire effect — `messages` back
  // to `[]` — with no click for App.tsx to hook; it does NOT fire for a clear on an
  // already-empty, thread-less conversation (attach, then a host-driven clear before
  // ever sending), since nothing here observably changes for that case — the button's
  // own handler is what covers the click-driven version of that one.
  const hadMessages = useRef(model.state.messages.length > 0);
  useEffect(() => {
    const has = model.state.messages.length > 0;
    if (!has && hadMessages.current) void aui.composer().clearAttachments();
    hadMessages.current = has;
  }, [model.state.messages.length, aui]);

  // A chip left `incomplete` (a refusal after at least one file had already started
  // uploading — `runtime.ts`'s `add()` throws once `store.upload()` settles) is cleaned
  // up here, not from the `attachmentAddError` EVENT: several refusals landing together
  // (a multi-file pick, all rejected) raced with each other at event time and sometimes
  // left one behind, still drawn as though it were ready to send.
  // Driven by the composer's own attachment list instead, which is reliable
  // regardless of how many refusals arrive at once or in what order. One notice for the
  // whole batch — each distinct reason shown once, not stacked — rather than whichever
  // single refusal happened to set it last. Skipped once the chat is already over: the
  // banner (App.tsx) says enough, the same way a fatal command's refusal shows no
  // separate notice either (`store.ts`'s `command()`). Checked via `store.latest()`, not
  // `model.conn.phase`: a fatal upload refusal ends the page from INSIDE the same
  // `store.upload()` promise `add()` awaits (`store.ts`), and `store.latest()` reflects
  // that the instant it happens — `model` only catches up once React's throttled publish
  // runs, which can lose the race against this effect and let the notice through anyway.
  useEffect(() => {
    const bad = attachments.filter((a): a is typeof a & { status: { type: 'incomplete'; message?: string } } => a.status.type === 'incomplete');
    if (bad.length === 0) return;
    if (store.latest().conn.phase !== 'over') refuse(bad.map((a) => a.status.message || 'The file could not be attached.'));
    for (const a of bad) void aui.composer().attachment({ id: a.id }).remove().catch(() => undefined);
  }, [attachments, store, aui]);

  // A refusal BEFORE any chip ever existed: assistant-ui itself refuses a wrong type or
  // a missing adapter before ours ever runs (`accept`, checked in composer core), so
  // there is no `incomplete` chip for the effect above to find, and `event.message`
  // there is assistant-ui's own developer string, not one of `uploadFailureText`'s — map
  // the two reasons that can reach here that way to the page's own copy. The over-cap
  // refusal (`runtime.ts`'s `add()`, over `MAX_ATTACHMENTS`) is a THIRD case that never
  // creates a chip either — it throws before its first `yield` — so it is also only ever
  // seen here, as an `adapter-error` with no `attachmentId`; `event.message` there is
  // already our own composed text. An `adapter-error` WITH an `attachmentId` means a
  // chip DID exist (our own `add()` threw only after `store.upload()` settled): left to
  // the effect above, which reads the chip's own status directly rather than racing
  // several simultaneous events the way showing the notice from here too once did.
  useAuiEvent('composer.attachmentAddError', (event) => {
    if (store.latest().conn.phase === 'over') return;
    if (event.reason === 'not-accepted') refuse([uploadFailureText('unsupported_media_type', '')]);
    else if (event.reason === 'no-adapter') refuse([uploadFailureText('attachments_disabled', '')]);
    else if (event.attachmentId === undefined) refuse([event.message]);
  });

  const gated = gate(model.state).gated || model.conn.phase === 'over';
  const atCap = attachmentCount >= MAX_ATTACHMENTS;
  const attachDisabled = gated || atCap;
  const attachTitle = gated ? 'Attach a file' : atCap ? 'You can attach up to ' + String(MAX_ATTACHMENTS) + ' files.' : 'Attach a file';

  return (
    <ComposerPrimitive.Root className="t2v-composer" onSubmit={(e) => (notReady ? e.preventDefault() : undefined)}>
      <div className="t2v-sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>
      {attachmentsOn ? (
        <div className="t2v-chips">
          <ComposerPrimitive.Attachments components={{ Attachment: ComposerChip }} />
        </div>
      ) : null}
      <div className="t2v-composer-row">
        {attachmentsOn ? (
          <ComposerPrimitive.AddAttachment
            className="t2v-icon-btn t2v-attach"
            aria-label="Attach a file"
            title={attachTitle}
            disabled={attachDisabled}
          >
            <Paperclip aria-hidden="true" size={16} />
          </ComposerPrimitive.AddAttachment>
        ) : null}
        <ComposerPrimitive.Input
          className="t2v-input"
          placeholder="Message (Enter sends, Shift+Enter adds a line)"
          aria-label="Message"
          minRows={1}
          maxRows={6}
          autoFocus
        />
        <AuiIf condition={(s) => !s.thread.isRunning}>
          <ComposerPrimitive.Send
            className="t2v-send"
            aria-label="Send"
            title={notReady ? 'Wait for every attachment to finish uploading.' : 'Send'}
            disabled={notReady}
          >
            <ArrowUp aria-hidden="true" size={16} />
          </ComposerPrimitive.Send>
        </AuiIf>
        <AuiIf condition={(s) => s.thread.isRunning}>
          <ComposerPrimitive.Cancel className="t2v-send t2v-stop" aria-label="Stop" title="Stop">
            <Square aria-hidden="true" size={14} />
          </ComposerPrimitive.Cancel>
        </AuiIf>
      </div>
    </ComposerPrimitive.Root>
  );
};
