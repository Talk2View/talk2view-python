import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  BUSY_MAX_MS,
  BUSY_MS,
  COMMAND_MS,
  CONNECT_MS,
  connectEvents,
  FIRST_EVENT_MS,
  IDLE_MS,
  postCommand,
  RETRY_FLOOR_MS,
  TOKEN_KEY,
  tokenFromHash,
  UPLOAD_MS,
  uploadFile,
  type ConnStatus,
  type OpsOutcome,
  type StreamSink,
} from '../src/transport';
import type { OpsEvent, SnapshotEvent } from '../src/wire';
import { FakeServer, json, settle, sse } from './fake-server';

class Sink implements StreamSink {
  held: number | null = null;
  snapshots: SnapshotEvent[] = [];
  batches: OpsEvent[] = [];
  statuses: ConnStatus[] = [];
  answer: OpsOutcome | null = null;

  seq(): number | null {
    return this.held;
  }
  snapshot(event: SnapshotEvent): void {
    this.snapshots.push(event);
    this.held = event.seq;
  }
  ops(event: OpsEvent): OpsOutcome {
    if (this.answer) return this.answer;
    if (event.base !== this.held) return 'gap';
    this.batches.push(event);
    this.held = event.seq;
    return 'applied';
  }
  status(status: ConnStatus): void {
    this.statuses.push(status);
  }
  get last(): ConnStatus {
    return this.statuses[this.statuses.length - 1]!;
  }
}

const snapshot = (seq: number) => sse('snapshot', { seq, state: { messages: [] } });
const ops = (seq: number, base: number) =>
  sse('ops', { seq, base, ops: [{ type: 'set', path: ['isLoading'], value: true }] });
const refusal = (type: string) => json(503, { ok: false, error: { type, message: 'no' } });

/** An EventSource that records what the page does with it, and answers as a test says. */
function fakeEventSource(behave: (source: Recorded) => void = () => {}) {
  const opened: Recorded[] = [];
  class Fake {
    onerror: (() => void) | null = null;
    listeners = new Map<string, (e: Event) => void>();
    closes = 0;
    constructor(public url: string) {
      opened.push(this as unknown as Recorded);
      queueMicrotask(() => behave(this as unknown as Recorded));
    }
    addEventListener(name: string, fn: (e: Event) => void) {
      this.listeners.set(name, fn);
    }
    close() {
      this.closes++;
    }
  }
  return { opened, EventSource: Fake as unknown as typeof EventSource };
}

interface Recorded {
  url: string;
  closes: number;
  onerror: (() => void) | null;
  listeners: Map<string, (e: Event) => void>;
}

describe('tokenFromHash', () => {
  it('reads t from the fragment, decoded', () => {
    expect(tokenFromHash('#t=abc-_1')).toBe('abc-_1');
    expect(tokenFromHash('#auto=1&t=a%2Bb')).toBe('a+b');
    expect(tokenFromHash('')).toBeNull();
    expect(tokenFromHash('#tt=x')).toBeNull();
    expect(tokenFromHash('#t=%E0%A4%A')).toBeNull();
  });
});

