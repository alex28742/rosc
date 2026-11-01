import type { FormEvent } from 'react';
import type { RegisterProjectViewProps } from '../../../app/presentation/contracts';
import { Feedback } from '../ui/Feedback';
import { text } from '../ui/form';
import { CloseIcon } from '../ui/icons';

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
    <form className="modal rosc-dialog" onSubmit={submit} onClick={e => e.stopPropagation()}>
      <div className="modal-title rosc-dialog__head">
        <div>
          <h2>Добавить проект</h2>
          <p>Проект регистрируется в Core без запуска Architect или Worker.</p>
        </div>
        <button type="button" className="rosc-dialog__close" onClick={onClose} aria-label="Закрыть"><CloseIcon /></button>
      </div>
      <div className="rosc-dialog__body">
        <Feedback {...feedback} />
        <fieldset className="rosc-mode-switch">
          <legend>Режим</legend>
          <label className="rosc-mode-switch__option"><input type="radio" name="mode" value="existing" defaultChecked /><span>Подключить существующий</span></label>
          <label className="rosc-mode-switch__option"><input type="radio" name="mode" value="new" /><span>Зарегистрировать новый пустой каталог</span></label>
        </fieldset>
        <div className="rosc-form-grid">
          <label className="rosc-field"><span>Название</span><input name="name" required maxLength={160} /></label>
          <label className="rosc-field"><span>Корневая директория</span><input name="root_path" required placeholder="/home/user/projects/example" /></label>
          <label className="rosc-field rosc-form-grid__wide"><span>Описание</span><textarea name="description" required rows={3} /></label>
          <label className="rosc-field"><span>Файл документации, необязательно</span><input name="docs_entrypoint" placeholder="README.md" /></label>
          <label className="rosc-check rosc-form-grid__wide"><input type="checkbox" name="no_external_agent_confirmed" /> <span>В этом checkout сейчас не работает внешний агент (для существующего проекта)</span></label>
        </div>
        <p className="rosc-create-note">Каталог уже должен существовать. ROSC создаст свои файлы отдельно от исходников проекта.</p>
      </div>
      <div className="rosc-dialog__footer">
        <button className="rosc-btn rosc-btn--primary" type="submit" disabled={busy} aria-disabled={busy}>Сохранить проект</button>
      </div>
    </form>
  </div>;
}
