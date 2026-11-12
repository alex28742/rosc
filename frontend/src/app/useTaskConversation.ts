import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ArchitectStatus, TaskArchitectStatus, ConversationMessage, MessageDelivery, MessageRecipient } from '../core/models';
import type { TaskConversationControls } from './presentation/contracts';

type ArchitectCache = { status: 'loading' | 'ready' | 'failed'; info: ArchitectStatus | null };
type TaskArchitectCache = { status: 'loading' | 'ready' | 'failed'; info: TaskArchitectStatus | null; busy: boolean };
type Cache = TaskConversationControls['state'] & { scope: string | null; architect: ArchitectCache; taskArchitect: TaskArchitectCache };

/** While a delivery awaits the Architect's reply the history is re-read on this interval; the Architect status too. */
const DELIVERY_POLL_MS = 2000;
const ARCHITECT_POLL_MS = 10000;

const emptyFor = (scope: string | null): Cache => ({
  scope, status: 'loading', conversationId: null, messages: [], hasMore: false, nextBefore: null, error: '',
  sending: false, olderLoading: false, architect: { status: 'loading', info: null },
  taskArchitect: { status: 'loading', info: null, busy: false },
});

/** Union by message_id in ordinal order; the server-confirmed record wins over a duplicate. */
const merge = (known: ConversationMessage[], incoming: ConversationMessage[]): ConversationMessage[] => {
  const byId = new Map<string, ConversationMessage>();
  for (const message of known) byId.set(message.message_id, message);
  for (const message of incoming) byId.set(message.message_id, message);
  return [...byId.values()].sort((a, b) => a.ordinal - b.ordinal);
};

/**
 * Task Conversation for the open Task, cached per (projectId, taskId) only while that Task is open. Switching Task
 * resets the cache synchronously, and every async result is dropped unless it still belongs to the active scope, so a
 * late response can never repaint another Task's chat. History is always re-read from Core.
 */
