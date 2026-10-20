/**
 * The frame (TS chat/chat.tsx): a header of one-tap views over a thread that never
 * unmounts, so its conversation, scroll position and a half-typed message survive a trip
 * through Settings or Account. Account never unmounts either: visiting Settings and coming back keeps the email of a half-typed sign-in,
 * the same reason Thread stays mounted (its password is cleared whenever the pane hides,
 * Account.tsx). Settings itself DOES unmount on close — its own
 * effect re-asks the skills list each time it opens (Settings.tsx), which staying mounted
 * would break. Behind the guest gate the thread is replaced by Account. An approval
 * arriving while Settings is open brings the thread back (Account is left alone: it may
 * hold that half-typed sign-in). A view change moves focus to the page's own title, which
 * already names the view, so it is never left on a button the view switch just hid.
 * Nothing shows until the first snapshot: the page never flashes an empty chat
 * that is about to fill.
 */
import { useEffect, useRef, useState, type FC, type RefObject } from 'react';
import { Plus, Settings, UserRound } from 'lucide-react';
import { AssistantRuntimeProvider, useAui } from '@assistant-ui/react';
import { useEmbedRuntime } from '../runtime';
import { gate } from '../selectors';
import type { PageStore } from '../store';
import { AccountView } from './Account';
import { StoreContext, useModel, useStore } from './context';
import { SettingsView } from './Settings';
import { applyTheme } from './theme';
import { Thread } from './Thread';

export type View = 'thread' | 'settings' | 'account';

const TITLES: Record<View, string> = { thread: 'Chat', settings: 'Settings', account: 'Account' };

/** The page's h1; the welcome's heading (ui/Thread.tsx) is the h2 under it. `titleRef`
 * (`tabIndex={-1}`) is what a view change focuses: it already renders the
 * new view's name, so no separate per-view heading is needed just to land focus on. */
const Header: FC<{ view: View; onView: (view: View) => void; titleRef: RefObject<HTMLHeadingElement | null> }> = ({
  view,
  onView,
  titleRef,
}) => {
  const store = useStore();
  const aui = useAui();
  const { state, conn } = useModel();
  const { features } = state.options;
  const signedIn = state.auth.isAuthenticated && !state.auth.isAnonymous;
  const toggle = (target: View) => () => onView(view === target ? 'thread' : target);
  return (
    <header className="t2v-header">
      <h1 ref={titleRef} tabIndex={-1} className="t2v-title">
        {TITLES[view]}
      </h1>
      {features.account ? (
        <button
          type="button"
          className="t2v-icon-btn t2v-header-btn"
          aria-label="Account"
          title="Account"
          aria-pressed={view === 'account'}
          onClick={toggle('account')}
        >
          <UserRound aria-hidden="true" size={16} />
          {signedIn ? <span className="t2v-dot" aria-hidden="true" /> : null}
        </button>
      ) : null}
      {features.settings ? (
        <button
          type="button"
          className="t2v-icon-btn t2v-header-btn"
          aria-label="Settings"
          title="Settings"
          aria-pressed={view === 'settings'}
          onClick={toggle('settings')}
        >
          <Settings aria-hidden="true" size={16} />
        </button>
      ) : null}
      <button
        type="button"
        className="t2v-icon-btn t2v-header-btn"
        aria-label="New chat"
        title="New chat"
        // `clear` holds isLoading until the old engine session has been deleted. A second
        // New chat inside that window abandons the first one's run and deletes a second
        // session, and a send in it comes back as "still working on the last message".
        disabled={state.isLoading || conn.phase === 'over'}
        onClick={() => {
          store.command({ type: 'newSession' });
          // A picked-but-unsent attachment, and a half-typed draft, belong to the
          // conversation they were made for, not the next one (Composer.tsx does the
          // same on an identity change, and again on its own once `messages` empties —
          // this call is the one case that catches a click on an ALREADY-empty, fresh
          // conversation, which that state-based watcher cannot see by itself).
          void aui.composer().reset();
          onView('thread');
        }}
      >
        <Plus aria-hidden="true" size={16} />
      </button>
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
  const [chosen, setChosen] = useState<View>('thread');
  const { gated, reason } = gate(state);
  const view: View = gated && chosen === 'thread' ? 'account' : chosen;

  const approvalId = state.pendingApproval?.toolCallId ?? null;
  const shown = useRef(view);
  shown.current = view;
  useEffect(() => {
    if (approvalId && shown.current === 'settings') setChosen('thread');
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

  return (
    <div className="t2v-app">
      <Header view={view} onView={setChosen} titleRef={titleRef} />
      <Banner />
      <div className="t2v-body">
        <div className={view === 'thread' ? 't2v-pane' : 't2v-pane t2v-pane-hidden'} aria-hidden={view !== 'thread'}>
          <Thread />
        </div>
        {view === 'settings' ? <SettingsView /> : null}
        {/* Never conditionally mounted (unlike Settings): a half-typed sign-in's email
            survives a trip through Settings, the same reason Thread never unmounts. */}
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
