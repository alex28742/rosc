import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { TEXT_SCALE_STEPS, type TextScale } from '../../../app/presentation/contracts';

type Props = {
  scopeLabel: string;
  scale: TextScale;
  onChange: (scale: TextScale) => void;
};

/**
 * Approved `Aa · Вид` control: five fixed steps for the current Work Area only. It is a small local
 * popover, not a settings surface, and it never writes to Core. The menu is portalled so an Engine
 * notice (higher layer) cannot cover it, matching the approved fixed quick-menu behaviour.
 */
export function TextViewControl({ scopeLabel, scale, onChange }: Props) {
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
      const width = popover.offsetWidth || 320;
      const height = popover.offsetHeight || 210;
      popover.style.left = `${Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8))}px`;
      popover.style.top = `${Math.max(8, Math.min(rect.bottom + 9, window.innerHeight - height - 8))}px`;
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
    document.addEventListener('mousedown', onPointer);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', place, true);
    };
  }, [open]);

  return <div className="rosc-view-control">
    <button ref={trigger} type="button" className="rosc-btn rosc-btn--small rosc-view-control__trigger"
      aria-haspopup="menu" aria-expanded={open} title="Размер текста текущей рабочей области"
      onClick={() => setOpen(current => !current)}>Aa · Вид</button>
    {open && createPortal(<div ref={menu} className="rosc-view-menu" role="menu"
      aria-label={`Размер текста: ${scopeLabel}`}>
      <div className="rosc-view-menu__head">
        <strong>{scopeLabel}</strong>
        <button type="button" className="rosc-view-menu__close" aria-label="Закрыть меню вида"
          onClick={() => setOpen(false)}>×</button>
      </div>
      <p className="rosc-view-menu__caption">Размер текста только для этой рабочей области</p>
      <div className="rosc-view-menu__sizes">
        {TEXT_SCALE_STEPS.map(value => <button key={value} type="button" role="menuitemradio" aria-checked={value === scale}
          className={value === scale ? 'rosc-view-menu__size rosc-view-menu__size--chosen' : 'rosc-view-menu__size'}
          onClick={() => onChange(value)}>{value}%</button>)}
      </div>
      <p className="rosc-view-menu__foot">Project и Task настраиваются независимо и сохраняются локально.</p>
    </div>, document.body)}
  </div>;
}
