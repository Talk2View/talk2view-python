/**
 * The conversation: assistant-ui's thread primitives over the store's messages. The
 * welcome and the openers come from the integrator's `options`; the error card retries
 * with `reload`; "Copy" goes through the host's clipboard (`copy`, only when
 * `capabilities.clipboard`), because navigator.clipboard does not work in a desktop web
 * view.
 */
import { useEffect, useMemo, useRef, useState, type FC } from 'react';
import { ArrowDown, Check, CircleAlert, Copy, FileText, Info, LoaderCircle, RefreshCw } from 'lucide-react';
import {
  ActionBarPrimitive,
  AttachmentPrimitive,
  AuiIf,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  ThreadPrimitive,
  useAui,
  useAuiState,
} from '@assistant-ui/react';
// `MessagePartComponent`: not re-exported from `@assistant-ui/react` (only its higher-level
// wrappers, `Parts`/`GroupedParts`, are) — `@assistant-ui/core/react` is already this
// package's own dependency, and its `./react` subpath is a documented public entry point,
// not a reach into an internal build path. `GroupedParts` (below) supplies no `components`
// prop of its own (0.15.20's real shape is
// `groupBy` + a `children` render function only, confirmed against
// `node_modules/@assistant-ui/core/dist/react/primitives/message/MessageGroupedParts.d.ts`):
// it hands each leaf part to `children` already wrapped in the same per-part context
// `<MessagePrimitive.Parts>` itself uses (`MessagePartChildren` → `PartByIndexProvider`),
// so this shared component — the one thing in the public API that reads that context and
// dispatches on `components` — is how a leaf actually gets drawn, without reimplementing
// the tool-UI/data-renderer registry lookups `MessagePrimitive.Parts` already does.
import { MessagePartComponent } from '@assistant-ui/core/react';
import { isRunning } from '../selectors';
import { resultOf, WATCH_MS } from '../store';
import { Composer } from './Composer';
import { useModel, useStore } from './context';
import { displayText } from './displayText';
import { COPIED_MS, markdownToText, MarkdownText, PlanPart } from './Markdown';
import { ToolCall } from './ToolCall';
import { ToolGroup } from './ToolGroup';

export const DEFAULT_WELCOME_HEADING = 'How can I help you today?';

/** The heading alone (TS welcome.tsx). It is an h2: the frame's title (ui/App.tsx) is the
 * page's h1. The host's own copy, shown as given (`ChatOptions`): a Persian or Indic
 * welcome needs its ZWNJ. */
const WelcomeHeading: FC = () => {
  const { welcome } = useModel().state.options;
  return (
    <div className="t2v-welcome aui-thread-welcome-root">
      <h2 className="t2v-welcome-heading aui-thread-welcome-message-inner">{welcome.heading ?? DEFAULT_WELCOME_HEADING}</h2>
      {/* Raw, not displayText() — the same host-copy trust level as
          the heading above and the caption (footer), not an untrusted string. */}
      {welcome.subheading ? <p className="t2v-welcome-sub">{welcome.subheading}</p> : null}
    </div>
  );
};

/**
 * The openers, under the composer as TS's ThreadSuggestions draws them (vendor/
 * thread.aui.tsx:388-416): shown only while the thread AND the composer are empty. The
 * busy latch is unchanged: an opener answers with a command, not a promise, and a second
 * click inside that window would reach a controller the first has already made busy
 * (the full reasoning is in the git history of this file, "a second click inside that
 * window").
 */
