/**
 * Catches a render crash inside the thread view. The primary defence is
 * `runtime.ts`'s `convertMessage`, which catches
 * `convert.ts`'s own throw (a stored or live message it cannot render) one message at a
 * time, with no unmount involved at all — this boundary is the backstop for a crash
 * ANYWHERE ELSE in `<Thread>`'s own render (`ToolCall.tsx`, `Markdown.tsx`, a future
 * change), which `convertMessage`'s try/catch cannot see. Without it, that throw unmounts
 * the whole React tree (assistant-ui has no boundary of its own), blanking the page.
 *
 * Scoped to `<Thread>` alone (`ui/App.tsx`), not the whole app: Header's New chat and
 * Chats buttons live outside it, so they stay reachable while the fallback shows.
 *
 * `resetKey` is an ordinary prop, never a React `key`: `<Thread>` is documented
 * (`ui/App.tsx`) to never unmount — its scroll position and a half-typed message survive
 * a trip through Settings — and a `key` that changed on every New chat or thread switch
 * would remount it on EVERY one of those, crashed or not (confirmed the hard way: it broke
 * attachments.spec.ts's identity-switch chip-clearing specs, which rely on `<Thread>`
 * staying mounted). `componentDidUpdate` below only touches state, and so only asks React
 * to render `children` again, when there IS a caught error to clear AND `resetKey` has
 * moved on since — the ordinary case (nothing ever crashed) never re-renders `children`
 * for this reason at all. The child that comes back is a fresh `<Thread>` either way,
 * because catching the error already unmounted the old one; this prop only decides *when*
 * to stop showing the fallback and try again, not whether a remount happens.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  resetKey: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ThreadErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('[t2v] the thread view crashed rendering a message:', error, info.componentStack);
  }

  componentDidUpdate(prevProps: Props): void {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="t2v-error" role="alert">
          <p className="t2v-error-text">Something went wrong showing this chat.</p>
        </div>
      );
    }
    return this.props.children;
  }
}
