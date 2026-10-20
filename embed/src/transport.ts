/**
 * Talking to the UI server: the token, the event stream, commands and uploads (the
 * UI server's wire contract).
 *
 * - The token arrives once in the URL fragment (`#t=…`), moves to sessionStorage and the
 *   hash is stripped. Every API request sends `Authorization: Bearer <token>`.
 * - `/events` is read with fetch + ReadableStream (measured streaming on Slicer's
 *   Chromium 83). EventSource with `?t=` is the fallback when a response cannot stream.
 * - An `ops` event applies only when its `base` is the seq the page holds. A gap resumes
 *   with `Last-Event-ID: <seq>`; an op that does not apply reconnects WITHOUT it, so the
 *   server sends a snapshot instead of replaying the same op forever.
 * - `stopped`, 401 and 403 mean this chat is over: the page never retries them.
 */
import type { Attachment } from '@sdk/types';
import { SseParser, type SseEvent } from './sse';
import type { Command, OpsEvent, SnapshotEvent } from './wire';

export const TOKEN_KEY = 't2v_ui_token';

/** Silence longer than this (the server sends `: hb` after 15 s) means a dead stream. */
export const IDLE_MS = 30000;
/** The server sends the first event at once; nothing in this long means no streaming. */
export const FIRST_EVENT_MS = 5000;
/** Consecutive failed connections before the page decides the server is gone. */
export const MAX_FAILURES = 3;
/** A connect that has not answered in this long is dead: the server is on this machine. */
export const CONNECT_MS = 10000;
/** A command POST that has not answered in this long is dead. */
export const COMMAND_MS = 15000;
/** An upload that has not answered in this long is dead. The Python SDK's own upload
 * budget behind `/upload` is 120 s (`Talk2View`'s `upload_timeout`, `client.py`); this
 * gives the server room to finish and answer before the page gives up on its behalf. */
export const UPLOAD_MS = 130000;
/** The least a reconnect waits when the last one ended for the same reason. */
export const RETRY_FLOOR_MS = 250;
/** The first wait for a server holding all its streams; it doubles up to BUSY_MAX_MS. */
export const BUSY_MS = 2000;
export const BUSY_MAX_MS = 15000;

export interface TransportEnv {
  fetch: typeof fetch;
  EventSource: typeof EventSource | undefined;
  setTimeout: (fn: () => void, ms: number) => number;
  clearTimeout: (id: number | undefined) => void;
}

export function browserEnv(): TransportEnv {
  return {
    fetch: (input, init) => fetch(input, init),
    EventSource: typeof EventSource === 'function' ? EventSource : undefined,
    setTimeout: (fn, ms) => window.setTimeout(fn, ms),
    clearTimeout: (id) => window.clearTimeout(id),
  };
}

