import { useEffect, useMemo, useRef, type MouseEvent } from 'react';
import type { DataMessagePartComponent, TextMessagePartComponent } from '@assistant-ui/react';
import { Check } from 'lucide-react';
import { renderSafeMarkdown } from '@sdk/ui/safeMarkdown';
import type { PageStore } from '../store';
import { displayText } from './displayText';
import { useModel, useStore } from './context';
import { parsePlan, type PlanStepState } from './plan';

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

// Accepts A-Za-z too: safeMarkdown.ts's own sanitiser keeps a `class` on
// `<code>` for any info string matching `/^language-[\w+#.-]+$/` (`\w` includes upper
// case), so `language-JSON`/`language-Python` already reach the page — a lowercase-only
// filter here silently emptied the label for exactly the fences models write capitalised
// most often. Not lowercased here: the label keeps the fence's own case, same as TS
// (`markdown-text.tsx` never touches the text, only the rendering, with CSS `lowercase`
// on `.aui-code-header-language`, kept here) — matching TS's own DOM shape, not
// just its pixels.
const LANGUAGE = /^[A-Za-z0-9_+#.-]{1,32}$/;
/** How long the tick shows after a copy (TS use-copy-to-clipboard.ts copiedDuration); also
 * the reply-level Copy, which imports this. */
export const COPIED_MS = 3000;
// Literal markup, not `renderToStaticMarkup` (lucide-react's `Copy`/`Check` at size 16),
// copied verbatim from the two icons' own rendered output: `react-dom/server` alone grew
// the bundle over 200 KiB (against a 20 KiB budget), pulling in code the
// browser build otherwise never needs for anything server-side.
const COPY_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-copy" aria-hidden="true"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"></rect><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"></path></svg>';
const CHECK_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-check" aria-hidden="true"><path d="M20 6 9 17l-5-5"></path></svg>';

/**
 * TS markdown-text.tsx's CodeHeader: a language label (only a plain
 * `language-xxx` class name, model-controlled, so it is filtered) and, when the host has a
 * clipboard, a Copy button — baked into the sanitised HTML *string* itself, not added
 * afterwards by an effect that mutates the live DOM. `Markdown`'s own `useMemo` now calls
 * this on `renderSafeMarkdown`'s output before it is ever handed to
 * `dangerouslySetInnerHTML`, so the header is part of `__html` from the first paint: no
 * wipe-and-reinsert window for assistant-ui's smooth streaming to race (`dangerouslySet
 * InnerHTML` replaces the whole container on every reveal step; an effect adding the
 * header afterwards raced that replacement for close to a second on an ordinary reply,
 * measured with a MutationObserver — the previous approach's own comment named the exact
 * number). A detached `<div>` parses and reserialises the markup — the same
 * `element.innerHTML =` / read-`.innerHTML`-back operation `dangerouslySetInnerHTML`
 * itself performs on the real container, just run once here first — so the label's text,
 * written through `element.textContent`, comes back through the browser's own HTML
 * serialiser exactly as escaped as `safeMarkdown.ts`'s own `escapeHtml()` would (that
 * function is unexported and the file is vendored — README.md, "Never edit it here" — so
 * this reuses the *browser's* serialiser rather than hand-rolling an equivalent one).
 * No longer idempotent-by-DOM-check: `renderSafeMarkdown`'s own output
 * never carries a header to begin with, so there is nothing to skip.
 */
function headCodeBlocksHtml(html: string, clipboard: boolean): string {
  const container = document.createElement('div');
  container.innerHTML = html;
  for (const pre of Array.from(container.querySelectorAll('pre'))) {
    const code = pre.querySelector('code');
    const match = /(?:^|\s)language-([^\s]+)/.exec(code?.className ?? '');
    const language = match && LANGUAGE.test(match[1]!) ? match[1]! : '';
    const header = document.createElement('div');
    header.className = 'aui-code-header-root';
    const label = document.createElement('span');
    label.className = 'aui-code-header-language';
    label.textContent = displayText(language);
    header.appendChild(label);
    if (clipboard) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 't2v-icon-btn t2v-code-copy';
      button.setAttribute('aria-label', 'Copy code');
      button.title = 'Copy';
      button.innerHTML = COPY_SVG;
      header.appendChild(button);
    }
    pre.classList.add('aui-md-pre');
    pre.parentNode!.insertBefore(header, pre);
  }
  for (const code of Array.from(container.querySelectorAll('code'))) {
    if (code.parentElement?.tagName !== 'PRE') code.classList.add('aui-md-inline-code');
  }
  return container.innerHTML;
}

