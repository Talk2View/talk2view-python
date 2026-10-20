import { describe, expect, it } from 'vitest';
import { SseParser } from '../src/sse';

describe('SseParser', () => {
  it('parses a named event with an id', () => {
    const p = new SseParser();
    expect(p.feed('event: snapshot\nid: 7\ndata: {"seq":7}\n\n')).toEqual([
      { event: 'snapshot', id: '7', data: '{"seq":7}' },
    ]);
  });

  it('accepts "data:" with and without the space', () => {
    const p = new SseParser();
    expect(p.feed('data:a\n\ndata: b\n\n').map((e) => e.data)).toEqual(['a', 'b']);
  });

  it('defaults the event name to "message" and keeps the last id', () => {
    const p = new SseParser();
    const [first, second] = p.feed('id: 3\ndata: x\n\ndata: y\n\n');
    expect(first).toEqual({ event: 'message', id: '3', data: 'x' });
    expect(second).toEqual({ event: 'message', id: '3', data: 'y' });
  });

  it('joins multi-line data with newlines', () => {
    expect(new SseParser().feed('data: one\ndata: two\n\n')[0]!.data).toBe('one\ntwo');
  });

  it('ignores comments and heartbeats, and never dispatches an event with no data', () => {
    const p = new SseParser();
    expect(p.feed(': t2v-ui stream open\n\n: hb\n\nevent: ops\n\n')).toEqual([]);
    // The dangling event name must not leak into the next event.
    expect(p.feed('data: z\n\n')).toEqual([{ event: 'message', id: null, data: 'z' }]);
  });

  it('survives a chunk boundary in the middle of a line', () => {
    const p = new SseParser();
    expect(p.feed('event: op')).toEqual([]);
    expect(p.feed('s\ndata: {"se')).toEqual([]);
    expect(p.feed('q":1}\n\n')).toEqual([{ event: 'ops', id: null, data: '{"seq":1}' }]);
  });

  it('survives a chunk boundary in the middle of a CRLF', () => {
    const p = new SseParser();
    expect(p.feed('data: a\r')).toEqual([]);
    expect(p.feed('\n\r\n')).toEqual([{ event: 'message', id: null, data: 'a' }]);
  });

  it('handles bare CR line endings', () => {
    expect(new SseParser().feed('data: a\r\rdata: b\r\r').map((e) => e.data)).toEqual(['a']);
  });

  it('returns every event completed by one chunk', () => {
    const events = new SseParser().feed('event: ops\ndata: 1\n\nevent: ops\ndata: 2\n\n');
    expect(events.map((e) => e.data)).toEqual(['1', '2']);
  });

  it('strips one leading byte order mark, and only from the start of the stream', () => {
    const p = new SseParser();
    expect(p.feed('\uFEFFdata: a\n\n')).toEqual([{ event: 'message', id: null, data: 'a' }]);
    expect(p.feed('data: \uFEFFb\n\n')[0]!.data).toBe('\uFEFFb');
  });

  it('strips one space after the colon, not every space', () => {
    expect(new SseParser().feed('data:   three\n\n')[0]!.data).toBe('  three');
  });

  it('ignores an id with a NUL in it, as the SSE specification says', () => {
    const p = new SseParser();
    expect(p.feed('id: 5\ndata: a\n\n')[0]!.id).toBe('5');
    expect(p.feed('id: a\u0000b\ndata: b\n\n')[0]!.id).toBe('5'); // the bad id is ignored, not taken
  });

  it('reads each character a fixed number of times, however many chunks a line arrives in', () => {
    // An 8 MiB snapshot is the server's own bound, and it arrives in hundreds of chunks. A
    // parser that searches its whole buffer again for every chunk is quadratic: 30 s for
    // that snapshot at 4 KiB a chunk, with the web view frozen throughout. This counts the
    // work rather than the wall time: every look at a character reads `buf`, so a constant
    // number of reads per character fed is the whole claim.
    let reads = 0;
    const parser = new Proxy(new SseParser(), {
      get(target, prop, receiver) {
        if (prop === 'buf') reads++;
        return Reflect.get(target, prop, receiver);
      },
    });
    const chunk = 'x'.repeat(4096);
    const total = 64 * chunk.length;
    parser.feed('data: ');
    for (let i = 0; i < 64; i++) expect(parser.feed(chunk)).toHaveLength(0);
    expect(reads).toBeLessThan(4 * total); // linear; rescanning is ~32x this and grows
    expect(parser.feed('\n\n')[0]!.data).toHaveLength(total);
  });
});
