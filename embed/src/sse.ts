export interface SseEvent {
  event: string;
  id: string | null;
  data: string;
}

/**
 * Incremental text/event-stream parser. Feed it decoded text in whatever
 * pieces the network delivers; it returns the events each piece completed.
 * Needed because EventSource cannot send an Authorization header, so the page
 * reads SSE with fetch + ReadableStream.
 */
export class SseParser {
  private buf = '';
  /**
   * How much of `buf` has already been searched for a line end. A snapshot may be 8 MiB —
   * the server's own bound — and arrives in hundreds of chunks: searching the whole buffer
   * again for each of them is quadratic, 30 s for that snapshot at 4 KiB a chunk.
   */
  private scanned = 0;
  private bomChecked = false;
  private event = '';
  private id: string | null = null;
  private data: string[] = [];

  feed(chunk: string): SseEvent[] {
    this.buf += chunk;
    if (!this.bomChecked && this.buf.length > 0) {
      this.bomChecked = true;
      if (this.buf.charCodeAt(0) === 0xfeff) this.buf = this.buf.slice(1); // one leading BOM
    }
    const out: SseEvent[] = [];
    let start = 0;
    let i = this.scanned;
    for (;;) {
      while (i < this.buf.length && this.buf[i] !== '\n' && this.buf[i] !== '\r') i++;
      if (i >= this.buf.length) break; // no complete line yet
      if (this.buf[i] === '\r' && i + 1 >= this.buf.length) break; // may be half of \r\n
      const line = this.buf.slice(start, i);
      start = i + (this.buf[i] === '\r' && this.buf[i + 1] === '\n' ? 2 : 1);
      i = start;
      const ev = this.line(line);
      if (ev) out.push(ev);
    }
    this.buf = this.buf.slice(start);
    this.scanned = i - start; // what is left has been searched up to here
    return out;
  }

  private line(line: string): SseEvent | null {
    if (line === '') {
      const ev = this.data.length
        ? { event: this.event || 'message', id: this.id, data: this.data.join('\n') }
        : null;
      this.event = '';
      this.data = [];
      return ev;
    }
    if (line[0] === ':') return null; // comment / heartbeat
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value[0] === ' ') value = value.slice(1);
    if (field === 'data') this.data.push(value);
    else if (field === 'event') this.event = value;
    else if (field === 'id' && value.indexOf('\0') === -1) this.id = value;
    return null;
  }
}
