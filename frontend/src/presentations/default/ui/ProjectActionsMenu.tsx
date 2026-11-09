import { useEffect, useRef, useState } from 'react';

type Props = {
  rootPath: string;
  onOpenSettings: () => void;
};

/**
 * Approved bottom `⋯ Действия проекта` menu. It shows the real root path as a fact, links to the existing
 * Settings tab, and keeps project archive visibly disabled because Core has no lifecycle API for it.
 */
export function ProjectActionsMenu({ rootPath, onOpenSettings }: Props) {
  const [open, setOpen] = useState(false);
  const wrapper = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!wrapper.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return <div className="rosc-project-actions" ref={wrapper}>
    <button type="button" className="rosc-btn rosc-btn--small" aria-haspopup="menu" aria-expanded={open}
      onClick={() => setOpen(current => !current)}>⋯ Действия проекта</button>
    {open && <div className="rosc-project-actions__menu" role="menu" aria-label="Действия проекта">
      <button type="button" role="menuitem" disabled
        title="Архивация проекта пока недоступна: Core API отсутствует">▣ Архивировать проект</button>
      <div className="rosc-project-actions__path">
        <span>Путь к каталогу проекта</span>
        <code>{rootPath}</code>
      </div>
      <button type="button" role="menuitem"
        onClick={() => { setOpen(false); onOpenSettings(); }}>Настройки проекта</button>
      <p className="rosc-project-actions__note">Архивация станет доступна после поддержки в Core.</p>
    </div>}
  </div>;
}
