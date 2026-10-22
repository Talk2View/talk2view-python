/**
 * Earlier conversations, over the thread (TS chat/views/thread-list.tsx over the vendored
 * thread-list.aui.tsx): a search box once there is something to search, rows grouped
 * Today / Yesterday / Earlier, Rename inline, Delete; Archive is left out on purpose (TS
 * does the same: the chat has no archived view). Picking a row closes the view. The
 * page has no popover, so Rename and Delete are two buttons that show on hover and focus.
 * Every title is the end-user's own first message or rename: untrusted, escaped — including
 * the Rename input itself: it is seeded with the same `displayText()` form
 * the row's own label shows, not the raw string, or a bidi override in a title would spoof
 * what the end-user believes they are editing, the same class of surface the approval
 * editor's own `escapeForJson` seeding closes (ToolCall.tsx).
 */
import { useCallback, useEffect, useMemo, useRef, useState, type FC, type KeyboardEvent } from 'react';
import { LoaderCircle, MessageSquare, Pencil, Plus, Search, Trash2 } from 'lucide-react';
import { isRunning, listedThreads, threadGroups, UNTITLED_THREAD, whenLabel } from '../selectors';
import type { ThreadItem } from '../wire';
import { useModel, useStore } from './context';
import { displayText } from './displayText';

const Rename: FC<{ item: ThreadItem; onDone: (restoreFocus: boolean) => void }> = ({ item, onDone }) => {
  const store = useStore();
  // The escaped form, not item.title itself — what the end-user sees is
  // what they edit. "Unchanged" is judged against this same escaped value, so leaving a
  // hostile title untouched sends nothing back (the raw string is still what the server
  // keeps; only an actual edit sends a new one).
  const seeded = displayText(item.title ?? '');
  const [value, setValue] = useState(seeded);
  const ref = useRef<HTMLInputElement>(null);
  const settled = useRef(false);
  useEffect(() => {
    ref.current?.select();
  }, []);
  const commit = (restoreFocus: boolean) => {
    if (settled.current) return;
    settled.current = true;
    const next = value.trim();
    if (next && next !== seeded) store.command({ type: 'threadRename', threadId: item.id, title: next });
    onDone(restoreFocus);
  };
  const cancel = () => {
    if (settled.current) return;
    settled.current = true;
    onDone(true);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commit(true);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      cancel();
    }
  };
  return (
    <input
      ref={ref}
      autoFocus
      data-slot="aui_thread-list-item-rename"
      className="t2v-text t2v-thread-rename"
      aria-label="Rename thread"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => commit(false)}
      onKeyDown={onKey}
    />
  );
};

const Row: FC<{
  item: ThreadItem;
  active: boolean;
  running: boolean;
  onSelect: () => void;
  onDelete: (item: ThreadItem) => void;
  registerTrigger: (id: string, el: HTMLButtonElement | null) => void;
}> = ({ item, active, running, onSelect, onDelete, registerTrigger }) => {
  const store = useStore();
  const [renaming, setRenaming] = useState(false);
  const [focused, setFocused] = useState(false);
  // TS thread-list.aui.tsx:280-295's own pattern: a rename that just closed
  // restores focus to the trigger, once it has actually remounted (it was not rendered at
  // all while renaming), never on a blur (focus already moved somewhere on purpose there).
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const restoreFocus = useRef(false);
  useEffect(() => {
    if (renaming || !restoreFocus.current) return;
    restoreFocus.current = false;
    triggerRef.current?.focus();
  }, [renaming]);
  const setTriggerRef = useCallback(
    (el: HTMLButtonElement | null) => {
      triggerRef.current = el;
      registerTrigger(item.id, el);
    },
    [item.id, registerTrigger],
  );
  const shown = displayText(item.title ?? UNTITLED_THREAD);
  return (
    <div
      data-slot="aui_thread-list-item"
      className={'t2v-thread-row' + (focused ? ' t2v-thread-row-focus' : '')}
      data-active={active ? '' : undefined}
      onFocus={() => setFocused(true)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setFocused(false);
      }}
    >
      {renaming ? (
        <Rename item={item} onDone={(restore) => {
          restoreFocus.current = restore;
          setRenaming(false);
        }} />
      ) : (
        <button
          ref={setTriggerRef}
          type="button"
          data-slot="aui_thread-list-item-trigger"
          className="t2v-thread-trigger"
          onClick={() => {
            if (!active) store.command({ type: 'threadSwitch', threadId: item.id });
            onSelect();
          }}
        >
          {/* TS marks the running thread's own row (thread-list.aui.tsx:317-329). */}
          {running ? <LoaderCircle className="t2v-spin t2v-thread-running" aria-hidden="true" size={14} /> : null}
          <span data-slot="aui_thread-list-item-title" className="t2v-thread-title">{shown}</span>
          {/* The row's time, at the right — hidden again the moment the row is
              hovered/active/focused, the same trigger .t2v-thread-more uses, since that is
              exactly when the rename/delete actions need the room instead. */}
          {!running ? <span className="t2v-thread-when">{whenLabel(item.updatedAt, Date.now())}</span> : null}
          {running ? <span className="t2v-sr-only">Running</span> : null}
        </button>
      )}
      <div className="t2v-thread-more">
        <button type="button" data-slot="aui_thread-list-item-more" className="t2v-icon-btn t2v-action-btn t2v-thread-rename-btn" aria-label="Rename" title="Rename" onClick={() => setRenaming(true)}>
          <Pencil aria-hidden="true" size={16} />
        </button>
        <button
          type="button"
          data-slot="aui_thread-list-item-more"
          className="t2v-icon-btn t2v-action-btn t2v-thread-delete"
          aria-label="Delete"
          title="Delete"
          onClick={() => onDelete(item)}
        >
          <Trash2 aria-hidden="true" size={16} />
        </button>
      </div>
    </div>
  );
};

