import { useEffect, useMemo, useState } from 'react';
import type { ProjectViewProps } from '../../../app/presentation/contracts';
import type { Task } from '../../../core/models';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { ConversationPanel } from '../ui/ConversationPanel';
import { ProjectActionsMenu } from '../ui/ProjectActionsMenu';
import { ProjectAgentsCard } from '../ui/ProjectAgentsCard';
import { ProjectSettingsPanel } from '../ui/ProjectSettingsPanel';
import { TaskItemMenu } from '../ui/TaskItemMenu';
import { TextViewControl } from '../ui/TextViewControl';
import { architectHeadline } from '../ui/architectView';
import { ChevronRightIcon } from '../ui/icons';
import { useApplyTextScale } from '../ui/useApplyTextScale';

type WorkAreaProps = Omit<ProjectViewProps, 'project' | 'projectStatus' | 'onClose' | 'projectId' | 'inert' | 'textScale'> & {
  project: NonNullable<ProjectViewProps['project']>;
};

type TaskViewMode = 'list' | 'cards';

const PAGE_SIZE = 10;
const STATUS_ALL = 'all';

/** A truthful secondary line: the first non-empty description line, never invented progress. */
function secondaryLine(task: Task): string {
  const first = task.description.split('\n').map(line => line.trim()).find(Boolean);
  return first ?? 'Описание не указано';
}

function TaskRows({ tasks, viewMode, onOpenTask }: { tasks: Task[]; viewMode: TaskViewMode; onOpenTask: (taskId: string) => void }) {
  if (viewMode === 'cards') {
    return <div className="rosc-task-cards">
      {tasks.map(task => <div key={task.id} className="rosc-task-card" role="button" tabIndex={0}
        aria-label={`Открыть задачу: ${task.title}`}
        onClick={() => onOpenTask(task.id)}
        onKeyDown={event => {
          if (event.target !== event.currentTarget) return;
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            onOpenTask(task.id);
          }
        }}>
        <TaskItemMenu taskId={task.id} taskTitle={task.title} onOpenTask={onOpenTask} />
        <span className="rosc-tag">{task.status}</span>
        <strong className="rosc-task-title">{task.id} · {task.title}</strong>
        <span className="rosc-task-sub">{secondaryLine(task)}</span>
      </div>)}
    </div>;
  }
  return <div className="rosc-task-list">
    {tasks.map(task => <div key={task.id} className="rosc-task-item" role="button" tabIndex={0}
      aria-label={`Открыть задачу: ${task.title}`}
      onClick={() => onOpenTask(task.id)}
      onKeyDown={event => {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpenTask(task.id);
        }
      }}>
      <TaskItemMenu taskId={task.id} taskTitle={task.title} onOpenTask={onOpenTask} />
      <span className="rosc-task-item__main">
        <span className="rosc-task-title">{task.id} · {task.title}</span>
        <span className="rosc-task-sub">{secondaryLine(task)}</span>
      </span>
      <span className="rosc-tag">{task.status}</span>
      <span className="rosc-task-item__chevron" aria-hidden="true"><ChevronRightIcon size="sm" /></span>
    </div>)}
  </div>;
}

/**
 * Approved v0.6 Project Work Area composition: left overview + tabs + Tasks/Settings, right persistent Project
 * Conversation. The task header and footer are fixed; only the task list or Settings content scrolls. UI-local
 * chat state (draft, collapse, search), list filters and text scale are keyed by Project so switching Projects
 * cannot leak them and a nested Task keeps them intact.
 */
