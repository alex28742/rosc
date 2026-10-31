import type { ShellProps } from '../../app/presentation/contracts';

export function Shell({ children }: ShellProps) {
  return <>
    <header className="topbar"><strong className="logo">ROSC</strong><span className="crumb">/ Рабочее пространство</span><span className="spacer" /><span className="subtle">Engine Slice 01 · агенты не запущены</span></header>
    {children}
  </>;
}
