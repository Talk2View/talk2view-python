/**
 * The page as an assistant-ui runtime: `useExternalStoreRuntime` over the store (the
 * page is a dumb view). Messages go through the TS SDK's own convert.ts; every
 * callback becomes a command. Approval cards answer with `approve` themselves (they carry
 * edited arguments, which assistant-ui's approval seam cannot), and New chat is a plain
 * `newSession` button (ui/App.tsx), so neither goes through the runtime.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react';
import { useExternalStoreRuntime } from '@assistant-ui/react';
import type {
  AppendMessage,
  AssistantRuntime,
  AttachmentAdapter,
  CompleteAttachment,
  ExternalStoreAdapter,
  PendingAttachment,
  ThreadMessageLike,
} from '@assistant-ui/react';
import { toThreadMessage } from '@sdk/assistant-ui/convert';
import { ATTACHMENT_ACCEPT } from '@sdk/constants';
import type { Attachment } from '@sdk/types';
import {
  gate,
  isRunning,
  lastUserId,
  threadMessages,
  toDisplayMessage,
  toPendingApproval,
  uploadFailureText,
} from './selectors';
import type { PageModel, PageStore } from './store';
import type { WireMessage } from './wire';

let uploads = 0;

/** Mirrors the server's own cap (`ui/wire.py`'s `MAX_ATTACHMENTS`): `send` refuses more
 * than this many ids, and refuses the WHOLE message — text included — when it does, so
 * the page must never let an 11th chip reach the composer at all (Composer.tsx disables
 * the attach button at the cap; `add()` below refuses over it too, since a multi-file
 * pick or a paste can hand several files to the adapter faster than a render). */
export const MAX_ATTACHMENTS = 10;

/** An upload that could not start the guest it needs (`PageStore.beforeUpload`). */
export const GUEST_FIRST = 'Send a message first, or sign in, to attach files.';

/**
 * Files upload the moment they are chosen, so a refusal shows while the end-user is
 * still composing (Composer.tsx). `uploaded` maps the composer's own
 * placeholder id (never sent to the server) to the server's `Attachment`, so `send`
 * below can hand assistant-ui the real id — the one `store.send`'s `attachmentIds` and
 * the engine both need.
 *
 * `admitted` counts every placeholder id currently occupying a chip slot — running or
 * ready, freed only by `remove()` — which is what the cap above is checked against;
 * `uploaded.size` alone would undercount a batch still mid-upload (a multi-file pick
 * admits all of them, synchronously, before any of their uploads resolve). `controllers`
 * lets `remove()` actually stop the network request, not just the local bookkeeping: an
 * upload nobody is waiting for any more (removed, or the identity/session it was for is
 * gone) should not keep running to completion regardless.
 */
function useAttachmentAdapter(store: PageStore, on: boolean): AttachmentAdapter {
  const uploaded = useRef(new Map<string, Attachment>());
  const admitted = useRef(new Set<string>());
  const controllers = useRef(new Map<string, AbortController>());
  // The host turned files off mid-session: every upload still running stops and
  // every slot is freed. Composer.tsx drops the chips; with the adapter already gone from
  // the runtime, assistant-ui's clearAttachments cannot call `remove` here to do it.
  useEffect(() => {
    if (on) return;
    controllers.current.forEach((controller) => controller.abort());
    controllers.current.clear();
    uploaded.current.clear();
    admitted.current.clear();
  }, [on]);
  return useMemo<AttachmentAdapter>(
    () => ({
      accept: ATTACHMENT_ACCEPT,
      async *add({ file }): AsyncGenerator<PendingAttachment, void> {
        if (admitted.current.size >= MAX_ATTACHMENTS) {
          throw new Error('You can attach up to ' + String(MAX_ATTACHMENTS) + ' files.');
        }
        const id = 'upload-' + String(++uploads);
        admitted.current.add(id);
        const base = {
          id,
          type: file.type.startsWith('image/') ? ('image' as const) : ('document' as const),
          name: file.name,
          contentType: file.type,
          file,
        };
        yield { ...base, status: { type: 'running', reason: 'uploading', progress: 0 } };
        const controller = new AbortController();
        controllers.current.set(id, controller);
        // Signed out, guests allowed: the guest starts first, as a send's does, so the file
        // is filed under the identity that sends it. A chip removed meanwhile has aborted
        // `controller`, and the upload below then fails at once.
        if (!(await store.beforeUpload())) {
          controllers.current.delete(id);
          throw new Error(GUEST_FIRST);
        }
        const result = await store.upload(file, controller.signal);
        controllers.current.delete(id);
        if (!result.ok) throw new Error(uploadFailureText(result.type, result.message));
        uploaded.current.set(id, result.attachment);
        yield { ...base, status: { type: 'requires-action', reason: 'composer-send' } };
      },
      async send(pending): Promise<CompleteAttachment> {
        const stored = uploaded.current.get(pending.id);
        if (!stored) throw new Error('Attach the file again.');
        // A sent chip frees its own slot too: `remove()` is the only other place that
        // does, and assistant-ui never calls it for a chip that was SENT (only for one
        // still in the composer when it is explicitly removed, or on clearAttachments()/
        // reset()) — without this, `admitted` only ever grows across a conversation, and
        // the cap silently locks out every future attach once ten files have been sent
        // in total, not ten at once.
        uploaded.current.delete(pending.id);
        admitted.current.delete(pending.id);
        return {
          id: stored.id,
          type: pending.type,
          name: stored.filename,
          contentType: stored.mime_type,
          status: { type: 'complete' },
          content: [],
        };
      },
      async remove(attachment) {
        uploaded.current.delete(attachment.id);
        admitted.current.delete(attachment.id);
        controllers.current.get(attachment.id)?.abort();
        controllers.current.delete(attachment.id);
      },
    }),
    [store],
  );
}

