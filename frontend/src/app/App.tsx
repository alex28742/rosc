import { useEffect, useRef } from 'react';
import { coreApi, type CoreApi } from '../core/api';
import { useNavigation } from './navigation';
import { resolvePresentation } from './presentation/registry';
import { useScrollRegions } from './scroll';
import { useTaskAuthoring } from './useTaskAuthoring';
import { useTaskConversation } from './useTaskConversation';
import { useTaskWorkers } from './useTaskWorkers';
import { useTaskRun } from './useTaskRun';
import { useTextScale } from './useTextScale';
import { useWorkspaceController } from './useWorkspaceController';

const presentation = resolvePresentation();

/** Application orchestration: route + Core controller in, presentation views out. */
export function App({ api = coreApi }: { api?: CoreApi }) {
  const nav = useNavigation();
  const core = useWorkspaceController(nav, api);
  const scroll = useScrollRegions();
  const { Shell, Dashboard, Project, Task, NewTask, RegisterProject, Settings } = presentation;
  const { route, go, close } = nav;
  const { actions, project, layer } = core;

  // While a drawer or modal is open the page behind it must not scroll or take input.
  useEffect(() => {
    document.body.style.overflow = layer === 'dashboard' ? '' : 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, [layer]);

  const projectId = route.kind === 'dashboard' || route.kind === 'settings' ? null : route.projectId;
  // Independent local text scales: Project and Task never share a value, and Dashboard has no scope.
  const projectTextScale = useTextScale(projectId ? `project:${projectId}` : null);
  const taskTextScale = useTextScale(route.kind === 'task' ? `task:${route.taskId}` : null);
  const taskConversation = useTaskConversation(api, route.kind === 'task' ? route.projectId : null,
    route.kind === 'task' ? route.taskId : null);
  const taskAuthoring = useTaskAuthoring(api, route.kind === 'task' ? route.projectId : null,
    route.kind === 'task' ? route.taskId : null);
  const taskWorkers = useTaskWorkers(api, route.kind === 'task' ? route.projectId : null,
    route.kind === 'task' ? route.taskId : null);
  // P054B: the Task's managed Run (reads only; the one launch is an explicit button). A state change refreshes authoring.
  const taskRun = useTaskRun(api, route.kind === 'task' ? route.projectId : null,
    route.kind === 'task' ? route.taskId : null, taskAuthoring.onReload);
  // P055 (QA-RUN-HANDOFF-UI-REFRESH): when THIS Task's Run read shows a handoff message the open Task Conversation does
  // not have yet, re-read that conversation once per Run read (bounded: Run reads happen on open/focus/holding poll only).
  const openTaskId = route.kind === 'task' ? route.taskId : null;
  const runForTask = taskRun.run && taskRun.run.task_id === openTaskId ? taskRun.run : null;
  const handoffId = runForTask?.progress?.handoff?.message_id ?? null;
  const handoffKnown = handoffId === null || taskConversation.state.messages.some(m => m.message_id === handoffId);
  const revalidateConversation = taskConversation.onRevalidate;
  useEffect(() => {
    if (handoffId && !handoffKnown && taskConversation.state.status === 'ready') revalidateConversation();
  }, [handoffId, handoffKnown, runForTask?.checked_at, taskConversation.state.status, revalidateConversation]);
  // P055 (QA-RUN-REVIEW-NEXT-STATE): a new message in the open Task Conversation (e.g. the Architect's reply after a
  // Tool call such as rosc_review_submit) re-reads the Run once; a changed review then refreshes the next action.
  const lastMessageId = taskConversation.state.messages.length
    ? taskConversation.state.messages[taskConversation.state.messages.length - 1].message_id : null;
  const seenMessage = useRef<{ task: string | null; id: string | null }>({ task: null, id: null });
  const reloadRun = taskRun.onReload;
  useEffect(() => {
    const previous = seenMessage.current;
    seenMessage.current = { task: openTaskId, id: lastMessageId };
    if (previous.task === openTaskId && previous.id !== null && lastMessageId !== previous.id) reloadRun();
  }, [openTaskId, lastMessageId, reloadRun]);
  // P047: Project-level Worker controls (read on open; only explicit buttons can start anything).
  const projectWorkers = useTaskWorkers(api, projectId, null);
  const taskLayerOpen = (route.kind === 'task' || route.kind === 'new-task') && project !== null;
  const modalOpen = core.registrationOpen;
  const workspace = (kind: 'project' | 'task') => ({
    busy: core.busy,
    onReconcile: () => void (kind === 'project' ? actions.reconcileProject() : actions.reconcileTask()),
    onRepair: (c: { readonly confirmed: true }) => void (kind === 'project' ? actions.repairProject(c) : actions.repairTask(c)),
  });

  return <Shell onOpenSettings={actions.openSettings} settingsOpen={route.kind === 'settings'}
    engineNotices={core.engineNotices} onDismissEngineNotice={core.dismissEngineNotice}>
    <Dashboard
      projects={core.projects}
      projectsStatus={core.projectsStatus}
      feedback={core.feedbackFor('dashboard')}
      inert={route.kind !== 'dashboard' || modalOpen}
      onOpenProject={id => go({ kind: 'project', projectId: id })}
      onRegisterProject={actions.openRegistration}
      onRetry={() => void actions.retryProjects()}
    />
    {projectId && <Project
      projectId={projectId}
      project={project}
      projectStatus={core.projectsStatus}
      tasks={core.tasks}
      tasksStatus={core.tasksStatus}
      feedback={core.feedbackFor('project')}
      inert={taskLayerOpen || modalOpen}
      scroll={scroll}
      workspace={workspace('project')}
      conversation={core.conversation}
      assignments={core.assignments}
      workers={projectWorkers}
      textScale={projectTextScale}
      tab={core.projectTab}
      onSelectTab={tab => go({ kind: 'project', projectId, tab: tab === 'tasks' ? undefined : tab })}
      onClose={close}
      onRetry={() => void actions.retry()}
      onOpenTask={taskId => go({ kind: 'task', projectId, taskId })}
      onNewTask={() => go({ kind: 'new-task', projectId })}
    />}
    {route.kind === 'new-task' && project && <NewTask
      projectName={project.name}
      feedback={core.feedbackFor('task')}
      busy={core.busy}
      inert={modalOpen}
      onSubmit={values => void actions.createTask(values)}
      onClose={close}
    />}
    {route.kind === 'task' && project && <Task
      key={route.taskId}
      projectName={project.name}
      taskId={route.taskId}
      task={core.task}
      tasksStatus={core.tasksStatus}
      feedback={core.feedbackFor('task')}
      inert={modalOpen}
      scroll={scroll}
      workspace={workspace('task')}
      conversation={taskConversation}
      authoring={taskAuthoring}
      workers={taskWorkers}
      run={taskRun}
      textScale={taskTextScale}
      onClose={close}
      onRetry={() => void actions.retry()}
    />}
    {route.kind === 'settings' && <Settings
      state={core.registryControls.state}
      projects={core.projects}
      onRetry={core.registryControls.onRetry}
      onSaveAgent={core.registryControls.onSaveAgent}
      onOpenProjectSettings={id => go({ kind: 'project', projectId: id, tab: 'settings' })}
      onClose={close}
      diagnostics={core.diagnosticsControls}
    />}
    {modalOpen && <RegisterProject
      feedback={core.feedbackFor('modal')}
      busy={core.busy}
      onSubmit={values => void actions.registerProject(values)}
      onClose={actions.closeRegistration}
    />}
  </Shell>;
}