/** Markdown the way the TS SDK renders it: never raw HTML, never a remote image. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const store = useStore();
  const clipboard = useModel().state.capabilities.clipboard;
  const html = useMemo(() => headCodeBlocksHtml(renderSafeMarkdown(text), clipboard), [text, clipboard]);
  const timers = useRef<number[]>([]);
  useEffect(() => () => timers.current.forEach((id) => window.clearTimeout(id)), []);
  const onClick = (event: MouseEvent<HTMLElement>) => {
    const button = (event.target as HTMLElement).closest('.t2v-code-copy');
    if (button instanceof HTMLButtonElement) {
      const pre = button.parentElement?.nextElementSibling;
      if (!(pre instanceof HTMLPreElement) || button.dataset.copied === '1') return;
      store.command({ type: 'copy', text: pre.textContent ?? '' });
      button.dataset.copied = '1';
      button.innerHTML = CHECK_SVG;
      const id = window.setTimeout(() => {
        delete button.dataset.copied;
        button.innerHTML = COPY_SVG;
        // Drop the id once it has fired, so a message with many copies
        // over a long conversation does not grow this array without bound.
        timers.current = timers.current.filter((pending) => pending !== id);
      }, COPIED_MS);
      timers.current.push(id);
      return;
    }
    openLinkOnClick(store, event);
  };
  return (
    <div
      className={'t2v-md aui-md' + (className ? ' ' + className : '')}
      onClick={onClick}
      // The middle button only; the right one belongs to the context menu.
      onAuxClick={(event) => {
        if (event.button === 1) openLinkOnClick(store, event);
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}

export const MarkdownText: TextMessagePartComponent = ({ text }) => <Markdown text={text} />;

const STEP_LABEL: Record<PlanStepState, string> = { done: 'Done: ', active: 'Next: ', pending: 'To do: ' };

/**
 * The agent's plan (convert.ts `data-plan`). The engine's checklist is drawn as steps,
 * after assistant-ui's AgentPlan element: a title and "N of M", a 3px bar, a mark per step.
 * The step under way has a Teal dot, not a spinner: the plan stays on the message after
 * its run ends. A plan that is not only a checklist (parsePlan) shows as markdown.
 */
export const PlanPart: DataMessagePartComponent<{ markdown: string }> = ({ data }) => {
  const plan = useMemo(() => parsePlan(data.markdown), [data.markdown]);
  if (!plan) {
    return (
      <div className="t2v-plan">
        <div className="t2v-plan-head"><span className="t2v-plan-title">Plan</span></div>
        <Markdown text={data.markdown} />
      </div>
    );
  }
  const total = plan.steps.length;
  return (
    <div className="t2v-plan">
      <div className="t2v-plan-head">
        <span className="t2v-plan-title">Plan</span>
        <span className="t2v-plan-count">{plan.done} of {total}</span>
      </div>
      <div className="t2v-plan-bar" role="progressbar" aria-label="Plan progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={plan.done}>
        <div className="t2v-plan-fill" style={{ width: `${(plan.done / total) * 100}%` }} />
      </div>
      <ul className="t2v-plan-steps">
        {plan.steps.map((step, i) => (
          <li key={i} className="t2v-plan-step" data-state={step.state}>
            <span className="t2v-plan-icon" aria-hidden="true">
              {step.state === 'done' ? <Check size={14} /> : <span className="t2v-plan-mark" />}
            </span>
            <span><span className="t2v-sr-only">{STEP_LABEL[step.state]}</span>{step.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
};
