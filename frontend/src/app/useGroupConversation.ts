import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ConversationMessage, ConversationMessageResult, GroupStatus } from '../core/models';
import type { GroupControls, LoadStatus } from './presentation/contracts';
import { useRoomEvents } from './useRoomEvents';
import type { HistoryRefresh } from './olderAssessments';

/** While Core reports work in this room (a running driver or a held turn) the status and history are re-read. */
const ACTIVE_POLL_MS = 1500;
/** P052 r03: after a legacy direct Architect reply the room is followed at most this long while a turn is still queued
 * (the driver may not have claimed it yet); never an indefinite poll of an idle room. */
const FOLLOW_WINDOW_MS = 20000;

type Cache = { scope: string | null; status: LoadStatus; info: GroupStatus | null; error: string; busy: string | null; notice: string };
const emptyFor = (scope: string | null): Cache => ({ scope, status: 'loading', info: null, error: '', busy: null, notice: '' });

/** P047: an honest note when a Send was stored and authorized but Core is holding dispatch. */
const BLOCKED_TEXT: Record<string, string> = {
  paused: 'Сообщение сохранено, ответ разрешён, но доставка на паузе — снимите паузу, чтобы агент ответил.',
  emergency_brake: 'Сообщение сохранено, но включён Emergency Brake — новые ответы не запускаются.',
  unknown_turn: 'Сообщение сохранено, но предыдущий ход в состоянии UNKNOWN — нужна сверка, повторной отправки не будет.',
};

/**
 * P046/P047: one room's group conversation (Project room when `taskId` is null, otherwise that Task's room).
 *
 * Reads are pure GETs and never launch anything; polling runs only while Core itself reports in-room work. Every async
 * result is dropped unless it still belongs to the open scope, so a Task switch can never show another room's state.
 * `onMessages` re-reads the canonical history; `onMessage` merges one Core-confirmed message immediately.
 */
