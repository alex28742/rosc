import type { ReactNode } from 'react';
import { CloseIcon } from './icons';

type Props = {
  variant: 'project' | 'task';
  label: string;
  closeLabel: string;
  inert: boolean;
  onClose: () => void;
  head: ReactNode;
  children: ReactNode;
  /** Sticky actions at the bottom of the layer; kept outside the scroll region. */
  footer?: ReactNode;
  /** When set, the layer body is a `<form>` (Task Draft) instead of a plain scroll region. */
  onSubmit?: (event: React.FormEvent<HTMLFormElement>) => void;
  bodyRef?: (element: HTMLElement | null) => void;
};

/**
 * Backdrop + right-hand drawer + left-edge close control shared by the Project and Task layers.
 * The scroll region is tagged by the caller so the app keeps its position across close/reopen.
 */
export function Drawer({ variant, label, closeLabel, inert, onClose, head, children, footer, onSubmit, bodyRef }: Props) {
  const scroll = <div className="rosc-drawer__scroll" ref={bodyRef}>{children}</div>;
  const body = onSubmit
    ? <form className="rosc-drawer__form" onSubmit={onSubmit}>{scroll}{footer && <div className="rosc-drawer__footer">{footer}</div>}</form>
    : <>{scroll}{footer && <div className="rosc-drawer__footer">{footer}</div>}</>;
  return <div className={variant === 'task' ? 'rosc-backdrop--nested' : 'rosc-backdrop'} onClick={onClose} inert={inert}>
    <section className={`rosc-drawer rosc-drawer--${variant} ${variant}-drawer`} onClick={e => e.stopPropagation()} aria-label={label}>
      <button className="rosc-edge-close" onClick={onClose} aria-label={closeLabel}><CloseIcon /></button>
      <span className="rosc-edge-caption" aria-hidden="true">{variant === 'task' ? 'TASK' : 'PROJECT'}</span>
      <header className="rosc-drawer__head">{head}</header>
      {body}
    </section>
  </div>;
}