describe('connectEvents', () => {
  let server: FakeServer;
  let sink: Sink;

  beforeEach(() => {
    server = new FakeServer();
    sink = new Sink();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('opens /events with the token, then hands on the snapshot and the ops', async () => {
    const stream = connectEvents('tok', sink, server.env());
    await settle();
    const [call] = server.eventCalls();
    expect(call!.headers).toEqual({ Authorization: 'Bearer tok', Accept: 'text/event-stream' });
    expect(call!.url).toBe('/events'); // the token is a header: never a query, never a log line
    server.streams[0]!.push(': t2v-ui stream open\n\n' + snapshot(4));
    server.streams[0]!.push(ops(5, 4));
    await settle();
    expect(sink.snapshots.map((s) => s.seq)).toEqual([4]);
    expect(sink.batches.map((b) => b.seq)).toEqual([5]);
    expect(sink.last.phase).toBe('live');
    stream.close();
    await settle();
    expect(server.streams[0]!.closed).toBe(true); // and the server's stream goes with it
  });

  it('resumes with Last-Event-ID after a gap', async () => {
    connectEvents('tok', sink, server.env());
    await settle();
    server.streams[0]!.push(snapshot(4) + ops(9, 7));
    await settle();
    expect(server.streams[0]!.closed).toBe(true);
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBe('4');
    expect(sink.last).toMatchObject({ phase: 'reconnecting', reason: 'gap', reconnects: 1 });
  });

  it('reconnects WITHOUT Last-Event-ID when an op does not apply', async () => {
    connectEvents('tok', sink, server.env());
    await settle();
    server.streams[0]!.push(snapshot(4));
    await settle();
    sink.answer = 'broken';
    server.streams[0]!.push(ops(5, 4));
    await settle();
    expect(server.eventCalls()).toHaveLength(2);
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBeUndefined();
  });

  it('resumes when the server ends the stream', async () => {
    connectEvents('tok', sink, server.env());
    await settle();
    server.streams[0]!.push(snapshot(4));
    server.streams[0]!.end();
    await settle();
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBe('4');
  });

  it('reconnects WITHOUT Last-Event-ID when an event cannot be read', async () => {
    connectEvents('tok', sink, server.env());
    await settle();
    server.streams[0]!.push(snapshot(4) + 'event: ops\ndata: {oops\n\n');
    await settle();
    expect(server.eventCalls()).toHaveLength(2);
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBeUndefined();
  });

  it('waits before repeating a reconnect for the same reason, but not the first time', async () => {
    // A server that keeps ending the stream at once (or keeps sending a gap, or an op that
    // will not apply) drew about 200 connects in 500 ms, each leaving a stream attached
    // server-side for up to 15 s against its cap of 16.
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(2);
    server.streams[1]!.end();
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS - 1);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(server.eventCalls()).toHaveLength(3);
  });

  it('is not held up by a wall clock set back: the floor never exceeds RETRY_FLOOR_MS', async () => {
    // The floor is kept in Date.now() terms, which is not monotonic: an operating system
    // clock set back an hour between two reconnects once meant the next one waited an hour.
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(2); // the first reconnect: at once
    vi.setSystemTime(Date.now() - 3600000); // the wall clock goes back an hour
    server.streams[1]!.end();
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS - 1);
    expect(server.eventCalls()).toHaveLength(2); // still floored
    await vi.advanceTimersByTimeAsync(1);
    expect(server.eventCalls()).toHaveLength(3); // and no later than the floor
  });

  it('waits for a server holding all its streams, instead of ending the page', async () => {
    // The server keeps a stream of a page that went away for 15 s: `too_many_streams` is a
    // queue to wait in, not a server that is gone.
    vi.useFakeTimers();
    for (let i = 0; i < 4; i++) server.eventAnswers.push(refusal('too_many_streams'));
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(BUSY_MS);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(BUSY_MS * 2);
    expect(server.eventCalls()).toHaveLength(3);
    expect(sink.last).toMatchObject({ phase: 'reconnecting', reason: 'too_many_streams' });
    await vi.advanceTimersByTimeAsync(BUSY_MS * 4);
    expect(server.eventCalls()).toHaveLength(4);
    await vi.advanceTimersByTimeAsync(BUSY_MAX_MS); // the wait is capped, not 16 s and doubling
    expect(server.eventCalls()).toHaveLength(5);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    expect(sink.last.phase).toBe('live');
  });

  it('starts the wait again once a stream has delivered', async () => {
    vi.useFakeTimers();
    server.eventAnswers.push(refusal('too_many_streams'));
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(BUSY_MS);
    expect(server.eventCalls()).toHaveLength(2);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    server.eventAnswers.push(refusal('too_many_streams'));
    server.streams[0]!.end();
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(BUSY_MS - 1);
    expect(server.eventCalls()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(1 + RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(4); // 2 s again, not 4 s
  });

  it('floors every reconnect by the time since the last one, whatever the reason', async () => {
    // Keyed on the reason, two reasons taking turns defeated the floor entirely: a gap
    // alternating with a stream that ends at once ran at about 400 connects a second.
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    const gap = async () => {
      server.streams[server.streams.length - 1]!.push(snapshot(4) + ops(9, 7));
      await vi.advanceTimersByTimeAsync(0);
    };
    const ended = async () => {
      server.streams[server.streams.length - 1]!.end();
      await vi.advanceTimersByTimeAsync(0);
    };

    await gap();
    expect(server.eventCalls()).toHaveLength(2); // the first reconnect is still at once
    await ended();
    expect(server.eventCalls()).toHaveLength(2); // another reason, and floored all the same
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(3);
    await gap();
    expect(server.eventCalls()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(4);
  });

  it('waits out a host that has frozen, and picks the chat up when it thaws', async () => {
    // The first host runs its tools on Slicer's main thread, and one sample-data download
    // froze that thread for about a minute. This page keeps counting in its own process, so
    // all it sees is silence and connects that hang: those must never end the chat.
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    for (let i = 0; i < 3; i++) server.eventAnswers.push(new Promise<Response>(() => {}));

    // The silence is noticed (with the floor as slack, so the exact millisecond of the idle
    // timer does not matter), and then each connect hangs until it is aborted.
    await vi.advanceTimersByTimeAsync(IDLE_MS + RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(CONNECT_MS + BUSY_MS);
    expect(server.eventCalls()).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(CONNECT_MS + BUSY_MS * 2);
    expect(server.eventCalls()).toHaveLength(4);
    await vi.advanceTimersByTimeAsync(CONNECT_MS + BUSY_MS * 4);
    expect(sink.last.phase).toBe('reconnecting'); // over a minute in, and still not over
    expect(server.eventCalls()).toHaveLength(5);

    expect(server.eventCalls()[4]!.headers['Last-Event-ID']).toBe('4');
    server.streams[server.streams.length - 1]!.push(ops(5, 4)); // the host thaws
    await vi.advanceTimersByTimeAsync(0);
    expect(sink.last.phase).toBe('live');
    expect(sink.batches.map((b) => b.seq)).toEqual([5]);
  });

  it('aborts a reconnect the server never answers, then waits and asks again', async () => {
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.push(snapshot(4)); // live: no first-event timer from here on
    await vi.advanceTimersByTimeAsync(0);
    server.eventAnswers.push(new Promise<Response>(() => {})); // a connect that hangs
    server.streams[0]!.end();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(CONNECT_MS - 1);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(BUSY_MS + RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(3);
  });

  it('resumes after the connection drops mid-stream, once it has waited', async () => {
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.fail();
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS);
    expect(server.eventCalls()).toHaveLength(2);
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBe('4');
  });

  it('forgets earlier failures once a stream has delivered', async () => {
    vi.useFakeTimers();
    server.eventAnswers.push('network-error', 'network-error');
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(250);
    await vi.advanceTimersByTimeAsync(500);
    expect(server.eventCalls()).toHaveLength(3);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(0);
    expect(sink.last.phase).toBe('live');
    server.eventAnswers.push('network-error', 'network-error');
    server.streams[0]!.end();
    await vi.advanceTimersByTimeAsync(RETRY_FLOOR_MS * 3); // the reconnect, then two failures
    expect(server.eventCalls()).toHaveLength(5);
    expect(sink.last.phase).toBe('reconnecting'); // three in a row, counted from the last one
  });

  it('drops its timers when the chat is over', async () => {
    vi.useFakeTimers();
    server.EventSource = fakeEventSource().EventSource;
    server.eventAnswers.push(json(503, { ok: false, error: { type: 'stopped', message: 'no' } }));
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    expect(sink.last).toMatchObject({ phase: 'over', reason: 'stopped' });
    const heard = sink.statuses.length;
    await vi.advanceTimersByTimeAsync(60000);
    expect(sink.statuses).toHaveLength(heard);
    expect(server.eventCalls()).toHaveLength(1);
  });

  it('ends the page when nothing can stream and there is no EventSource', async () => {
    vi.useFakeTimers();
    for (let i = 0; i < 3; i++) server.eventAnswers.push(new Response(null, { status: 200 }));
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(250);
    await vi.advanceTimersByTimeAsync(500);
    expect(sink.last.phase).toBe('over');
    expect(sink.last.reason).toMatch(/^unreachable/);
  });

  it('is over, for good, on stopped, 401 and 403', async () => {
    for (const [status, type] of [[503, 'stopped'], [401, 'unauthorized'], [403, 'forbidden']] as const) {
      const s = new FakeServer();
      const k = new Sink();
      s.eventAnswers.push(json(status, { ok: false, error: { type, message: 'no' } }));
      connectEvents('tok', k, s.env());
      await settle();
      expect(k.last).toMatchObject({ phase: 'over', reason: type });
      await new Promise((resolve) => setTimeout(resolve, 300));
      expect(s.eventCalls()).toHaveLength(1);
    }
  });

  it('retries a refused connection with backoff, then gives up', async () => {
    vi.useFakeTimers();
    server.eventAnswers.push('network-error', 'network-error', 'network-error');
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    expect(server.eventCalls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(250);
    expect(server.eventCalls()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(499);
    expect(server.eventCalls()).toHaveLength(2); // the wait doubles: 500 ms, not another 250
    await vi.advanceTimersByTimeAsync(1);
    expect(server.eventCalls()).toHaveLength(3);
    expect(sink.last.phase).toBe('over');
    expect(sink.last.reason).toMatch(/^unreachable/);
  });

  it('resumes a stream that has been silent for 30 s', async () => {
    vi.useFakeTimers();
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    server.streams[0]!.push(snapshot(4));
    await vi.advanceTimersByTimeAsync(IDLE_MS - 1000);
    server.streams[0]!.push(': hb\n\n'); // a heartbeat is traffic
    await vi.advanceTimersByTimeAsync(IDLE_MS - 1000);
    expect(server.eventCalls()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(server.eventCalls()).toHaveLength(2);
    expect(server.eventCalls()[1]!.headers['Last-Event-ID']).toBe('4');
    // The dropped stream's own read fails on the abort: it must not reconnect a second time.
    await vi.advanceTimersByTimeAsync(1000);
    expect(server.eventCalls()).toHaveLength(2);
  });

  it('falls back to EventSource with ?t= when the response cannot stream', async () => {
    const opened: string[] = [];
    class FakeEventSource {
      onerror: (() => void) | null = null;
      listeners = new Map<string, (e: Event) => void>();
      constructor(url: string) {
        opened.push(url);
        queueMicrotask(() =>
          this.listeners.get('snapshot')?.({ data: JSON.stringify({ seq: 2, state: {} }), lastEventId: '2' } as unknown as Event),
        );
      }
      addEventListener(name: string, fn: (e: Event) => void) {
        this.listeners.set(name, fn);
      }
      close() {}
    }
    server.EventSource = FakeEventSource as unknown as typeof EventSource;
    server.eventAnswers.push(new Response(null, { status: 200 }));
    connectEvents('a b', sink, server.env());
    await settle();
    expect(opened).toEqual(['/events?t=a%20b']);
    expect(sink.snapshots.map((s) => s.seq)).toEqual([2]);
    expect(sink.last).toMatchObject({ phase: 'live', mode: 'eventsource', fellBack: true });
  });

  it('falls back to EventSource when the first event never comes', async () => {
    vi.useFakeTimers();
    const es = fakeEventSource();
    server.EventSource = es.EventSource;
    connectEvents('tok', sink, server.env()); // the stream opens and says nothing
    await vi.advanceTimersByTimeAsync(0);
    expect(es.opened).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(FIRST_EVENT_MS + 1);
    expect(es.opened.map((s) => s.url)).toEqual(['/events?t=tok']);
    expect(sink.last).toMatchObject({ mode: 'eventsource', fellBack: true });
  });

  it('opens another EventSource when the first one delivers nothing either', async () => {
    // EventSource surfaces no heartbeat, so the first event is the page's only liveness
    // check: without it a dead fallback is never noticed.
    vi.useFakeTimers();
    const es = fakeEventSource();
    server.EventSource = es.EventSource;
    server.eventAnswers.push(new Response(null, { status: 200 }));
    connectEvents('tok', sink, server.env());
    await vi.advanceTimersByTimeAsync(0);
    expect(es.opened).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(FIRST_EVENT_MS + 250);
    expect(es.opened).toHaveLength(2);
  });

  it('asks the server why the EventSource failed, and ends the page when it has stopped', async () => {
    // EventSource never says why it failed. HEAD /events runs the whole gate — peer, token,
    // stopped — and then answers 405, so it tells a refused page from a dropped connection.
    const es = fakeEventSource();
    server.EventSource = es.EventSource;
    server.eventAnswers.push(new Response(null, { status: 200 }));
    server.probeAnswers.push(json(503, { ok: false, error: { type: 'stopped', message: 'This chat has stopped.' } }));
    connectEvents('tok', sink, server.env());
    await settle();
    expect(es.opened).toHaveLength(1);
    es.opened[0]!.onerror!(); // the browser reports an error and says nothing else
    // Closed at once, while the page is still asking why: otherwise the browser reconnects
    // on its own schedule behind the page's back.
    expect(es.opened[0]!.closes).toBe(1);
    await settle();
    expect(server.probes()).toHaveLength(1);
    expect(sink.last).toMatchObject({ phase: 'over', reason: 'stopped' });
  });

  it('keeps going when the server says it is well and only the EventSource failed', async () => {
    const es = fakeEventSource();
    server.EventSource = es.EventSource;
    server.eventAnswers.push(new Response(null, { status: 200 }));
    connectEvents('tok', sink, server.env()); // the probe answers 405, as the server does
    await settle();
    es.opened[0]!.onerror!();
    await settle();
    expect(server.probes()).toHaveLength(1);
    expect(sink.last.phase).toBe('reconnecting');
  });
});

describe('postCommand', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('posts the id with the command and answers null on 202', async () => {
    const server = new FakeServer();
    expect(await postCommand('tok', 'id-1', { type: 'cancel' }, server.env())).toBeNull();
    const [call] = server.calls;
    expect(call).toMatchObject({
      url: '/command',
      method: 'POST',
      headers: { Authorization: 'Bearer tok', 'Content-Type': 'application/json' },
      body: { id: 'id-1', type: 'cancel' },
    });
  });

  it('tries a full queue again, and reports any other refusal', async () => {
    const server = new FakeServer();
    server.commandAnswers.push(json(503, { ok: false, error: { type: 'queue_full', message: 'Try again.' } }));
    expect(await postCommand('tok', 'id-2', { type: 'reload' }, server.env())).toBeNull();
    expect(server.commands()).toHaveLength(2);

    server.commandAnswers.push(json(400, { ok: false, error: { type: 'bad_command', message: 'no' } }));
    expect(await postCommand('tok', 'id-3', { type: 'reload' }, server.env())).toEqual({
      status: 400,
      type: 'bad_command',
      message: 'no',
    });
    expect(server.commands()).toHaveLength(3);
  });

  it('waits between tries at a full queue, and reports it after four', async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    const full = () => json(503, { ok: false, error: { type: 'queue_full', message: 'Try again.' } });
    for (let i = 0; i < 5; i++) server.commandAnswers.push(full());
    const answer = postCommand('tok', 'id-4', { type: 'reload' }, server.env());
    await vi.advanceTimersByTimeAsync(0);
    expect(server.commands()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(199);
    expect(server.commands()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(server.commands()).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(400 + 600);
    expect(server.commands()).toHaveLength(4); // and no fifth: the queue is not draining
    await expect(answer).resolves.toEqual({ status: 503, type: 'queue_full', message: 'Try again.' });
  });

  it('gives up on a command the server never answers', async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    server.commandAnswers.push(new Promise<Response>(() => {}));
    const answer = postCommand('tok', 'id-5', { type: 'cancel' }, server.env());
    await vi.advanceTimersByTimeAsync(COMMAND_MS);
    await expect(answer).resolves.toMatchObject({ status: 0, type: 'network_error' });
  });
});

describe('uploadFile', () => {
  it('sends one "file" part and returns the attachment', async () => {
    const server = new FakeServer();
    const result = await uploadFile('tok', new File([new Uint8Array([1, 2, 3, 4])], 'a.png', { type: 'image/png' }), server.env());
    expect(result).toEqual({ ok: true, attachment: { id: 'att-1', filename: 'a.png', mime_type: 'image/png', size_bytes: 4 } });
    const body = server.calls[0]!.body as FormData;
    expect((body.get('file') as File).name).toBe('a.png');
    expect(server.calls[0]!.headers).toEqual({ Authorization: 'Bearer tok' });
  });

  it('reports 409 account_changed as a refusal', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => json(409, { ok: false, error: { type: 'account_changed', message: 'Upload the file again.' } });
    const result = await uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env());
    expect(result).toEqual({ ok: false, status: 409, type: 'account_changed', message: 'Upload the file again.' });
  });

  it('reports an answer it cannot read as a refusal, rather than throwing at the caller', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => new Response('<html>a proxy said no</html>', { status: 200 });
    const result = await uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env());
    expect(result).toMatchObject({ ok: false, status: 200, type: 'bad_response' });
  });

  it('reports a 200 that is not an attachment as a refusal', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => json(200, { ok: true });
    const result = await uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env());
    expect(result).toMatchObject({ ok: false, type: 'bad_response' });
  });

  // The `{ id: 'x' }`-only reply: a valid `id` alone must not
  // pass as a complete attachment — `send()` (runtime.ts) reads `filename`/`mime_type`
  // straight off this value, and an `undefined` there would reach the composer's own
  // sent-message chip, not a JSON parse error caught somewhere safe.
  it('reports a reply missing filename, mime_type or size_bytes as a refusal, even with a valid id', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => json(200, { id: 'att-1' });
    const result = await uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env());
    expect(result).toMatchObject({ ok: false, type: 'bad_response' });
  });

  // The caller's own way out (Remove, an identity change, New chat: runtime.ts/Composer.tsx):
  // aborting the signal it passed in ends the request the same way UPLOAD_MS does, without
  // waiting for the timer.
  it('aborts on the caller’s own signal, not only on UPLOAD_MS', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => new Promise<Response>(() => {});
    const controller = new AbortController();
    const answer = uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env(), controller.signal);
    await settle();
    expect(server.calls).toHaveLength(1); // the request really started
    controller.abort();
    await expect(answer).resolves.toMatchObject({ ok: false, status: 0, type: 'network_error' });
  });

  it('a signal already aborted before the call refuses the upload at once', async () => {
    const server = new FakeServer();
    server.uploadAnswer = () => new Promise<Response>(() => {});
    const controller = new AbortController();
    controller.abort();
    const result = await uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env(), controller.signal);
    expect(result).toMatchObject({ ok: false, status: 0, type: 'network_error' });
  });

  it('gives up on an upload the server never answers', async () => {
    vi.useFakeTimers();
    const server = new FakeServer();
    server.uploadAnswer = () => new Promise<Response>(() => {});
    const answer = uploadFile('tok', new File(['x'], 'a.pdf', { type: 'application/pdf' }), server.env());
    await vi.advanceTimersByTimeAsync(UPLOAD_MS - 1);
    expect(server.calls).toHaveLength(1); // still waiting: no verdict yet either way
    await vi.advanceTimersByTimeAsync(1);
    await expect(answer).resolves.toMatchObject({ ok: false, status: 0, type: 'network_error' });
  });
});

