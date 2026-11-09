import type { FormEvent } from 'react';
import type { NewTaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { PaperclipIcon } from '../ui/icons';
import { text } from '../ui/form';

export function NewTaskView({ projectName, feedback, busy, inert, onSubmit, onClose }: NewTaskViewProps) {
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    const form = new FormData(e.currentTarget);
    onSubmit({ title: text(form, 'title'), description: text(form, 'description'), acceptance_criteria: text(form, 'acceptance_criteria') });
  };
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">PROJECT / НОВАЯ ЗАДАЧА · DRAFT</p>
      <h2>Новая задача</h2>
      <p>{projectName} · создание Task не запускает Worker</p>
    </div>
    <div className="rosc-drawer__controls">
      <span className="rosc-chip rosc-new-task__draft-chip">Черновик · без Prompt / Run</span>
    </div>
  </>;
  const footer = <div className="rosc-new-task__footer">
    <button className="rosc-btn" type="button" onClick={onClose}>Отмена</button>
    <button className="rosc-btn rosc-btn--primary" type="submit" disabled={busy} aria-busy={busy}>
      {busy ? 'Создание Task Draft…' : 'Создать Task Draft'}
    </button>
  </div>;
  return <Drawer variant="task" label="Новая Task" closeLabel="Закрыть новую задачу" inert={inert} onClose={onClose} head={head} onSubmit={submit} footer={footer}>
    <div className="rosc-form rosc-new-task">
      <div className="rosc-new-task__intro">
        <h3>Постановка новой задачи</h3>
        <p>Owner и Architect пользуются одним Core-инструментом создания задачи.</p>
      </div>
      <Feedback {...feedback} />
      <div className="rosc-field">
        <label htmlFor="rosc-new-task-title">Название задачи *</label>
        <input id="rosc-new-task-title" name="title" required maxLength={160} placeholder="Например, Добавить проверку статусов API" />
      </div>
      <div className="rosc-new-task__field">
        <div className="rosc-field">
          <label htmlFor="rosc-new-task-description">Описание и контекст *</label>
          <textarea id="rosc-new-task-description" name="description" required rows={6} placeholder="Что требуется реализовать, ограничения и исходные решения" />
        </div>
        <div className="rosc-new-task__attach">
          <button type="button" className="rosc-btn rosc-btn--small" disabled
            title="Прикрепление файлов пока недоступно" aria-describedby="rosc-new-task-attach-hint">
            <PaperclipIcon size="sm" />Прикрепить файлы
          </button>
          <p className="rosc-new-task__hint" id="rosc-new-task-attach-hint">Прикрепление файлов пока недоступно: ROSC ещё не поддерживает вложения и не добавляет ссылки в описание задачи.</p>
        </div>
      </div>
      <div className="rosc-new-task__field">
        <div className="rosc-field">
          <label htmlFor="rosc-new-task-criteria">Критерии приёмки *</label>
          <textarea id="rosc-new-task-criteria" name="acceptance_criteria" required rows={5} placeholder="Как Worker, Architect и Owner проверят результат" />
        </div>
        <p className="rosc-new-task__hint">Критерии станут общим источником проверки для Worker, Architect и Owner. До утверждения Run их можно редактировать.</p>
      </div>
      <div className="rosc-new-task__note">Создание Task Draft не создаёт Prompt и не запускает агентскую сессию.</div>
    </div>
  </Drawer>;
}
