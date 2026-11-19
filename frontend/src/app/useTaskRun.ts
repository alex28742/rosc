import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, describeError, type CoreApi } from '../core/api';
import type { RunJournalPage, RunReportPage, RunView } from '../core/models';
import type { LoadStatus, TaskRunControls } from './presentation/contracts';

/** A holding Run is re-read this often (GET only: it never sends to the CLI, imports or invokes a model). */
const POLL_MS = 4000;

type Cache = { scope: string | null; status: LoadStatus; run: RunView | null; stale: boolean };
const empty = (scope: string | null): Cache => ({ scope, status: 'loading', run: null, stale: false });

const newKey = () => {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
  return `ui-${random}`;
};

/**
 * P054B: the Task's latest managed Run. Opening the Task, focusing the window and polling only READ. `onLaunch` is the one
 * explicit Owner action; its launch key belongs to that click and survives a network/5xx uncertainty, so «Повторить»
 * can never start a second Run. A 409 for a holding/foreign key shows the existing Run instead of trying a fresh key.
 */
export function useTaskRun(api: CoreApi, projectId: string | null, taskId: string | null, onChanged?: () => void): TaskRunControls {
  const scope = projectId && taskId ? `${projectId}/${taskId}` : null;
  const [cache, setCache] = useState<Cache>(() => empty(scope));
  const [launching, setLaunching] = useState(false);
  const [launchError, setLaunchError] = useState<TaskRunControls['launchError']>(null);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const seq = useRef(0);
  /** The pending launch key of the CURRENT click, per scope + Worker; cleared only by a definite answer. */
  const pending = useRef<Record<string, string>>({});
  const inFlight = useRef(false);
  const changed = useRef(onChanged);
  changed.current = onChanged;
  if (cache.scope !== scope) setCache(empty(scope));
  const visible = cache.scope === scope ? cache : empty(scope);

  const load = useCallback(async (forScope: string, pid: string, tid: string) => {
    const mine = ++seq.current;
    try {
      const runs = await api.listTaskRuns(pid, tid);
      const latest = runs.length ? runs[runs.length - 1] : null;
      const run = latest ? await api.getRun(pid, latest.run_id) : null;
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => {
        if (prev.scope !== forScope) return prev;
        // P055: a recorded Architect Review changes the next action without changing the Run state.
        const reviewOf = (r: RunView) => `${r.review?.state ?? ''}:${r.review?.latest?.version ?? 0}`;
        if (prev.run && run && (prev.run.state !== run.state || reviewOf(prev.run) !== reviewOf(run))) changed.current?.();
        return { ...prev, status: 'ready', run, stale: false };
      });
    } catch {
      if (mine !== seq.current || scopeRef.current !== forScope) return;
      setCache(prev => prev.scope !== forScope ? prev : prev.run ? { ...prev, stale: true } : { ...prev, status: 'failed' });
    }
  }, [api]);

  useEffect(() => {
    if (!scope || !projectId || !taskId) return;
    void load(scope, projectId, taskId);
    const again = () => { if (document.visibilityState !== 'hidden') void load(scope, projectId, taskId); };
    window.addEventListener('focus', again);
    document.addEventListener('visibilitychange', again);
    return () => { window.removeEventListener('focus', again); document.removeEventListener('visibilitychange', again); };
  }, [scope, projectId, taskId, load]);

  const holding = visible.run?.holding === true;
  useEffect(() => {
    if (!scope || !projectId || !taskId || !holding) return;
    const timer = setInterval(() => void load(scope, projectId, taskId), POLL_MS);
    return () => clearInterval(timer);
  }, [scope, projectId, taskId, holding, load]);

  const onLaunch = useCallback(async (agentId: string) => {
    const current = scopeRef.current;
    if (!current || !projectId || !taskId || inFlight.current) return; // a second click while the first runs does nothing
    const slot = `${current}/${agentId}`;
    const key = pending.current[slot] ?? newKey();
    pending.current[slot] = key;
    inFlight.current = true;
    setLaunching(true);
    setLaunchError(null);
    try {
      const run = await api.launchRun(projectId, taskId, agentId, key);
      delete pending.current[slot];
      if (scopeRef.current === current) {
        seq.current += 1; // an older read must not overwrite this answer
        setCache(prev => prev.scope !== current ? prev : { ...prev, status: 'ready', run, stale: false });
        changed.current?.();
      }
    } catch (e) {
      const uncertain = !(e instanceof ApiError) || e.kind === 'network' || e.kind === 'server';
      if (!uncertain) delete pending.current[slot]; // a definite refusal: the next click is a new attempt
      if (scopeRef.current === current) {
        const holdingConflict = e instanceof ApiError && (e.code === 'run_in_progress' || e.code === 'launch_key_conflict');
        setLaunchError({ message: holdingConflict ? `${describeError(e)} Показан текущий Run этой Task.` : describeError(e), uncertain });
        void load(current, projectId, taskId);
        changed.current?.();
      }
    } finally {
      inFlight.current = false;
      if (scopeRef.current === current) setLaunching(false);
    }
  }, [api, projectId, taskId, load]);

  const onReload = useCallback(() => {
    if (scope && projectId && taskId) void load(scope, projectId, taskId);
  }, [scope, projectId, taskId, load]);

  const runId = visible.run?.run_id ?? null;
  const onLoadJournal = useCallback(async (after = 0): Promise<RunJournalPage | string> => {
    if (!projectId || !runId) return 'Run ещё не создан.';
    try { return await api.getRunJournal(projectId, runId, after, 100); } catch (e) { return describeError(e); }
  }, [api, projectId, runId]);
  const onLoadReport = useCallback(async (): Promise<RunReportPage | string> => {
    if (!projectId || !runId) return 'Run ещё не создан.';
    try { return await api.getRunReport(projectId, runId); } catch (e) { return describeError(e); }
  }, [api, projectId, runId]);

  return { status: visible.status, run: visible.run, stale: visible.stale, launching, launchError, onLaunch, onReload,
    onLoadJournal, onLoadReport };
}
