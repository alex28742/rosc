import type { FormEvent } from 'react';
import type { RegisterProjectViewProps } from '../../../app/presentation/contracts';
import { Feedback } from '../ui/Feedback';
import { text } from '../ui/form';

export function RegisterProjectView({ feedback, busy, onSubmit, onClose }: RegisterProjectViewProps) {
  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    onSubmit({
      name: text(form, 'name'), description: text(form, 'description'), root_path: text(form, 'root_path'), mode: text(form, 'mode'),
      docs_entrypoint: text(form, 'docs_entrypoint'), no_external_agent_confirmed: form.get('no_external_agent_confirmed') === 'on',
    });
  };
  return <div className="modal-back" onClick={onClose}>
    <form className="modal" onSubmit={submit} onClick={e => e.stopPropagation()}>
      <div className="modal-title"><h2>Добавить проект</h2><button type="button" onClick={onClose} aria-label="Закрыть">×</button></div>
      <Feedback {...feedback} />
      <label>Режим<select name="mode" defaultValue="existing"><option value="existing">Подключить существующий</option><option value="new">Зарегистрировать новый пустой каталог</option></select></label>
      <label>Название<input name="name" required maxLength={160} /></label>
      <label>Описание<textarea name="description" required rows={2} /></label>
      <label>Корневая директория<input name="root_path" required placeholder="/home/user/projects/example" /></label>
      <label>Файл документации, необязательно<input name="docs_entrypoint" placeholder="README.md" /></label>
      <label className="check"><input type="checkbox" name="no_external_agent_confirmed" /> В этом checkout сейчас не работает внешний агент (для существующего проекта)</label>
      <p className="subtle">Каталог уже должен существовать. ROSC создаст свои файлы отдельно от исходников проекта.</p>
      <button className="primary" type="submit" disabled={busy} aria-disabled={busy}>Сохранить проект</button>
    </form>
  </div>;
}
