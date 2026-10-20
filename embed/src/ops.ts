/**
 * The op applier: a port of `_apply_op` in src/talk2view/state.py (at
 * the repo root), rule for rule, so the page and Python agree on every batch. The shared
 * cases in contract/ops/cases.json hold both to it.
 *
 * - `set` at an index equal to a list's length appends, at any depth; a value is
 *   deep-copied, so state never shares an object with a batch.
 * - `append-text` needs a string on both sides.
 * - Every step before the last must exist: a missing key or an index out of range is
 *   an error, never a silent create.
 * - `["seq"]` is never a path: the batch carries seq.
 *
 * Unlike Python, which mutates in place, this returns a new state that copies only the
 * nodes on the path, so React sees new identities exactly where something changed, and
 * a batch that fails half way leaves the page's state untouched (the page then resyncs
 * from a fresh snapshot).
 */
import type { Op, PathKey } from './wire';

export class OpError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'OpError';
  }
}

type Json = unknown;
type JsonRecord = Record<string, Json>;

function isRecord(node: Json): node is JsonRecord {
  return node !== null && typeof node === 'object' && !Array.isArray(node);
}

function isIndex(key: PathKey): key is number {
  return typeof key === 'number' && Number.isInteger(key);
}

function show(path: PathKey[]): string {
  return JSON.stringify(path);
}

function kindOf(node: Json): string {
  if (node === null) return 'null';
  if (Array.isArray(node)) return 'list';
  return typeof node === 'object' ? 'dict' : typeof node;
}

function deepCopy(value: Json): Json {
  if (Array.isArray(value)) return value.map(deepCopy);
  if (isRecord(value)) {
    const copy: JsonRecord = {};
    // `copy[key] = …` would treat a `__proto__` key as the setter it is: the key would
    // vanish from the copy and change the object's prototype instead. `JSON.parse` makes
    // `__proto__` an ordinary own key, and a tool's arguments are whatever the agent
    // wrote, so define every key instead of assigning it. (The object spread and computed
    // keys used elsewhere in this file already define rather than assign.)
    for (const key of Object.keys(value)) {
      Object.defineProperty(copy, key, { value: deepCopy(value[key]), writable: true, enumerable: true, configurable: true });
    }
    return copy;
  }
  return value;
}

/** `_child`: one step down an existing path. */
function child(node: Json, key: PathKey, path: PathKey[]): Json {
  if (isRecord(node) && typeof key === 'string') {
    if (!Object.prototype.hasOwnProperty.call(node, key)) {
      throw new OpError(`path ${show(path)}: key ${JSON.stringify(key)} does not exist`);
    }
    return node[key];
  }
  if (Array.isArray(node) && isIndex(key)) {
    if (key < 0 || key >= node.length) {
      throw new OpError(`path ${show(path)}: index ${key} is out of range`);
    }
    return node[key];
  }
  throw new OpError(`path ${show(path)}: cannot step into ${kindOf(node)} with ${JSON.stringify(key)}`);
}

/** A copy of `node` with `key` (already validated) replaced by `value`. */
function withChild(node: Json, key: PathKey, value: Json): Json {
  if (Array.isArray(node)) {
    const copy = node.slice();
    copy[key as number] = value;
    return copy;
  }
  return { ...(node as JsonRecord), [key as string]: value };
}

/** The new parent after the op, given the parent `node` of the path's last key. */
function applyAtParent(node: Json, op: Op): Json {
  const path = op.path;
  const last = path[path.length - 1]!;
  if (op.type === 'set') {
    const value = deepCopy(op.value);
    if (Array.isArray(node)) {
      if (!isIndex(last)) throw new OpError(`path ${show(path)}: a list needs an integer index`);
      if (last === node.length) return [...node, value]; // append == set at index len
      if (last >= 0 && last < node.length) return withChild(node, last, value);
      throw new OpError(`path ${show(path)}: index ${last} is out of range`);
    }
    if (isRecord(node)) {
      if (typeof last !== 'string') throw new OpError(`path ${show(path)}: a dict needs a string key`);
      return withChild(node, last, value);
    }
    throw new OpError(`path ${show(path)}: cannot set inside ${kindOf(node)}`);
  }
  if (op.type === 'append-text') {
    const current = child(node, last, path);
    if (typeof current !== 'string' || typeof op.value !== 'string') {
      throw new OpError(`path ${show(path)}: append-text needs a string on both sides`);
    }
    return withChild(node, last, current + op.value);
  }
  throw new OpError(`unknown op type ${JSON.stringify((op as { type: unknown }).type)}`);
}

function applyFrom(node: Json, op: Op, depth: number): Json {
  const path = op.path;
  if (depth === path.length - 1) return applyAtParent(node, op);
  const key = path[depth]!;
  return withChild(node, key, applyFrom(child(node, key, path), op, depth + 1));
}

/** Apply one op; returns the new root. Throws `OpError` when the op does not apply. */
export function applyOp<S>(state: S, op: Op): S {
  if (!Array.isArray(op.path) || op.path.length === 0) throw new OpError('empty path');
  if (op.path.length === 1 && op.path[0] === 'seq') {
    throw new OpError('seq is carried by the batch, never by an op');
  }
  return applyFrom(state, op, 0) as S;
}

/** Apply every op of a batch in order; all or nothing. */
export function applyOps<S>(state: S, ops: Op[]): S {
  let next = state;
  for (const op of ops) next = applyOp(next, op);
  return next;
}
