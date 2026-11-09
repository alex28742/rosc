import { useEffect, useRef, useState } from 'react';

type Props = {
  taskId: string;
  taskTitle: string;
  onOpenTask: (taskId: string) => void;
};

/**
 * One independent `☰` menu per Task row/card. Open is the only real action; archive and trash stay disabled
 * because Core has no such lifecycle operation. The trigger never bubbles into the Task item click.
 */
export function TaskItemMenu({ taskId, taskTitle, onOpenTask }: Props) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const menu = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const place = () => {
      const button = trigger.current;
      const popover = menu.current;
      if (!button || !popover) return;
      const rect = button.getBoundingClientRect();
      const width = popover.offsetWidth || 235;
      const height = popover.offsetHeight || 160;
      popover.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - width - 8))}px`;
      popover.style.top = `${Math.max(8, Math.min(rect.bottom + 6, window.innerHeight - height - 8))}px`;
    };
    place();
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false);
        trigger.current?.focus();
      }
    };
    // A fixed popover must not drift away from its row when the list or the window moves.
    const onMove = () => setOpen(false);
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    window.addEventListener('scroll', onMove, true);
    window.addEventListener('resize', onMove);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('scroll', onMove, true);
      window.removeEventListener('resize', onMove);
    };
  }, [open]);

  return <>
    <button ref={trigger} type="button" className="rosc-task-menu-button" aria-haspopup="menu"
      aria-expanded={open} aria-label={`Действия с задачей ${taskTitle}`} title="Действия с задачей"
      onClick={event => {
        event.stopPropagation();
        setOpen(current => !current);
      }}>☰</button>
    {open && <div ref={menu} className="rosc-task-menu" role="menu" aria-label={`Действия с задачей ${taskTitle}`}
      data-task-menu="true" onClick={event => event.stopPropagation()}>
      <button type="button" role="menuitem" className="rosc-task-menu__item"
        onClick={() => { setOpen(false); onOpenTask(taskId); }}>↗ Открыть задачу</button>
      <button type="button" role="menuitem" className="rosc-task-menu__item" disabled
        title="Архивация пока недоступна: Core не поддерживает">▣ Архивировать задачу</button>
      <button type="button" role="menuitem" className="rosc-task-menu__item rosc-task-menu__item--danger" disabled
        title="Корзина пока недоступна: Core не поддерживает">⌫ Переместить в корзину</button>
      <p className="rosc-task-menu__note">Архив и корзина появятся после поддержки в Core.</p>
    </div>}
  </>;
}
