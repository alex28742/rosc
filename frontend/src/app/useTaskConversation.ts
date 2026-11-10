import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ConversationMessage, MessageRecipient } from '../core/models';
import type { TaskConversationControls } from './presentation/contracts';

type Cache = TaskConversationControls['state'] & { scope: string | null };

const emptyFor = (scope: string | null): Cache => ({
  scope, status: 'loading', conversationId: null, messages: [], hasMore: false, nextBefore: null, error: '',
  sending: false, olderLoading: false,
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
      return null;
    } catch (e) {
      return describeError(e);
    } finally {
      if (scopeRef.current === current) sending.current = false;
      patch(current, { sending: false });
    }
  }, [api, patch, projectId, taskId]);

  const { scope: _scope, ...state } = visible;
  void _scope;
  return { state, onReload, onLoadOlder: () => void onLoadOlder(), onSend };
}
