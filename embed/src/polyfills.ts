/**
 * MUST be the first import of main.tsx. Chromium 83 lacks every API below (measured
 * on Slicer's Chromium 83). core-js entries install only when the native one is missing.
 * WeakRef / FinalizationRegistry (Chromium 84+) cannot be polyfilled: the build fails
 * if either appears in the bundle (scripts/check-dist.mjs).
 */
import 'core-js/actual/array/at';
import 'core-js/actual/string/at';
import 'core-js/actual/object/has-own';
import 'core-js/actual/array/find-last';
import 'core-js/actual/array/find-last-index';
import 'core-js/actual/structured-clone';
import 'core-js/actual/string/replace-all';
import 'core-js/actual/promise/any';
import 'core-js/actual/aggregate-error';

/** Names of the APIs this file had to install by hand. Read by the runtime guard's probes. */
export const INSTALLED_BY_HAND: string[] = [];

// crypto.randomUUID — Chromium 92+. Command ids must be unique for the server's lifetime.
const cryptoLike = globalThis.crypto as unknown as {
  randomUUID?: () => string;
  getRandomValues(array: Uint8Array): Uint8Array;
};
if (cryptoLike && typeof cryptoLike.randomUUID !== 'function') {
  cryptoLike.randomUUID = function randomUUID(): string {
    const b = cryptoLike.getRandomValues(new Uint8Array(16));
    b[6] = (b[6]! & 0x0f) | 0x40; // version 4
    b[8] = (b[8]! & 0x3f) | 0x80; // RFC 4122 variant
    let hex = '';
    for (let i = 0; i < 16; i++) hex += (b[i]! < 16 ? '0' : '') + b[i]!.toString(16);
    return (
      hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' +
      hex.slice(16, 20) + '-' + hex.slice(20)
    );
  };
  INSTALLED_BY_HAND.push('crypto.randomUUID');
}

// ParentNode.replaceChildren — Chromium 86+.
const parents: Array<[string, object]> =
  typeof Element === 'undefined'
    ? []
    : [
        ['Element', Element.prototype],
        ['Document', Document.prototype],
        ['DocumentFragment', DocumentFragment.prototype],
      ];
for (const [name, proto] of parents) {
  if (typeof (proto as { replaceChildren?: unknown }).replaceChildren !== 'function') {
    Object.defineProperty(proto, 'replaceChildren', {
      configurable: true,
      writable: true,
      value: function replaceChildren(this: ParentNode, ...nodes: Array<Node | string>): void {
        while (this.lastChild) this.removeChild(this.lastChild);
        this.append(...nodes);
      },
    });
    INSTALLED_BY_HAND.push(name + '.prototype.replaceChildren');
  }
}

// AbortSignal.timeout — Chromium 103+.
const abortSignalLike = AbortSignal as unknown as { timeout?: (ms: number) => AbortSignal };
if (typeof abortSignalLike.timeout !== 'function') {
  abortSignalLike.timeout = function timeout(ms: number): AbortSignal {
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  };
  INSTALLED_BY_HAND.push('AbortSignal.timeout');
}
