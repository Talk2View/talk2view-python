// vendored-from: Talk2View/talk2view-typescript src/ui/safeMarkdown.ts
// vendored-commit: b042b005611c59e8bc6fbd304ea1f028d11fb88b
// local-delta: none
//
// A copy of the public TS SDK's reply sanitiser: every reply and every plan the page
// shows is rendered through it. Below this header the file is the canonical
// one, byte for byte, and CI fails when it differs from canonical main
// (scripts/check-vendored-sdk.mjs). Never edit it here: re-vendor it (see README.md,
// "Where the TS SDK code comes from").
// end of vendoring header
import DOMPurify from 'dompurify';
import { Marked } from 'marked';
import type { Tokens } from 'marked';

/**
 * Model replies are untrusted: prompt injection through web search, a document,
 * email content or a tool result can make the model write anything. A reply is
 * therefore rendered as markdown only and never loads remote content on its own.
 * A reply that could make the browser fetch a URL of the model's choosing is an
 * exfiltration channel: the URL itself carries whatever the model puts in it,
 * and the request goes out before anyone reads the reply. So:
 *  - raw HTML in the reply is shown as text, never parsed;
 *  - an image becomes a link the end-user can choose to open;
 *  - links open in a new tab and must be http(s) or mailto;
 *  - the output then passes a strict allow-list sanitizer.
 */

