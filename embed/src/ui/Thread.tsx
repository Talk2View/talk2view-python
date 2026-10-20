/**
 * The conversation: assistant-ui's thread primitives over the store's messages. The
 * welcome and the openers come from the integrator's `options`; the error card retries
 * with `reload`; "Copy" goes through the host's clipboard (`copy`, only when
 * `capabilities.clipboard`), because navigator.clipboard does not work in a desktop web
 * view.
 */
import { useEffect, useMemo, useState, type FC } from 'react';
import { ArrowDown, Copy, FileText, RotateCcw } from 'lucide-react';
import {
  ActionBarPrimitive,
  AttachmentPrimitive,
  AuiIf,
  ErrorPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
} from '@assistant-ui/react';
import { isRunning } from '../selectors';
import { resultOf, WATCH_MS } from '../store';
import { Composer } from './Composer';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import { markdownToText, MarkdownText, PlanPart } from './Markdown';
import { ToolCall } from './ToolCall';

export const DEFAULT_WELCOME_HEADING = 'How can I help you today?';

/** The heading is an h2: the frame's own title (ui/App.tsx) is the page's h1. The heading
 * and the suggestions are the host's own copy (`ChatOptions`), shown as given: they come
 * from the party that serves the page, and a Persian or Indic welcome needs its ZWNJ. */
