import { describe, expect, it } from 'vitest';
import { findMentions } from './mentions';
import parity from './mentionParity.json';

type Case = { note: string; text: string; names: Record<string, string[]>; refs: string[]; ambiguous: string[]; unknown: string[] };

describe('P052 r02 @mention parity with the authoritative Python parser', () => {
  for (const c of (parity as unknown as { cases: Case[] }).cases) {
    it(c.note || c.text, () => {
      expect(findMentions(c.text, c.names)).toEqual({ refs: c.refs, ambiguous: c.ambiguous, unknown: c.unknown });
    });
  }
});