export function useTaskConversation(api: CoreApi, projectId: string | null, taskId: string | null): TaskConversationControls {
  const scope = projectId && taskId ? `${projectId}/${taskId}` : null;
  const [cache, setCache] = useState<Cache>(() => emptyFor(scope));
  const scopeRef = useRef(scope);
  const cacheRef = useRef(cache);
  const seq = useRef(0);
  const sending = useRef(false);
  const onSendToArchitectRef = useRef<(messageId: string) => Promise<string | null>>(async () => null);
  cacheRef.current = cache;

  // A render for a new Task must never show the previous Task's messages, even for one frame.
  const visible = cache.scope === scope ? cache : emptyFor(scope);

  const patch = useCallback((forScope: string, change: Partial<Cache>) => {
    setCache(prev => (prev.scope === forScope ? { ...prev, ...change } : prev));
  }, []);

  const load = useCallback(async (forScope: string, pid: string, tid: string) => {
    const mine = ++seq.current;
    try {
      const page = await api.getTaskConversation(pid, tid);
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => {
        if (prev.scope !== forScope) return prev;
        const olderKnown = prev.messages.length > 0 && prev.messages[0].ordinal < (page.messages[0]?.ordinal ?? Infinity);
        return {
          ...prev, status: 'ready', error: '', conversationId: page.conversation?.conversation_id ?? prev.conversationId,
          messages: merge(prev.messages, page.messages),
          hasMore: olderKnown ? prev.hasMore : page.page.has_more,
          nextBefore: olderKnown ? prev.nextBefore : page.page.next_before,
        };
      });
    } catch (e) {
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      patch(forScope, { status: 'failed', error: describeError(e) });
    }
  }, [api, patch]);

  useEffect(() => {
    scopeRef.current = scope;
    seq.current++;
    sending.current = false;
    setCache(emptyFor(scope));
    if (scope && projectId && taskId) void load(scope, projectId, taskId);
  }, [scope, projectId, taskId, load]);

  const archSeq = useRef(0);
  /** Read-only Core status probe; a result for a Task that is no longer open is dropped. */
  const loadArchitect = useCallback(async (forScope: string, pid: string) => {
    const mine = ++archSeq.current;
    try {
      const info = await api.getArchitect(pid);
      if (mine !== archSeq.current || scopeRef.current !== forScope) return;
      patch(forScope, { architect: { status: 'ready', info } });
    } catch {
      if (mine !== archSeq.current || scopeRef.current !== forScope) return;
      patch(forScope, { architect: { status: 'failed', info: null } });
    }
  }, [api, patch]);

  useEffect(() => {
    if (!scope || !projectId) return;
    void loadArchitect(scope, projectId);
    const timer = setInterval(() => void loadArchitect(scope, projectId), ARCHITECT_POLL_MS);
    return () => clearInterval(timer);
  }, [scope, projectId, loadArchitect]);

  const taskArchSeq = useRef(0);
  const activating = useRef(false);
  /** Read-only status of THIS Task's own native session; a result for a Task that is no longer open is dropped. */
  const loadTaskArchitect = useCallback(async (forScope: string, pid: string, tid: string) => {
    const mine = ++taskArchSeq.current;
    try {
      const info = await api.getTaskArchitect(pid, tid);
      if (mine !== taskArchSeq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, taskArchitect: { ...prev.taskArchitect, status: 'ready', info } });
    } catch {
      if (mine !== taskArchSeq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, taskArchitect: { ...prev.taskArchitect, status: 'failed', info: null } });
    }
  }, [api]);

  // Opening a Task, or coming back to it, only READS this status; there is deliberately no polling.
  useEffect(() => {
    if (!scope || !projectId || !taskId) return;
    void loadTaskArchitect(scope, projectId, taskId);
    const again = () => { if (document.visibilityState !== 'hidden') void loadTaskArchitect(scope, projectId, taskId); };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
    return () => { window.removeEventListener('focus', again); document.removeEventListener('visibilitychange', again); };
  }, [scope, projectId, taskId, loadTaskArchitect]);

  const onActivateTaskArchitect = useCallback(async (): Promise<string | null> => {
    const current = scopeRef.current;
    if (!current || !projectId || !taskId) return 'Задача не открыта.';
    if (activating.current) return null; // a second click while one request is running does nothing
    activating.current = true;
    setCache(prev => prev.scope !== current ? prev : { ...prev, taskArchitect: { ...prev.taskArchitect, busy: true } });
    try {
      const info = await api.activateTaskArchitect(projectId, taskId);
      if (scopeRef.current === current) {
        taskArchSeq.current++;
        setCache(prev => prev.scope !== current ? prev : { ...prev, taskArchitect: { status: 'ready', info, busy: false } });
        void loadArchitect(current, projectId);
      }
      return null;
    } catch (e) {
      if (scopeRef.current === current) {
        setCache(prev => prev.scope !== current ? prev : { ...prev, taskArchitect: { ...prev.taskArchitect, busy: false } });
        void loadTaskArchitect(current, projectId, taskId);
      }
      return describeError(e);
    } finally {
      activating.current = false;
    }
  }, [api, loadArchitect, loadTaskArchitect, projectId, taskId]);

  const awaiting = visible.messages.some(message => message.delivery?.state === 'pending');
  useEffect(() => {
    if (!awaiting || !scope || !projectId || !taskId) return;
    const timer = setInterval(() => void load(scope, projectId, taskId), DELIVERY_POLL_MS);
    return () => clearInterval(timer);
  }, [awaiting, scope, projectId, taskId, load]);

  const setDelivery = useCallback((forScope: string, messageId: string, delivery: MessageDelivery) => {
    setCache(prev => prev.scope !== forScope ? prev : {
      ...prev, messages: prev.messages.map(message => message.message_id === messageId ? { ...message, delivery } : message),
    });
  }, []);

  /** Runs one delivery action for a message of the open Task and re-reads the history, never repainting another Task. */
  const deliveryAction = useCallback(async (messageId: string,
    call: (pid: string, tid: string, id: string) => Promise<{ delivery: MessageDelivery }>): Promise<string | null> => {
    const current = scopeRef.current;
    if (!current || !projectId || !taskId) return 'Задача не открыта.';
    try {
      const result = await call(projectId, taskId, messageId);
      setDelivery(current, messageId, result.delivery);
      void load(current, projectId, taskId);
      void loadArchitect(current, projectId);
      void loadTaskArchitect(current, projectId, taskId);
      return null;
    } catch (e) {
      void loadArchitect(current, projectId);
      void loadTaskArchitect(current, projectId, taskId);
      return describeError(e);
    }
  }, [load, loadArchitect, loadTaskArchitect, projectId, setDelivery, taskId]);

  const onSendToArchitect = useCallback((messageId: string) =>
    deliveryAction(messageId, (pid, tid, id) => api.sendTaskMessageToArchitect(pid, tid, id)), [api, deliveryAction]);
  onSendToArchitectRef.current = onSendToArchitect;
  const onReconcile = useCallback((messageId: string) =>
    deliveryAction(messageId, (pid, _tid, id) => api.reconcileDelivery(pid, id)), [api, deliveryAction]);
  const onRetryDelivery = useCallback((messageId: string) =>
    deliveryAction(messageId, (pid, _tid, id) => api.retryDelivery(pid, id)), [api, deliveryAction]);

  const onReload = useCallback(() => {
    const current = scopeRef.current;
    if (!current || !projectId || !taskId) return;
    patch(current, { status: 'loading', error: '' });
    void load(current, projectId, taskId);
  }, [load, patch, projectId, taskId]);

  const onLoadOlder = useCallback(async () => {
    const current = scopeRef.current;
    const state = cacheRef.current;
    if (!current || !projectId || !taskId || state.scope !== current || !state.hasMore || state.nextBefore === null
      || state.olderLoading) return;
    const cursor = state.nextBefore;
    patch(current, { olderLoading: true });
    try {
      const page = await api.getTaskConversation(projectId, taskId, cursor);
      setCache(prev => prev.scope !== current ? prev : {
        ...prev, olderLoading: false, messages: merge(prev.messages, page.messages),
        hasMore: page.page.has_more, nextBefore: page.page.next_before,
      });
    } catch (e) {
      patch(current, { olderLoading: false, error: describeError(e) });
    }
  }, [api, patch, projectId, taskId]);

  const onSend = useCallback(async (body: string, clientKey: string, recipient: MessageRecipient): Promise<string | null> => {
    const current = scopeRef.current;
    const state = cacheRef.current;
    if (!current || !projectId || !taskId) return 'Задача не открыта.';
    if (state.scope !== current || state.status !== 'ready') return 'История задачи ещё не загружена.';
    if (sending.current) return 'Предыдущее сообщение ещё сохраняется.';
    sending.current = true;
    patch(current, { sending: true });
    try {
      const result = await api.sendTaskConversationMessage(projectId, taskId, { client_key: clientKey, body, recipient });
      // A message confirmed by Core is kept whatever happened to the history requests meanwhile.
      setCache(prev => prev.scope !== current ? prev
        : { ...prev, conversationId: result.message.conversation_id, messages: merge(prev.messages, [result.message]) });
      // Saving never delivers. Only an Architect-addressed NEW message, and only while Core reports a verified live
      // Architect, is handed over right away; any failure leaves it `not_sent` with an explicit send action.
      if (recipient === 'architect' && !result.replayed && cacheRef.current.architect.info?.state === 'ready') {
        void onSendToArchitectRef.current(result.message.message_id);
      }
      return null;
    } catch (e) {
      return describeError(e);
    } finally {
      if (scopeRef.current === current) sending.current = false;
      patch(current, { sending: false });
    }
  }, [api, patch, projectId, taskId]);

  const { scope: _scope, architect, taskArchitect, ...state } = visible;
  void _scope;
  const onRefreshArchitect = useCallback(() => {
    const current = scopeRef.current;
    if (current && projectId && taskId) { void loadArchitect(current, projectId); void loadTaskArchitect(current, projectId, taskId); }
  }, [loadArchitect, loadTaskArchitect, projectId, taskId]);
  return { state, architect, taskArchitect, onActivateTaskArchitect, onRefreshArchitect, onReload, onLoadOlder: () => void onLoadOlder(), onSend, onSendToArchitect, onReconcile,
    onRetryDelivery };
}
