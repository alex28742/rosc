import { useCallback, useEffect, useRef, useState } from 'react';
import { coreApi, describeError, type CoreApi } from '../core/api';
import { toProjectInput, toTaskInput, type Project, type ProjectFormValues, type RepairConfirmation, type RepairResult, type Task, type TaskFormValues } from '../core/models';
import type { Navigation } from './navigation';
import type { Feedback, LoadStatus } from './presentation/contracts';
import { projectIdOf } from './route';

export type Layer = 'dashboard' | 'project' | 'task' | 'modal';

type TaskCache = { projectId: string | null; items: Task[]; status: LoadStatus };
const NO_TASKS: TaskCache = { projectId: null, items: [], status: 'loading' };
const NO_FEEDBACK: Feedback = { error: '', notice: '' };

const repairNotice = (done: RepairResult<unknown>) => done.repair
  ? `Манифест восстановлен. Копия сохранена в карантин: ${done.repair.backup_id}`
  : 'Манифест уже в порядке.';

/**
 * Owns Core data loading, mutations, messages and the registration modal. Rules enforced here, not in views:
 * - a Task list for Project A is never exposed while the route points at Project B;
 * - messages belong to the layer/route where the action started, and are dropped when the user navigates away;
 * - a success notice is only set after the API call succeeded (a 409/503 never produces one);
 * - one mutation at a time; Draft Task creation and repair stay explicit user actions.
 */
export function useWorkspaceController({ route, routeKey, go }: Navigation, api: CoreApi = coreApi) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsStatus, setProjectsStatus] = useState<LoadStatus>('loading');
  const [tasks, setTasks] = useState<TaskCache>(NO_TASKS);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [registrationOpen, setRegistrationOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const routeRef = useRef(route);
  routeRef.current = route;
  const epoch = useRef(0);
  const projectsSeq = useRef(0);
  const tasksSeq = useRef(0);
  const busyRef = useRef(false);

  const loadProjects = useCallback(async (quiet = false) => {
    const seq = ++projectsSeq.current;
    const started = epoch.current;
    try {
      const list = await api.listProjects();
      if (seq !== projectsSeq.current) return;
      setProjects(list);
      setProjectsStatus('ready');
    } catch (e) {
      if (seq !== projectsSeq.current) return;
      setProjectsStatus('failed');
      if (!quiet && epoch.current === started) setError(describeError(e));
    }
  }, [api]);

  const loadTasks = useCallback(async (projectId: string, quiet = false) => {
    const seq = ++tasksSeq.current;
    const started = epoch.current;
    const current = () => seq === tasksSeq.current && projectIdOf(routeRef.current) === projectId;
    try {
      const items = await api.listTasks(projectId);
      if (current()) setTasks({ projectId, items, status: 'ready' });
    } catch (e) {
      if (!current()) return;
      setTasks(prev => ({ projectId, items: prev.projectId === projectId ? prev.items : [], status: 'failed' }));
      if (!quiet && epoch.current === started) setError(describeError(e));
    }
  }, [api]);

  const projectId = projectIdOf(route);

  // Effect order matters: the first one starts a new message epoch before the loaders capture it.
  useEffect(() => { epoch.current++; setError(''); setNotice(''); }, [routeKey]);
  useEffect(() => { void loadProjects(); }, [loadProjects]);
  useEffect(() => {
    if (projectId) void loadTasks(projectId);
    else { tasksSeq.current++; setTasks(NO_TASKS); }
  }, [projectId, routeKey, loadTasks]);

  const project = projects.find(p => p.id === projectId) ?? null;
  const taskList = tasks.projectId === projectId && projectId ? tasks : NO_TASKS;
  const task = route.kind === 'task' ? taskList.items.find(t => t.id === route.taskId) ?? null : null;

  const layer: Layer = registrationOpen ? 'modal'
    : (route.kind === 'task' || route.kind === 'new-task') && project ? 'task'
      : route.kind !== 'dashboard' ? 'project' : 'dashboard';

  const feedbackFor = (target: Layer): Feedback => (layer === target ? { error, notice } : NO_FEEDBACK);

  /** Runs one mutation. `live()` is false once the user navigated or closed the modal, so late results do not yank the UI. */
  const mutate = useCallback(async (run: (live: () => boolean) => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError('');
    setNotice('');
    const started = epoch.current;
    try { await run(() => epoch.current === started); }
    finally { busyRef.current = false; setBusy(false); }
  }, []);

  const fail = (live: () => boolean, e: unknown) => { if (live()) setError(describeError(e)); };

  const registerProject = (values: ProjectFormValues) => mutate(async live => {
    try {
      const created = await api.createProject(toProjectInput(values));
      await loadProjects();
      // `live()` is false once this modal was dismissed or replaced; the Project stays created and cached, but the UI is not touched.
      if (live()) { setRegistrationOpen(false); go({ kind: 'project', projectId: created.id }); }
    } catch (e) {
      fail(live, e);
      void loadProjects(true);
    }
  });

  const createTask = (values: TaskFormValues) => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'new-task') return;
    try {
      const created = await api.createTask(at.projectId, toTaskInput(values));
      await loadTasks(at.projectId);
      if (live()) go({ kind: 'task', projectId: at.projectId, taskId: created.id });
    } catch (e) {
      fail(live, e);
      void loadTasks(at.projectId, true);
    }
  });

  const reconcileProject = () => mutate(async live => {
    const id = projectIdOf(routeRef.current);
    if (!id) return;
    try { await api.reconcileProject(id); await loadProjects(); }
    catch (e) { fail(live, e); void loadProjects(true); }
  });

  const repairProject = (_confirmation: RepairConfirmation) => mutate(async live => {
    const id = projectIdOf(routeRef.current);
    if (!id) return;
    try {
      const done = await api.repairProject(id);
      await loadProjects();
      if (live()) setNotice(repairNotice(done));
    } catch (e) {
      fail(live, e);
      void loadProjects(true);
    }
  });

  const reconcileTask = () => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'task') return;
    try { await api.reconcileTask(at.projectId, at.taskId); await loadTasks(at.projectId); }
    catch (e) { fail(live, e); void loadTasks(at.projectId, true); }
  });

  const repairTask = (_confirmation: RepairConfirmation) => mutate(async live => {
    const at = routeRef.current;
    if (at.kind !== 'task') return;
    try {
      const done = await api.repairTask(at.projectId, at.taskId);
      await loadTasks(at.projectId);
      if (live()) setNotice(repairNotice(done));
    } catch (e) {
      fail(live, e);
      void loadTasks(at.projectId, true);
    }
  });

  const retry = async () => {
    setError('');
    setNotice('');
    await loadProjects();
    if (projectId) await loadTasks(projectId);
  };

  return {
    layer, projects, projectsStatus, project, tasks: taskList.items, tasksStatus: taskList.status, task, busy, registrationOpen,
    feedbackFor,
    actions: {
      retry,
      // Each modal instance gets its own epoch and starts and ends with clean, modal-owned feedback.
      openRegistration: () => { epoch.current++; setError(''); setNotice(''); setRegistrationOpen(true); },
      closeRegistration: () => { epoch.current++; setError(''); setNotice(''); setRegistrationOpen(false); },
      registerProject, createTask, reconcileProject, repairProject, reconcileTask, repairTask,
    },
  };
}