/** The `t` of a `#t=…&…` fragment, decoded; null when there is none. */
export function tokenFromHash(hash: string): string | null {
  const match = /(?:^#|&)t=([^&]+)/.exec(hash);
  if (!match) return null;
  try {
    return decodeURIComponent(match[1]!);
  } catch {
    return null;
  }
}

let memoryToken: string | null = null;

/** The launch token: from the fragment (then stored, and the hash stripped), else from sessionStorage. */
export function bootstrapToken(): string | null {
  const fromHash = tokenFromHash(location.hash);
  let token = fromHash;
  try {
    if (fromHash) sessionStorage.setItem(TOKEN_KEY, fromHash);
    else token = sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // No sessionStorage: the in-memory copy serves this load.
  }
  if (fromHash !== null) history.replaceState(null, '', location.pathname + location.search);
  memoryToken = token || memoryToken;
  return memoryToken;
}

export type Phase = 'connecting' | 'live' | 'reconnecting' | 'over';

export interface ConnStatus {
  phase: Phase;
  mode: 'fetch-stream' | 'eventsource';
  reconnects: number;
  fellBack: boolean;
  /** Why the last stream ended, or why the chat is over. */
  reason: string | null;
}

export const INITIAL_CONN: ConnStatus = {
  phase: 'connecting',
  mode: 'fetch-stream',
  reconnects: 0,
  fellBack: false,
  reason: null,
};

export type OpsOutcome = 'applied' | 'gap' | 'broken';

export interface StreamSink {
  /** The seq the page holds, sent as `Last-Event-ID` on a resume; null before a snapshot. */
  seq(): number | null;
  snapshot(event: SnapshotEvent): void;
  /** 'gap': `base` is not the page's seq. 'broken': an op did not apply. */
  ops(event: OpsEvent): OpsOutcome;
  status(status: ConnStatus): void;
}

interface Stream {
  close(): void;
}

interface ErrorBody {
  type: string;
  message: string;
}

async function readError(res: Response): Promise<ErrorBody> {
  try {
    const body = (await res.json()) as { error?: { type?: unknown; message?: unknown } };
    const type = typeof body.error?.type === 'string' ? body.error.type : 'http_' + String(res.status);
    const message = typeof body.error?.message === 'string' ? body.error.message : 'Request failed.';
    return { type, message };
  } catch {
    return { type: 'http_' + String(res.status), message: 'Request failed.' };
  }
}

/** Keep one `/events` stream open until `close()` or until the chat is over. */
export function connectEvents(token: string, sink: StreamSink, env: TransportEnv = browserEnv()): Stream {
  let conn: ConnStatus = { ...INITIAL_CONN };
  let closed = false;
  let generation = 0;
  let failures = 0;
  let busyTries = 0;
  /** No reconnect may open before this (`Date.now()`), whatever ended the last stream. */
  let nextOpenAt = 0;
  let deliveredEver = false;
  let current: Stream | null = null;
  let retryTimer: number | undefined;
  let idleTimer: number | undefined;
  let firstTimer: number | undefined;

  const report = (next: Partial<ConnStatus>) => {
    conn = { ...conn, ...next };
    sink.status(conn);
  };

  const stopTimers = () => {
    env.clearTimeout(retryTimer);
    env.clearTimeout(idleTimer);
    env.clearTimeout(firstTimer);
  };

  const drop = () => {
    generation += 1; // silences every callback of the stream being dropped
    stopTimers();
    if (current) current.close();
    current = null;
  };

  const over = (reason: string) => {
    if (closed) return;
    closed = true;
    drop();
    report({ phase: 'over', reason });
  };

  const reconnect = (resume: boolean, reason: string, delayMs = 0) => {
    if (closed) return;
    drop();
    // A condition the server repeats — a gap, an op that will not apply, an unreadable
    // event, a stream that ends at once — would otherwise spin the page: measured at about
    // 400 connects a second, each leaving a stream attached server-side for up to 15 s
    // against its cap of 16. The rate is what matters, not the reason: keyed on the reason,
    // two of them taking turns spun just as fast. A reconnect after a quiet spell is still
    // immediate, because the slot it has to wait for is already in the past.
    // `Date.now()` is not monotonic: a wall clock set back an hour would otherwise hold
    // the next reconnect for that hour. The floor itself is never more than RETRY_FLOOR_MS.
    const at = Date.now();
    const wait = Math.max(delayMs, Math.min(nextOpenAt - at, RETRY_FLOOR_MS));
    nextOpenAt = at + wait + RETRY_FLOOR_MS;
    report({ phase: 'reconnecting', reconnects: conn.reconnects + 1, reason });
    retryTimer = env.setTimeout(() => open(resume), wait);
  };

  const failed = (reason: string) => {
    failures += 1;
    if (failures >= MAX_FAILURES) over('unreachable: ' + reason);
    else reconnect(true, reason, 250 * Math.pow(2, failures - 1));
  };

  /**
   * The far side is busy, not gone: wait longer each time, for as long as it takes, and
   * never end the chat. Two things look like a failure and are not.
   *
   * - The server is holding all 16 of its streams. It drops the stream of a page that went
   *   away after 15 s, so this is a queue to wait in.
   * - A connect timed out. The host itself may be frozen: the first one runs its tools on
   *   Slicer's main thread, and one sample-data download held that thread for about a
   *   minute, during which nothing answers and no heartbeat is sent — while this page, in
   *   its own renderer process, goes on counting. Ending the chat there would throw a
   *   conversation away over a tool call that was about to finish.
   *
   * Only a fast, definite refusal is a server that has gone, and only that counts toward
   * MAX_FAILURES (`failed`).
   */
  const busy = (reason = 'too_many_streams') => {
    busyTries += 1;
    reconnect(true, reason, Math.min(BUSY_MS * Math.pow(2, busyTries - 1), BUSY_MAX_MS));
  };

  /** What a refused request means for the page. */
  const refused = (res: Response, error: ErrorBody) => {
    if (res.status === 401 || res.status === 403 || error.type === 'stopped') over(error.type);
    else if (error.type === 'too_many_streams') busy();
    else failed(error.type);
  };

  const fallBack = (reason: string) => {
    if (conn.mode === 'eventsource' || !env.EventSource) {
      failed(reason);
      return;
    }
    report({ mode: 'eventsource', fellBack: true });
    reconnect(false, reason);
  };

  /** Handles one parsed event; false when it dropped the stream. */
  const dispatch = (ev: SseEvent): boolean => {
    let payload: unknown;
    try {
      payload = JSON.parse(ev.data);
    } catch {
      reconnect(false, 'unreadable event');
      return false;
    }
    if (!deliveredEver || conn.phase !== 'live') {
      deliveredEver = true;
      failures = 0;
      busyTries = 0;
      env.clearTimeout(firstTimer);
      report({ phase: 'live', reason: null });
    }
    if (ev.event === 'snapshot') {
      sink.snapshot(payload as SnapshotEvent);
      return true;
    }
    if (ev.event === 'ops') {
      const outcome = sink.ops(payload as OpsEvent);
      if (outcome === 'gap') {
        reconnect(true, 'gap');
        return false;
      }
      if (outcome === 'broken') {
        reconnect(false, 'an op did not apply');
        return false;
      }
    }
    return true;
  };

  const armIdle = (mine: number) => {
    env.clearTimeout(idleTimer);
    idleTimer = env.setTimeout(() => {
      if (mine === generation) reconnect(true, 'silent for ' + String(IDLE_MS / 1000) + ' s');
    }, IDLE_MS);
  };

  const openFetch = (mine: number, resume: boolean): Stream => {
    const controller = new AbortController();
    const live = () => !closed && mine === generation;
    const headers: Record<string, string> = {
      Authorization: 'Bearer ' + token,
      Accept: 'text/event-stream',
    };
    const seq = sink.seq();
    if (resume && seq !== null) headers['Last-Event-ID'] = String(seq);
    // Chromium 83 has no AbortSignal.timeout: a connect that never answers is aborted by
    // hand. Only the first connect of a page has the first-event timer to fall back on.
    let timedOut = false;
    let connectTimer: number | undefined = env.setTimeout(() => {
      timedOut = true;
      if (live()) controller.abort();
    }, CONNECT_MS);
    void (async () => {
      let res: Response;
      try {
        res = await env.fetch('/events', { headers, cache: 'no-store', signal: controller.signal });
      } catch (err) {
        // A connect that is merely slow is a host to wait for, not a server that has gone.
        if (live()) {
          if (timedOut) busy('the connect timed out');
          else failed('fetch failed: ' + String(err));
        }
        return;
      } finally {
        env.clearTimeout(connectTimer);
        connectTimer = undefined;
      }
      if (!live()) return;
      if (!res.ok) {
        const error = await readError(res);
        if (!live()) return;
        refused(res, error);
        return;
      }
      if (!res.body || typeof res.body.getReader !== 'function') {
        fallBack('the response cannot stream');
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8');
      const parser = new SseParser();
      armIdle(mine);
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (!live()) return;
          if (done) break;
          armIdle(mine);
          for (const ev of parser.feed(decoder.decode(value, { stream: true }))) {
            if (!dispatch(ev)) return;
          }
        }
        reconnect(true, 'the stream ended');
      } catch (err) {
        if (live()) reconnect(true, 'read failed: ' + String(err), 250);
      }
    })();
    return {
      close: () => {
        env.clearTimeout(connectTimer);
        controller.abort();
      },
    };
  };

  /**
   * Why did the EventSource fail? It never says: the browser reports an error and hides
   * the status. `HEAD /events` runs the whole gate — peer, token, stopped — and then
   * answers 405, so it tells a page that has been refused (401, 403, `stopped`) or a
   * server holding every stream from a connection that merely dropped.
   */
  const classify = (mine: number, fallback: string): void => {
    const live = () => !closed && mine === generation;
    void (async () => {
      let res: Response;
      try {
        res = await env.fetch('/events', {
          method: 'HEAD',
          headers: { Authorization: 'Bearer ' + token },
          cache: 'no-store',
        });
      } catch {
        if (live()) failed(fallback);
        return;
      }
      if (!live()) return;
      if (res.ok || res.status === 405) {
        failed(fallback); // the server is well: this stream is not
        return;
      }
      const error = await readError(res);
      if (live()) refused(res, error);
    })();
  };

  const openEventSource = (mine: number): Stream => {
    const ES = env.EventSource!;
    // EventSource cannot set headers: the server takes ?t= on GET /events only.
    const source = new ES('/events?t=' + encodeURIComponent(token));
    const live = () => !closed && mine === generation;
    const forward = (name: string) => (e: Event) => {
      if (!live()) return;
      const message = e as MessageEvent<string>;
      dispatch({ event: name, id: message.lastEventId || null, data: message.data });
    };
    source.addEventListener('snapshot', forward('snapshot'));
    source.addEventListener('ops', forward('ops'));
    source.onerror = () => {
      source.close(); // this page owns the reconnect policy
      if (live()) classify(mine, 'EventSource error');
    };
    return { close: () => source.close() };
  };

  const open = (resume: boolean) => {
    if (closed) return;
    generation += 1;
    const mine = generation;
    if (conn.phase !== 'reconnecting') report({ phase: 'connecting' });
    current = conn.mode === 'fetch-stream' ? openFetch(mine, resume) : openEventSource(mine);
    // The server sends the first event at once. In fetch mode its absence means the runtime
    // cannot stream, and the page falls back; in EventSource mode, which surfaces no
    // heartbeat, it is the page's only sign that a silent stream is dead.
    if (!deliveredEver) {
      firstTimer = env.setTimeout(() => {
        if (mine === generation && !deliveredEver) fallBack('no event within ' + String(FIRST_EVENT_MS / 1000) + ' s');
      }, FIRST_EVENT_MS);
    }
  };

  open(false);
  return {
    close() {
      closed = true;
      drop();
    },
  };
}

