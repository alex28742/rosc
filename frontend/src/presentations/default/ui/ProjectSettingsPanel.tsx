import { useState } from 'react';
import type { ProjectAssignmentsControls, ProjectAssignmentsState } from '../../../app/presentation/contracts';
import { agentIcon, roleEligible, runtimeLabel } from '../../../app/presentation/contracts';
import { Feedback } from './Feedback';

const activeArchitectOf = (state: ProjectAssignmentsState) =>
  state.assignments.find(item => item.role === 'architect' && item.state === 'active') ?? null;
const activeWorkersOf = (state: ProjectAssignmentsState) =>
  state.assignments.filter(item => item.role === 'worker' && item.state === 'active' && item.scope === 'project');

function architectGate(state: ProjectAssignmentsState): string | null {
  if (!state.architectKnown || state.architect === null) {
    return 'Состояние Architect не удалось проверить. Смена назначения заблокирована достоверной проверкой Core.';
  }
  const info = state.architect;
  if (!(info.state === 'not_configured' || info.state === 'stopped') || info.can_deactivate) {
    return 'Architect не подтверждённо остановлен. Сначала явно деактивируйте его в Project Conversation и дождитесь остановки.';
  }
  return null;
}

function runtimeFact(state: ProjectAssignmentsState): string {
  if (!state.architectKnown || state.architect === null) return 'состояние не проверено';
  switch (state.architect.state) {
    case 'ready': return 'активен (проверен)';
    case 'starting': return 'запускается';
    case 'error': return 'требует внимания';
    case 'not_configured': return 'не настроен';
    default: return 'остановлен';
  }
}

