/**
 * The runtime guard's probes, run on the
 * real page once it shows the thread: the polyfilled APIs, the CSS the stylesheet relies
 * on, the focus ring, the token's storage, the font, and both SSE transports.
 */
import { INSTALLED_BY_HAND } from '../polyfills';
import { mayAlwaysAllow } from '../selectors';
import type { PageStore } from '../store';
import { TOKEN_KEY } from '../transport';
import { monitors } from './monitors';

export type ProbeStatus = 'PASS' | 'FAIL' | 'INFO';

export interface Probe {
  id: string;
  label: string;
  status: ProbeStatus;
  detail: string;
}

const pass = (id: string, label: string, ok: boolean, detail: string): Probe => ({ id, label, status: ok ? 'PASS' : 'FAIL', detail });
const info = (id: string, label: string, detail: string): Probe => ({ id, label, status: 'INFO', detail });

function isNative(fn: unknown): boolean {
  return typeof fn === 'function' && /\[native code\]/.test(Function.prototype.toString.call(fn));
}

/* eslint-disable @typescript-eslint/no-explicit-any -- the probes call APIs TS's lib may not know. */
const API_PROBES: Array<{ name: string; get: () => unknown; works: () => boolean }> = [
  { name: 'Array.prototype.at', get: () => (Array.prototype as any).at, works: () => ([1, 2, 3] as any).at(-1) === 3 },
  { name: 'String.prototype.at', get: () => (String.prototype as any).at, works: () => ('abc' as any).at(-1) === 'c' },
  { name: 'Object.hasOwn', get: () => (Object as any).hasOwn, works: () => (Object as any).hasOwn({ a: 1 }, 'a') === true },
  { name: 'Array.prototype.findLast', get: () => (Array.prototype as any).findLast, works: () => ([1, 2, 3] as any).findLast((n: number) => n < 3) === 2 },
  { name: 'Array.prototype.findLastIndex', get: () => (Array.prototype as any).findLastIndex, works: () => ([1, 2, 3] as any).findLastIndex((n: number) => n < 3) === 1 },
  { name: 'structuredClone', get: () => (globalThis as any).structuredClone, works: () => (globalThis as any).structuredClone({ a: [1] }).a[0] === 1 },
  { name: 'String.prototype.replaceAll', get: () => (String.prototype as any).replaceAll, works: () => ('a-a' as any).replaceAll('a', 'b') === 'b-b' },
  { name: 'Promise.any', get: () => (Promise as any).any, works: () => typeof (Promise as any).any([Promise.resolve(1)]).then === 'function' },
  { name: 'AggregateError', get: () => (globalThis as any).AggregateError, works: () => new (globalThis as any).AggregateError([new Error('x')], 'm').errors.length === 1 },
  { name: 'crypto.randomUUID', get: () => (crypto as any).randomUUID, works: () => /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test((crypto as any).randomUUID()) },
  {
    name: 'Element.prototype.replaceChildren',
    get: () => (Element.prototype as any).replaceChildren,
    works: () => {
      const el = document.createElement('div');
      el.appendChild(document.createElement('b'));
      el.appendChild(document.createElement('b'));
      (el as any).replaceChildren('x');
      return el.childNodes.length === 1 && el.textContent === 'x';
    },
  },
  { name: 'AbortSignal.timeout', get: () => (AbortSignal as any).timeout, works: () => (AbortSignal as any).timeout(60000) instanceof AbortSignal },
];
/* eslint-enable @typescript-eslint/no-explicit-any */

function apiProbes(): Probe[] {
  return API_PROBES.map(({ name, get, works }) => {
    let ok = false;
    let note = '';
    try {
      ok = typeof get() === 'function' && works();
    } catch (err) {
      note = ' threw ' + String(err);
    }
    return pass('api:' + name, name, ok, (isNative(get()) ? 'native' : 'polyfilled') + note);
  });
}

function fixture(css: string, children: string): HTMLDivElement {
  const el = document.createElement('div');
  el.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;' + css;
  el.innerHTML = children;
  document.body.appendChild(el);
  return el;
}

