import { useEffect } from 'react';
import { coreApi, type CoreApi } from '../core/api';
import { useNavigation } from './navigation';
import { resolvePresentation } from './presentation/registry';
import { useScrollRegions } from './scroll';
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
      textScale={projectTextScale}
      tab={core.projectTab}
      onSelectTab={tab => go({ kind: 'project', projectId, tab: tab === 'settings' ? 'settings' : undefined })}
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
      projectName={project.name}
      taskId={route.taskId}
      task={core.task}
      tasksStatus={core.tasksStatus}
      feedback={core.feedbackFor('task')}
      inert={modalOpen}
      scroll={scroll}
      workspace={workspace('task')}
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
    />}
    {modalOpen && <RegisterProject
      feedback={core.feedbackFor('modal')}
      busy={core.busy}
      onSubmit={values => void actions.registerProject(values)}
      onClose={actions.closeRegistration}
    />}
  </Shell>;
}
