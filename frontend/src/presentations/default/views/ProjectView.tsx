import { useState } from 'react';
import type { ProjectViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { WorkspaceActions } from '../ui/WorkspaceActions';
import { ConversationPanel } from '../ui/ConversationPanel';
import { ProjectSettingsPanel } from '../ui/ProjectSettingsPanel';
import { ChevronRightIcon, DocumentIcon } from '../ui/icons';

function architectState({ architect }: ProjectViewProps['conversation']): string {
  if (architect.busy) return 'запускается';
  if (architect.status !== 'ready' || architect.info === null) return 'проверяется';
  switch (architect.info.state) {
    case 'ready': return 'активен (проверен)';
    case 'starting': return 'запускается';
    case 'error': return 'требует внимания';
    case 'not_configured': return 'не настроен';
    default: return 'не активирован';
  }
}

type WorkAreaProps = Omit<ProjectViewProps, 'project' | 'projectStatus' | 'onClose' | 'projectId' | 'inert'> & {
  project: NonNullable<ProjectViewProps['project']>;
};

/**
 * Approved v0.6 Project Work Area composition: left overview + tabs + Tasks/Settings, right persistent Project
 * Conversation. Both columns scroll independently; the conversation is not a left-side tab. UI-local chat state
 * (draft, collapse, search) lives here and is keyed by Project, so switching Projects cannot leak it and a nested
 * Task keeps it intact.
 */
function ProjectWorkArea({ project, tasks, tasksStatus, feedback, scroll, workspace, conversation, assignments, tab,
  onSelectTab, onRetry, onOpenTask, onNewTask }: WorkAreaProps) {
  const [collapsed, setCollapsed] = useState(false);
  const architect = architectState(conversation);
  return <div className={collapsed ? 'rosc-project-workspace rosc-project-workspace--collapsed' : 'rosc-project-workspace'}>
    <section className="rosc-project-left" aria-label="Обзор проекта и задачи" ref={scroll.region(`project:${project.id}`)}>
      <Feedback {...feedback} />
      <div className="rosc-hero">
        <section className="rosc-panel-card">
          <h3>Контекст проекта</h3>
          <p>{project.description}</p>
          <dl className="rosc-facts">
            <dt>Architect</dt><dd>{architect}</dd>
            <dt>Workers</dt><dd>{project.active_workers}</dd>
            <dt>Workspace</dt><dd>{project.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</dd>
            <dt>Документация</dt><dd>{project.docs_entrypoint ?? 'не указана'}</dd>
          </dl>
        </section>
        <section className="rosc-panel-card">
          <h3>Состояние Workspace</h3>
          {/* Read-only Core fact: a healthy Workspace has no action, only the state it reports. */}
          {project.workspace_state === 'ready'
            ? <div className="rosc-repair"><p>Манифест Workspace соответствует записи в базе.</p></div>
            : <WorkspaceActions key={`${project.id}:${project.workspace_problem}`} state={project.workspace_state} problem={project.workspace_problem} controls={workspace} />}
        </section>
      </div>
      <div className="rosc-tabs" role="tablist" aria-label="Разделы проекта">
        <button className={tab === 'tasks' ? 'rosc-tab rosc-tab--active' : 'rosc-tab'} role="tab"
          aria-selected={tab === 'tasks'} onClick={() => onSelectTab('tasks')}>Tasks ({tasks.length})</button>
        <button className="rosc-tab" role="tab" aria-selected="false" disabled>Activity</button>
        <button className={tab === 'settings' ? 'rosc-tab rosc-tab--active' : 'rosc-tab'} role="tab"
          aria-selected={tab === 'settings'} onClick={() => onSelectTab('settings')}>Настройки проекта</button>
      </div>
      {tab === 'settings'
        ? <ProjectSettingsPanel key={`${project.id}:settings`} projectId={project.id} controls={assignments} />
        : <>
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
    </section>
    <ConversationPanel key={project.id} projectId={project.id}
      conversation={conversation} collapsed={collapsed} onCollapsedChange={setCollapsed} />
  </div>;
}

export function ProjectView({ projectId, project, projectStatus, tasks, tasksStatus, feedback, inert, scroll, workspace,
  conversation, assignments, tab, onSelectTab, onClose, onRetry, onOpenTask, onNewTask }: ProjectViewProps) {
  const architect = architectState(conversation);
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">PROJECT / WORK AREA</p>
      <h2>{project?.name ?? projectId}</h2>
      <p>{project?.root_path ?? 'Проект не загружен'}</p>
    </div>
    <div className="rosc-drawer__controls">
      <span className="rosc-chip">Project Architect · {architect}</span>
      {project && <span className={project.workspace_state === 'ready' ? 'rosc-tag rosc-tag--success' : 'rosc-tag rosc-tag--warning'}>
        {project.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка'}
      </span>}
    </div>
  </>;
  return <Drawer variant="project" label="Project Work Area" closeLabel="Закрыть проект" inert={inert} onClose={onClose} head={head} fill>
    {!project ? <div className="rosc-drawer__scroll" ref={scroll.region(`project:${projectId}`)}>
      <Feedback {...feedback} />
      {projectStatus === 'loading'
        ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
        : projectStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить проект. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
          : <div className="rosc-state"><p>Проект не найден. Закройте окно и выберите проект на дашборде.</p></div>}
    </div> : <ProjectWorkArea key={project.id} project={project} tasks={tasks} tasksStatus={tasksStatus}
      feedback={feedback} scroll={scroll} workspace={workspace} conversation={conversation} assignments={assignments}
      tab={tab} onSelectTab={onSelectTab} onRetry={onRetry} onOpenTask={onOpenTask} onNewTask={onNewTask} />}
  </Drawer>;
}