export const ThreadListView: FC<{ onSelect: () => void; onNew: () => void }> = ({ onSelect, onNew }) => {
  const { state } = useModel();
  const store = useStore();
  const signedIn = state.auth.isAuthenticated && !state.auth.isAnonymous;
  const [query, setQuery] = useState('');
  const items = listedThreads(state);
  const hasThreads = items.length > 0;
  const groups = threadGroups(items, Date.now(), hasThreads ? query : '');
  const flat = useMemo(() => groups.flatMap((g) => g.items), [groups]);
  const searchRef = useRef<HTMLInputElement>(null);
  const triggerRefs = useRef<Map<string, HTMLButtonElement>>(new Map());
  const registerTrigger = useCallback((id: string, el: HTMLButtonElement | null) => {
    if (el) triggerRefs.current.set(id, el);
    else triggerRefs.current.delete(id);
  }, []);
  // Not proven anywhere in TS's own source (the vendored file, the
  // assistant-ui primitives behind ThreadListItemPrimitive.Delete and its Radix
  // Collection all leave a deleted row's focus wherever the browser drops it, same as
  // this page did before this fix) — a deliberate accessibility improvement past what TS
  // itself does, not a citation. Computed against the CURRENTLY VISIBLE (filtered,
  // grouped) order, since that is what is spatially adjacent on screen. The command is
  // sent, and the target focused, before the server's own confirmation: the trigger
  // refs and the search box are still the currently-mounted ones at click time either way.
  const onDelete = (item: ThreadItem) => {
    const idx = flat.findIndex((i) => i.id === item.id);
    const target = flat[idx + 1] ?? flat[idx - 1] ?? null;
    store.command({ type: 'threadDelete', threadId: item.id });
    if (target) triggerRefs.current.get(target.id)?.focus();
    else searchRef.current?.focus();
  };
  return (
    <div className="t2v-view t2v-chat-thread-list aui-modal-thread-list" role="region" aria-label="Chats">
      {hasThreads ? (
        <div data-slot="aui_thread-list-search" className="t2v-thread-search">
          <Search aria-hidden="true" size={16} className="t2v-thread-search-icon" />
          <input ref={searchRef} type="search" className="t2v-text t2v-thread-search-input" aria-label="Search chats" placeholder="Search chats" value={query} onChange={(e) => setQuery(e.target.value)} />
        </div>
      ) : null}
      <div data-slot="aui_thread-list-items" className="t2v-thread-items">
        {!hasThreads ? (
          <div data-slot="aui_thread-list-empty" className="t2v-thread-list-empty">
            <MessageSquare aria-hidden="true" size={28} />
            <h3>No chats yet</h3>
            <p>
              Conversations you start show up here.
              {/* This half of the sentence is an invitation to sign
                  in, so it says nothing once there is no one left to invite. */}
              {signedIn ? null : ' Sign in to keep them across sessions.'}
            </p>
            <button type="button" className="t2v-btn t2v-btn-outline" onClick={onNew}>
              <Plus aria-hidden="true" size={14} />
              New chat
            </button>
          </div>
        ) : null}
        {hasThreads && query.trim() && groups.length === 0 ? (
          <div data-slot="aui_thread-list-empty" className="t2v-thread-list-empty">
            <h3>No chats match “{displayText(query)}”</h3>
            <p>Try a different word.</p>
          </div>
        ) : null}
        {groups.map((group) => (
          <div key={group.label}>
            <div data-slot="aui_thread-list-group-label" className="t2v-thread-group">{group.label}</div>
            {group.items.map((item) => (
              <Row
                key={item.id}
                item={item}
                active={item.id === state.threads?.currentId}
                running={item.id === state.threads?.currentId && isRunning(state)}
                onSelect={onSelect}
                onDelete={onDelete}
                registerTrigger={registerTrigger}
              />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
};
