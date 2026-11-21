import type { ReactNode } from 'react';
import type { ConversationMessage } from '../../../core/models';
import { ConversationMarkdown, visibleText } from './ConversationMarkdown';

export type Match = { messageId: string; start: number; end: number; id: string };

export const MAX_MATCHES = 500;

/**
 * Case-insensitive, client-local search. IDs are unique to the mounted Conversation, not global match indices.
 * P063: offsets index the text the reader SEES (the rendered Markdown projection), not the raw body: `**`, backticks
 * and link destinations are not searchable, and highlights land inside the formatted nodes.
 */
export function findMatches(messages: ConversationMessage[], term: string, instanceId: string): Match[] {
  const needle = term.toLocaleLowerCase('ru');
  const matches: Match[] = [];
  if (!needle) return matches;
  for (const message of messages) {
    const haystack = visibleText(message.body).toLocaleLowerCase('ru');
    let from = 0;
    let at = haystack.indexOf(needle, from);
    while (at !== -1) {
      if (matches.length >= MAX_MATCHES) return matches;
      matches.push({ messageId: message.message_id, start: at, end: at + needle.length,
        id: `rosc-chat-match-${instanceId}-${matches.length}` });
      from = at + needle.length;
      at = haystack.indexOf(needle, from);
    }
  }
  return matches;
}

/** Renders the body through the single safe Markdown renderer, wrapping search hits in <mark>. Never inserts HTML. */
export function renderBody(body: string, matches: Match[], currentId: string | null): ReactNode {
  return <ConversationMarkdown body={body} matches={matches} currentId={currentId} />;
}