function cssProbes(): Probe[] {
  const out: Probe[] = [];
  const vars = fixture('--t2v-probe:7px;width:var(--t2v-probe);', '');
  const width = getComputedStyle(vars).width;
  // A literal-valued token (declared directly as a hex, no var()), on purpose: this
  // half's only claim is that custom properties exist and are read back correctly.
  const teal = getComputedStyle(document.documentElement).getPropertyValue('--t2v-teal').trim().toUpperCase();
  vars.remove();
  // Every semantic token (`--t2v-primary: var(--t2v-teal)`, and the
  // same shape for `--t2v-foreground`, `--t2v-muted`, ...) is a TWO-level chain, which
  // nothing here proved — an older comment on this probe claimed
  // `resolvedBorderColor()`/`focusRingProbe()` below already covered it (a later change pointed
  // that helper — then named `resolvedHairlineStrong()`, single-purpose — at
  // `--t2v-primary` instead of `--t2v-hairline-strong`, which was a literal `rgba()`, not
  // a `var()` reference, so it never exercised chained substitution either way). `.t2v-send`'s own
  // `background: var(--t2v-primary)` is a real property on a real, already-mounted
  // element (the probes run once the thread shows, same as `css:composer-row` below
  // relies on `.t2v-send` existing) — the shape every semantic token depends on, not a
  // synthetic fixture.
  const sendBtn = document.querySelector<HTMLElement>('.t2v-send');
  const sendBg = sendBtn ? getComputedStyle(sendBtn).backgroundColor : null;
  const chained = sendBg === 'rgb(38, 200, 184)';
  out.push(
    pass(
      'css:custom-properties',
      'CSS custom properties',
      width === '7px' && teal === '#26C8B8' && chained,
      'var() width ' + width + ', --t2v-teal ' + teal + ', .t2v-send background (--t2v-primary -> --t2v-teal) ' + String(sendBg),
    ),
  );

  // The layout rule the stylesheet relies on instead of `gap`.
  const flex = fixture(
    'display:flex;width:300px;',
    '<i style="display:block;width:40px;height:10px"></i>' +
      '<i style="display:block;width:40px;height:10px;margin-left:8px"></i>' +
      '<i style="display:block;width:40px;height:10px;margin-left:8px"></i>',
  );
  const offset = Math.round(flex.children[2]!.getBoundingClientRect().left - flex.children[0]!.getBoundingClientRect().left);
  flex.remove();
  out.push(pass('css:flex-margins', 'flex row spaced with margins', offset === 96, 'third child offset ' + String(offset) + ' px (want 96)'));

  // Expected to be UNSUPPORTED on Chromium 83: this is why `gap` is banned.
  const gap = fixture(
    'display:flex;flex-direction:column;gap:10px;',
    '<i style="display:block;height:10px"></i><i style="display:block;height:10px"></i>',
  );
  const gapWorks = gap.scrollHeight === 30;
  gap.remove();
  out.push(info('css:flex-gap', 'flexbox gap (banned; informational)', gapWorks ? 'supported' : 'NOT supported'));

  // css:sticky moved to autorun.ts's auto:sticky step: the footer is only
  // pinned to the viewport's bottom edge (delta <= 1px) once the thread has something to
  // scroll — the empty state's own centring (t2v-thread-empty) leaves it mid-viewport on
  // purpose, which this probe would otherwise have read as a FAIL on every guard run.

  // The shimmer text (the running tool row, the tool group's own trigger while
  // active, "Working…") relies on -webkit-background-clip: text staying transparent-
  // filled and animating — Chromium 83's own WebKit-era prefix, kept ahead of the
  // unprefixed background-clip (Chrome 120) the stylesheet also declares as a second,
  // harmless declaration 83 drops. A synthetic fixture, not a live shimmer: nothing in
  // the scripted run leaves one showing long enough to read (backend.py's/testing.py's
  // fake tool calls resolve too fast, and this is the mechanism the shimmer relies on
  // everywhere it appears, not a claim about any one of them).
  const shimmer = fixture('', '<span class="t2v-shimmer">shimmer</span>');
  const shimmerSpan = shimmer.querySelector<HTMLElement>('.t2v-shimmer')!;
  const shimmerStyle = getComputedStyle(shimmerSpan);
  const clip = shimmerStyle.getPropertyValue('-webkit-background-clip');
  const animation = shimmerStyle.animationName;
  shimmer.remove();
  out.push(
    pass(
      'css:shimmer',
      'shimmer text: -webkit-background-clip + animation',
      clip === 'text' && animation === 't2v-shimmer',
      '-webkit-background-clip ' + clip + ', animation-name ' + animation,
    ),
  );

  const input = document.querySelector<HTMLTextAreaElement>('.t2v-input');
  const send = document.querySelector<HTMLElement>('.t2v-send');
  if (input && send) {
    const a = input.getBoundingClientRect();
    const b = send.getBoundingClientRect();
    // The input is its own full-width row (thread.aui.tsx's composer shell is
    // flex-col); the send button sits in a separate action row below it, not beside it on
    // one row as the page's older single-row layout had.
    const stacked = a.bottom <= b.top + 0.5;
    const inside = b.right <= window.innerWidth + 0.5 && a.left >= 0 && b.bottom <= window.innerHeight + 0.5;
    out.push(pass('css:composer-row', 'composer: input above the send button, inside the viewport', stacked && inside, 'input bottom ' + String(Math.round(a.bottom)) + ' px, button top ' + String(Math.round(b.top)) + ' of ' + String(window.innerWidth) + 'x' + String(window.innerHeight)));
  } else {
    out.push(pass('css:composer-row', 'composer: input above the send button, inside the viewport', false, '.t2v-input or .t2v-send not found'));
  }
  return out;
}