const Openers: FC = () => {
  const store = useStore();
  const model = useModel();
  const { welcome } = model.state.options;
  const running = useAuiState((s) => s.thread.isRunning);
  const composerEmpty = useAuiState((s) => s.composer.isEmpty);
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
  if (welcome.suggestions.length === 0 || !composerEmpty) return null;
  return (
    <div className="t2v-suggestions aui-thread-welcome-suggestions">
      {welcome.suggestions.map((text) => (
        <div key={text} className="t2v-suggestion-row aui-thread-welcome-suggestion-display">
          <button
            type="button"
            className="t2v-suggestion aui-thread-welcome-suggestion"
            disabled={sent !== null || running}
            onClick={() => setSent(store.send(text))}
          >
            <span className="t2v-suggestion-mark" aria-hidden="true">{'>'}</span>
            <span className="t2v-suggestion-text aui-thread-welcome-suggestion-text-1">{text}</span>
          </button>
        </div>
      ))}
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
 * An assistant message with nothing to show yet: the agent's status, or "Working…" as a
 * shimmering line (TS parity), not the old three dots. Rendered for
 * `GroupedParts`'s synthetic `"indicator"` slot, which — unlike the old `Empty` slot on
 * `MessagePrimitive.Parts` — carries no `status` prop to check: `shouldShowIndicator`
 * (MessageGroupedParts.js) only ever emits it while the message is running in the first
 * place, under the same "no-text" default `unstable_showEmptyOnNonTextEnd` used (last part
 * isn't text/reasoning, or there are no parts yet), so the old `status.type !== 'running'`
 * guard is now unreachable dead code and is dropped along with the prop. Also covers a
 * message that is DONE but ends on a tool call or a plan — a turn that calls several tools
 * in a row with no closing reply leaves exactly that (`_reducer.py`'s `finalize` keeps such
 * a segment; it has steps, even with no content) — the indicator does not fire there since
 * the message is not running, so nothing is left showing "Working…" once the turn ends. Not
 * itself a live region — it mounts with its text already in it, so it would announce
 * nothing; `LiveRegion` says these same words.
 */
const Working: FC = () => {
  const agentStatus = useModel().state.agentStatus;
  return (
    <div className="t2v-working">
      <LoaderCircle className="t2v-spin" aria-hidden="true" size={16} />
      <span className="t2v-shimmer">{displayText(agentStatus?.message ?? 'Working…')}</span>
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
  <MessagePrimitive.Root className="t2v-msg t2v-msg-user aui-user-message-root" data-role="user">
    <div className="t2v-user-col">
      <div className="t2v-chips">
        <MessagePrimitive.Attachments components={{ Attachment: AttachmentChip }} />
      </div>
      <div className="t2v-bubble aui-user-message-content">
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
        <CircleAlert className="t2v-error-icon" aria-hidden="true" size={16} />
        <div className="t2v-error-body">
          <ErrorPrimitive.Message className="t2v-error-text">{displayText(error ?? '')}</ErrorPrimitive.Message>
          {isLast ? (
            <div className="t2v-actions">
              <ActionBarPrimitive.Reload className="t2v-btn t2v-btn-outline t2v-btn-sm">Try again</ActionBarPrimitive.Reload>
              <button type="button" className="t2v-btn t2v-btn-ghost t2v-btn-sm" onClick={() => store.command({ type: 'clearError' })}>
                Dismiss
              </button>
            </div>
          ) : null}
        </div>
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

/** Copy (the host's clipboard, with TS's 3 s tick) and, on the newest reply, Refresh. */
const AssistantActions: FC = () => {
  const store = useStore();
  const aui = useAui();
  const { state } = useModel();
  const clipboard = state.capabilities.clipboard;
  // Off while a turn runs, as Refresh is: a send then would only be refused as busy.
  const running = useAuiState((s) => s.thread.isRunning);
  const isLast = useAuiState((s) => s.message.isLast);
  const done = useAuiState((s) => s.message.status?.type === 'complete');
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), COPIED_MS);
    return () => window.clearTimeout(id);
  }, [copied]);
  if (!done) return null;
  return (
    <div className="t2v-msg-actions aui-assistant-action-bar-root">
      {clipboard ? (
        <button
          type="button"
          className="t2v-icon-btn t2v-action-btn"
          aria-label="Copy"
          title="Copy"
          onClick={() => {
            if (copied) return;
            store.command({ type: 'copy', text: aui.message().getCopyText() });
            setCopied(true);
          }}
        >
          {copied ? <Check aria-hidden="true" size={16} /> : <Copy aria-hidden="true" size={16} />}
        </button>
      ) : null}
      {isLast ? (
        <ActionBarPrimitive.Reload className="t2v-icon-btn t2v-action-btn" aria-label="Refresh" title="Refresh">
          <RefreshCw aria-hidden="true" size={16} />
        </ActionBarPrimitive.Reload>
      ) : null}
    </div>
  );
};

// Adjacent tool calls fold into one "N tool calls" line (`ToolGroup`); reasoning
// and a standalone tool call (an MCP-app card, say — this page registers neither today,
// but the key costs nothing to declare and keeps the mapping TS's own) stay ungrouped, an
// empty path being `groupPartByType`'s own way of saying "leave this alone".
const groupBy = groupPartByType({ reasoning: [], 'tool-call': ['group-tool'], 'standalone-tool-call': [] });

const assistantPartComponents = {
  Text: MarkdownText,
  tools: { Fallback: ToolCall },
  data: { by_name: { plan: PlanPart } },
};

const AssistantMessage: FC = () => {
  // `ToolGroup` remounts the moment a lone approved/denied tool call's id switches from
  // the engine's own (pending) to `stepToolCallId`'s derived one (settled) — convert.ts's
  // doing, not this component's (see ToolGroup.tsx's own doc comment) — so its own
  // "opens while live, stays open once settled" state does not survive that. This message
  // never remounts for it, so it is where the memory has to live instead: once any part of
  // it has been running or waiting on the end-user, every `ToolGroup` under it opens on
  // mount from then on, even a freshly-keyed instance.
  const everLiveRef = useRef(false);
  const hasLivePart = useAuiState(
    (s) => s.message.status?.type === 'running' || s.message.parts.some((p) => p.type === 'tool-call' && p.status.type === 'requires-action'),
  );
  if (hasLivePart) everLiveRef.current = true;
  return (
    <MessagePrimitive.Root className="t2v-msg t2v-msg-assistant aui-assistant-message-root" data-role="assistant">
      <div className="aui-assistant-message-content">
        <MessagePrimitive.GroupedParts groupBy={groupBy}>
          {({ part, children }) => {
            switch (part.type) {
              case 'group-tool':
                return (
                  <ToolGroup group={part} everLive={everLiveRef.current}>
                    {children}
                  </ToolGroup>
                );
              case 'indicator':
                return <Working />;
              default:
                return <MessagePartComponent components={assistantPartComponents} />;
            }
          }}
        </MessagePrimitive.GroupedParts>
      </div>
      <ErrorCard />
      <AssistantActions />
    </MessagePrimitive.Root>
  );
};

const Message: FC<{ role: string }> = ({ role }) => (role === 'user' ? <UserMessage /> : <AssistantMessage />);

/** The last command failure, until dismissed. Exported: Settings.tsx
 * reuses it so a refused pick or skill toggle is visible without leaving the view. */
export const Notice: FC = () => {
  const store = useStore();
  const { notice, noticeAction } = useModel();
  if (!notice) return null;
  return (
    <div className="t2v-notice" role="status">
      <Info className="t2v-notice-icon" aria-hidden="true" size={14} />
      {/* A refused command's reason can carry the server's own raw text through as far
          as `default: message || …` (selectors.ts): displayText keeps it plain text
          shown as written, the same rule as everywhere else untrusted text is drawn. */}
      <span className="t2v-notice-text">{displayText(notice)}</span>
      {/* One action (the Skills view's Undo): it runs, and the notice goes. */}
      {noticeAction ? (
        <button
          type="button"
          className="t2v-link t2v-notice-action"
          onClick={() => {
            store.dismissNotice();
            noticeAction.onClick();
          }}
        >
          {displayText(noticeAction.label)}
        </button>
      ) : null}
      <button type="button" className="t2v-link" onClick={() => store.dismissNotice()}>
        Dismiss
      </button>
    </div>
  );
};

export const Thread: FC = () => {
  // An empty thread centres the welcome and the composer as ONE group (thread.aui.tsx
  // `justify-center` while empty; its footer is sticky/`mt-auto` only once there is
  // something to scroll). A state class rather than reading this inline in app.css:
  // Tailwind's `empty:` there is a structural pseudo-class over the DOM, which the page
  // does not have a Chromium-83-safe equivalent for (`:empty` on `.t2v-messages` covers
  // that one case; the viewport's own centring needs the thread's own state instead).
  const empty = useAuiState((s) => s.thread.messages.length === 0);
  const footer = useModel().state.options.footer;
  return (
    <ThreadPrimitive.Root className={'t2v-thread' + (empty ? ' t2v-thread-empty' : '')}>
      <LiveRegion />
      <ThreadPrimitive.Viewport className="t2v-viewport">
        <AuiIf condition={(s) => s.thread.messages.length === 0}>
          <WelcomeHeading />
        </AuiIf>
        <div className="t2v-messages aui-message-group">
          <ThreadPrimitive.Messages>{({ message }) => <Message role={message.role} />}</ThreadPrimitive.Messages>
        </div>
        <ThreadPrimitive.ViewportFooter className="t2v-footer aui-thread-viewport-footer">
          <ThreadPrimitive.ScrollToBottom className="t2v-icon-btn t2v-scroll aui-thread-scroll-to-bottom" aria-label="Scroll to the latest" title="Latest">
            <ArrowDown aria-hidden="true" size={16} />
          </ThreadPrimitive.ScrollToBottom>
          <Notice />
          <Composer />
          {/* The host's own copy, shown as given — raw, not through displayText()
              (plain-text.spec.ts's own "ZWNJ unescaped" check), the same trust level as
              App.tsx's old .t2v-chat-footer this replaces. */}
          {footer ? <p className="t2v-caption">{footer}</p> : null}
          <AuiIf condition={(s) => s.thread.messages.length === 0}>
            <Openers />
          </AuiIf>
        </ThreadPrimitive.ViewportFooter>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};