export interface Refusal {
  status: number;
  type: string;
  message: string;
}

/** POST /command. Null when the server queued it (202); otherwise why not. */
export async function postCommand(
  token: string,
  id: string,
  command: Command,
  env: TransportEnv = browserEnv(),
): Promise<Refusal | null> {
  const body = JSON.stringify({ id, ...command });
  for (let attempt = 1; ; attempt++) {
    let res: Response;
    // The store posts commands one after another, so one that never answers would hold up
    // every command after it. Chromium 83 has no AbortSignal.timeout.
    const controller = new AbortController();
    const timer = env.setTimeout(() => controller.abort(), COMMAND_MS);
    try {
      res = await env.fetch('/command', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
        body,
        cache: 'no-store',
        signal: controller.signal,
      });
    } catch (err) {
      return { status: 0, type: 'network_error', message: String(err) };
    } finally {
      env.clearTimeout(timer);
    }
    if (res.status === 202) return null;
    const error = await readError(res);
    // 256 commands waiting: worth trying again. Nothing else is.
    if (res.status === 503 && error.type === 'queue_full' && attempt < 4) {
      await new Promise<void>((resolve) => env.setTimeout(resolve, 200 * attempt));
      continue;
    }
    return { status: res.status, ...error };
  }
}

export type UploadResult = { ok: true; attachment: Attachment } | ({ ok: false } & Refusal);