describe('bootstrapToken', () => {
  /** Each case gets the module fresh: the launch token is read once per page load. */
  async function bootstrap(hash: string, held: Record<string, string> | null) {
    const stored = new Map(Object.entries(held ?? {}));
    const replaced: unknown[][] = [];
    Object.defineProperty(globalThis, 'location', {
      configurable: true,
      value: { hash, pathname: '/chat', search: '?a=1' },
    });
    Object.defineProperty(globalThis, 'history', {
      configurable: true,
      value: { replaceState: (...args: unknown[]) => replaced.push(args) },
    });
    Object.defineProperty(globalThis, 'sessionStorage', {
      configurable: true,
      ...(held === null
        ? {
            get() {
              throw new Error('storage is blocked'); // a private window, or a locked-down web view
            },
          }
        : {
            value: {
              getItem: (key: string) => stored.get(key) ?? null,
              setItem: (key: string, value: string) => void stored.set(key, value),
              removeItem: (key: string) => void stored.delete(key),
            },
          }),
    });
    vi.resetModules();
    const fresh = await import('../src/transport');
    return { token: fresh.bootstrapToken(), key: fresh.TOKEN_KEY, stored, replaced };
  }

  afterEach(() => {
    for (const name of ['location', 'history', 'sessionStorage']) Reflect.deleteProperty(globalThis, name);
    vi.resetModules();
  });

  it('takes the token out of the fragment, keeps it, and strips the hash', async () => {
    const { token, key, stored, replaced } = await bootstrap('#t=abc-_1', {});
    expect(token).toBe('abc-_1');
    expect(stored.get(key)).toBe('abc-_1');
    expect(replaced).toEqual([[null, '', '/chat?a=1']]); // no token left in the URL to leak
  });

  it('reads it back from sessionStorage after a reload, and leaves the URL alone', async () => {
    const { token, replaced } = await bootstrap('', { [TOKEN_KEY]: 'abc-_1' });
    expect(token).toBe('abc-_1');
    expect(replaced).toEqual([]);
  });

  it('is null when the page was opened with no token at all', async () => {
    expect((await bootstrap('', {})).token).toBeNull();
  });

  it('still serves this page load when sessionStorage cannot be touched', async () => {
    expect((await bootstrap('#t=abc-_1', null)).token).toBe('abc-_1');
  });
});
