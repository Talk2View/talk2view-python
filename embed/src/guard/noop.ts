import type { PageStore } from '../store';

/** The shipped page has no self-test: `vite build --mode guard` swaps in ./index.ts. */
export function installGuard(_store: PageStore): void {}
