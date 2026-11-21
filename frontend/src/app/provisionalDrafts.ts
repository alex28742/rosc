import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { DraftFrame, ProvisionalDraft } from '../core/models';

/**
 * P062: provisional (non-canonical) agent text of ONE open room, fed by the room SSE `draft` frames.
 *
 * Runtime/model/role agnostic: the frames carry only text, a turn id, an agent id and a state — the UI never learns which
 * runtime or model produced them and never picks a renderer by role.  The draft is only shown; it is not stored, sent,
 * searched or counted.  Its end is decided by Core: a `published` frame names the committed canonical message, and the
 * bubble is replaced by that message once it is in the loaded history (see `ProvisionalDrafts`).  P062.1: the same frames
 * carry a direct (non-group) Architect delivery too; its draft is matched to the canonical reply only by that committed
 * message id, never by a guess.
 */

/** Rendering is batched: many small deltas inside this window become one React update (no re-render storm). */
export const DRAFT_RENDER_MS = 50;
/** Defensive bound of what one room keeps in memory (the server bounds it too). */
const MAX_DRAFT_CHARS = 200_000;
const MAX_DRAFTS = 8;
const STATES = new Set(['streaming', 'published', 'ended', 'gone']);

export function parseDraftFrame(raw: unknown): DraftFrame | null {
  let value: unknown;
  try { value = typeof raw === 'string' ? JSON.parse(raw) : null; } catch { return null; }
  if (!value || typeof value !== 'object') return null;
  const f = value as Record<string, unknown>;
  if (typeof f.turn !== 'string' || !f.turn || typeof f.offset !== 'number' || !Number.isInteger(f.offset) || f.offset < 0
    || typeof f.text !== 'string' || typeof f.state !== 'string' || !STATES.has(f.state)) return null;
  return {
    turn: f.turn, agent: typeof f.agent === 'string' ? f.agent : null, offset: f.offset, text: f.text,
    state: f.state as DraftFrame['state'], message: typeof f.message === 'string' ? f.message : null,
    truncated: f.truncated === true,
  };
}

/** Pure reducer: append at the exact offset, replace at offset 0, drop on end; an out-of-sync frame is ignored. */
export function applyDraftFrame(drafts: Map<string, ProvisionalDraft>, frame: DraftFrame): Map<string, ProvisionalDraft> {
  const current = drafts.get(frame.turn);
  if (frame.state === 'ended' || frame.state === 'gone') {
    if (!current) return drafts;
    const next = new Map(drafts);
    next.delete(frame.turn);
    return next;
  }
  let text: string;
  if (frame.offset === 0) text = frame.text;
  else if (current && frame.offset === current.text.length) text = current.text + frame.text;
  else return drafts; // never guess: the next snapshot (reconnect) repairs it
  const agentId = frame.agent ?? current?.agentId;
  if (!agentId) return drafts;
  const next = new Map(drafts);
  if (frame.state === 'streaming') {
    // One Agent generates one turn at a time: its newer streaming turn supersedes its older still-streaming one.  A
    // draft of ANOTHER Agent is left alone (P062.1: a direct Architect reply may stream beside a Worker group turn);
    // Core itself ends superseded drafts with an explicit `ended` frame.
    for (const [turn, draft] of next) {
      if (turn !== frame.turn && draft.state === 'streaming' && draft.agentId === agentId) next.delete(turn);
    }
  }
  next.set(frame.turn, {
    turnId: frame.turn, agentId, text: text.slice(0, MAX_DRAFT_CHARS),
    state: frame.state === 'published' ? 'published' : 'streaming',
    messageId: frame.state === 'published' ? frame.message : null,
    truncated: frame.truncated || text.length > MAX_DRAFT_CHARS,
  });
  while (next.size > MAX_DRAFTS) next.delete(next.keys().next().value as string);
  return next;
}

/** The drafts of the room `scope` plus the frame sink for `useRoomEvents`.  A scope change clears everything at once. */
export function useProvisionalDrafts(scope: string | null, onPublished: () => void): [ProvisionalDraft[], (frame: DraftFrame | null) => void] {
  const [state, setState] = useState<{ scope: string | null; drafts: Map<string, ProvisionalDraft> }>(
    () => ({ scope, drafts: new Map() }));
  const pending = useRef<DraftFrame[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const scopeRef = useRef(scope);
  const onPublishedRef = useRef(onPublished);
  onPublishedRef.current = onPublished;

  useEffect(() => {
    scopeRef.current = scope;
    pending.current = [];
    if (timer.current !== undefined) { clearTimeout(timer.current); timer.current = undefined; }
    setState({ scope, drafts: new Map() });
  }, [scope]);
  useEffect(() => () => { if (timer.current !== undefined) clearTimeout(timer.current); }, []);

  const onDraft = useCallback((frame: DraftFrame | null) => {
    if (frame === null) {
      // A reset (closed stream, fresh connection) is applied at once: queued frames of the old state are dropped and the
      // bubbles disappear without waiting for the render batch or for any reconnect.
      pending.current = [];
      if (timer.current !== undefined) { clearTimeout(timer.current); timer.current = undefined; }
      const forScope = scopeRef.current;
      setState(prev => (prev.scope !== forScope || !prev.drafts.size ? prev : { scope: forScope, drafts: new Map() }));
      return;
    }
    pending.current.push(frame);
    if (timer.current !== undefined) return;
    const forScope = scopeRef.current;
    timer.current = setTimeout(() => {
      timer.current = undefined;
      const frames = pending.current;
      pending.current = [];
      if (scopeRef.current !== forScope) return;
      setState(prev => {
        if (prev.scope !== forScope) return prev;
        let drafts = prev.drafts;
        for (const f of frames) drafts = applyDraftFrame(drafts, f);
        return drafts === prev.drafts ? prev : { scope: forScope, drafts };
      });
      // Core committed a reply: read the newest page now so the canonical bubble replaces the draft promptly.
      if (frames.some(f => f.state === 'published')) onPublishedRef.current();
    }, DRAFT_RENDER_MS);
  }, []);

  const visible = state.scope === scope ? state.drafts : null;
  const drafts = useMemo(() => (visible ? Array.from(visible.values()) : []), [visible]);
  return [drafts, onDraft];
}
