import type { ShellProps } from '../../app/presentation/contracts';

/** Application frame: brand, the approved Settings entry and the truthful Engine state chip. */
export function Shell({ children, onOpenSettings, settingsOpen }: ShellProps) {
  return <div className="rosc-app">
    <header className="rosc-topbar">
      <div className="rosc-brand">
        <strong className="rosc-brand__name">ROSC</strong>
        <span className="rosc-brand__slash" aria-hidden="true">/</span>
        <span className="rosc-brand__context">Рабочее пространство</span>
      </div>
      <div className="rosc-topbar__actions">
        <span className="rosc-chip" title="Текущее состояние Engine Slice 01"><span className="rosc-chip__dot" aria-hidden="true" />Engine Slice 01 · агенты не запущены</span>
        <button type="button" className="rosc-btn rosc-btn--small" onClick={onOpenSettings}
          aria-current={settingsOpen ? 'page' : undefined}>Настройки</button>
      </div>
    </header>
    {children}
  </div>;
}
