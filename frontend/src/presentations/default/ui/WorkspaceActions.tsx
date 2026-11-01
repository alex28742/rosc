import { useState } from 'react';
import type { WorkspaceProblem, WorkspaceState } from '../../../core/models';
import type { WorkspaceControls } from '../../../app/presentation/contracts';

type Props = { state: WorkspaceState; problem: WorkspaceProblem | null; controls: WorkspaceControls };

/** Workspace manifest controls. Repair is only reachable after an explicit confirmation. */
export function WorkspaceActions({ state, problem, controls }: Props) {
  const [confirmed, setConfirmed] = useState(false);
  if (state === 'ready') return <div className="rosc-repair">
    <p>Манифест Workspace соответствует записи в базе.</p>
    <button className="rosc-btn rosc-btn--small" aria-disabled={controls.busy} onClick={controls.onReconcile}>Проверить Workspace</button>
  </div>;
  if (problem === 'corrupt' || problem === 'drift') {
    return <div className="rosc-repair">
      <p>{problem === 'corrupt' ? 'Манифест Workspace повреждён.' : 'Манифест Workspace расходится с записью в базе.'} Перед заменой текущий файл будет сохранён в карантин ROSC.</p>
      <label className="rosc-check"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> Подтверждаю замену манифеста данными из базы</label>
      <button className="rosc-btn rosc-btn--small" disabled={!confirmed} aria-disabled={controls.busy} onClick={() => { if (confirmed) controls.onRepair({ confirmed: true }); }}>Восстановить манифест</button>
    </div>;
  }
  if (problem === 'oversized') return <p className="rosc-repair">Манифест Workspace слишком большой, чтобы его безопасно сохранить в карантин. Файл не изменён, автоматическое исправление отключено, нужно ручное разрешение.</p>;
  if (problem === 'foreign' || problem === 'unsafe') return <p className="rosc-repair">Манифест принадлежит другой записи или лежит в небезопасном месте. Автоматическое исправление отключено, нужно ручное разрешение.</p>;
  return <div className="rosc-repair">
    <p>Манифест Workspace ещё не сверен с записью в базе.</p>
    <button className="rosc-btn rosc-btn--small" aria-disabled={controls.busy} onClick={controls.onReconcile}>Проверить Workspace</button>
  </div>;
}
