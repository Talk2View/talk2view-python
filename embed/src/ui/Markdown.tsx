import { useMemo, type MouseEvent } from 'react';
import type { DataMessagePartComponent, TextMessagePartComponent } from '@assistant-ui/react';
import { renderSafeMarkdown } from '@sdk/ui/safeMarkdown';
import type { PageStore } from '../store';
import { useStore } from './context';

/**
 * A click on a link in a reply. The web view treats every navigation as its own (a
 * followed link would replace the chat, and popups are blocked), so an http(s) link
 * goes to the host's browser through `openLink`; anything else does nothing.
 *
 * Both buttons end up here. The middle one fires `auxclick` and never `click`, and its
 * default is a window of the web view's own — which a host whose `createWindow` hands
 * back the same view would open over the chat.
 */
export function openLinkOnClick(store: PageStore, event: MouseEvent<HTMLElement>): void {
  const anchor = (event.target as HTMLElement).closest('a');
  if (!anchor) return;
  event.preventDefault();
  const href = anchor.getAttribute('href') ?? '';
  if (/^https?:\/\//i.test(href)) store.command({ type: 'openLink', url: href });
}

/**
 * A reply as a screen reader should hear it: the same render, then its text alone. Read
 * out raw, a reply says "hash hash Segmentation plan" and spells its own table pipes.
 */
export function markdownToText(text: string): string {
  const html = renderSafeMarkdown(text);
  return new DOMParser().parseFromString(html, 'text/html').body.textContent ?? '';
}

/** Markdown the way the TS SDK renders it: never raw HTML, never a remote image. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const store = useStore();
  const html = useMemo(() => renderSafeMarkdown(text), [text]);
  return (
    <div
      className={'t2v-md' + (className ? ' ' + className : '')}
      onClick={(event) => openLinkOnClick(store, event)}
      // The middle button only; the right one belongs to the context menu.
      onAuxClick={(event) => {
        if (event.button === 1) openLinkOnClick(store, event);
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

export const MarkdownText: TextMessagePartComponent = ({ text }) => <Markdown text={text} />;

/** The agent's plan (convert.ts `data-plan`): a markdown checklist. */
export const PlanPart: DataMessagePartComponent<{ markdown: string }> = ({ data }) => (
  <div className="t2v-plan">
    <div className="t2v-plan-title">Plan</div>
    <Markdown text={data.markdown} />
  </div>
);
