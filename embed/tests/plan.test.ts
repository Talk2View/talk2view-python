import { describe, expect, it } from 'vitest';
import { parsePlan } from '../src/ui/plan';

describe('parsePlan', () => {
  it('reads the engine\'s `- [ ]` / `- [x]` checklist as steps, the first open one active', () => {
    expect(parsePlan('- [x] Load MRHead\n- [ ] Threshold the brain\n- [ ] Measure the volume')).toEqual({
      done: 1,
      steps: [
        { text: 'Load MRHead', state: 'done' },
        { text: 'Threshold the brain', state: 'active' },
        { text: 'Measure the volume', state: 'pending' },
      ],
    });
  });

  it('takes `*`/`+` bullets, an upper-case X, indentation, CRLF and blank lines', () => {
    expect(parsePlan('\r\n  * [X] One\r\n\r\n+ [ ] Two\r\n')).toEqual({
      done: 1,
      steps: [
        { text: 'One', state: 'done' },
        { text: 'Two', state: 'active' },
      ],
    });
  });

  it('has no active step once every step is done', () => {
    expect(parsePlan('- [x] One\n- [x] Two')!.steps.map((s) => s.state)).toEqual(['done', 'done']);
  });

  it('gives up on anything that is not only a checklist, so it shows as markdown', () => {
    expect(parsePlan('')).toBeNull();
    expect(parsePlan('Plan:\n- [ ] One')).toBeNull();
    expect(parsePlan('- [ ] One\n- plain bullet')).toBeNull();
    expect(parsePlan('1. Load\n2. Threshold')).toBeNull();
    expect(parsePlan('- [ ]   ')).toBeNull();
  });
});
