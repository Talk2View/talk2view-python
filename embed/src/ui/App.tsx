/**
 * The frame (TS chat/chat.tsx): a header of one-tap views over a thread that never
 * unmounts, so its conversation, scroll position and a half-typed message survive a trip
 * through Settings, Skills or Account. Account never unmounts either: visiting Settings
 * and coming back keeps the email of a half-typed
 * sign-in, the same reason Thread stays mounted (its password is cleared whenever the pane
 * hides, Account.tsx). Settings and Skills themselves DO unmount on close — Skills' own
 * effect re-asks the skills list each time it opens (Skills.tsx), which staying mounted
 * would break. Behind the guest gate the thread is replaced by Account. An approval
 * arriving while Settings, Skills or the thread list is open brings the thread back
 * (Account is left alone: it may hold that half-typed sign-in). A view change moves focus
 * to the page's own title, which
 * already names the view, so it is never left on a button the view switch just hid.
 * Nothing shows until the first snapshot: the page never flashes an empty chat
 * that is about to fill.
 */
import { useEffect, useMemo, useRef, useState, type FC, type RefObject } from 'react';
import { BookOpen, History, Plus, Settings, UserRound } from 'lucide-react';
import { AssistantRuntimeProvider, useAui } from '@assistant-ui/react';
import { useEmbedRuntime } from '../runtime';
import { gate, threadTitle } from '../selectors';
import type { PageStore } from '../store';
import { AccountView } from './Account';
import { OpenSkillsContext, StoreContext, useModel, useStore } from './context';
import { displayText } from './displayText';
import { SettingsView } from './Settings';
import { SkillsView } from './Skills';
import { applyTheme } from './theme';
import { Thread } from './Thread';
import { ThreadErrorBoundary } from './ThreadErrorBoundary';
import { ThreadListView } from './ThreadList';

export type View = 'thread' | 'list' | 'settings' | 'skills' | 'account';

/** TS chat.tsx VIEW_TITLES: an end-user starts a chat, not a thread. */
const TITLES: Record<View, string> = { thread: 'New chat', list: 'Chats', settings: 'Settings', skills: 'Skills', account: 'Account' };

/** The page's h1; the welcome's heading (ui/Thread.tsx) is the h2 under it. `titleRef`
 * (`tabIndex={-1}`) is what a view change focuses: it already renders the
 * new view's name, so no separate per-view heading is needed just to land focus on. */