export function useEmbedRuntime(store: PageStore, model: PageModel): AssistantRuntime {
  const s = model.state;
  const messages = useMemo(() => threadMessages(s), [s]);
  const pendingApproval = useMemo(() => toPendingApproval(s.pendingApproval), [s.pendingApproval]);
  const error = s.error;
  const running = isRunning(s);
  const disabled = gate(s).gated || model.conn.phase === 'over';
  const attachmentsOn = s.options.features.attachments;
  const attachments = useAttachmentAdapter(store, attachmentsOn);

  const convertMessage = useCallback(
    (m: WireMessage, index: number): ThreadMessageLike => {
      try {
        const display = toDisplayMessage(m);
        return toThreadMessage(display, { isLast: index === messages.length - 1, pendingApproval, error });
      } catch (err) {
        // convert.ts (vendored, never
        // edited locally — see its own header) reads every stored step's `status` and
        // `name` unguarded. The SDK now rejects a malformed one before it reaches a
        // stored snapshot (`talk2view.state.valid_tool_step_dict`, wired into
        // `conversations.py` and `controller.py`), but a LIVE turn's steps have no such
        // validator in front of them, and `useExternalStoreRuntime` calls this from
        // `useEmbedRuntime`, above `<Shell>` in `ui/App.tsx` — no boundary around
        // `<Thread>` alone ever sees this throw (React error boundaries only catch a
        // DESCENDANT's render, never an ancestor's). Caught here, one message at a
        // time, so one bad message shows its own row instead of taking the whole
        // runtime — and so the whole page — down with it.
        //
        // `toDisplayMessage(m)` is inside this try too, not just
        // `toThreadMessage` — its own `m.steps.map(toStep)` (selectors.ts) throws just as
        // hard on a live `steps` that is truthy but not a list at all (only a buggy
        // foreign server would ever send one; this SDK's own reducer only builds lists
        // and the store rejects a non-list on read). Since `toDisplayMessage` may be what
        // threw, the fallback below is built from the raw wire message `m`, never from
        // its (possibly never-produced) `DisplayMessage`.
        console.error('[t2v] a message could not be shown:', err);
        const createdAt = new Date(m.timestamp);
        return {
          id: m.id,
          role: m.role,
          createdAt: Number.isNaN(createdAt.getTime()) ? new Date(0) : createdAt,
          content: [{ type: 'text', text: 'This message could not be shown.' }],
          ...(m.role === 'assistant' ? { status: { type: 'complete', reason: 'stop' } as const } : {}),
        };
      }
    },
    [messages.length, pendingApproval, error],
  );

  const onNew = useCallback(
    async (message: AppendMessage) => {
      const text = message.content
        .filter((p): p is { type: 'text'; text: string } => p.type === 'text')
        .map((p) => p.text)
        .join('\n');
      store.send(text, (message.attachments ?? []).map((a) => a.id));
    },
    [store],
  );

  // The engine can only regenerate the latest turn; reload of an older reply is a no-op.
  const onReload = useCallback(
    async (parentId: string | null) => {
      if (parentId !== null && parentId === lastUserId(store.latest().state)) store.command({ type: 'reload' });
    },
    [store],
  );

  // Stop, or deny the waiting approval with "Cancelled by the user" (the server decides).
  const onCancel = useCallback(async () => {
    store.command({ type: 'cancel' });
  }, [store]);

  const adapter = useMemo<ExternalStoreAdapter<WireMessage>>(
    () => ({
      messages,
      isRunning: running,
      isDisabled: disabled,
      convertMessage,
      onNew,
      onReload,
      onCancel,
      adapters: attachmentsOn ? { attachments } : {},
      // The host's clipboard, not navigator.clipboard: see ui/Thread.tsx.
      unstable_capabilities: { copy: false },
    }),
    [messages, running, disabled, convertMessage, onNew, onReload, onCancel, attachmentsOn, attachments],
  );
  return useExternalStoreRuntime(adapter);
}
