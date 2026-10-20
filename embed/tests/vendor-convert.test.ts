/**
 * This page's additions to the vendored convert.ts (src/vendor/sdk/assistant-ui/
 * convert.ts and its declared delta, convert.ts.delta.diff). They are tested here because
 * the canonical TS SDK, the public talk2view-typescript repository, does not carry that
 * work yet.
 */
import { describe, expect, it } from 'vitest';
import { APPROVAL_OPTION_IDS, approvalPart, stepToolCallId, toThreadMessage } from '../src/vendor/sdk/assistant-ui/convert';
import type { DisplayMessage, PendingApproval } from '../src/vendor/sdk/types';

const at = new Date('2026-09-16T10:00:00Z');
const quiet = { isLast: false, pendingApproval: null, error: null };

/** An approval part's own fields: this package type-checks its tests, and the part's
 * declared type is assistant-ui's union of every part kind. */
function toolPart(part: unknown): { approval?: unknown; artifact?: unknown } {
  return part as { approval?: unknown; artifact?: unknown };
}

function assistant(over: Partial<DisplayMessage> = {}): DisplayMessage {
  return { id: 'a1', role: 'assistant', content: 'Hello', timestamp: at, ...over };
}

describe('engine approvals and tool displays (Python SDK wire)', () => {
  const engine: PendingApproval = {
    toolCallId: 'call-9',
    toolName: 'send_email',
    arguments: { to: 'a@b.c' },
    description: 'Email the scan results to Alex',
    kind: 'engine',
    details: [{ label: 'To', value: 'a@b.c' }],
  };

  it('offers no "Always allow" for an engine approval, and carries its details', () => {
    const part = approvalPart(engine);
    expect(part).toMatchObject({
      toolCallId: 'call-9',
      approval: {
        id: 'call-9',
        prompt: 'Email the scan results to Alex',
        options: [
          { id: APPROVAL_OPTION_IDS.once, kind: 'allow-once' },
          { id: APPROVAL_OPTION_IDS.deny, kind: 'reject-once' },
        ],
      },
      artifact: { approval: { kind: 'engine', details: [{ label: 'To', value: 'a@b.c' }] } },
    });
  });

  it('treats a client approval with a kind exactly as one without', () => {
    const client: PendingApproval = { ...engine, kind: 'client', details: undefined };
    const plain: PendingApproval = { toolCallId: 'call-9', toolName: 'send_email', arguments: { to: 'a@b.c' }, description: 'Email the scan results to Alex' };
    expect(toolPart(approvalPart(client)).approval).toEqual(toolPart(approvalPart(plain)).approval);
    expect(toolPart(approvalPart(client)).artifact).toEqual({ approval: { kind: 'client', details: [] } });
    expect('artifact' in approvalPart(plain)).toBe(false);
  });

  it('carries a step display through as the part artifact', () => {
    const display = { kind: 'image_failure', reason: 'content_filter' };
    const out = toThreadMessage(
      assistant({ content: '', steps: [{ name: 'generate_image', status: 'used', result: 'no', display }] }),
      quiet,
    );
    expect(out.content).toEqual([
      {
        type: 'tool-call', toolCallId: stepToolCallId('a1', 0), toolName: 'generate_image', args: {},
        result: 'no', isError: false, artifact: { display },
      },
    ]);
  });

  // Fail closed: any `kind` that is not exactly `undefined` or `'client'` must never offer
  // "Always allow" — including a `kind` the engine has not invented yet. A refactor that
  // narrows the client check to `kind === 'engine'` instead of "not client" would ship this
  // open (a mutation that must fail here: `const client = (approval.kind as string) !== 'engine'`).
  it.each(['', 'future', 42, null])(
    'treats an unrecognised approval kind (%j) as engine: never "Always allow"',
    (kind) => {
      const approval: PendingApproval = { ...engine, kind: kind as unknown as PendingApproval['kind'] };
      const part = approvalPart(approval);
      expect(part).toMatchObject({
        approval: {
          options: [
            { id: APPROVAL_OPTION_IDS.once, kind: 'allow-once' },
            { id: APPROVAL_OPTION_IDS.deny, kind: 'reject-once' },
          ],
        },
        artifact: { approval: { kind: 'engine' } },
      });
    },
  );

  // `details` alone (no `kind` at all) is still a client approval: the SDK may attach a
  // summary to a client approval too, and that must not flip it into the engine's stricter
  // options (a mutation that must fail here: gate the artifact on `kind` alone, dropping `details`).
  it('keeps "Always allow" for an approval with details but no kind, and carries them', () => {
    const detailsOnly: PendingApproval = {
      toolCallId: 'call-9',
      toolName: 'send_email',
      arguments: { to: 'a@b.c' },
      description: 'Email the scan results to Alex',
      details: [{ label: 'To', value: 'a@b.c' }],
    };
    const part = approvalPart(detailsOnly);
    expect(part).toMatchObject({
      approval: {
        options: [
          { id: APPROVAL_OPTION_IDS.once, kind: 'allow-once' },
          { id: APPROVAL_OPTION_IDS.always, kind: 'allow-always' },
          { id: APPROVAL_OPTION_IDS.deny, kind: 'reject-once' },
        ],
      },
      artifact: { approval: { kind: 'client', details: [{ label: 'To', value: 'a@b.c' }] } },
    });
  });
});
