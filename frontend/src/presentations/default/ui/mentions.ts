/**
 * P052 r02: preview of the canonical `@DisplayName` rule. The AUTHORITATIVE parser is `src/rosc/mentions.py`: every
 * server route re-parses the text and decides. The UI uses this copy only to choose the Send route and to show which
 * addressees the text adds; the explicit ids it sends are the Owner's chips alone, so a divergent preview can never
 * address an Agent the server would not (the server refuses the send instead, before anything is stored).
 *
 * Rule (identical to the Python module; shared parity cases: `mentionParity.json`):
 * - text and names are NFC-normalized; a backtick code span is literal; an `@` right after a letter, digit, mark, `_`,
 *   `\\`, `/`, `.` or `@` is not a mention (e-mail, escape, path);
 * - names compare case-insensitively under Python `str.casefold` (full Unicode case folding), exactly, against ORIGINAL
 *   text characters folded one code point at a time, so an expanding fold (`ß` -> `ss`, `İ` -> `i̇`) never shifts the span;
 * - the next character is not a letter, digit, mark, `_` or `-`; the longest span wins; a folded name shared by two
 *   candidates is ambiguous; an `@` followed by a letter that matches nobody is an UNKNOWN recipient.
 *
 * JavaScript has no `casefold`: `foldChar` is `toLowerCase` per code point plus FOLD_EXCEPTIONS, the code points whose
 * Python fold class `toLowerCase` does not reproduce (generated from Python 3.12 / Unicode 15 against Node / ICU). A
 * character outside that table and that Unicode version may still diverge; then the server decision prevails.
 */
export type MentionResult = { refs: string[]; ambiguous: string[]; unknown: string[] };

const FOLD_EXCEPTIONS: Record<string, string> = Object.fromEntries((
  '\u00b5:\u03bc \u00df:\u0073\u0073 \u0149:\u02bc\u006e \u017f:\u0073 \u0345:\u03b9 \u03c2:\u03c3 \u03d0:\u03b2 ' +
  '\u03d1:\u03b8 \u03d5:\u03c6 \u03d6:\u03c0 \u03f0:\u03ba \u03f1:\u03c1 \u03f5:\u03b5 \u0587:\u0565\u0582 ' +
  '\u1c80:\u0432 \u1c81:\u0434 \u1c82:\u043e \u1c83:\u0441 \u1c84:\u0442 \u1c85:\u0442 \u1c86:\u044a \u1c87:\u0463 ' +
  '\u1c88:\ua64b \u1e9a:\u0061\u02be \u1e9b:\u1e61 \u1e9e:\u0073\u0073 \u1f80:\u1f00\u03b9 \u1f81:\u1f01\u03b9 ' +
  '\u1f82:\u1f02\u03b9 \u1f83:\u1f03\u03b9 \u1f84:\u1f04\u03b9 \u1f85:\u1f05\u03b9 \u1f86:\u1f06\u03b9 ' +
  '\u1f87:\u1f07\u03b9 \u1f88:\u1f00\u03b9 \u1f89:\u1f01\u03b9 \u1f8a:\u1f02\u03b9 \u1f8b:\u1f03\u03b9 ' +
  '\u1f8c:\u1f04\u03b9 \u1f8d:\u1f05\u03b9 \u1f8e:\u1f06\u03b9 \u1f8f:\u1f07\u03b9 \u1f90:\u1f20\u03b9 ' +
  '\u1f91:\u1f21\u03b9 \u1f92:\u1f22\u03b9 \u1f93:\u1f23\u03b9 \u1f94:\u1f24\u03b9 \u1f95:\u1f25\u03b9 ' +
  '\u1f96:\u1f26\u03b9 \u1f97:\u1f27\u03b9 \u1f98:\u1f20\u03b9 \u1f99:\u1f21\u03b9 \u1f9a:\u1f22\u03b9 ' +
  '\u1f9b:\u1f23\u03b9 \u1f9c:\u1f24\u03b9 \u1f9d:\u1f25\u03b9 \u1f9e:\u1f26\u03b9 \u1f9f:\u1f27\u03b9 ' +
  '\u1fa0:\u1f60\u03b9 \u1fa1:\u1f61\u03b9 \u1fa2:\u1f62\u03b9 \u1fa3:\u1f63\u03b9 \u1fa4:\u1f64\u03b9 ' +
  '\u1fa5:\u1f65\u03b9 \u1fa6:\u1f66\u03b9 \u1fa7:\u1f67\u03b9 \u1fa8:\u1f60\u03b9 \u1fa9:\u1f61\u03b9 ' +
  '\u1faa:\u1f62\u03b9 \u1fab:\u1f63\u03b9 \u1fac:\u1f64\u03b9 \u1fad:\u1f65\u03b9 \u1fae:\u1f66\u03b9 ' +
  '\u1faf:\u1f67\u03b9 \u1fb2:\u1f70\u03b9 \u1fb3:\u03b1\u03b9 \u1fb4:\u03ac\u03b9 \u1fb7:\u03b1\u0342\u03b9 ' +
  '\u1fbc:\u03b1\u03b9 \u1fc2:\u1f74\u03b9 \u1fc3:\u03b7\u03b9 \u1fc4:\u03ae\u03b9 \u1fc7:\u03b7\u0342\u03b9 ' +
  '\u1fcc:\u03b7\u03b9 \u1ff2:\u1f7c\u03b9 \u1ff3:\u03c9\u03b9 \u1ff4:\u03ce\u03b9 \u1ff7:\u03c9\u0342\u03b9 ' +
  '\u1ffc:\u03c9\u03b9 \ufb00:\u0066\u0066 \ufb01:\u0066\u0069 \ufb02:\u0066\u006c \ufb03:\u0066\u0066\u0069 ' +
  '\ufb04:\u0066\u0066\u006c \ufb05:\u0073\u0074 \ufb06:\u0073\u0074 \ufb13:\u0574\u0576 \ufb14:\u0574\u0565 ' +
  '\ufb15:\u0574\u056b \ufb16:\u057e\u0576 \ufb17:\u0574\u056d'
).split(' ').map(pair => pair.split(':') as [string, string]));

