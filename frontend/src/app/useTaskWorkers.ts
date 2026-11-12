import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ProjectWorker, WorkerSession } from '../core/models';
import type { LoadStatus, TaskWorkerControls } from './presentation/contracts';

/** A session that is still starting is re-read this often; every other state is read only on open/focus/explicit check. */
const CONNECTING_POLL_MS = 3000;

type Cache = { scope: string | null; status: LoadStatus; workers: ProjectWorker[]; selectedId: string; busy: boolean };
const empty = (scope: string | null): Cache => ({ scope, status: 'loading', workers: [], selectedId: '', busy: false });

/**
 * P042A: Project Workers and the selected Worker's external CLI-session state for one open Task.
 * Opening, selecting, focusing and checking only READ. The single request that may start a CLI is `onActivate`,
 * reachable only from the explicit «Активировать сессию» button.
 */
export function useTaskWorkers(api: CoreApi, projectId: string | null, taskId: string | null): TaskWorkerControls {
  const scope = projectId && taskId ? `${projectId}/${taskId}` : null;
  const [cache, setCache] = useState<Cache>(() => empty(scope));
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const seq = useRef(0);
  const activating = useRef(false);
  if (cache.scope !== scope) setCache(empty(scope));
  const visible = cache.scope === scope ? cache : empty(scope);

  const load = useCallback(async (forScope: string, pid: string) => {
    const mine = ++seq.current;
    try {
      const workers = await api.listProjectWorkers(pid);
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, status: 'ready', workers });
    } catch {
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : { ...prev, status: 'failed' });
    }
  }, [api]);

  useEffect(() => {
    if (!scope || !projectId) return;
    void load(scope, projectId);
    const again = () => { if (document.visibilityState !== 'hidden') void load(scope, projectId); };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
    return () => { window.removeEventListener('focus', again); document.removeEventListener('visibilitychange', again); };
  }, [scope, projectId, load]);

  const selected = visible.workers.find(item => item.agent.agent_id === visible.selectedId) ?? null;
  const connecting = selected?.session.state === 'connecting';
  useEffect(() => {
    if (!scope || !projectId || !connecting) return;
    const timer = setInterval(() => void load(scope, projectId), CONNECTING_POLL_MS);
    return () => clearInterval(timer);
  }, [scope, projectId, connecting, load]);

  const merge = (forScope: string, session: WorkerSession) =>
    setCache(prev => prev.scope !== forScope ? prev : {
      ...prev, workers: prev.workers.map(item => item.agent.agent_id === session.agent_id ? { ...item, session } : item) });

  const onSelect = useCallback((agentId: string) => {
    setCache(prev => prev.scope !== scopeRef.current ? prev : { ...prev, selectedId: agentId });
    if (scope && projectId) void load(scope, projectId);
  }, [scope, projectId, load]);

  const onCheck = useCallback(async (): Promise<string | null> => {
    const current = scopeRef.current;
    if (!current || !projectId || !selected) return 'Worker не выбран.';
    try {
      const session = await api.getWorkerSession(projectId, selected.agent.agent_id);
      if (scopeRef.current === current) merge(current, session);
      return null;
    } catch (e) { return describeError(e); }
  }, [api, projectId, selected?.agent.agent_id]);

  const onActivate = useCallback(async (): Promise<string | null> => {
    const current = scopeRef.current;
    if (!current || !projectId || !selected) return 'Worker не выбран.';
    if (activating.current) return null; // a second click while one request is running does nothing
    activating.current = true;
    setCache(prev => prev.scope !== current ? prev : { ...prev, busy: true });
    try {
      const session = await api.activateWorkerSession(projectId, selected.agent.agent_id);
      if (scopeRef.current === current) { merge(current, session); setCache(prev => prev.scope !== current ? prev : { ...prev, busy: false }); }
      return null;
    } catch (e) {
      if (scopeRef.current === current) { setCache(prev => prev.scope !== current ? prev : { ...prev, busy: false }); void load(current, projectId); }
      return describeError(e);
    } finally { activating.current = false; }
  }, [api, projectId, selected?.agent.agent_id, load]);

  return { status: visible.status, workers: visible.workers, selectedId: visible.selectedId, busy: visible.busy, onSelect, onCheck, onActivate };
}
