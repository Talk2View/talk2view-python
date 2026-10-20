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
