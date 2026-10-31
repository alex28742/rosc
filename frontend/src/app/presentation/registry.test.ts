import { describe, expect, it } from 'vitest';
import type { Presentation } from './contracts';
import { ACTIVE_PRESENTATION, presentations, resolvePresentation } from './registry';

describe('presentation registry', () => {
  it('resolves the configured package and it provides every logical view', () => {
    const p = resolvePresentation();
    expect(p.id).toBe(ACTIVE_PRESENTATION);
    for (const view of ['Shell', 'Dashboard', 'Project', 'Task', 'NewTask', 'RegisterProject'] as const) expect(p[view]).toBeTypeOf('function');
  });

  it('fails explicitly for unknown keys, including inherited object keys', () => {
    expect(() => resolvePresentation('mobile')).toThrow(/Unknown presentation "mobile"/);
    expect(() => resolvePresentation('toString')).toThrow(/Unknown presentation/);
  });

  it('rejects an entry whose declared id differs from its key', () => {
    const wrong = { ...presentations.default, id: 'other' } as Presentation;
    expect(() => resolvePresentation('default', { default: wrong })).toThrow(/declares id "other"/);
  });
});
