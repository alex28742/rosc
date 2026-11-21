import type { ConversationMessage, ConversationPage } from '../core/models';

/** P061: which history a room-change re-read covers.  `latest` = the newest page (the long-standing behaviour);
 * `loaded` = the newest page plus any OLDER loaded message whose Core assessment may still change; `older` = only
 * the latter (used while the existing active-turn polling already re-reads the newest page). */
export type HistoryRefresh = 'latest' | 'loaded' | 'older';

/** P060 states a Core assessment can still leave (`error` may be retried); `assessed` / `owner_action` are final. */
const OPEN_ASSESSMENT = new Set(['awaiting', 'assessing', 'error']);
/** Core's default page size (DEFAULT_PAGE_SIZE): the newest page that an ordinary re-read already covers. */
export const LATEST_PAGE = 50;
/** Hard bound on extra page reads per refresh (each read covers one ordinary page ending at a pending message). */
const MAX_OLDER_READS = 5;

/** Ordinals (newest first) of loaded messages older than `latestFirst` whose assessment is not final. */
export function pendingOlderOrdinals(messages: ConversationMessage[], latestFirst: number): number[] {
  return messages.filter(m => m.ordinal < latestFirst && m.group?.assessment && OPEN_ASSESSMENT.has(m.group.assessment.state))
    .map(m => m.ordinal).sort((a, b) => b - a);
}

/** Replace-only merge: updates messages that are already loaded (by id) and never adds one, so the loaded window,
 * its pagination cursor and the order stay exactly as they were. */
export const replaceKnown = (known: ConversationMessage[], incoming: ConversationMessage[]): ConversationMessage[] => {
  const byId = new Map(incoming.map(message => [message.message_id, message]));
  let changed = false;
  const next = known.map(message => {
    const fresh = byId.get(message.message_id);
    if (!fresh) return message;
    changed = true;
    return fresh;
  });
  return changed ? next : known;
};

/**
 * Re-reads, through the ordinary paged GET (`before` cursor), only the pages that hold an older loaded message with a
 * non-final assessment, and hands each page to `apply` (which must replace known messages only and return false once
 * the result no longer belongs to the open room).  Nothing is read when no such message exists.
 */
export async function refreshOlderAssessments(pending: number[], fetchBefore: (before: number) => Promise<ConversationPage>,
  apply: (messages: ConversationMessage[]) => boolean): Promise<void> {
  let left = pending;
  for (let reads = 0; left.length > 0 && reads < MAX_OLDER_READS; reads++) {
    const top = left[0];
    const page = await fetchBefore(top + 1); // the page ENDING at this pending message (it is included)
    if (!apply(page.messages)) return;
    const low = page.messages[0]?.ordinal ?? top;
    left = left.filter(ordinal => ordinal < low);
  }
}
