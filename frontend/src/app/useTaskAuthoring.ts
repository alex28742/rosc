import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, describeError, type CoreApi } from '../core/api';
import type { PromptRevision, SpecRevision, SpecRevisionSummary, TaskAuthoring } from '../core/models';
import type { TaskAuthoringControls } from './presentation/contracts';

/** Bounded refresh so a Draft the Architect saved through its scoped Tool shows up without a manual reload. */
const POLL_MS = 4000;

type Cache = { scope: string | null; status: TaskAuthoringControls['status']; data: TaskAuthoring | null; history: SpecRevisionSummary[]; error: string };
const emptyFor = (scope: string | null): Cache => ({ scope, status: 'loading', data: null, history: [], error: '' });

/**
 * Spec / Prompt Draft state of the open Task, read from Core. Scoped like the Task Conversation: switching Task resets
 * synchronously and every late result for another Task is dropped. Nothing here is stored client-side as truth.
 */
export function useTaskAuthoring(api: CoreApi, projectId: string | null, taskId: string | null): TaskAuthoringControls {
  const scope = projectId && taskId ? `${projectId}/${taskId}` : null;
  const [cache, setCache] = useState<Cache>(() => emptyFor(scope));
  const [busy, setBusy] = useState(false);
  const scopeRef = useRef(scope);
  const seq = useRef(0);
  const dataRef = useRef<TaskAuthoring | null>(null);
  const busyRef = useRef(false);
  const visible = cache.scope === scope ? cache : emptyFor(scope);
  dataRef.current = visible.data;

  const load = useCallback(async (forScope: string, pid: string, tid: string, quiet: boolean) => {
    const mine = ++seq.current;
    try {
      const data = await api.getTaskAuthoring(pid, tid);
      const history = await api.listSpecRevisions(pid, tid);
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope === forScope ? { scope: forScope, status: 'ready', data, history, error: '' } : prev);
    } catch (e) {
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      if (quiet) return;
      setCache(prev => prev.scope === forScope ? { ...prev, status: prev.data ? 'ready' : 'failed', error: describeError(e) } : prev);
    }
  }, [api]);

  useEffect(() => {
    scopeRef.current = scope;
    seq.current++;
    busyRef.current = false;
    setBusy(false);
    setCache(emptyFor(scope));
    if (!scope || !projectId || !taskId) return undefined;
    void load(scope, projectId, taskId, false);
    const timer = window.setInterval(() => { if (!busyRef.current) void load(scope, projectId, taskId, true); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [scope, projectId, taskId, load]);

  const run = useCallback(async <T,>(work: (pid: string, tid: string, data: TaskAuthoring) => Promise<T>): Promise<{ ok: true; value: T } | { ok: false; error: string }> => {
    const data = dataRef.current;
    if (!projectId || !taskId || !data || busyRef.current) return { ok: false, error: 'Данные Task ещё загружаются. Повторите.' };
    const forScope = scopeRef.current;
    busyRef.current = true;
    setBusy(true);
    try {
      const value = await work(projectId, taskId, data);
      if (scopeRef.current === forScope) await load(forScope!, projectId, taskId, false);
      return { ok: true, value };
    } catch (e) {
      if (scopeRef.current === forScope && e instanceof ApiError && e.kind === 'conflict') await load(forScope!, projectId, taskId, true);
      return { ok: false, error: describeError(e) };
    } finally {
      busyRef.current = false;
      if (scopeRef.current === forScope) setBusy(false);
    }
  }, [projectId, taskId, load]);

  return {
    status: visible.status, data: visible.data, specHistory: visible.history, error: visible.error, busy,
    onReload: () => { if (scope && projectId && taskId) void load(scope, projectId, taskId, false); },
    onSaveSpec: async input => {
      const result = await run((pid, tid) => api.saveSpec(pid, tid, input));
      return result.ok ? { error: null, created: result.value.created } : { error: result.error, created: false };
    },
    onLoadSpecRevision: async (revision: number): Promise<SpecRevision | string> => {
      if (!projectId || !taskId) return 'Task не открыт.';
      try { return await api.getSpecRevision(projectId, taskId, revision); } catch (e) { return describeError(e); }
    },
    onLoadPromptRevision: async (revision: number): Promise<PromptRevision | string> => {
      if (!projectId || !taskId) return 'Task не открыт.';
      try { return await api.getPromptRevision(projectId, taskId, revision); } catch (e) { return describeError(e); }
    },
    onSavePrompt: async (body, summary) => {
      const result = await run((pid, tid, data) => api.savePrompt(pid, tid, {
        expected_revision: data.prompt?.revision ?? null, source_spec_revision: data.spec.revision, body, summary }));
      return result.ok ? null : result.error;
    },
    onApprove: async () => {
      const result = await run((pid, tid, data) => {
        if (!data.prompt) return Promise.reject(new Error('Нет сохранённого Draft.'));
        return api.approvePrompt(pid, tid, data.prompt.revision, data.prompt.digest);
      });
      return result.ok ? null : result.error;
    },
  };
}
