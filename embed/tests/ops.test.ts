import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { applyOp, applyOps, OpError } from '../src/ops';
import type { Op } from '../src/wire';

interface Case {
  id: string;
  state: Record<string, unknown>;
  ops: Op[];
  expect?: Record<string, unknown>;
  error?: boolean;
}

const casesFile = fileURLToPath(new URL('../../contract/ops/cases.json', import.meta.url));
const cases = (JSON.parse(readFileSync(casesFile, 'utf8')) as { cases: Case[] }).cases;

describe('the shared op cases (contract/ops/cases.json)', () => {
  it('has cases', () => {
    expect(cases.length).toBeGreaterThanOrEqual(25);
  });

  for (const c of cases) {
    it(c.id, () => {
      const before = JSON.stringify(c.state);
      if (c.error) {
        expect(() => applyOps(c.state, c.ops)).toThrow(OpError);
      } else {
        expect(applyOps(c.state, c.ops)).toEqual(c.expect);
      }
      expect(JSON.stringify(c.state)).toBe(before); // never mutates its input
    });
  }
});

describe('applyOp', () => {
  it('copies only the nodes on the path', () => {
    const state = { messages: [{ id: 'u1', content: 'hi' }, { id: 'a1', content: '' }], skills: { local: [] } };
    const next = applyOp(state, { type: 'append-text', path: ['messages', 1, 'content'], value: 'x' });
    expect(next).not.toBe(state);
    expect(next.messages).not.toBe(state.messages);
    expect(next.messages[0]).toBe(state.messages[0]);
    expect(next.messages[1]).not.toBe(state.messages[1]);
    expect(next.skills).toBe(state.skills);
  });

  it('deep-copies a set value, so state never shares an object with the batch', () => {
    const value = { id: 'a1', steps: [{ name: 't' }] };
    const next = applyOp({ messages: [] as unknown[] }, { type: 'set', path: ['messages', 0], value });
    value.steps[0]!.name = 'changed later';
    expect(next.messages[0]).toEqual({ id: 'a1', steps: [{ name: 't' }] });
  });

  it('leaves the state untouched when a later op of the batch fails', () => {
    const state = { messages: [] as unknown[] };
    expect(() =>
      applyOps(state, [
        { type: 'set', path: ['messages', 0], value: { id: 'u1' } },
        { type: 'set', path: ['messages', 5], value: { id: 'x' } },
      ]),
    ).toThrow(OpError);
    expect(state.messages).toEqual([]);
  });
});