const ALLOWED_TAGS = [
  'p', 'br', 'strong', 'em', 'del', 'code', 'pre', 'blockquote',
  'ul', 'ol', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'a',
  'table', 'thead', 'tbody', 'tr', 'th', 'td', 'input',
];
const ALLOWED_ATTR = [
  'href', 'title', 'target', 'rel', 'class', 'align', 'start', 'type', 'checked', 'disabled',
];
// Link targets a reply may keep. DOMPurify's default URI check still runs first
// (it strips javascript:, data: and the like). A custom ALLOWED_URI_REGEXP can't
// be used for this: DOMPurify applies it to every non-URI-safe attribute value,
// which would strip align="center", start="3" and type="checkbox".
const ALLOWED_HREF = /^(?:https?:|mailto:)/i;
const WEB_URI = /^https?:/i;
const LANGUAGE_CLASS = /^language-[\w+#.-]+$/;
const TABLE_ALIGN = new Set(['left', 'center', 'right']);

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function webHost(href: string): string | null {
  try {
    const url = new URL(href);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.host : null;
  } catch {
    return null;
  }
}

// A raw markdown href can carry HTML character references (e.g. `&sol;`,
// `&#64;`) that the browser decodes when the rendered HTML is parsed. Decode
// them here, once, so webHost() and the title fallback see the same string
// the href will actually resolve to. A detached <textarea> never runs
// anything it's given: its innerHTML setter only ever produces text nodes
// under it, and nothing reads or attaches this element beyond .value.
function decodeHtmlEntities(text: string): string {
  const textarea = document.createElement('textarea');
  textarea.innerHTML = text;
  return textarea.value;
}

const parser = new Marked({ gfm: true, breaks: true });
parser.use({
  // marked 15 flips lexer.state.inRawBlock on an inline <pre>/<code>/<kbd>/
  // <script> tag, even though our html() renderer below escapes the tag
  // itself. While that state is on, the default text renderer emits
  // following text tokens unescaped (marked treats them as already-safe raw
  // HTML), and the state carries across paragraphs until a matching close
  // tag. Raw HTML is never markup here, so always escape it.
  walkTokens(token) {
    if (token.type === 'text' && 'escaped' in token && token.escaped) {
      token.escaped = false;
    }
  },
  renderer: {
    // Raw HTML is shown as the text the model wrote, never parsed — except an
    // exact <br>, the only way GFM lets a reply break a line inside a table
    // cell (a cell can't hold a literal newline). It has no attributes to
    // carry an event handler or style, and DOMPurify still runs on it.
    html({ text }: Tokens.HTML | Tokens.Tag): string {
      if (/^<br\s*\/?>\s*$/i.test(text)) return '<br>';
      return escapeHtml(text);
    },
    // Loading an image would send its URL, and anything packed into it, to
    // whoever runs that server, with no click. Show a link instead.
    image({ href, title, tokens }: Tokens.Image): string {
      const alt = this.parser.parseInline(tokens, this.parser.textRenderer);
      const label = alt ? `Image: ${escapeHtml(alt)}` : 'Image';
      const decodedHref = decodeHtmlEntities(href);
      const host = webHost(decodedHref);
      if (!host) return label;
      let safeHref: string;
      try {
        // Same encoding marked's own link renderer uses (cleanUrl), so an
        // image link's href decodes the same way a markdown link's does.
        safeHref = encodeURI(decodedHref).replace(/%25/g, '%');
      } catch {
        return label;
      }
      return `<a href="${escapeHtml(safeHref)}" title="${escapeHtml(title || decodedHref)}">${label} (${escapeHtml(host)})</a>`;
    },
  },
});

function hardenNode(node: Node): void {
  if (node.nodeType !== 1) return;
  const element = node as Element;
  const tag = element.nodeName;

  if (tag === 'A') {
    const rawHref = element.getAttribute('href');
    if (rawHref !== null && !ALLOWED_HREF.test(rawHref)) element.removeAttribute('href');
    const href = element.getAttribute('href') ?? '';
    if (WEB_URI.test(href)) {
      element.setAttribute('target', '_blank');
      element.setAttribute('rel', 'noopener noreferrer');
      if (!element.getAttribute('title')) element.setAttribute('title', href);
    } else {
      element.removeAttribute('target');
      element.removeAttribute('rel');
    }
  }

  const className = element.getAttribute('class');
  if (className !== null && !(tag === 'CODE' && LANGUAGE_CLASS.test(className))) {
    element.removeAttribute('class');
  }

  const align = element.getAttribute('align');
  if (align !== null && !((tag === 'TH' || tag === 'TD') && TABLE_ALIGN.has(align))) {
    element.removeAttribute('align');
  }

  const start = element.getAttribute('start');
  if (start !== null && !(tag === 'OL' && /^\d{1,9}$/.test(start))) {
    element.removeAttribute('start');
  }

  if (tag === 'INPUT') {
    // Only GFM task-list boxes reach here: always a disabled checkbox, never a field.
    const checked = element.hasAttribute('checked');
    for (const name of element.getAttributeNames()) element.removeAttribute(name);
    element.setAttribute('type', 'checkbox');
    element.setAttribute('disabled', '');
    if (checked) element.setAttribute('checked', '');
  } else {
    element.removeAttribute('type');
    element.removeAttribute('checked');
    element.removeAttribute('disabled');
  }
}

let purifier: typeof DOMPurify | null = null;

// A private instance: hooks added here never reach the host app's own DOMPurify.
function getPurifier(): typeof DOMPurify | null {
  if (purifier) return purifier;
  if (typeof window === 'undefined') return null;
  const instance = DOMPurify(window);
  if (!instance.isSupported) return null;
  instance.addHook('afterSanitizeAttributes', hardenNode);
  purifier = instance;
  return purifier;
}

/**
 * Run already-parsed HTML through the private sanitizer: a strict allow-list
 * plus the hook above (the second line of defence — ADR 0009). Without a DOM
 * (server rendering) it falls back to escaping the HTML as plain text, since
 * the sanitizer can't run there.
 *
 * Exported for tests; partners use `renderSafeMarkdown`.
 */
export function sanitizeReplyHtml(html: string): string {
  const purify = getPurifier();
  if (!purify) return escapeHtml(html);
  return purify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
}

/**
 * Render an untrusted model reply as safe HTML: markdown only, no remote content
 * loaded on its own. Use this for any custom chat UI; `<ChatPanel>` already does.
 * Without a DOM (server rendering) the reply comes back as escaped plain text,
 * because the sanitizer can't run there.
 */
export function renderSafeMarkdown(markdown: string): string {
  if (!getPurifier()) return escapeHtml(markdown);
  const html = parser.parse(markdown, { async: false }) as string;
  return sanitizeReplyHtml(html);
}
