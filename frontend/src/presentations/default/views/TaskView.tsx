import { useState } from 'react';
import type { TaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { TextViewControl } from '../ui/TextViewControl';
import { TaskConversationPanel } from '../ui/TaskConversationPanel';
import { WorkspaceActions } from '../ui/WorkspaceActions';
import { useApplyTextScale } from '../ui/useApplyTextScale';

const LATER = 'Будет доступно позже';

/** A visible control for a capability Core does not have yet: natively disabled, no handler, nothing simulated. */
function Later({ children, label }: { children: string; label?: string }) {
  return <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER} aria-label={label ?? `${children} — ${LATER.toLowerCase()}`}>{children}</button>;
}

/**
 * Task Work Area. The structure follows the approved prototype (Blocker slot, Task heading, Specification,
 * Working Plan | Context & Actions, full-height Task Conversation, full-width head/footer). Core data fills the
 * regions it has; the rest are honest empty states with disabled controls, never simulated state.
 */
export function TaskView({ projectName, taskId, task, tasksStatus, feedback, inert, scroll, workspace, conversation, textScale, onClose, onRetry }: TaskViewProps) {
  const scaleRef = useApplyTextScale(textScale.scale);
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [specCollapsed, setSpecCollapsed] = useState(false);
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">TASK / WORK AREA</p>
      <h2>{task ? `${task.id} · ${task.title}` : taskId}</h2>
      <p>{projectName} · чтение Task не запускает агента</p>
    </div>
    <div className="rosc-drawer__controls">
      {task && <span className="rosc-tag rosc-tag--info">{task.status}</span>}
      <span className="rosc-chip">Агенты не запущены</span>
      <TextViewControl scopeLabel="Task Work Area" scale={textScale.scale} onChange={textScale.onChange} />
    </div>
  </>;
  const footer = <>
    <span className="rosc-task-foot__note">Task Draft сохранена · Prompt, Run и приёмка появятся в следующих срезах</span>
    <span className="rosc-task-foot__actions">
      <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER}>Принять задачу</button>
      <button type="button" className="rosc-btn rosc-btn--small" onClick={onClose}>К проекту</button>
    </span>
  </>;
  const state = (task === null)
    ? (tasksStatus === 'loading'
      ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
      : tasksStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить задачу. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
        : <div className="rosc-state"><p>Задача не найдена в этом проекте.</p></div>)
    : null;
  return <Drawer variant="task" label="Task Work Area" closeLabel="Закрыть задачу" inert={inert} onClose={onClose}
    head={head} rootRef={scaleRef} fill footer={task ? footer : undefined}>
    {!task ? <div className="rosc-task-state"><Feedback {...feedback} />{state}</div>
      : <div className={chatCollapsed ? 'rosc-task-workspace rosc-task-workspace--collapsed' : 'rosc-task-workspace'}>
        <div className="rosc-task-left" ref={scroll.region(`task:${taskId}`)}>
          <Feedback {...feedback} />
          <section className="rosc-task-notice" aria-label="Блокеры">
            <span className="rosc-task-notice__mark" aria-hidden="true">!</span>
            <div>
              <strong>Активных блокеров нет</strong>
              <p>Данные блокеров пока не подключены к Core; здесь появятся запросы к Owner.</p>
            </div>
            <Later>Ответить на блокер</Later>
          </section>
          <div className="rosc-task-heading">
            <div>
              <h3>{task.title}</h3>
              <p>Task {task.id} · Spec Draft · Run не создан · Исполнитель: не назначен</p>
            </div>
            <div className="rosc-task-heading__flags">
              <span className="rosc-chip">Проект: {projectName}</span>
              <span className="rosc-tag rosc-tag--info">Task: {task.status}</span>
            </div>
          </div>
          <section className={specCollapsed ? 'rosc-task-panel rosc-task-spec rosc-task-spec--collapsed' : 'rosc-task-panel rosc-task-spec'} aria-label="Спецификация задачи">
            <div className="rosc-task-panel__top"><h3>Спецификация задачи</h3><span className="rosc-chip">Draft</span></div>
            {!specCollapsed && <div className="rosc-task-spec__body">
              <div><h4>Описание задачи</h4><p className="rosc-task-text">{task.description}</p></div>
              <div><h4>Критерии приёмки</h4><p className="rosc-task-text">{task.acceptance_criteria}</p></div>
            </div>}
            <div className="rosc-task-panel__foot">
              <span>Редактирование Spec и версии появятся позже.</span>
              <span className="rosc-task-panel__foot-actions">
                <Later>Сохранить Spec</Later>
                <button type="button" className="rosc-task-link" aria-expanded={!specCollapsed}
                  onClick={() => setSpecCollapsed(value => !value)}>{specCollapsed ? 'Развернуть спецификацию ⌄' : 'Свернуть спецификацию ⌃'}</button>
              </span>
            </div>
          </section>
          <div className="rosc-task-lower">
            <section className="rosc-task-panel rosc-task-plan" aria-label="Рабочий план Worker">
              <div className="rosc-task-panel__top"><h3>Рабочий план Worker</h3><span className="rosc-chip">Revision —</span></div>
              <div className="rosc-task-plan__progress" aria-label="Прогресс плана недоступен">
                <div className="rosc-task-plan__label"><strong>Прогресс не определён</strong><span>—</span></div>
                <div className="rosc-task-plan__track" aria-hidden="true" />
              </div>
              <div className="rosc-task-panel__scroll">
                <div className="rosc-state"><p>Рабочий план пока недоступен.</p><p>Шаги появятся, когда Worker подготовит план для этой Task.</p></div>
              </div>
              <div className="rosc-task-panel__foot">
                <span>Ревизии плана будут доступны позже.</span>
                <Later>История ревизий</Later>
              </div>
            </section>
            <section className="rosc-task-panel rosc-task-context" aria-label="Контекст и действия">
              <div className="rosc-task-panel__top"><h3>Контекст и действия</h3><span className="rosc-chip">Следующий ход пока не определён</span></div>
              <div className="rosc-task-panel__scroll">
                <dl className="rosc-task-facts">
                  <div><dt>Проект</dt><dd>{projectName}</dd></div>
                  <div><dt>Task</dt><dd>{task.status}</dd></div>
                  <div><dt>Workspace</dt><dd>{task.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</dd></div>
                  <div><dt>Worker</dt><dd>не назначен</dd></div>
                  <div><dt>Blocker</dt><dd>нет данных</dd></div>
                  <div><dt>Run / Checkpoint</dt><dd>не создан</dd></div>
                </dl>
                <WorkspaceActions key={`${task.id}:${task.workspace_problem}`} state={task.workspace_state} problem={task.workspace_problem} controls={workspace} />
                <div className="rosc-task-agents">
                  <div>
                    <div className="rosc-task-agents__head"><strong>Project Architect</strong><span className="rosc-chip">Не активен</span></div>
                    <p>Архитектор проекта не запускается из Task Work Area.</p>
                    <Later>Architect в Task</Later>
                  </div>
                  <div>
                    <div className="rosc-task-agents__head"><strong>Worker этой Task</strong><span className="rosc-chip">Не запущен</span></div>
                    <p>Назначение Worker и Run ещё не подключены.</p>
                    <Later>Выбрать Worker</Later>
                  </div>
                </div>
                <div className="rosc-task-actions">
                  <h4>Следующие действия</h4>
                  <p className="rosc-hint">Подготовка Prompt и разрешение Run появятся в следующих срезах.</p>
                  <div className="rosc-task-actions__row">
                    <Later>Prompt Draft</Later>
                    <Later>Разрешить запуск</Later>
                    <Later>Журнал выполнения</Later>
                  </div>
                </div>
              </div>
              <div className="rosc-task-panel__foot"><span>Действия включатся, когда Core получит соответствующий API.</span></div>
            </section>
          </div>
        </div>
        <TaskConversationPanel key={`${task.project_id}/${task.id}`} taskId={task.id} conversation={conversation}
          collapsed={chatCollapsed} onCollapsedChange={setChatCollapsed} />
      </div>}
  </Drawer>;
}
