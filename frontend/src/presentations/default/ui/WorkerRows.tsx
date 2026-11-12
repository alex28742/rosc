import { useState } from 'react';
import type { TaskWorkerControls } from '../../../app/presentation/contracts';
import { describeBootstrap, describeWorker, showsTerminalHelp } from './workerView';

const toneClass = (tone: string) => tone === 'ok' ? 'rosc-tag--success' : tone === 'neutral' ? 'rosc-tag--info' : 'rosc-tag--warning';

/**
 * «Workers проекта» (approved UX v3): every active Project Worker is its own row with independent actions, shown
 * whether or not a Prompt is approved. Names come from the Agent Registry; identity is the stable agent_id, never the
 * displayed @name. No Worker is called chat-ready or run-ready: those transports do not exist yet.
 */
export function WorkerRows({ controls }: { controls: TaskWorkerControls }) {
  const [helpFor, setHelpFor] = useState<string | null>(null);
  const [copied, setCopied] = useState<Record<string, 'ok' | 'fail'>>({});
  const copy = async (agentId: string, command: string) => {
    try { await navigator.clipboard.writeText(command); setCopied(prev => ({ ...prev, [agentId]: 'ok' })); }
    catch { setCopied(prev => ({ ...prev, [agentId]: 'fail' })); }
  };
  const active = controls.workers.filter(item => item.session.state === 'active').length;
  return <section className="rosc-task-workers" aria-label="Workers проекта">
    <div className="rosc-task-agents__head"><strong>Workers проекта</strong>
      {controls.workers.length > 0 && <span className="rosc-tag rosc-tag--info">{active} из {controls.workers.length}: CLI-сессия активна</span>}</div>
    <p className="rosc-task-workers__note">Каждый Worker активируется отдельно, независимо от Prompt и от других Workers. Это не запуск Run.</p>
    {controls.status === 'failed' && <p className="rosc-task-agents__err" role="alert">Не удалось загрузить список Workers.</p>}
    {controls.status === 'loading' && <p className="rosc-hint">Загружаем Workers…</p>}
    {controls.status === 'ready' && controls.workers.length === 0
      && <p className="rosc-hint">В проекте нет назначенных Workers. Назначьте Worker в настройках проекта (Agent Registry).</p>}
    <ul className="rosc-worker-list">
      {controls.workers.map(item => {
        const id = item.agent.agent_id;
        const busy = controls.busy[id];
        const view = describeWorker(item, busy);
        const boot = describeBootstrap(item.session);
        const error = controls.errors[id];
        const command = item.session.terminal?.attach_command ?? null;
        const helpOpen = helpFor === id;
        return <li key={id} className={`rosc-worker-row rosc-worker-row--${item.session.state}`} data-worker-row={id}>
          <div className="rosc-worker-row__top">
            <label className="rosc-worker-row__name">
              <input type="checkbox" disabled aria-label={`Адресовать ${item.agent.display_name}`}
                title="Адресная доставка в PC/TC появится позже" />
              <strong>@{item.agent.display_name}</strong>
            </label>
            <span className={`rosc-tag ${toneClass(view.tone)}`} aria-live="polite">{view.chip}</span>
          </div>
          <p className="rosc-worker-row__meta">Worker · {item.agent.runtime_adapter}</p>
          <p className={error || view.tone === 'error' ? 'rosc-task-agents__err' : 'rosc-worker-row__line'} role={error ? 'alert' : undefined}>{error || view.note}</p>
          <p className={`rosc-worker-row__line${boot.tone === 'ok' ? ' rosc-task-agents__ok' : ''}`}>{boot.line}</p>
          <div className="rosc-worker-row__actions">
            {view.activating
              ? <button type="button" className="rosc-btn rosc-btn--small" disabled>{busy === 'activate' ? 'Активация…' : 'Подключение…'}</button>
              : view.button === 'activate'
                ? <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" onClick={() => void controls.onActivate(id)}>Активировать сессию</button>
                : view.button === 'check'
                  ? <button type="button" className="rosc-btn rosc-btn--small" disabled={busy === 'check'} onClick={() => void controls.onCheck(id)}>Проверить сессию</button>
                  : null}
            {boot.button && <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={busy === 'bootstrap'}
              onClick={() => void controls.onBootstrap(id)}>{busy === 'bootstrap' ? 'Отправка…' : boot.button}</button>}
            {showsTerminalHelp(item.session) && <button type="button" className="rosc-btn rosc-btn--small rosc-btn--ghost" aria-expanded={helpOpen}
              onClick={() => setHelpFor(helpOpen ? null : id)}>Как открыть терминал</button>}
          </div>
          {helpOpen && <div className="rosc-worker-row__help" role="group" aria-label={`Терминал @${item.agent.display_name}`}>
            <p>Если Claude Code спросил, доверять ли каталогу проекта, подтвердите это в терминале: ROSC сам на этот вопрос не отвечает.</p>
            {command
              ? <>
                <code>{command}</code>
                <div className="rosc-worker-row__actions">
                  <button type="button" className="rosc-btn rosc-btn--small" onClick={() => void copy(id, command)}>Копировать команду</button>
                  {copied[id] === 'ok' && <span className="rosc-task-agents__ok" role="status">Команда скопирована.</span>}
                  {copied[id] === 'fail' && <span className="rosc-task-agents__err" role="status">Копирование недоступно: выделите команду вручную.</span>}
                </div>
                <p>Выйти из терминала, не останавливая CLI: Ctrl-b, затем d.</p>
              </>
              : <p className="rosc-task-agents__err">Команда подключения недоступна: ROSC не может подтвердить свою сессию этого Worker.</p>}
          </div>}
        </li>;
      })}
    </ul>
    <p className="rosc-task-workers__note">«CLI-сессия активна» — проверенный процесс и hook CLI. «Контекст подготовлен» — отдельное подтверждение чтения entry point. Диалог в PC/TC и Run появятся в следующих срезах.</p>
  </section>;
}
