import { createContext, useContext, useSyncExternalStore } from 'react';
import type { PageModel, PageStore } from '../store';

export const StoreContext = createContext<PageStore | null>(null);

export function useStore(): PageStore {
  const store = useContext(StoreContext);
  if (!store) throw new Error('useStore() outside <StoreContext.Provider>');
  return store;
}

/** The published page model; re-renders at most once per animation frame. */
export function useModel(): PageModel {
  const store = useStore();
  return useSyncExternalStore(store.subscribe, store.get);
}

/**
 * Opens the Skills view (App.tsx's own view switch), for a part deep inside the thread
 * that has no props path to it: the save_skill card's settled line. Null
 * outside App (a test mounting the thread alone), where the link is simply not drawn.
 */
export const OpenSkillsContext = createContext<(() => void) | null>(null);
