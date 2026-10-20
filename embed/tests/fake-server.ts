/**
 * A scripted stand-in for the UI server's HTTP side, for the transport and store tests:
 * every fetch is recorded, `/events` answers with a stream the test writes into, and
 * `/command` and `/upload` answer what the test says.
 */
import type { TransportEnv } from '../src/transport';

export interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface EventStream {
  push(text: string): void;
  end(): void;
  /** The connection drops mid-stream: the body errors while the page is reading it. */
  fail(): void;
  closed: boolean;
}

type Answer = Response | Promise<Response> | 'network-error';

export class FakeServer {
  calls: Call[] = [];
  streams: EventStream[] = [];
  /** What the next `/events` requests answer; a stream when the list is empty. */
  eventAnswers: Answer[] = [];
  /** What the next `/command` requests answer; 202 when the list is empty. */
  commandAnswers: Array<Response | Promise<Response>> = [];
  /** What the next `HEAD /events` liveness probes answer; 405, as the server does. */
  probeAnswers: Answer[] = [];
  uploadAnswer: () => Response | Promise<Response> = () =>
    json(200, { id: 'att-1', filename: 'a.png', mime_type: 'image/png', size_bytes: 4 });
  EventSource: TransportEnv['EventSource'] = undefined;

  env(): TransportEnv {
    return {
      fetch: (input, init) => this.fetch(String(input), init ?? {}),
      EventSource: this.EventSource,
      setTimeout: (fn, ms) => setTimeout(fn, ms) as unknown as number,
      clearTimeout: (id) => clearTimeout(id),
    };
  }

  commands(): Array<Record<string, unknown>> {
    return this.calls.filter((c) => c.url === '/command').map((c) => c.body as Record<string, unknown>);
  }

  /** The stream opens (`GET /events`), not the liveness probes. */
  eventCalls(): Call[] {
    return this.calls.filter((c) => c.url.startsWith('/events') && c.method !== 'HEAD');
  }

  probes(): Call[] {
    return this.calls.filter((c) => c.method === 'HEAD');
  }

  private async fetch(url: string, init: RequestInit): Promise<Response> {
    const headers = { ...((init.headers as Record<string, string>) ?? {}) };
    let body: unknown = init.body;
    if (typeof init.body === 'string') body = JSON.parse(init.body);
    const method = init.method ?? 'GET';
    this.calls.push({ url, method, headers, body });
    const signal = init.signal ?? null;
    if (url === '/events' && method === 'HEAD') {
      const probe = this.probeAnswers.shift();
      if (probe === 'network-error') throw new TypeError('Failed to fetch');
      return abortable(probe ?? json(405, { ok: false, error: { type: 'method_not_allowed', message: 'Use GET.' } }), signal);
    }
    if (url === '/events') {
      const answer = this.eventAnswers.shift();
      if (answer === 'network-error') throw new TypeError('Failed to fetch');
      if (answer) return abortable(answer, signal);
      return this.openStream(signal);
    }
    if (url === '/command') return abortable(this.commandAnswers.shift() ?? json(202, { ok: true, id: null }), signal);
    if (url === '/upload') return abortable(this.uploadAnswer(), signal);
    return json(404, { ok: false, error: { type: 'not_found', message: 'nope' } });
  }

  private openStream(signal: AbortSignal | null): Response {
    const encoder = new TextEncoder();
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const stream: EventStream = {
      closed: false,
      push: (text) => controller.enqueue(encoder.encode(text)),
      end: () => {
        stream.closed = true;
        controller.close();
      },
      fail: () => {
        stream.closed = true;
        controller.error(new TypeError('network error'));
      },
    };
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    signal?.addEventListener('abort', () => {
      if (stream.closed) return;
      stream.closed = true;
      controller.error(new DOMException('aborted', 'AbortError'));
    });
    this.streams.push(stream);
    return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  }
}

/** A pending answer, rejected as `fetch` rejects when the caller aborts first — including
 * a signal that was ALREADY aborted before the call, as a real `fetch` also refuses
 * synchronously rather than hanging on a listener for an event that already fired. */
function abortable(answer: Response | Promise<Response>, signal: AbortSignal | null): Response | Promise<Response> {
  if (!signal || answer instanceof Response) return answer;
  if (signal.aborted) return Promise.reject(new DOMException('aborted', 'AbortError'));
  return new Promise<Response>((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')));
    answer.then(resolve, reject);
  });
}

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

export function sse(event: string, payload: unknown): string {
  return 'event: ' + event + '\ndata: ' + JSON.stringify(payload) + '\n\n';
}

/** Let every pending promise and zero-delay timer run. */
export async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setTimeout(resolve, 0));
}
