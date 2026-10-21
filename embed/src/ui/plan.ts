/**
 * The agent's plan as steps. The engine's `write_todos` takes free markdown, and its
 * system prompt asks for a `- [ ] step` list with `- [x]` on the finished ones; only a
 * plan that is nothing but that list becomes steps. Anything else (a heading, a plain
 * bullet, a numbered list) is `null`, and PlanPart shows the markdown as written.
 */

export type PlanStepState = 'done' | 'active' | 'pending';

export interface PlanStep {
  text: string;
  state: PlanStepState;
}

export interface ParsedPlan {
  steps: PlanStep[];
  done: number;
}

const ITEM = /^\s*[-*+]\s+\[([ xX])\]\s+(\S.*?)\s*$/;

export function parsePlan(markdown: string): ParsedPlan | null {
  const items: Array<{ text: string; done: boolean }> = [];
  for (const line of markdown.split(/\r?\n/)) {
    if (!line.trim()) continue;
    const [, mark, text] = ITEM.exec(line) ?? [];
    if (mark === undefined || text === undefined) return null;
    items.push({ text, done: mark !== ' ' });
  }
  if (items.length === 0) return null;

  // The first open step is the one under way; the rest wait behind it.
  const active = items.findIndex((item) => !item.done);
  const steps = items.map(
    (item, i): PlanStep => ({ text: item.text, state: item.done ? 'done' : i === active ? 'active' : 'pending' }),
  );
  return { steps, done: items.filter((item) => item.done).length };
}
