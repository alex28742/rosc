import type { ProjectViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { WorkspaceActions } from '../ui/WorkspaceActions';

export function ProjectView({ projectId, project, projectStatus, tasks, tasksStatus, feedback, inert, scroll, workspace, onClose, onRetry, onOpenTask, onNewTask }: ProjectViewProps) {
  const head = <>
    <div><p className="eyebrow">PROJECT / WORK AREA</p><h2>{project?.name ?? projectId}</h2><p>{project?.root_path}</p></div>
    <span className="pill">Агенты не запущены</span>
  </>;
  return <Drawer variant="project" label="Project Work Area" closeLabel="Закрыть проект" inert={inert} onClose={onClose} head={head} bodyRef={scroll.region(`project:${projectId}`)}>
    <Feedback {...feedback} />
    {!project && (projectStatus === 'loading' ? <p>Загрузка…</p> : projectStatus === 'failed' ? <p>Не удалось загрузить проект. <button onClick={onRetry}>Повторить</button></p> : <p>Проект не найден. Закройте окно и выберите проект на дашборде.</p>)}
    {project && <>
      <div className="project-summary">
        <h3>Контекст проекта</h3><p>{project.description}</p>
        <div><span>Architect: не активен</span><span>Workers: 0</span><span>Workspace: {project.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</span></div>
        <WorkspaceActions key={`${project.id}:${project.workspace_problem}`} state={project.workspace_state} problem={project.workspace_problem} controls={workspace} />
      </div>
      <div className="tabs"><button className="active">Tasks ({tasks.length})</button><button disabled>Activity</button><button disabled>Настройки проекта</button></div>
      <div className="task-toolbar"><h3>Задачи проекта</h3><button className="primary" onClick={onNewTask}>＋ Новая задача</button></div>
      <div className="task-list">
        {tasksStatus === 'failed' ? <p>Не удалось загрузить задачи. <button onClick={onRetry}>Повторить</button></p>
          : tasks.length === 0 ? <p>{tasksStatus === 'loading' ? 'Загрузка…' : 'Задач пока нет.'}</p>
            : tasks.map(t => <button key={t.id} onClick={() => onOpenTask(t.id)}><span>{t.id} · {t.title}</span><span className="pill">{t.status}</span></button>)}
      </div>
    </>}
  </Drawer>;
}