/** Project-scoped Architect/Worker intent. Nothing here launches, stops or replaces a session. */
export function ProjectSettingsPanel({ projectId, controls }: { projectId: string; controls: ProjectAssignmentsControls }) {
  const { state } = controls;
  const currentArchitect = activeArchitectOf(state);
  const currentWorkers = activeWorkersOf(state);
  const signature = [
    currentArchitect?.assignment_id ?? '', currentArchitect?.version ?? '', currentArchitect?.agent_id ?? '',
    currentArchitect?.readiness ?? '',
    ...currentWorkers.map(item => `${item.assignment_id}:${item.version}:${item.agent_id}:${item.readiness}`),
  ].join('|');

  // The draft is reset synchronously whenever Core returns a different authoritative assignment set. A render-phase
  // update (not an effect) means "Сохранить" can never serialize a stale empty draft right after a load.
  const [draft, setDraft] = useState(() => ({
    signature,
    architectId: currentArchitect?.agent_id ?? '',
    workerIds: currentWorkers.map(item => item.agent_id),
  }));
  const [confirming, setConfirming] = useState(false);
  let current = draft;
  if (draft.signature !== signature) {
    current = { signature, architectId: currentArchitect?.agent_id ?? '', workerIds: currentWorkers.map(item => item.agent_id) };
    setDraft(current);
    if (confirming) setConfirming(false);
  }
  const architectId = current.architectId;
  const workerIds = current.workerIds;

  const architectChanging = (currentArchitect?.agent_id ?? null) !== (architectId || null);
  const needsConfirmation = architectChanging && currentArchitect !== null;
  const gate = architectChanging ? architectGate(state) : null;

  const chooseArchitect = (next: string) => {
    setDraft(prev => ({ ...prev, architectId: next }));
    setConfirming(false);
  };
  const toggleWorker = (agentId: string, checked: boolean) => {
    setDraft(prev => ({
      ...prev,
      workerIds: checked ? [...new Set([...prev.workerIds, agentId])] : prev.workerIds.filter(id => id !== agentId),
    }));
  };

  const save = async (confirmed: boolean) => {
    await controls.onSave({ architectAgentId: architectId || null, workerAgentIds: workerIds }, confirmed);
    setConfirming(false);
  };

  const currentName = currentArchitect
    ? state.agents.find(agent => agent.agent_id === currentArchitect.agent_id)?.display_name ?? currentArchitect.agent_id
    : null;
  const nextName = architectId
    ? state.agents.find(agent => agent.agent_id === architectId)?.display_name ?? architectId
    : null;

  if (state.status === 'loading') {
    return <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка назначений…</div>;
  }
  if (state.status === 'failed') {
    return <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить назначения.
      <button type="button" className="rosc-btn rosc-btn--small" onClick={controls.onRetry}>Повторить</button></p></div>;
  }

  return <div className="rosc-settings__panel" data-project={projectId}>
    <Feedback error={state.error} notice={state.notice} />
    {state.unresolved.length > 0 && <div className="rosc-settings__hint rosc-settings__hint--warn">
      <strong>Не подтверждено:</strong>
      <ul>{state.unresolved.map(item => <li key={item}>{item}</li>)}</ul>
      Требуется решение Owner; автоматического отката нет.
    </div>}

    <div className="rosc-settings__panel-head">
      <div>
        <h3>Агенты проекта · назначения</h3>
        <p className="rosc-hint">Назначение ≠ запуск сессии. Core хранит версию каждого назначения.</p>
      </div>
      <button type="button" className="rosc-btn rosc-btn--small" onClick={controls.onOpenRegistry}>Глобальный Agent Registry</button>
    </div>

    {state.agents.length === 0 && <div className="rosc-settings__hint">
      Реестр пуст. Сначала добавьте Agent Definition в глобальных настройках.
    </div>}

    <label className="rosc-field"><span>Project Architect</span>
      <select value={architectId} onChange={event => chooseArchitect(event.target.value)}>
        <option value="">Подключить позже / отозвать назначение</option>
        {state.agents.map(agent => {
          const eligible = roleEligible(agent, 'architect');
          const assigned = currentArchitect?.agent_id === agent.agent_id;
          return <option key={agent.agent_id} value={agent.agent_id} disabled={!eligible && !assigned}>
            {agent.display_name}{eligible ? '' : ' · недостаточно capabilities'}
          </option>;
        })}
      </select>
    </label>

    <div className="rosc-settings__hint">
      <strong>Сейчас:</strong> {currentName ?? 'Architect не назначен'} ·{' '}
      {currentArchitect ? `readiness: ${currentArchitect.readiness}` : 'не настроен'} · Runtime {runtimeFact(state)}.
      <strong> Только отдельная активация Owner</strong> может запустить Architect.
    </div>

    <strong className="rosc-settings__label">Доступные Workers</strong>
    <p className="rosc-hint">Выбор разрешает будущие Task-назначения; Worker Run здесь не создаётся.</p>
    <div className="rosc-settings__roles">
      {state.agents.length === 0 ? <p className="rosc-hint">Агенты отсутствуют.</p>
        : state.agents.map(agent => {
          const eligible = roleEligible(agent, 'worker');
          const assignment = currentWorkers.find(item => item.agent_id === agent.agent_id);
          const checked = workerIds.includes(agent.agent_id);
          const compatible = eligible ? `Declared capabilities достаточны · readiness: ${assignment?.readiness ?? 'unverified'}`
            : assignment ? `Назначен, но readiness: ${assignment.readiness} · можно отозвать`
              : 'Недоступен: не объявлены обязательные Worker capabilities';
          return <label key={agent.agent_id} className={!eligible && !assignment ? 'rosc-settings__role rosc-settings__role--disabled' : 'rosc-settings__role'}>
            <input type="checkbox" checked={checked} disabled={!eligible && !assignment}
              onChange={event => toggleWorker(agent.agent_id, event.target.checked)} />
            <span>
              <strong>{agentIcon(agent.icon_label)} {agent.display_name}</strong>
              <small>{compatible} · {runtimeLabel(agent.runtime_adapter)}</small>
            </span>
          </label>;
        })}
    </div>

    {gate && <div className="rosc-settings__hint rosc-settings__hint--warn">{gate} Изменение Architect не будет отправлено.</div>}

    {confirming && needsConfirmation && <div className="rosc-settings__confirm" role="group" aria-label="Подтверждение смены Architect">
      <p><strong>{architectId ? 'Подтвердить замену Architect?' : 'Подтвердить отзыв Architect?'}</strong>{' '}
        Текущий: {currentName}. После отзыва будет создано {architectId ? `новое назначение: ${nextName}` : 'состояние без Architect'}.
        Core выполняет операции последовательно; при ошибке второго шага старый Architect не восстановится автоматически.</p>
      <div className="rosc-settings__editor-actions">
        <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={state.busy}
          onClick={() => void save(true)}>Подтвердить</button>
        <button type="button" className="rosc-btn rosc-btn--small" disabled={state.busy}
          onClick={() => setConfirming(false)}>Отмена</button>
      </div>
    </div>}

    <div className="rosc-settings__editor-actions">
      <button type="button" className="rosc-btn rosc-btn--primary" disabled={state.busy || gate !== null}
        onClick={() => needsConfirmation ? setConfirming(true) : void save(false)}>
        {state.busy ? 'Сохранение…' : 'Сохранить назначения'}
      </button>
    </div>

    <div className="rosc-settings__hint">
      <strong>Смена Architect:</strong> сначала отзыв текущего назначения, затем создание нового — Core не гарантирует
      атомарность этой последовательности. Версионные конфликты и частичные результаты показываются явно.
    </div>
    <div className="rosc-settings__hint">
      Права, инструкции и доступные инструменты определяются ролью, а не иконкой. Declared capabilities — не
      доказательство установленного CLI или авторизации. Открытие Project ничего не запускает.
    </div>
  </div>;
}
