/**
 * P052: preview of the canonical `@DisplayName` rule (authoritative copy: `src/rosc/mentions.py`). The UI uses it only to
 * route the Send and show the Owner which addressees the typed text adds; Core re-parses the text and decides.
 *
 * `@` starts the text or follows a non-word character; the name matches exactly with Unicode case folding; the next
 * character is not a letter, digit, `_` or `-`; the longest name wins; a name shared by two candidates is ambiguous.
 */
export type MentionResult = { refs: string[]; ambiguous: string[] };

const word = (ch: string) => /[\p{L}\p{N}_]/u.test(ch);
const fold = (text: string) => text.toLocaleLowerCase('und').normalize('NFC');

export function findMentions(text: string, candidates: Record<string, string[]>): MentionResult {
  const byName = new Map<string, Set<string>>();
  for (const [ref, names] of Object.entries(candidates)) {
    for (const raw of names) {
      const name = fold(raw.trim());
      if (!name) continue;
      if (!byName.has(name)) byName.set(name, new Set());
      byName.get(name)!.add(ref);
    }
  }
  const refs: string[] = [];
  const ambiguous: string[] = [];
  if (byName.size === 0 || !text.includes('@')) return { refs, ambiguous };
  const ordered = [...byName.keys()].sort((a, b) => b.length - a.length);
  let pos = text.indexOf('@');
  while (pos !== -1) {
    let next = pos + 1;
    if (pos === 0 || !word(text[pos - 1])) {
      for (const name of ordered) {
        const end = pos + 1 + name.length;
        if (end > text.length || fold(text.slice(pos + 1, end)) !== name) continue;
        if (end < text.length && (word(text[end]) || text[end] === '-')) continue;
        const owners = byName.get(name)!;
        if (owners.size > 1) { if (!ambiguous.includes(name)) ambiguous.push(name); }
        else {
          const ref = [...owners][0];
          if (!refs.includes(ref)) refs.push(ref);
        }
        next = end;
        break;
      }
    }
    pos = text.indexOf('@', next);
  }
  return { refs, ambiguous };
}
