// vendored-from: Talk2View/talk2view-typescript src/assistant-ui/convert.ts
// vendored-commit: b042b005611c59e8bc6fbd304ea1f028d11fb88b
// local-delta: convert.ts.delta.diff
//
// A copy of the public TS SDK's assistant-ui message conversion, plus one local delta,
// declared in full in convert.ts.delta.diff beside this file. The delta is this page's own
// addition. approvalPart() offers "Always allow" only
// for a client approval, so any other kind, including one the page does not know, is
// allowed or denied per call. The approval's kind and details travel in the part's
// artifact.approval (ApprovalArtifact), and a finished step's display in artifact.display.
// Why: the Python SDK sends engine approvals and tool displays, and the
// canonical repository has neither yet. CI checks that this body is the recorded commit
// plus that diff, and warns when canonical main has changed the file since the commit
// (scripts/check-vendored-sdk.mjs). Re-vendor rather than edit (README.md, "Where the TS
// SDK code comes from").
// end of vendoring header
/**
 * Pure mapping from the SDK's chat state onto assistant-ui's message shape.
 *
 * assistant-ui renders `ThreadMessageLike`; the SDK keeps `DisplayMessage`.
 * Everything here is a function of (message, surrounding state) so it can be
 * unit-tested without React or a runtime.
 */
import type { ThreadMessageLike, ToolCallMessagePart } from '@assistant-ui/react';
import type { ApprovalDetail, Attachment, DisplayMessage, HumanDecision, PendingApproval, ToolStep } from '../types.js';

/** What a message needs to know about the thread around it. */
export interface ConvertContext {
  /** True for the newest message — the only one that can be streaming or waiting on a decision. */
  isLast: boolean;
  /** A tool call waiting for the end-user; rendered as an approval gate on the last assistant message. */
  pendingApproval: PendingApproval | null;
  /** The SDK's error state; shown on the last assistant message. */
  error: string | null;
}

type ToolApproval = NonNullable<ToolCallMessagePart['approval']>;
type ThreadPart = Extract<ThreadMessageLike['content'], readonly unknown[]>[number];

/** Option ids the stock assistant-ui approval card sends back; we map them onto `HumanDecision`. */
export const APPROVAL_OPTION_IDS = {
  once: 'allow-once',
  always: 'allow-always',
  deny: 'reject-once',
} as const;

const APPROVAL_OPTIONS: ToolApproval['options'] = [
  { id: APPROVAL_OPTION_IDS.once, kind: 'allow-once', label: 'Allow once' },
  { id: APPROVAL_OPTION_IDS.always, kind: 'allow-always', label: 'Always allow' },
  { id: APPROVAL_OPTION_IDS.deny, kind: 'reject-once', label: 'Deny' },
];

/** An engine approval is allowed or denied per call: nothing remembers "always" for one. */
const ENGINE_APPROVAL_OPTIONS: ToolApproval['options'] = APPROVAL_OPTIONS.filter(
  (option) => option.id !== APPROVAL_OPTION_IDS.always,
);

/** What an approval part carries besides assistant-ui's own fields (`part.artifact.approval`). */
export interface ApprovalArtifact {
  kind: 'client' | 'engine';
  details: ApprovalDetail[];
}

/** Stable id for a completed step: steps carry no id of their own. */
export function stepToolCallId(messageId: string, index: number): string {
  return `${messageId}:step:${index}`;
}

function stepPart(messageId: string, step: ToolStep, index: number): ThreadPart {
  const denied = step.status === 'denied';
  return {
    type: 'tool-call',
    toolCallId: stepToolCallId(messageId, index),
    toolName: step.name,
    args: (step.args ?? {}) as Record<string, never>,
    // A running step has no result yet, which is how assistant-ui shows a spinner.
    ...(step.status === 'running'
      ? {}
      : { result: step.result ?? (denied ? 'Denied by the user' : ''), isError: denied }),
    ...(step.display ? { artifact: { display: step.display } } : {}),
  };
}

/**
 * The approval gate assistant-ui draws for a tool call that needs a decision. Anything
 * but a client approval (fail closed) is offered allow or deny only. A `kind` or
 * `details` the SDK set travels in `artifact.approval` (`ApprovalArtifact`).
 */
