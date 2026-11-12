import type { ReactNode } from 'react';
import type { ConversationMessage } from '../../../core/models';

export type Match = { messageId: string; start: number; end: number; id: string };

export const MAX_MATCHES = 500;

/** Case-insensitive, client-local search over the messages that are already loaded. */
export function findMatches(messages: ConversationMessage[], term: string): Match[] {
  const needle = term.toLocaleLowerCase('ru');
  const matches: Match[] = [];
  if (!needle) return matches;
  for (const message of messages) {
    const haystack = message.body.toLocaleLowerCase('ru');
    let from = 0;
    let at = haystack.indexOf(needle, from);
    while (at !== -1) {
      if (matches.length >= MAX_MATCHES) return matches;
      matches.push({ messageId: message.message_id, start: at, end: at + needle.length,
        id: `rosc-chat-match-${matches.length}` });
      from = at + needle.length;
      at = haystack.indexOf(needle, from);
    }
  }
  return matches;
}

/** Renders plain text, wrapping search hits in <mark>. Never uses HTML insertion: the body is text, not markup. */
export function renderBody(body: string, matches: Match[], currentId: string | null): ReactNode {
  if (matches.length === 0) return body;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  for (const match of matches) {
    if (match.start > cursor) nodes.push(body.slice(cursor, match.start));
    nodes.push(<mark key={match.id} id={match.id}
      className={match.id === currentId ? 'rosc-chat-mark rosc-chat-mark--current' : 'rosc-chat-mark'}>
      {body.slice(match.start, match.end)}
    </mark>);
    cursor = match.end;
  }
  if (cursor < body.length) nodes.push(body.slice(cursor));
  return nodes;
}