const Header: FC<{ view: View; onView: (view: View) => void; titleRef: RefObject<HTMLHeadingElement | null>; onNew: () => void }> = ({
  view,
  onView,
  titleRef,
  onNew,
}) => {
  const { state, conn } = useModel();
  const { features } = state.options;
  const signedIn = state.auth.isAuthenticated && !state.auth.isAnonymous;
  const toggle = (target: View) => () => onView(view === target ? 'thread' : target);
  const hasThreadList = features.threadList && state.threads !== undefined;
  return (
    <header className="t2v-chat-header aui-modal-header">
      <h1 ref={titleRef} tabIndex={-1} className="t2v-title aui-modal-title">
        {view === 'thread' ? displayText(threadTitle(state)) : TITLES[view]}
      </h1>
      <div className="t2v-header-actions">
        {features.account ? (
          <button
            type="button"
            className="t2v-icon-btn t2v-header-btn"
            aria-label="Account"
            title={view === 'account' ? 'Back to chat' : 'Account'}
            aria-pressed={view === 'account'}
            onClick={toggle('account')}
          >
            <UserRound aria-hidden="true" size={16} />
            {signedIn ? <span className="t2v-dot" aria-hidden="true" /> : null}
          </button>
        ) : null}
        {features.skills !== false ? (
          <button
            type="button"
            className="t2v-icon-btn t2v-header-btn"
            aria-label="Skills"
            title={view === 'skills' ? 'Back to chat' : 'Skills'}
            aria-pressed={view === 'skills'}
            onClick={toggle('skills')}
          >
            <BookOpen aria-hidden="true" size={16} />
          </button>
        ) : null}
        {features.settings ? (
          <button
            type="button"
            className="t2v-icon-btn t2v-header-btn"
            aria-label="Settings"
            title={view === 'settings' ? 'Back to chat' : 'Settings'}
            aria-pressed={view === 'settings'}
            onClick={toggle('settings')}
          >
            <Settings aria-hidden="true" size={16} />
          </button>
        ) : null}
        {hasThreadList ? (
          <button
            type="button"
            className="t2v-icon-btn t2v-header-btn"
            aria-label="Chats"
            title={view === 'list' ? 'Back to chat' : 'Chats'}
            aria-pressed={view === 'list'}
            // Never disabled, even with nothing behind it yet —
            // an empty list has its own "No chats yet" state (ThreadList.tsx) rather than
            // being unreachable, and a reply streaming is no reason to block it either.
            onClick={toggle('list')}
          >
            <History aria-hidden="true" size={16} />
          </button>
        ) : null}
        <button
          type="button"
          className="t2v-icon-btn t2v-header-btn"
          aria-label="New chat"
          title="New chat"
          // TS's own New chat is never disabled — a switch abandons a
          // running turn the same way (ThreadsState.new_thread/switch both detach, no
          // cancel), so threadNew gets the same rule. newSession is different: `clear`
          // holds isLoading until the old engine session has been DELETED, and a second
          // New chat inside that window would delete a second session, with a send in it
          // coming back as "still working on the last message" — that guard stays for the
          // no-thread-list / older-server path, which still uses newSession.
          disabled={(!hasThreadList && state.isLoading) || conn.phase === 'over'}
          onClick={onNew}
        >
          <Plus aria-hidden="true" size={16} />
        </button>
      </div>
    </header>
  );
};

/** True once `on` has held for `ms`: a resync that takes a moment shows no banner. */
function useSustained(on: boolean, ms: number): boolean {
  const [sustained, setSustained] = useState(false);
  useEffect(() => {
    if (!on) {
      setSustained(false);
      return;
    }
    const id = window.setTimeout(() => setSustained(true), ms);
    return () => window.clearTimeout(id);
  }, [on, ms]);
  return on && sustained;
}

const Banner: FC = () => {
  const { conn } = useModel();
  const reconnecting = useSustained(conn.phase === 'reconnecting', 1000);
  if (conn.phase === 'over') {
    return (
      <div className="t2v-banner" role="alert">
        This chat has stopped. Open it again from the app.
      </div>
    );
  }
  if (reconnecting) {
    return (
      <div className="t2v-banner t2v-banner-quiet" role="status">
        Reconnecting…
      </div>
    );
  }
  return null;
};

/** Is focus nowhere useful: on <body>, or inside a pane that is now hidden? */
function focusWasLost(): boolean {
  const active = document.activeElement;
  return active === null || active === document.body || (active instanceof HTMLElement && active.closest('.t2v-pane-hidden') !== null);
}

