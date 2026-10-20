/**
 * Always-on instrumentation for the runtime guard, installed before React mounts so that
 * nothing thrown during start-up is missed.
 */
interface HeapSample {
  t: number;
  used: number;
}

export const monitors = {
  errorCount: 0,
  errors: [] as string[],
  cspViolations: 0,
  jank: { frames: 0, longGaps: 0, maxGapMs: 0, streamingMs: 0 },
  heap: { supported: false, first: null as number | null, last: null as number | null, max: null as number | null, samples: [] as HeapSample[] },
};

function record(kind: string, detail: string): void {
  monitors.errorCount += 1;
  if (monitors.errors.length < 50) monitors.errors.push(kind + ': ' + detail.slice(0, 400));
}

/**
 * The guard's own way of putting a failure of ITS OWN into the same counters the judge
 * already reads: the watchdog giving up on a page that never drew a thread, a probe that
 * threw, a run that ran out of time (index.ts). A self-test that gave up must never come
 * home looking like a clean run, and `live.errorCount` is the one number every caller
 * checks. The line reads `guard: <what happened>`.
 */
export function recordGuardFailure(detail: string): void {
  record('guard', detail);
}

// Capture phase: also sees resource load failures (a font or script 404).
window.addEventListener(
  'error',
  (e: Event) => {
    const ev = e as ErrorEvent;
    if (ev.message) {
      record('error', ev.message + ' @ ' + (ev.filename || '?') + ':' + String(ev.lineno || 0));
      return;
    }
    const el = e.target as (HTMLElement & { src?: string; href?: string }) | null;
    record('resource', el ? el.tagName + ' ' + (el.src || el.href || '') : 'unknown');
  },
  true,
);

window.addEventListener('unhandledrejection', (e: PromiseRejectionEvent) => {
  const reason = e.reason as { message?: string } | string | undefined;
  record('unhandledrejection', typeof reason === 'string' ? reason : (reason && reason.message) || String(reason));
});

document.addEventListener('securitypolicyviolation', (e: SecurityPolicyViolationEvent) => {
  monitors.cspViolations += 1;
  record('csp', e.violatedDirective + ' ' + e.blockedURI);
});

// ── jank meter: requestAnimationFrame gaps above 100 ms while a reply streams (reported, never judged) ──
let streaming = false;
let lastFrame = 0;

export function setStreaming(on: boolean): void {
  if (on && !streaming) lastFrame = 0;
  streaming = on;
}

function frame(now: number): void {
  if (streaming && document.visibilityState === 'visible') {
    if (lastFrame > 0) {
      const gap = now - lastFrame;
      monitors.jank.frames += 1;
      monitors.jank.streamingMs += gap;
      if (gap > 100) monitors.jank.longGaps += 1;
      if (gap > monitors.jank.maxGapMs) monitors.jank.maxGapMs = Math.round(gap);
    }
    lastFrame = now;
  } else {
    lastFrame = 0;
  }
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ── JS heap: Chromium-only and coarse (reported, never judged) ──
function sampleHeap(): void {
  const memory = (performance as unknown as { memory?: { usedJSHeapSize: number } }).memory;
  if (!memory) return;
  const used = memory.usedJSHeapSize;
  const heap = monitors.heap;
  heap.supported = true;
  if (heap.first === null) heap.first = used;
  heap.last = used;
  heap.max = heap.max === null ? used : Math.max(heap.max, used);
  if (heap.samples.length < 240) heap.samples.push({ t: Math.round(performance.now()), used });
}
sampleHeap();
setInterval(sampleHeap, 5000);
