import type { ProjectViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { WorkspaceActions } from '../ui/WorkspaceActions';
import { ChevronRightIcon, DocumentIcon } from '../ui/icons';

export function ProjectView({ projectId, project, projectStatus, tasks, tasksStatus, feedback, inert, scroll, workspace, onClose, onRetry, onOpenTask, onNewTask }: ProjectViewProps) {
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">PROJECT / WORK AREA</p>
      <h2>{project?.name ?? projectId}</h2>
      <p>{project?.root_path ?? 'Проект не загружен'}</p>
    </div>
    <div className="rosc-drawer__controls">
      <span className="rosc-chip">Агенты не запущены</span>
      {project && <span className={project.workspace_state === 'ready' ? 'rosc-tag rosc-tag--success' : 'rosc-tag rosc-tag--warning'}>
        {project.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка'}
      </span>}
    </div>
  </>;
  return <Drawer variant="project" label="Project Work Area" closeLabel="Закрыть проект" inert={inert} onClose={onClose} head={head} bodyRef={scroll.region(`project:${projectId}`)}>
    <Feedback {...feedback} />
    {!project && (projectStatus === 'loading'
      ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
      : projectStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить проект. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
        : <div className="rosc-state"><p>Проект не найден. Закройте окно и выберите проект на дашборде.</p></div>)}
    {project && <>
      <div className="rosc-hero">
        <section className="rosc-panel-card">
          <h3>Контекст проекта</h3>
          <p>{project.description}</p>
          <dl className="rosc-facts">
            <dt>Architect</dt><dd>не активен</dd>
            <dt>Workers</dt><dd>{project.active_workers}</dd>
            <dt>Workspace</dt><dd>{project.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</dd>
            <dt>Документация</dt><dd>{project.docs_entrypoint ?? 'не указана'}</dd>
          </dl>
        </section>
        <section className="rosc-panel-card">
          <h3>Состояние Workspace</h3>
          <WorkspaceActions key={`${project.id}:${project.workspace_problem}`} state={project.workspace_state} problem={project.workspace_problem} controls={workspace} />
        </section>
      </div>
      <div className="rosc-tabs" role="tablist" aria-label="Разделы проекта">
        <button className="rosc-tab rosc-tab--active" role="tab" aria-selected="true">Tasks ({tasks.length})</button>
        <button className="rosc-tab" role="tab" aria-selected="false" disabled>Activity</button>
        <button className="rosc-tab" role="tab" aria-selected="false" disabled>Настройки проекта</button>
      </div>
      <div className="rosc-work-toolbar">
        <div className="rosc-work-toolbar__title"><h3>Задачи проекта</h3><span className="rosc-counter">{tasks.length}</span></div>
        <button className="rosc-btn rosc-btn--primary" onClick={onNewTask}>＋ Новая задача</button>
      </div>
      <div className="rosc-task-list">
        {tasksStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить задачи. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
          : tasks.length === 0 ? <div className="rosc-state">{tasksStatus === 'loading' ? 'Загрузка…' : 'Задач пока нет.'}</div>
            : tasks.map(t => <button className="rosc-task-item" key={t.id} onClick={() => onOpenTask(t.id)}>
              <span className="rosc-task-item__glyph" aria-hidden="true"><DocumentIcon size="sm" /></span>
              <span className="rosc-task-item__main"><span className="rosc-task-title">{t.id} · {t.title}</span></span>
              <span className="rosc-tag">{t.status}</span>
              <span className="rosc-task-item__chevron" aria-hidden="true"><ChevronRightIcon size="sm" /></span>
            </button>)}
      </div>
    </>}
  </Drawer>;
}