const CODE_SPAN = /(`+)[\s\S]*?\1/g;
const LITERAL_BEFORE = '_\\/.@';
const MAX_UNKNOWN = 64;
const word = (ch: string) => /[\p{L}\p{N}\p{M}_]/u.test(ch);
const letter = (ch: string) => /\p{L}/u.test(ch);
const foldChar = (ch: string) => FOLD_EXCEPTIONS[ch] ?? ch.toLowerCase();
const fold = (text: string) => Array.from(text.normalize('NFC'), foldChar).join('');

/** End index (exclusive, in code points) where `chars[start:end]` folds exactly to `name`, or -1. */
function matchEnd(chars: string[], start: number, name: string): number {
  let acc = '';
  for (let j = start; j < chars.length; j++) {
    acc += foldChar(chars[j]);
    if (acc === name) return j + 1;
    if (!name.startsWith(acc)) return -1;
  }
  return -1;
}

export function findMentions(text: string, candidates: Record<string, string[]>): MentionResult {
  const byName = new Map<string, Set<string>>();
  for (const [ref, names] of Object.entries(candidates)) {
    for (const raw of names) {
      const trimmed = raw.trim();
      if (!trimmed) continue;
      const name = fold(trimmed);
      if (!byName.has(name)) byName.set(name, new Set());
      byName.get(name)!.add(ref);
    }
  }
  const refs: string[] = [];
  const ambiguous: string[] = [];
  const unknown: string[] = [];
  if (!text.includes('@')) return { refs, ambiguous, unknown };
  const chars = Array.from(text.normalize('NFC').replace(CODE_SPAN, ' '));
  let pos = chars.indexOf('@');
  while (pos !== -1) {
    let next = pos + 1;
    if (pos === 0 || !(word(chars[pos - 1]) || LITERAL_BEFORE.includes(chars[pos - 1]))) {
      let best: [number, string] | null = null;
      for (const name of byName.keys()) {
        const end = matchEnd(chars, pos + 1, name);
        if (end < 0 || (end < chars.length && (word(chars[end]) || chars[end] === '-'))) continue;
        if (best === null || end > best[0]) best = [end, name];
      }
      if (best !== null) {
        const [end, name] = best;
        const owners = byName.get(name)!;
        if (owners.size > 1) { if (!ambiguous.includes(name)) ambiguous.push(name); }
        else {
          const ref = [...owners][0];
          if (!refs.includes(ref)) refs.push(ref);
        }
        next = end;
      } else if (pos + 1 < chars.length && letter(chars[pos + 1])) {
        let end = pos + 1;
        while (end < chars.length && (word(chars[end]) || chars[end] === '-') && end - pos <= MAX_UNKNOWN) end++;
        const token = chars.slice(pos + 1, end).join('');
        if (!unknown.includes(token)) unknown.push(token);
        next = end;
      }
    }
    pos = chars.indexOf('@', next);
  }
  return { refs, ambiguous, unknown };
}