export function useGroupConversation(api: CoreApi, projectId: string | null, taskId: string | null,
  onMessages: (refresh?: HistoryRefresh) => void, onMessage: (message: ConversationMessage) => void, revalidateKey: string | null = null,
  live = true): GroupControls {
  const scope = projectId ? `${projectId}/${taskId ?? 'PC'}` : null;
  const [cache, setCache] = useState<Cache>(() => emptyFor(scope));
  const scopeRef = useRef(scope);
  const seq = useRef(0);
  const busyRef = useRef(false);
  const onMessagesRef = useRef(onMessages);
  const onMessageRef = useRef(onMessage);
  onMessagesRef.current = onMessages;
  onMessageRef.current = onMessage;
  const visible = cache.scope === scope ? cache : emptyFor(scope);

  const load = useCallback(async (forScope: string, pid: string, tid: string | null) => {
    const mine = ++seq.current;
    try {
      const info = await api.getGroupStatus(pid, tid);
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, status: 'ready', info, error: '' });
    } catch (e) {
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, status: 'failed', error: describeError(e) });
    }
  }, [api]);

  useEffect(() => {
    scopeRef.current = scope;
    seq.current++;
    busyRef.current = false;
    setCache(emptyFor(scope));
    if (scope && projectId) void load(scope, projectId, taskId);
  }, [scope, projectId, taskId, load]);

  /** P055 (QA-ARCH-STATUS-UI): the roster's eligibility is a snapshot.  When the caller's independently verified
   * Architect status changes (activated, became ready, agent/session changed) the room status is re-read ONCE, so a
   * fresh room never refuses a legitimate group Send on a stale «Architect не активен»; Core still decides eligibility. */
  const lastKey = useRef<{ scope: string | null; key: string | null }>({ scope, key: revalidateKey });
  useEffect(() => {
    const previous = lastKey.current;
    lastKey.current = { scope, key: revalidateKey };
    if (!scope || !projectId || previous.scope !== scope || previous.key === revalidateKey) return;
    void load(scope, projectId, taskId);
  }, [revalidateKey, scope, projectId, taskId, load]);

  /** P061: while this room is OPEN AND VISIBLE, Core's read-only SSE says when its canonical state may have changed
   * (a published reply, a delivery/assessment transition, ...) and the room status + history are re-read via the
   * ordinary GETs.  This also covers changes after the round finished (no polling then).  Stale results of another
   * scope are dropped by `load` and the history loaders. */
  const liveKey = live && scope ? scope : null;
  const workingRef = useRef(false);
  const onRoomChange = useCallback(() => {
    const current = scopeRef.current;
    if (!current || !projectId || current !== scope) return;
    // While a turn is active the existing poll (needed for the CLI activity line, which is not a Core DB fact)
    // already re-reads the status and the newest page every ACTIVE_POLL_MS: only the older loaded pages are added.
    if (workingRef.current) { onMessagesRef.current('older'); return; }
    void load(current, projectId, taskId);
    onMessagesRef.current('loaded');
  }, [load, projectId, taskId, scope]);
  const openEvents = useCallback(() => (projectId ? api.openRoomEvents(projectId, taskId) : null), [api, projectId, taskId]);
  const liveState = useRoomEvents(liveKey, openEvents, onRoomChange);

  const followUntil = useRef(0);
  const info = visible.info;
  const blocked = Boolean(info && (info.controls.paused || info.controls.emergency_brake || info.catchup));
  // A queued turn of an OPEN round counts as work only inside the bounded follow window (see onFollow).
  const queued = Boolean(info && info.round?.state === 'open' && info.waiting.length > 0 && !blocked
    && Date.now() < followUntil.current);
  const working = Boolean(info && (info.driver.running || (info.holding_turn && info.holding_turn.state !== 'unknown') || queued));
  workingRef.current = working;
  const wasWorking = useRef(false);
  useEffect(() => {
    if (!scope || !projectId) return;
    if (!working) {
      // The work just finished: one final history read so the published reply appears without a manual reload.
      if (wasWorking.current) onMessagesRef.current();
      wasWorking.current = false;
      return;
    }
    wasWorking.current = true;
    const timer = setInterval(() => { void load(scope, projectId, taskId); onMessagesRef.current(); }, ACTIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [working, scope, projectId, taskId, load]);

  /** One Owner action at a time; the room state and history are re-read from Core afterwards. */
  const act = useCallback(async (name: string, run: (pid: string, tid: string | null) => Promise<unknown>,
    after?: (result: unknown) => void): Promise<string | null> => {
    const current = scopeRef.current;
    if (!current || !projectId) return 'Разговор не открыт.';
    if (busyRef.current) return 'Предыдущее действие ещё выполняется.';
    busyRef.current = true;
    setCache(prev => prev.scope !== current ? prev : { ...prev, busy: name });
    try {
      const result = await run(projectId, taskId);
      if (scopeRef.current === current) after?.(result);
      return null;
    } catch (e) {
      return describeError(e);
    } finally {
      if (scopeRef.current === current) {
        busyRef.current = false;
        setCache(prev => prev.scope !== current ? prev : { ...prev, busy: null });
        void load(current, projectId, taskId);
        onMessagesRef.current();
      }
    }
  }, [load, projectId, taskId]);

  const onReload = useCallback(() => {
    if (scopeRef.current && projectId) void load(scopeRef.current, projectId, taskId);
  }, [load, projectId, taskId]);

  /** P052 r03: a legacy direct Architect delivery finished.  Its reply may have opened a fresh bounded round whose peer
   * turn runs right away: re-read the room AND the canonical history once, and follow a still-queued turn for a bounded
   * window (polling itself only continues while Core reports work). */
  const onFollow = useCallback(() => {
    if (!scopeRef.current || !projectId) return;
    followUntil.current = Date.now() + FOLLOW_WINDOW_MS;
    void load(scopeRef.current, projectId, taskId);
    onMessagesRef.current();
  }, [load, projectId, taskId]);

  /** P047: ONE Owner Send = stored message + authorization; Core starts the reply itself (no Authorize/Start). */
  const onPost = useCallback((body: string, clientKey: string, to: string[]) =>
    act('post', (pid, tid) => api.sendGroupMessage(pid, tid, { client_key: clientKey, body, to }),
      result => {
        const typed = result as ConversationMessageResult;
        onMessageRef.current(typed.message);
        const blocked = typed.dispatch?.blocked;
        const current = scopeRef.current;
        setCache(prev => prev.scope !== current ? prev : { ...prev, notice: blocked ? BLOCKED_TEXT[blocked] ?? blocked : '' });
      }), [act, api]);
  const onAuthorize = useCallback((agents: string[], quota: number, renew: boolean) =>
    act(renew ? 'renew' : 'authorize', (pid, tid) => api.authorizeGroupRound(pid, tid, { agents, quota, renew })), [act, api]);
  const onStart = useCallback(() => act('start', (pid, tid) => api.startGroupTurns(pid, tid)), [act, api]);
  const onControls = useCallback((change: { paused?: boolean; emergency_brake?: boolean }) =>
    act('controls', (pid, tid) => api.setGroupControls(pid, tid, change)), [act, api]);
  const onRelease = useCallback((agentId: string) =>
    act('release', (pid, tid) => api.releaseGroupMember(pid, tid, agentId)), [act, api]);
  const onKeep = useCallback((agentId: string) =>
    act('keep', (pid, tid) => api.keepGroupMember(pid, tid, agentId)), [act, api]);
  const onCatchup = useCallback((turnId: string, choice: 'send_full' | 'skip_backlog') =>
    act('catchup', async (pid, tid) => {
      await api.chooseGroupCatchup(pid, tid, turnId, choice);
      // The decision belongs to a round the Owner already authorized: continue exactly that queue.
      await api.startGroupTurns(pid, tid);
    }), [act, api]);
  const onReconcile = useCallback(() => act('reconcile', (pid, tid) => api.reconcileGroup(pid, tid)), [act, api]);
  const onContinue = useCallback(() => act('continue', (pid, tid) => api.continueGroup(pid, tid)), [act, api]);

  return { status: visible.status, info: visible.info, error: visible.error, busy: visible.busy, notice: visible.notice, live: liveState, onReload, onPost,
    onAuthorize, onStart, onControls, onRelease, onKeep, onCatchup, onReconcile, onContinue, onFollow };
}
