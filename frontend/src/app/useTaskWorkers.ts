import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ProjectWorker, WorkerSession } from '../core/models';
import type { LoadStatus, TaskWorkerControls, WorkerBusy } from './presentation/contracts';

/** A session that is still starting, or a bootstrap awaiting its acknowledgement, is re-read this often; nothing else polls. */
const POLL_MS = 3000;

type Cache = { scope: string | null; status: LoadStatus; workers: ProjectWorker[]; busy: Record<string, WorkerBusy>; errors: Record<string, string> };
const empty = (scope: string | null): Cache => ({ scope, status: 'loading', workers: [], busy: {}, errors: {} });

const inFlight = (item: ProjectWorker) =>
  item.session.state === 'connecting' || (item.session.state === 'active' && item.session.bootstrap.state === 'pending');

/**
 * P042A r02: all active Project Workers, each independently observable and activatable.
 * Opening, focusing, polling and «Проверить» only READ. Two requests can start something and each is reachable only from
 * its own explicit button: `onActivate` (may create a CLI) and `onBootstrap` (asks an active CLI to read entry points).
 * Busy state, errors and in-flight guards are keyed by agent_id, so one Worker never blocks or relabels another.
 */
export function useTaskWorkers(api: CoreApi, projectId: string | null, taskId: string | null): TaskWorkerControls {
  const scope = projectId && taskId ? `${projectId}/${taskId}` : null;
  const [cache, setCache] = useState<Cache>(() => empty(scope));
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const seq = useRef(0);
  const running = useRef<Set<string>>(new Set());
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

  const polling = visible.workers.some(inFlight);
  useEffect(() => {
    if (!scope || !projectId || !polling) return;
    const timer = setInterval(() => void load(scope, projectId), POLL_MS);
    return () => clearInterval(timer);
  }, [scope, projectId, polling, load]);

  const setBusy = (forScope: string, agentId: string, value: WorkerBusy | null) =>
    setCache(prev => {
      if (prev.scope !== forScope) return prev;
      const busy = { ...prev.busy };
      if (value) busy[agentId] = value; else delete busy[agentId];
      return { ...prev, busy };
    });
  const setError = (forScope: string, agentId: string, text: string | null) =>
    setCache(prev => {
      if (prev.scope !== forScope) return prev;
      const errors = { ...prev.errors };
      if (text) errors[agentId] = text; else delete errors[agentId];
      return { ...prev, errors };
    });
  const merge = (forScope: string, session: WorkerSession) => {
    seq.current += 1; // a list read that started earlier must not overwrite this newer, per-agent answer
    setCache(prev => prev.scope !== forScope ? prev : {
      ...prev, workers: prev.workers.map(item => item.agent.agent_id === session.agent_id ? { ...item, session } : item) });
  };

  const act = useCallback((kind: WorkerBusy, call: (pid: string, agentId: string) => Promise<WorkerSession>) =>
    async (agentId: string): Promise<void> => {
      const current = scopeRef.current;
      if (!current || !projectId) return;
      const key = `${current}/${agentId}`;
      if (running.current.has(key)) return; // a second click on the SAME Worker while its request runs does nothing
      running.current.add(key);
      setError(current, agentId, null);
      setBusy(current, agentId, kind);
      try {
        const session = await call(projectId, agentId);
        if (scopeRef.current === current) merge(current, session);
      } catch (e) {
        if (scopeRef.current === current) { setError(current, agentId, describeError(e)); void load(current, projectId); }
      } finally {
        running.current.delete(key);
        if (scopeRef.current === current) setBusy(current, agentId, null);
      }
    }, [projectId, load]);

  const onCheck = useCallback((agentId: string) => act('check', api.getWorkerSession)(agentId), [act, api]);
  const onActivate = useCallback((agentId: string) => act('activate', api.activateWorkerSession)(agentId), [act, api]);
  const onBootstrap = useCallback((agentId: string) => act('bootstrap', api.bootstrapWorkerSession)(agentId), [act, api]);

  return { status: visible.status, workers: visible.workers, busy: visible.busy, errors: visible.errors, onCheck, onActivate, onBootstrap };
}