/** Resolves with the ms waited once the window has focus, or when `ms` runs out. */
function waitForWindowFocus(ms: number): Promise<number> {
  const start = performance.now();
  if (document.hasFocus()) return Promise.resolve(0);
  return new Promise((resolve) => {
    let settled = false;
    const done = (): void => {
      if (settled) return;
      settled = true;
      window.removeEventListener('focus', done);
      window.clearInterval(poll);
      window.clearTimeout(timer);
      resolve(Math.round(performance.now() - start));
    };
    const poll = window.setInterval(() => {
      if (document.hasFocus()) done();
    }, 100);
    const timer = window.setTimeout(done, ms);
    window.addEventListener('focus', done);
  });
}

/**
 * A `--t2v-*` colour token as the browser resolves it, in the same `rgb()`/`rgba()` form a
 * computed `border-color` comes back in. Read from the live page rather than hard-coded: the
 * host's Theme may legitimately repaint the token (`applyTheme`, ui/theme.ts writes each
 * `--t2v-*` inline on <html>), and a fixed hex would then false-FAIL a themed Slicer while
 * proving nothing extra — `css:custom-properties` already pins the value this backend
 * serves. Resolving it through a real element also handles any colour syntax, which parsing
 * the raw token text would not. Parameterised (renamed from
 * `resolvedHairlineStrong()`): `focusRingProbe()` below now reads `--t2v-primary` instead
 * of `--t2v-hairline-strong` for the focused composer shell's border.
 */
function resolvedBorderColor(token: string): string {
  const el = fixture('border:1px solid var(' + token + ');', '');
  const colour = getComputedStyle(el).borderTopColor;
  el.remove();
  return colour;
}

/** Polls `read` until it returns `want`, or gives up after `ms`; the last read either way. */
async function pollFor(read: () => string, want: string, ms: number): Promise<string> {
  const start = performance.now();
  let last = read();
  while (last !== want && performance.now() - start < ms) {
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    last = read();
  }
  return last;
}