const Shell: FC = () => {
  const model = useModel();
  const { state } = model;
  const store = useStore();
  const aui = useAui();
  const [chosen, setChosen] = useState<View>('thread');
  const { gated, reason } = gate(state);
  const view: View = gated && chosen === 'thread' ? 'account' : chosen;

  const approvalId = state.pendingApproval?.toolCallId ?? null;
  const shown = useRef(view);
  shown.current = view;
  useEffect(() => {
    if (approvalId && (shown.current === 'settings' || shown.current === 'list' || shown.current === 'skills')) setChosen('thread');
  }, [approvalId]);

  // Land focus on the page's own title — it already names the new view — whenever `view`
  // changes, so it is never left on a now-hidden button (an approval pulling
  // the thread back once left focus stranded on the Settings toggle). Skipped on the very first
  // render: nothing should steal focus from the page the instant it loads. One exception:
  // when the guest gate closes under the end-user — focus in the thread that just
  // hid, or already dropped to <body> — the sign-in form's first field is what they need
  // next, so it gets focus instead of the title.
  const titleRef = useRef<HTMLHeadingElement>(null);
  const mounted = useRef(false);
  useEffect(() => {
    if (!mounted.current) {
      mounted.current = true;
      return;
    }
    if (view === 'account' && gated && focusWasLost()) {
      const field = document.querySelector<HTMLInputElement>('.t2v-account input');
      if (field) {
        field.focus();
        return;
      }
    }
    titleRef.current?.focus();
  }, [view]);

  // Tells ThreadErrorBoundary when the conversation on screen has moved on -- New chat
  // empties `messages` and, with the thread list on, mints a new `currentId`; a thread
  // switch or a sign-in's restore loads a different transcript, whose first message has
  // a different id either way. This is an ordinary prop, not a React `key`: <Thread> is
  // documented to never unmount (scroll position, a half-typed message), and a key that
  // changed here would remount it on every one of these, crashed or not -- the boundary
  // itself only remounts its children when it actually has an error to clear.
  const threadResetKey = (state.threads?.currentId ?? '') + ':' + (state.messages[0]?.id ?? '');
  const hasThreadList = state.options.features.threadList && state.threads !== undefined;
  // Lifted out of Header so ThreadListView's own empty-state "New chat" button
  // (nothing to switch back to yet, so it has no row to pick) can start the same command
  // the header's button does, not a second, diverging copy of it.
  const onNew = () => {
    store.command({ type: hasThreadList ? 'threadNew' : 'newSession' });
    // A picked-but-unsent attachment, and a half-typed draft, belong to the conversation
    // they were made for, not the next one (Composer.tsx does the same on an identity
    // change, and again on its own once `messages` empties — this call is the one case
    // that catches a click on an ALREADY-empty, fresh conversation, which that
    // state-based watcher cannot see by itself).
    void aui.composer().reset();
    setChosen('thread');
  };
  // The save_skill card's settled line links here; only while the view exists.
  const openSkills = useMemo(
    () => (state.options.features.skills !== false ? () => setChosen('skills') : null),
    [state.options.features.skills],
  );
  return (
    <div className="t2v-app t2v-chat-shell aui-root">
      <Header view={view} onView={setChosen} titleRef={titleRef} onNew={onNew} />
      <Banner />
      <div className="t2v-body t2v-chat-body aui-modal-body">
        <div className={view === 'thread' ? 't2v-pane' : 't2v-pane t2v-pane-hidden'} aria-hidden={view !== 'thread'}>
          <OpenSkillsContext.Provider value={openSkills}>
            <ThreadErrorBoundary resetKey={threadResetKey}>
              <Thread />
            </ThreadErrorBoundary>
          </OpenSkillsContext.Provider>
        </div>
        {view === 'settings' ? <SettingsView onAccount={() => setChosen('account')} /> : null}
        {view === 'skills' ? <SkillsView onAccount={() => setChosen('account')} /> : null}
        {view === 'list' && state.threads ? <ThreadListView onSelect={() => setChosen('thread')} onNew={onNew} /> : null}
        {/* Never conditionally mounted (unlike Settings/Skills): a half-typed sign-in's
            email survives a trip through either, the same reason Thread never unmounts. */}
        <div className={view === 'account' ? undefined : 't2v-pane-hidden'} aria-hidden={view !== 'account'}>
          <AccountView reason={reason} gated={gated} visible={view === 'account'} onDone={() => setChosen('thread')} />
        </div>
      </div>
    </div>
  );
};

const Connected: FC<{ store: PageStore }> = ({ store }) => {
  const model = useModel();
  const runtime = useEmbedRuntime(store, model);
  const { theme, preferences } = model.state;
  useEffect(() => applyTheme(theme, preferences.fontSize), [theme, preferences.fontSize]);
  if (model.seq === null) {
    return (
      <div className="t2v-app t2v-center" role="status">
        {model.conn.phase === 'over' ? 'This chat has stopped. Open it again from the app.' : 'Connecting…'}
      </div>
    );
  }
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Shell />
    </AssistantRuntimeProvider>
  );
};

export const App: FC<{ store: PageStore }> = ({ store }) => (
  <StoreContext.Provider value={store}>
    <Connected store={store} />
  </StoreContext.Provider>
);
