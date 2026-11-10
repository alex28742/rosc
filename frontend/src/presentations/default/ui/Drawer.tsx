import type { ReactNode, Ref } from 'react';
import { CloseIcon } from './icons';

type Props = {
  variant: 'project' | 'task' | 'new-task' | 'settings';
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
  /** The layer section itself; a Work Area uses it to scope its local text scale. */
  rootRef?: Ref<HTMLElement>;
  /** The body manages its own scrolling regions (two-column Project Work Area) instead of one padded scroll box. */
  fill?: boolean;
};

/**
 * Backdrop + right-hand drawer + left-edge close control shared by the Project and Task layers.
 * The scroll region is tagged by the caller so the app keeps its position across close/reopen.
 */
export function Drawer({ variant, label, closeLabel, inert, onClose, head, children, footer, onSubmit, bodyRef, rootRef, fill }: Props) {
  const scroll = <div className={fill ? 'rosc-drawer__scroll rosc-drawer__scroll--fill' : 'rosc-drawer__scroll'} ref={bodyRef}>{children}</div>;
  const body = onSubmit
    ? <form className="rosc-drawer__form" onSubmit={onSubmit}>{scroll}{footer && <div className="rosc-drawer__footer">{footer}</div>}</form>
    : <>{scroll}{footer && <div className="rosc-drawer__footer">{footer}</div>}</>;
  const nested = variant === 'task' || variant === 'new-task';
  return <div className={nested ? 'rosc-backdrop--nested' : 'rosc-backdrop'} onClick={onClose} inert={inert}>
    <section ref={rootRef} className={`rosc-drawer rosc-drawer--${variant} ${variant}-drawer`} onClick={e => e.stopPropagation()} aria-label={label}>
      <button className="rosc-edge-close" onClick={onClose} aria-label={closeLabel}><CloseIcon /></button>
      <span className="rosc-edge-caption" aria-hidden="true">{nested ? 'TASK' : variant === 'settings' ? 'SETTINGS' : 'PROJECT'}</span>
      <header className="rosc-drawer__head">{head}</header>
      {body}
    </section>
  </div>;
}