const Welcome: FC = () => {
  const store = useStore();
  const model = useModel();
  const { welcome } = model.state.options;
  const running = useAuiState((s) => s.thread.isRunning);
  // An opener answers the click with a command, not a promise, so nothing about the page
  // changes until the server's first op comes back. A second click inside that window
  // reaches a controller the first has already made busy, and its refusal — "The assistant
  // is still working on the last message." — would be the first thing the end-user ever
  // sees of the chat. `running` covers the openers that come back after a New chat, whose
  // turn is still being cleared. `sent` holds the clicked command's own id, so it can be
  // let go again the moment that command is answered — its result OR its refusal, both of
  // which reach `resultOf` (an HTTP refusal is recorded there too). Watching the notice
  // LINE instead (a past bug) let a stale notice from an earlier refusal — still showing,
  // unrelated to this click — free the latch the instant it was set, before this send's
  // own answer had arrived, so a double click could post two real sends. An answer that
  // never comes frees the latch after WATCH_MS, with the notice "That took too long. Try
  // again.", as every other busy latch does.
  const [sent, setSent] = useState<string | null>(null);
  const result = resultOf(model, sent);
  useEffect(() => {
    if (sent !== null && result !== null) setSent(null);
  }, [sent, result]);
  const waiting = sent !== null && result === null;
  useEffect(() => {
    if (!waiting) return;
    const id = window.setTimeout(() => {
      setSent(null);
      store.showNotice('That took too long. Try again.');
    }, WATCH_MS);
    return () => window.clearTimeout(id);
  }, [waiting, sent, store]);
  const send = (text: string): void => {
    setSent(store.send(text));
  };
  return (
    <div className="t2v-welcome">
      <h2 className="t2v-welcome-heading">{welcome.heading ?? DEFAULT_WELCOME_HEADING}</h2>
      {welcome.suggestions.length > 0 ? (
        <div className="t2v-suggestions">
          {welcome.suggestions.map((text) => (
            <button
              key={text}
              type="button"
              className="t2v-suggestion"
              disabled={sent !== null || running}
              onClick={() => send(text)}
            >
              {text}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
};

/**
 * What a screen reader hears, and the only part of the thread that announces itself. It
 * is on the page from the first paint and empty, because a live region reports what
 * changes while it is there: one that mounts with its words already in it says nothing.
 * It carries what the agent is doing while a turn runs, and the reply once one has ended.
 *
 * A turn waiting on the approval card is a decision only the end-user can make, not
 * ordinary work in progress: saying "Working…" there would leave a screen reader user
 * thinking nothing needs them. `isRunning` folds the wait into "running" (a turn waiting
 * on an approval is still in progress, for Stop's sake), so this checks `pendingApproval`
 * ahead of it and announces the ask instead.
 */
const LiveRegion: FC = () => {
  const { state } = useModel();
  const running = isRunning(state);
  const waiting = state.pendingApproval;
  const last = state.messages[state.messages.length - 1];
  const reply = !running && last && last.role === 'assistant' ? last.content : '';
  const said = useMemo(() => (reply ? markdownToText(reply) : ''), [reply]);
  return (
    <div className="t2v-sr-only" aria-live="polite" aria-atomic="true">
      {waiting
        ? `The assistant wants to run ${displayText(waiting.toolName)} and is waiting for your permission.`
        : running
          ? displayText(state.agentStatus?.message ?? 'Working…')
          : said}
    </div>
  );
};

/**
 * An assistant message with nothing in it yet: the agent's status, or "Working…". It is
 * not itself a live region — it mounts with its text already in it, so it would announce
 * nothing; `LiveRegion` says these same words.
 */
const Working: FC = () => {
  const status = useModel().state.agentStatus;
  return (
    <div className="t2v-working">
      <span className="t2v-dots" aria-hidden="true">
        <i />
        <i />
        <i />
      </span>
      <span>{displayText(status?.message ?? 'Working…')}</span>
    </div>
  );
};

// The name is read from the attachment state and escaped through `displayText`, not
// `<AttachmentPrimitive.Name />` (a fixed component that always renders it raw): whoever
// attached the file chose its name, so it is untrusted text the same way a tool argument
// is (ToolCall.tsx) — a bidi override in it must show as a visible escape, not flip a
// "*.exe" into what reads like a "*.png" (Composer.tsx's own chip, same rule).
const AttachmentChip: FC = () => {
  const name = useAuiState((s) => s.attachment.name);
  return (
    <AttachmentPrimitive.Root className="t2v-chip">
      <FileText aria-hidden="true" size={14} />
      <span className="t2v-chip-name">{displayText(name)}</span>
    </AttachmentPrimitive.Root>
  );
};

const UserMessage: FC = () => (
  <MessagePrimitive.Root className="t2v-msg t2v-msg-user">
    <div className="t2v-user-col">
      <div className="t2v-chips">
        <MessagePrimitive.Attachments components={{ Attachment: AttachmentChip }} />
      </div>
      <div className="t2v-bubble">
        <MessagePrimitive.Parts />
      </div>
    </div>
  </MessagePrimitive.Root>
);

/** The failed turn's card: try it again, or put the error away. The text is the SDK's
 * `error`, which convert.ts puts on the last message, shown through `displayText`. */
const ErrorCard: FC = () => {
  const store = useStore();
  const error = useModel().state.error;
  const isLast = useAuiState((s) => s.message.isLast);
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root className="t2v-error" role="alert">
        <ErrorPrimitive.Message className="t2v-error-text">{displayText(error ?? '')}</ErrorPrimitive.Message>
        {isLast ? (
          <div className="t2v-actions">
            <ActionBarPrimitive.Reload className="t2v-btn t2v-btn-primary">Try again</ActionBarPrimitive.Reload>
            <button type="button" className="t2v-btn t2v-btn-ghost" onClick={() => store.command({ type: 'clearError' })}>
              Dismiss
            </button>
          </div>
        ) : null}
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

/** Copy (the host's clipboard) and, on the newest reply, Retry. */
const AssistantActions: FC = () => {
  const store = useStore();
  const aui = useAui();
  const clipboard = useModel().state.capabilities.clipboard;
  const isLast = useAuiState((s) => s.message.isLast);
  const done = useAuiState((s) => s.message.status?.type === 'complete');
  if (!done) return null;
  return (
    <div className="t2v-msg-actions">
      {clipboard ? (
        <button
          type="button"
          className="t2v-icon-btn"
          aria-label="Copy"
          title="Copy"
          onClick={() => store.command({ type: 'copy', text: aui.message().getCopyText() })}
        >
          <Copy aria-hidden="true" size={14} />
        </button>
      ) : null}
      {isLast ? (
        <ActionBarPrimitive.Reload className="t2v-icon-btn" aria-label="Retry" title="Retry">
          <RotateCcw aria-hidden="true" size={14} />
        </ActionBarPrimitive.Reload>
      ) : null}
    </div>
  );
};

const AssistantMessage: FC = () => (
  <MessagePrimitive.Root className="t2v-msg t2v-msg-assistant">
    <MessagePrimitive.Parts
      components={{
        Text: MarkdownText,
        Empty: Working,
        tools: { Fallback: ToolCall },
        data: { by_name: { plan: PlanPart } },
      }}
    />
    <ErrorCard />
    <AssistantActions />
  </MessagePrimitive.Root>
);

const Message: FC<{ role: string }> = ({ role }) => (role === 'user' ? <UserMessage /> : <AssistantMessage />);

/** The last command failure, until dismissed. Exported: Settings.tsx
 * reuses it so a refused pick or skill toggle is visible without leaving the view. */
export const Notice: FC = () => {
  const store = useStore();
  const notice = useModel().notice;
  if (!notice) return null;
  return (
    <div className="t2v-notice" role="status">
      {/* A refused command's reason can carry the server's own raw text through as far
          as `default: message || …` (selectors.ts): displayText keeps it plain text
          shown as written, the same rule as everywhere else untrusted text is drawn. */}
      <span className="t2v-notice-text">{displayText(notice)}</span>
      <button type="button" className="t2v-link" onClick={() => store.dismissNotice()}>
        Dismiss
      </button>
    </div>
  );
};

export const Thread: FC = () => {
  const footer = useModel().state.options.footer;
  return (
    <ThreadPrimitive.Root className="t2v-thread">
      <LiveRegion />
      <ThreadPrimitive.Viewport className="t2v-viewport">
        <AuiIf condition={(s) => s.thread.messages.length === 0}>
          <Welcome />
        </AuiIf>
        <ThreadPrimitive.Messages>{({ message }) => <Message role={message.role} />}</ThreadPrimitive.Messages>
        <ThreadPrimitive.ViewportFooter className="t2v-footer">
          <ThreadPrimitive.ScrollToBottom className="t2v-icon-btn t2v-scroll" aria-label="Scroll to the latest" title="Latest">
            <ArrowDown aria-hidden="true" size={16} />
          </ThreadPrimitive.ScrollToBottom>
          <Notice />
          <Composer />
          {/* The host's own copy, shown as given (see Welcome). */}
          {footer ? <p className="t2v-footnote">{footer}</p> : null}
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};
