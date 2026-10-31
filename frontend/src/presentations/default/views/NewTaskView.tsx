import type { FormEvent } from 'react';
import type { NewTaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { text } from '../ui/form';

export function NewTaskView({ projectName, feedback, busy, inert, onSubmit, onClose }: NewTaskViewProps) {
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    onSubmit({ title: text(form, 'title'), description: text(form, 'description'), acceptance_criteria: text(form, 'acceptance_criteria') });
  };
  const head = <div><p className="eyebrow">TASK / DRAFT</p><h2>Новая задача</h2><p>{projectName} · создание не запускает Worker</p></div>;
  return <Drawer variant="task" label="Новая Task" closeLabel="Закрыть новую задачу" inert={inert} onClose={onClose} head={head} onSubmit={submit}>
    <Feedback {...feedback} />
    <label>Название<input name="title" required maxLength={160} /></label>
    <label>Описание и контекст<textarea name="description" required rows={6} /></label>
    <label>Критерии приёмки<textarea name="acceptance_criteria" required rows={5} /></label>
    <p className="subtle">Draft сохраняется в Core. Prompt и Run ещё не создаются.</p>
    <button className="primary" type="submit" aria-disabled={busy}>Создать Task Draft</button>
  </Drawer>;
}
