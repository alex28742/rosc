import type { TaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { WorkspaceActions } from '../ui/WorkspaceActions';

export function TaskView({ projectName, taskId, task, tasksStatus, feedback, inert, scroll, workspace, onClose, onRetry }: TaskViewProps) {
  const head = <>
    <div><p className="eyebrow">TASK / WORK AREA</p><h2>{task ? `${task.id} · ${task.title}` : taskId}</h2><p>{projectName} · чтение Task не запускает агента</p></div>
    {task && <span className="pill">{task.status}</span>}
  </>;
  return <Drawer variant="task" label="Task Work Area" closeLabel="Закрыть задачу" inert={inert} onClose={onClose} head={head} bodyRef={scroll.region(`task:${taskId}`)}>
    <Feedback {...feedback} />
    {!task && (tasksStatus === 'loading' ? <p>Загрузка…</p> : tasksStatus === 'failed' ? <p>Не удалось загрузить задачу. <button onClick={onRetry}>Повторить</button></p> : <p>Задача не найдена в этом проекте.</p>)}
    {task && <>
      <div className="project-summary"><h3>Спецификация задачи · Draft</h3><h4>Описание</h4><p>{task.description}</p><h4>Критерии приёмки</h4><p>{task.acceptance_criteria}</p></div>
      <div className="project-summary">
        <h3>Контекст и действия</h3>
        <p>Task Draft сохранена. Architect и Worker не назначены и не запущены.</p>
        <p>Workspace: {task.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</p>
        <WorkspaceActions key={`${task.id}:${task.workspace_problem}`} state={task.workspace_state} problem={task.workspace_problem} controls={workspace} />
        <p>Подготовка Prompt и разрешение Run появятся в следующих срезах.</p>
      </div>
    </>}
  </Drawer>;
}