// :focus matches only inside a focused window, which can get focus a moment after the page
// loads, so wait for it, bounded, before measuring the ring.
//
// The ring is the composer SHELL's border-color, JS-driven (Composer.tsx's onFocus/onBlur
// set t2v-composer-focused; an earlier change removed the native `.t2v-input:focus`
// outline this probe used to read, matching the TS chat's own shell-darkens-on-focus design
// instead of an outline). A later restyle changed what "darkens" means: the unfocused
// border is hairline-strong ALWAYS now, and focus turns it Teal (--t2v-primary)
// plus a soft box-shadow ring, not a darker hairline. `border-color` transitions over
// 0.15s, so this polls rather than reading once right after `.focus()` — a single read can
// catch it mid-transition.
async function focusRingProbe(): Promise<Probe> {
  const input = document.querySelector<HTMLTextAreaElement>('.t2v-input');
  const shell = document.querySelector<HTMLElement>('[data-slot="aui_composer-shell"]');
  if (!input || !shell) {
    return pass('css:focus-ring', 'composer shell borders Teal on focus', false, (input ? '' : '.t2v-input') + (shell ? '' : ' [data-slot="aui_composer-shell"]') + ' not found');
  }
  const waited = await waitForWindowFocus(5000);
  input.focus();
  const teal = resolvedBorderColor('--t2v-primary');
  const borderColor = await pollFor(() => getComputedStyle(shell).borderTopColor, teal, 500);
  const style = getComputedStyle(shell);
  const ring = style.borderTopStyle === 'solid' && borderColor === teal;
  return pass(
    'css:focus-ring',
    // Not "ring": .t2v-input has outline: none, and this is the
    // JS-driven composer shell's 1px border turning Teal on focus, the same claim the
    // detail below proves — a reader of the guard report should not be misled into
    // expecting an actual outline.
    'composer shell borders Teal on focus',
    ring,
    style.borderTopStyle + ' ' + style.borderTopWidth + ' ' + borderColor + ' (the page draws solid 1px ' + teal +
      '); document.hasFocus ' + String(document.hasFocus()) + ' after ' + String(waited) + ' ms',
  );
}

async function fontProbe(): Promise<Probe> {
  try {
    const faces = await document.fonts.load('500 16px "Space Grotesk"');
    const loaded = faces.filter((f) => f.status === 'loaded').length;
    return pass('font', 'Space Grotesk loaded', loaded > 0, String(loaded) + ' face(s) loaded via document.fonts');
  } catch (err) {
    return pass('font', 'Space Grotesk loaded', false, String(err));
  }
}

function storageProbe(): Probe {
  try {
    const tokenStored = !!sessionStorage.getItem(TOKEN_KEY);
    const hashGone = location.hash.indexOf('t=') === -1;
    return pass('storage', 'sessionStorage holds the token; the hash is stripped', tokenStored && hashGone, 'token stored ' + String(tokenStored) + ', hash stripped ' + String(hashGone));
  } catch (err) {
    return pass('storage', 'sessionStorage holds the token; the hash is stripped', false, String(err));
  }
}

/** The page's own stream: fetch + ReadableStream with the bearer header, live, never fallen back. */
function fetchStreamProbe(store: PageStore): Probe {
  const { conn, stats } = store.latest();
  const ok = conn.mode === 'fetch-stream' && !conn.fellBack && conn.phase === 'live' && stats.snapshots >= 1;
  return pass('sse:fetch-stream', 'SSE over fetch + ReadableStream (Authorization header)', ok, conn.mode + ', ' + conn.phase + ', fellBack ' + String(conn.fellBack) + ', snapshots ' + String(stats.snapshots));
}

/** The fallback: EventSource with ?t= must get a snapshot too. */
function eventSourceProbe(token: string): Promise<Probe> {
  return new Promise((resolve) => {
    const started = performance.now();
    const source = new EventSource('/events?t=' + encodeURIComponent(token));
    const finish = (ok: boolean, detail: string) => {
      window.clearTimeout(timer);
      source.close();
      resolve(pass('sse:eventsource', 'SSE over EventSource (?t= fallback)', ok, detail));
    };
    const timer = window.setTimeout(() => finish(false, 'no snapshot within 5 s'), 5000);
    source.addEventListener('snapshot', (e) => {
      const seq = (JSON.parse((e as MessageEvent<string>).data) as { seq?: unknown }).seq;
      finish(typeof seq === 'number', 'snapshot seq ' + String(seq) + ' after ' + String(Math.round(performance.now() - started)) + ' ms');
    });
    source.onerror = () => finish(false, 'EventSource error');
  });
}