/**
 * POST /upload with one file. On 409 `account_changed` the caller asks for the file
 * again. Nothing waits forever: an upload that has not answered in `UPLOAD_MS` is
 * aborted, same as `postCommand`'s own `COMMAND_MS` guard, so a frozen host or a dead
 * connection still leaves the end-user with a refusal and a way out (the composer's
 * Remove button) instead of a chip stuck on "Uploading…" for good.
 *
 * `signal`, when given, is a second way out: the caller's own abort (Remove clicked
 * mid-upload, an identity change, New chat) ends the request early too. Chromium 83 has
 * no `AbortSignal.any()`, so the two signals are combined by hand — whichever fires
 * first aborts the single `AbortController` the fetch actually uses.
 */
export async function uploadFile(
  token: string,
  file: File,
  env: TransportEnv = browserEnv(),
  signal?: AbortSignal,
): Promise<UploadResult> {
  const form = new FormData();
  form.append('file', file, file.name);
  const controller = new AbortController();
  const timer = env.setTimeout(() => controller.abort(), UPLOAD_MS);
  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) controller.abort();
    else signal.addEventListener('abort', onExternalAbort);
  }
  let res: Response;
  try {
    res = await env.fetch('/upload', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
      body: form,
      cache: 'no-store',
      signal: controller.signal,
    });
  } catch (err) {
    return { ok: false, status: 0, type: 'network_error', message: String(err) };
  } finally {
    env.clearTimeout(timer);
    signal?.removeEventListener('abort', onExternalAbort);
  }
  if (res.ok) {
    // Anything may sit between the page and the server: an answer that is not a
    // complete attachment is a refusal to report, never an exception thrown at the
    // composer. All four fields are checked, not just `id`: a response missing
    // `filename`/`mime_type`/`size_bytes` would otherwise pass as "complete" and reach
    // `send()` with `undefined` where the wire and the message's own chip need a string.
    const body = (await res.json().catch(() => null)) as Attachment | null;
    if (
      !body ||
      typeof body.id !== 'string' ||
      typeof body.filename !== 'string' ||
      typeof body.mime_type !== 'string' ||
      typeof body.size_bytes !== 'number'
    ) {
      return { ok: false, status: res.status, type: 'bad_response', message: 'The upload could not be read.' };
    }
    return { ok: true, attachment: body };
  }
  return { ok: false, status: res.status, ...(await readError(res)) };
}
