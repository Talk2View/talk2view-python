/**
 * The run of tool calls in one assistant turn, as one "N tool calls" line (TS
 * tool-fallback.tsx ToolGroup over vendor/tool-group.aui.tsx, ghost variant). TS wraps
 * every run in this group, including a run of one — `buildGroupTree` has no "at least
 * two" floor, and TS's own trigger spells the singular
 * (`${count} tool ${count === 1 ? "call" : "calls"}`, tool-group.aui.tsx:112) — so a lone
 * tool call is a "1 tool call" collapsible here too, not a bare row (an earlier version
 * read `groupPartByType`'s doc comment as "groups two or more" and special-cased count 1
 * to bypass this component's own markup; that premise was wrong, and the bypass diverged
 * from TS on the single commonest turn shape there is).
 * Upstream's group starts closed; a closed group with an approval card inside would leave
 * the end-user looking at "1 tool call" while the agent waits on them, so this opens while
 * the run is live and leaves whatever they last chose standing once it settles.
 *
 * "Live" is not just `group.status` (deviation found empirically, backend.py's
 * /toolsonly): a tool call's own status goes straight to "complete" the instant its
 * result lands, with no observable "running" gap in between, even for a fake controller's
 * simulated calls that plainly are not done yet — the SECOND and third tool call in a
 * three-call turn have not happened at all while the first is the only one in the group,
 * but `group.status` already reads "complete" by then. `s.message.status` (the enclosing
 * turn, read straight off the same ambient scope `AssistantActions`/`ErrorCard` already
 * read from, no prop needed) is what is actually still moving in that window — a
 * `requires-action` part (an approval card, real and not instant) is kept as its own
 * signal alongside it, since a turn waiting on the end-user does not itself read
 * "running" everywhere upstream might check. Harmless against the real engine (there a
 * tool call genuinely streams `running` before its result, so `group.status` alone would
 * already agree); this extra term exists only for backend.py's atomic call+result
 * updates, so do not read it as a TS divergence and remove it — without it `/toolsonly`
 * and this component's own spec ("opened itself while live") would fail.
 *
 * `everLive` (from `AssistantMessage`, which does not remount for this) covers a case
 * neither of the above does: convert.ts gives a pending approval's tool-call part the
 * ENGINE's own id (`approval.toolCallId`) and a settled step a DIFFERENT, derived one
 * (`stepToolCallId`, `messageId:step:index`) — vendored TS behaviour, not this page's own
 * (README.md: "re-vendor rather than edit"). `buildGroupTree` keys a group on its first
 * part's id, so the id changing out from under a group whose only call was approved (or
 * denied) makes React remount THIS component the moment the decision resolves — a fresh
 * `useState(live)` on the fresh instance sees `live` already false (a fast, e.g.
 * `--chunk-delay 0`, resolution) and initialises `open` closed, hiding the very row the
 * end-user just approved (found running `Always allow` 8/8 after the count-1 bypass came
 * out — TS's own `ToolGroup` would remount the identical way, since it groups a lone
 * approved call the same way now). `everLive` is memory that survives the remount: once
 * true for a message it stays true, so a fresh instance still opens correctly.
 */
import { useState, type FC, type ReactNode } from 'react';
import { ChevronDown, LoaderCircle } from 'lucide-react';
import { useAuiState, type MessagePrimitive } from '@assistant-ui/react';

export const ToolGroup: FC<{ group: MessagePrimitive.GroupedParts.GroupPart; everLive: boolean; children?: ReactNode }> = ({
  group,
  everLive,
  children,
}) => {
  const running = group.status.type === 'running';
  const messageRunning = useAuiState((s) => s.message.status?.type === 'running');
  const live = running || group.status.type === 'requires-action' || messageRunning || everLive;
  const [open, setOpen] = useState(live);
  const [wasLive, setWasLive] = useState(live);
  if (live !== wasLive) {
    setWasLive(live);
    if (live) setOpen(true);
  }
  const count = group.indices.length;
  const label = String(count) + ' tool ' + (count === 1 ? 'call' : 'calls');
  return (
    <div className="t2v-tool-group aui-tool-group-root" data-variant="ghost" data-state={open ? 'open' : 'closed'}>
      <button type="button" className="t2v-tool-group-head aui-tool-group-trigger" aria-expanded={open} onClick={() => setOpen(!open)}>
        {running ? <LoaderCircle className="t2v-spin aui-tool-group-trigger-loader" aria-hidden="true" size={12} /> : null}
        <span className={'aui-tool-group-trigger-label-wrapper' + (running ? ' t2v-shimmer' : '')}>{label}</span>
        <ChevronDown className={'t2v-chevron-down aui-tool-group-trigger-chevron' + (open ? ' t2v-chevron-down-open' : '')} aria-hidden="true" size={12} />
      </button>
      {open ? <div className="t2v-tool-group-body aui-tool-group-content">{children}</div> : null}
    </div>
  );
};