/**
 * The one approval rule with no DOM: "Always allow" is offered only for a client tool the
 * host has not marked destructive. The scripted run clicks the button on a tool that is
 * not destructive; this proves the other three answers on the same Chromium.
 */
function alwaysAllowPolicyProbe(): Probe {
  const plain = { activity: 'Loading sample data', destructive: false, warning: null };
  const danger = { activity: 'Capturing the view', destructive: true, warning: null };
  const answers = [
    mayAlwaysAllow('client', plain),
    mayAlwaysAllow(undefined, undefined),
    mayAlwaysAllow('client', danger),
    mayAlwaysAllow('engine', plain),
    mayAlwaysAllow('something-new', plain),
  ];
  const want = [true, true, false, false, false];
  const ok = answers.every((a, i) => a === want[i]);
  return pass(
    'policy:always-allow',
    '“Always allow” only for a client tool that is not destructive',
    ok,
    'client/plain, none/none, client/destructive, engine, unknown kind → ' + answers.join(', '),
  );
}

/** The environment lines: reported, never judged. */
function infoProbes(): Probe[] {
  const major = /Chrome\/(\d+)/.exec(navigator.userAgent);
  return [
    info('ua', 'navigator.userAgent', navigator.userAgent),
    info('chromium', 'Chromium major version', major ? major[1]! : 'not found'),
    info('secure-context', 'window.isSecureContext', String(window.isSecureContext)),
    info('weakref', 'typeof WeakRef / FinalizationRegistry', typeof (globalThis as { WeakRef?: unknown }).WeakRef + ' / ' + typeof (globalThis as { FinalizationRegistry?: unknown }).FinalizationRegistry),
    info('heap-api', 'performance.memory present', String(monitors.heap.supported)),
    info('hand-rolled', 'polyfills installed by hand', INSTALLED_BY_HAND.join(', ') || 'none needed'),
  ];
}

/**
 * Run one probe, or one group of them, and never let it take the report with it. A probe
 * that throws — `cssProbes` is five probes in one function, and the whole group would
 * otherwise be lost, along with every probe after it and the scripted run — reports a
 * FAIL for each id it owes the judge, naming the throw. The judge requires a fixed list of
 * probe ids, so a missing one is not something it can be handed.
 */
async function group(ids: string[], run: () => Probe[] | Promise<Probe[]>): Promise<Probe[]> {
  try {
    return await run();
  } catch (err) {
    return ids.map((id) => pass(id, id, false, 'the probe itself threw: ' + String(err)));
  }
}

export async function runProbes(store: PageStore): Promise<Probe[]> {
  const out: Probe[] = [];
  out.push(...(await group(['ua', 'chromium', 'secure-context', 'weakref', 'heap-api', 'hand-rolled'], () => infoProbes())));
  out.push(...(await group(API_PROBES.map((p) => 'api:' + p.name), () => apiProbes())));
  out.push(...(await group(['css:custom-properties', 'css:flex-margins', 'css:flex-gap', 'css:shimmer', 'css:composer-row'], () => cssProbes())));
  out.push(...(await group(['css:focus-ring'], async () => [await focusRingProbe()])));
  out.push(...(await group(['storage'], () => [storageProbe()])));
  out.push(...(await group(['policy:always-allow'], () => [alwaysAllowPolicyProbe()])));
  out.push(...(await group(['font'], async () => [await fontProbe()])));
  out.push(...(await group(['sse:fetch-stream'], () => [fetchStreamProbe(store)])));
  out.push(...(await group(['sse:eventsource'], async () => [await eventSourceProbe(store.token)])));
  return out;
}
