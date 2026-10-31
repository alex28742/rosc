import type { ReactNode } from 'react';

type Props = {
  variant: 'project' | 'task';
  label: string;
  closeLabel: string;
  inert: boolean;
  onClose: () => void;
  head: ReactNode;
  children: ReactNode;
  /** When set, the body is a `<form>` (Task Draft) instead of a plain scroll region. */
  onSubmit?: (event: React.FormEvent<HTMLFormElement>) => void;
  bodyRef?: (element: HTMLElement | null) => void;
};

/** Backdrop + right-hand drawer + left-edge close control shared by the Project and Task layers. */
export function Drawer({ variant, label, closeLabel, inert, onClose, head, children, onSubmit, bodyRef }: Props) {
  const nested = variant === 'task';
  const body = onSubmit
    ? <form className="drawer-body task-form" onSubmit={onSubmit} ref={bodyRef}>{children}</form>
    : <div className="drawer-body" ref={bodyRef}>{children}</div>;
  return <div className={nested ? 'nested-backdrop' : 'backdrop'} onClick={onClose} inert={inert}>
    <section className={`drawer ${variant}-drawer`} onClick={e => e.stopPropagation()} aria-label={label}>
      <button className={nested ? 'edge-close second' : 'edge-close'} onClick={onClose} aria-label={closeLabel}>×</button>
      <div className="drawer-head">{head}</div>
      {body}
    </section>
  </div>;
}