export function approvalPart(approval: PendingApproval): ThreadPart {
  const client = approval.kind === undefined || approval.kind === 'client';
  const artifact: { approval: ApprovalArtifact } | null =
    approval.kind !== undefined || approval.details !== undefined
      ? { approval: { kind: client ? 'client' : 'engine', details: approval.details ?? [] } }
      : null;
  return {
    type: 'tool-call',
    toolCallId: approval.toolCallId,
    toolName: approval.toolName,
    args: approval.arguments as Record<string, never>,
    approval: {
      id: approval.toolCallId,
      prompt: approval.description,
      display: 'decision',
      // A reason may accompany the decision; the SDK forwards it as feedback.
      allowFreeform: true,
      options: client ? APPROVAL_OPTIONS : ENGINE_APPROVAL_OPTIONS,
    },
    ...(artifact ? { artifact } : {}),
  };
}

/**
 * The SDK records a `running` step for the tool awaiting approval, so the
 * gate replaces that step rather than sitting beside it; the part keeps the
 * engine's tool-call id so the decision can be matched back.
 */
function withApprovalGate(parts: ThreadPart[], approval: PendingApproval): ThreadPart[] {
  const gate = approvalPart(approval);
  for (let i = parts.length - 1; i >= 0; i--) {
    const p = parts[i]!;
    if (p.type === 'tool-call' && p.toolName === approval.toolName && p.result === undefined) {
      return [...parts.slice(0, i), gate, ...parts.slice(i + 1)];
    }
  }
  return [...parts, gate];
}

function attachmentType(mime: string): 'image' | 'document' {
  return mime.startsWith('image/') ? 'image' : 'document';
}

function toThreadAttachment(a: Attachment): NonNullable<ThreadMessageLike['attachments']>[number] {
  return {
    id: a.id,
    type: attachmentType(a.mime_type),
    name: a.filename,
    contentType: a.mime_type,
    status: { type: 'complete' },
    // The engine resolves the bytes by id; nothing is inlined into the message.
    content: [],
  };
}

/** Convert one SDK message for assistant-ui. */
export function toThreadMessage(msg: DisplayMessage, ctx: ConvertContext): ThreadMessageLike {
  if (msg.role === 'user') {
    return {
      id: msg.id,
      role: 'user',
      createdAt: msg.timestamp,
      content: [{ type: 'text', text: msg.content }],
      ...(msg.attachments?.length ? { attachments: msg.attachments.map(toThreadAttachment) } : {}),
    };
  }

  let parts: ThreadPart[] = (msg.steps ?? []).map((step, i) => stepPart(msg.id, step, i));
  const waiting = ctx.isLast && ctx.pendingApproval ? ctx.pendingApproval : null;
  if (waiting) parts = withApprovalGate(parts, waiting);
  if (msg.plan) parts.push({ type: 'data-plan', data: { markdown: msg.plan } });
  if (msg.content || parts.length === 0) parts.push({ type: 'text', text: msg.content });

  const status: ThreadMessageLike['status'] = waiting
    ? { type: 'requires-action', reason: 'tool-calls' }
    : msg.isStreaming
      ? { type: 'running' }
      : ctx.isLast && ctx.error
        ? { type: 'incomplete', reason: 'error', error: ctx.error }
        : { type: 'complete', reason: 'stop' };

  return { id: msg.id, role: 'assistant', createdAt: msg.timestamp, content: parts, status };
}

/** Turn the stock approval card's answer into the SDK's `HumanDecision`. */
export function toHumanDecision(response: {
  approved: boolean;
  optionId?: string;
  text?: string;
  reason?: string;
}): HumanDecision {
  const feedback = response.text ?? response.reason;
  const action: HumanDecision['action'] =
    response.optionId === APPROVAL_OPTION_IDS.always
      ? 'always'
      : response.optionId === APPROVAL_OPTION_IDS.once || (response.optionId === undefined && response.approved)
        ? 'once'
        : 'deny';
  return feedback ? { action, feedback } : { action };
}
