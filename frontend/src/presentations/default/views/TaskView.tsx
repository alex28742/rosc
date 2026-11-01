import type { TaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { WorkspaceActions } from '../ui/WorkspaceActions';

export function TaskView({ projectName, taskId, task, tasksStatus, feedback, inert, scroll, workspace, onClose, onRetry }: TaskViewProps) {
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">TASK / WORK AREA</p>
      <h2>{task ? `${task.id} · ${task.title}` : taskId}</h2>
      <p>{projectName} · чтение Task не запускает агента</p>
    </div>
    <div className="rosc-drawer__controls">
      {task && <span className="rosc-tag rosc-tag--info">{task.status}</span>}
      <span className="rosc-chip">Агенты не запущены</span>
    </div>
  </>;
  return <Drawer variant="task" label="Task Work Area" closeLabel="Закрыть задачу" inert={inert} onClose={onClose} head={head} bodyRef={scroll.region(`task:${taskId}`)}>
    <Feedback {...feedback} />
    {!task && (tasksStatus === 'loading'
      ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
      : tasksStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить задачу. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
        : <div className="rosc-state"><p>Задача не найдена в этом проекте.</p></div>)}
    {task && <div className="rosc-hero">
      <section className="rosc-panel-card">
        <h3>Спецификация задачи · Draft</h3>
        <dl className="rosc-spec">
          <dt>Описание</dt><dd>{task.description}</dd>
          <dt>Критерии приёмки</dt><dd>{task.acceptance_criteria}</dd>
        </dl>
      </section>
      <section className="rosc-panel-card">
        <h3>Контекст и действия</h3>
        <p>Task Draft сохранена. Architect и Worker не назначены и не запущены.</p>
        <dl className="rosc-facts">
          <dt>Workspace</dt><dd>{task.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</dd>
          <dt>Worker</dt><dd>не назначен</dd>
        </dl>
        <WorkspaceActions key={`${task.id}:${task.workspace_problem}`} state={task.workspace_state} problem={task.workspace_problem} controls={workspace} />
        <p className="rosc-hint">Подготовка Prompt и разрешение Run появятся в следующих срезах.</p>
      </section>
    </div>}
  </Drawer>;
}