function ProjectWorkArea({ project, tasks, tasksStatus, feedback, scroll, workspace, conversation, assignments, tab,
  onSelectTab, onRetry, onOpenTask, onNewTask }: WorkAreaProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState(STATUS_ALL);
  const [viewMode, setViewMode] = useState<TaskViewMode>('list');
  const [page, setPage] = useState(1);

  const statuses = useMemo(() => [...new Set(tasks.map(task => task.status))].sort(), [tasks]);
  useEffect(() => {
    if (statusFilter !== STATUS_ALL && !statuses.includes(statusFilter)) {
      setStatusFilter(STATUS_ALL);
      setPage(1);
    }
  }, [statuses, statusFilter]);

  const filtered = useMemo(() => {
    const term = search.trim().toLocaleLowerCase('ru');
    return tasks.filter(task => {
      if (statusFilter !== STATUS_ALL && task.status !== statusFilter) return false;
      if (!term) return true;
      return [task.id, task.title, task.description].join(' ').toLocaleLowerCase('ru').includes(term);
    });
  }, [tasks, search, statusFilter]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(Math.max(page, 1), pageCount);
  const visible = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);

  return <div className={collapsed ? 'rosc-project-workspace rosc-project-workspace--collapsed' : 'rosc-project-workspace'}>
    <section className="rosc-project-left" aria-label="Обзор проекта и задачи">
      <Feedback {...feedback} />
      <div className="rosc-hero">
        <section className="rosc-panel-card">
          <h3>Контекст проекта</h3>
          <p>{project.description}</p>
          <dl className="rosc-facts">
            <dt>Документация</dt><dd>{project.docs_entrypoint ?? 'не указана'}</dd>
          </dl>
        </section>
        <ProjectAgentsCard project={project} conversation={conversation} workspace={workspace}
          onOpenSettings={() => onSelectTab('settings')} />
      </div>
      <div className="rosc-tabs-toolbar">
        <div className="rosc-tabs" role="tablist" aria-label="Разделы проекта">
          <button className={tab === 'tasks' ? 'rosc-tab rosc-tab--active' : 'rosc-tab'} role="tab"
            aria-selected={tab === 'tasks'} onClick={() => onSelectTab('tasks')}>Tasks ({tasks.length})</button>
          <button className="rosc-tab" role="tab" aria-selected="false" disabled>Activity</button>
          <button className={tab === 'settings' ? 'rosc-tab rosc-tab--active' : 'rosc-tab'} role="tab"
            aria-selected={tab === 'settings'} onClick={() => onSelectTab('settings')}>Настройки проекта</button>
        </div>
        <button className="rosc-btn rosc-btn--primary rosc-tabs-toolbar__new" onClick={onNewTask}>＋ Новая задача</button>
      </div>
      {tab === 'settings'
        ? <div className="rosc-project-settings" ref={scroll.region(`project:${project.id}:settings`)}>
          <ProjectSettingsPanel key={`${project.id}:settings`} projectId={project.id} controls={assignments} />
        </div>
        : <div className="rosc-task-panel">
          <div className="rosc-task-panel__head">
            <h3>Задачи проекта <span className="rosc-counter">{filtered.length} из {tasks.length}</span></h3>
            <div className="rosc-task-tools">
              <input type="search" aria-label="Поиск по задачам" placeholder="Найти задачу…" value={search}
                onChange={event => { setSearch(event.target.value); setPage(1); }} />
              <select aria-label="Раздел задач" defaultValue="active" title="Доступна только реальная коллекция текущих задач">
                <option value="active">Текущие</option>
                <option value="archived" disabled>Архив — недоступно</option>
                <option value="trashed" disabled>Корзина — недоступно</option>
              </select>
              <select aria-label="Фильтр задач" value={statusFilter}
                onChange={event => { setStatusFilter(event.target.value); setPage(1); }}>
                <option value={STATUS_ALL}>Все статусы</option>
                {statuses.map(status => <option key={status} value={status}>{status}</option>)}
              </select>
              <div className="rosc-task-view" role="group" aria-label="Вид списка задач">
                <button type="button" aria-pressed={viewMode === 'list'}
                  className={viewMode === 'list' ? 'rosc-task-view__btn rosc-task-view__btn--active' : 'rosc-task-view__btn'}
                  onClick={() => setViewMode('list')}>☰ Список</button>
                <button type="button" aria-pressed={viewMode === 'cards'}
                  className={viewMode === 'cards' ? 'rosc-task-view__btn rosc-task-view__btn--active' : 'rosc-task-view__btn'}
                  onClick={() => setViewMode('cards')}>▦ Карточки</button>
              </div>
            </div>
          </div>
          <div className="rosc-task-scroll" ref={scroll.region(`project:${project.id}:tasks`)}>
            {tasksStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить задачи.
              <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
              : tasksStatus === 'loading' ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
                : tasks.length === 0 ? <div className="rosc-state">Задач пока нет.</div>
                  : filtered.length === 0 ? <div className="rosc-state">
                    <p>По выбранным условиям задач нет.</p>
                    <button className="rosc-btn rosc-btn--small" onClick={() => { setSearch(''); setStatusFilter(STATUS_ALL); setPage(1); }}>Сбросить фильтры</button>
                  </div>
                    : <TaskRows tasks={visible} viewMode={viewMode} onOpenTask={onOpenTask} />}
          </div>
          <div className="rosc-task-panel__foot">
            <span className="rosc-muted">Рабочие задачи · статусы из Core</span>
            {pageCount > 1 && <nav className="rosc-pager" aria-label="Страницы задач">
              <button type="button" className="rosc-pager__btn" disabled={currentPage <= 1}
                onClick={() => setPage(currentPage - 1)}>‹ Назад</button>
              <span className="rosc-pager__label" aria-live="polite">Страница {currentPage} из {pageCount}</span>
              <button type="button" className="rosc-pager__btn" disabled={currentPage >= pageCount}
                onClick={() => setPage(currentPage + 1)}>Вперёд ›</button>
            </nav>}
          </div>
        </div>}
      <footer className="rosc-project-footer">
        <ProjectActionsMenu rootPath={project.root_path} onOpenSettings={() => onSelectTab('settings')} />
        <span className="rosc-project-footer__note">Открытие Project не запускает агентов</span>
      </footer>
    </section>
    <ConversationPanel key={project.id} projectId={project.id}
      conversation={conversation} collapsed={collapsed} onCollapsedChange={setCollapsed} />
  </div>;
}

export function ProjectView({ projectId, project, projectStatus, tasks, tasksStatus, feedback, inert, scroll, workspace,
  conversation, assignments, textScale, tab, onSelectTab, onClose, onRetry, onOpenTask, onNewTask }: ProjectViewProps) {
  const architect = conversation.architect;
  const scaleRef = useApplyTextScale(textScale.scale);
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">PROJECT / WORK AREA</p>
      <h2>{project?.name ?? projectId}</h2>
      <p>{project?.root_path ?? 'Проект не загружен'}</p>
    </div>
    <div className="rosc-drawer__controls">
      <span className="rosc-chip">Project Architect · {architectHeadline(architect.status, architect.info, architect.busy)}</span>
      {project && <span className="rosc-chip">
        <span className="rosc-chip__dot" aria-hidden="true" />
        Worker: {project.active_workers > 0 ? `${project.active_workers} в работе` : 'не активен'}
      </span>}
      {project && <span className={project.workspace_state === 'ready' ? 'rosc-tag rosc-tag--success' : 'rosc-tag rosc-tag--warning'}>
        {project.workspace_state === 'ready' ? 'Workspace готов' : 'Нужна сверка'}
      </span>}
      <TextViewControl scopeLabel="Project Work Area" scale={textScale.scale} onChange={textScale.onChange} />
    </div>
  </>;
  return <Drawer variant="project" label="Project Work Area" closeLabel="Закрыть проект" inert={inert} onClose={onClose}
    head={head} fill rootRef={scaleRef}>
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
