import { useEffect, useState } from 'react';
import type { TaskRunControls, TaskWorkerControls } from '../../../app/presentation/contracts';
import type { NextAction, ProjectWorker, RunJournalEvent, RunReportPage, RunState, RunStep, RunView } from '../../../core/models';

/** Core-recorded Run states in Owner language. Delivery/import facts only — never "verified" or "accepted". */
export const RUN_STATE_TEXT: Record<RunState, string> = {
  authorized: 'передаётся Worker', dispatch_pending: 'передаётся Worker', dispatching: 'передан Worker (E0)',
  submitted: 'ожидает ACK Worker (E0)', acknowledged: 'Worker принял (E0)', declined: 'Worker отклонил',
  unknown: 'состояние неизвестно', not_delivered: 'не доставлен', exec_dispatching: 'передано исполнение',
  running: 'выполняется Worker', reported: 'отчёт Worker получен', not_executed: 'не начат',
};
const IMPL_TEXT: Record<string, string> = {
  PENDING: 'ожидает', IN_PROGRESS: 'в работе', PRODUCED: 'сделан', BLOCKED: 'заблокирован', FAILED: 'не удался', UNCERTAIN: 'неясно',
};
const TEST_TEXT: Record<string, string> = {
  NOT_TESTED: 'не тестирован', TESTING: 'тестируется', PASSED: 'пройден', FAILED: 'не пройден', INCONCLUSIVE: 'без вывода', STALE: 'устарел',
};
const OUTCOME_TEXT: Record<string, string> = { completed: 'завершён', partial: 'частично', blocked: 'заблокирован', failed: 'не удался' };

const implTone = (value: string) => value === 'PRODUCED' ? 'rosc-tag--success' : value === 'PENDING' || value === 'IN_PROGRESS' ? 'rosc-tag--info' : 'rosc-tag--warning';
const testTone = (value: string) => value === 'PASSED' ? 'rosc-tag--success' : value === 'NOT_TESTED' || value === 'TESTING' ? 'rosc-tag--info' : 'rosc-tag--warning';

export const formatTime = (iso: string | null | undefined) => {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

/** A Worker can receive a Run only with a verified active CLI and a confirmed project bootstrap (Core re-checks). */
export const eligibleWorkers = (workers: TaskWorkerControls) =>
  workers.workers.filter(item => item.session.state === 'active' && item.session.bootstrap.state === 'confirmed');

type LaunchProps = { next: NextAction; workers: TaskWorkerControls; run: TaskRunControls; busy: boolean; selected: string | null; onSelect: (agentId: string) => void };

/**
 * «Запустить Run»: one explicit Owner action for one selected Worker (stable agent_id). Disabled states always say why.
 * A request whose outcome is unknown is retried with the SAME launch key (never a second Run).
 */
export function RunLaunchAction({ next, workers, run, busy, selected, onSelect }: LaunchProps) {
  const eligible = eligibleWorkers(workers);
  const chosen: ProjectWorker | null = eligible.length === 1 ? eligible[0] : eligible.find(item => item.agent.agent_id === selected) ?? null;
  const reason = !next.enabled ? next.hint
    : workers.status !== 'ready' ? 'Список Workers ещё загружается.'
      : eligible.length === 0 ? 'Нет Worker с активной CLI-сессией и подтверждённым контекстом проекта: активируйте Worker и подготовьте контекст в «Workers проекта».'
        : chosen === null ? 'Выберите Worker, который получит Run.' : '';
  const enabled = reason === '' && !busy && !run.launching && chosen !== null;
  const error = run.launchError;
  return <>
    {eligible.length > 1 && <fieldset className="rosc-run-choice" aria-label="Исполнитель Run">
      <legend>Исполнитель Run</legend>
      {eligible.map(item => <label key={item.agent.agent_id}>
        <input type="radio" name="rosc-run-worker" value={item.agent.agent_id} checked={selected === item.agent.agent_id}
          onChange={() => onSelect(item.agent.agent_id)} />@{item.agent.display_name}</label>)}
    </fieldset>}
    {chosen && <p className="rosc-hint" data-testid="run-target">Run получит @{chosen.agent.display_name}. Один клик — один Run; второй Run этой Task не запускается.</p>}
    <div className="rosc-task-actions__row">
      <button type="button" className="rosc-btn rosc-btn--primary" disabled={!enabled} title={enabled ? undefined : reason}
        onClick={() => chosen && void run.onLaunch(chosen.agent.agent_id)}>{run.launching ? 'Запуск…' : next.label}</button>
      {error?.uncertain && chosen && <button type="button" className="rosc-btn rosc-btn--small" disabled={run.launching}
        onClick={() => void run.onLaunch(chosen.agent.agent_id)}>Повторить с тем же ключом</button>}
    </div>
    {!enabled && reason && <p className="rosc-hint" data-testid="run-disabled-reason">{reason}</p>}
    {error && <p className="rosc-task-agents__err" role="alert">{error.uncertain
      ? `Результат запуска неизвестен: ${error.message} Повтор использует тот же ключ и не создаст второй Run.` : error.message}</p>}
  </>;
}

function StepRow({ step }: { step: RunStep }) {
  return <li className="rosc-run-step" data-step={step.id}>
    <div className="rosc-run-step__top">
      <strong>{step.id}</strong><span className="rosc-run-step__title">{step.title}</span>
    </div>
    <div className="rosc-run-step__axes">
      <span className={`rosc-tag ${implTone(step.implementation)}`} title={`Реализация: ${IMPL_TEXT[step.implementation] ?? step.implementation}`}>Реализация · {step.implementation}</span>
      <span className={`rosc-tag ${testTone(step.test)}`} title={`Тест: ${TEST_TEXT[step.test] ?? step.test}`}>Тест · {step.test}</span>
      {step.tests.length > 0 && <span className="rosc-run-step__tests">тесты: {step.tests.join(', ')}</span>}
      {step.depends_on.length > 0 && <span className="rosc-run-step__tests">после: {step.depends_on.join(', ')}</span>}
    </div>
  </li>;
}

/** «Рабочий план Worker» body: imported Plan revision, Step axes, blockers and freshness. No spinner from tmux activity. */
export function WorkingPlan({ run, controls }: { run: RunView | null; controls: TaskRunControls }) {
  if (controls.status === 'failed' && !run) {
    return <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить Run этой Task. <button type="button" className="rosc-task-link" onClick={controls.onReload}>Повторить</button></p></div>;
  }
  if (!run) return <div className="rosc-state"><p>Рабочий план пока недоступен.</p><p>Шаги появятся, когда Worker подготовит план для этой Task.</p></div>;
  const progress = run.progress ?? null;
  const notices = <>
    {controls.stale && <p className="rosc-task-agents__err" role="status">Не удалось обновить состояние Run: показаны последние полученные данные.</p>}
    {run.state === 'unknown' && <p className="rosc-task-agents__err" role="status">Состояние Run неизвестно ({run.reason}). ROSC ничего не отправляет повторно; второй Run этой Task не запускается.</p>}
    {progress?.import.problem && <p className="rosc-task-agents__err" role="alert">Импорт остановлен: {progress.import.problem}{progress.import.problem_detail ? ` (${progress.import.problem_detail})` : ''}. Ранее импортированные данные сохранены.</p>}
  </>;
  if (!progress) {
    return <div className="rosc-state">{notices}<p>Run {RUN_STATE_TEXT[run.state] ?? run.state}.</p><p>Рабочий план появится после того, как Worker начнёт исполнение и опубликует его.</p></div>;
  }
  const blockers = progress.blockers.filter(item => item.open);
  return <div className="rosc-run-plan">
    {notices}
    {progress.plan_revision === 0
      ? <div className="rosc-state"><p>Worker ещё не опубликовал Working Plan.</p></div>
      : <ol className="rosc-run-steps" aria-label="Шаги плана">{progress.steps.map(step => <StepRow key={step.id} step={step} />)}</ol>}
    {blockers.length > 0 && <div className="rosc-run-blockers" role="group" aria-label="Блокеры">
      <strong>Блокеры</strong>
      <ul>{blockers.map(item => <li key={item.blocker_id}><span className="rosc-tag rosc-tag--warning">{item.blocker_id}</span> {item.summary}
        {item.next_action ? ` — следующий шаг: ${item.next_action}` : ''}{item.owner_decision ? ' (нужно решение Owner)' : ''}</li>)}</ul>
    </div>}
  </div>;
}

export function planProgress(run: RunView | null) {
  const progress = run?.progress?.progress ?? null;
  if (!progress || progress.total === 0) return null;
  // Quantised to tenths (CSS classes, no inline styles); 0 only when nothing is produced.
  const decile = progress.produced === 0 ? 0 : Math.max(1, Math.round((progress.produced / progress.total) * 10));
  return { produced: progress.produced, passed: progress.test_passed, total: progress.total, decile };
}

const eventSummary = (event: RunJournalEvent) => {
  const data = event.data as Record<string, unknown>;
  switch (event.type) {
    case 'step.status': return `${event.step_id}: ${String(data.from)} → ${String(data.to)}${data.note ? ` — ${String(data.note)}` : ''}`;
    case 'test.recorded': return `${String(data.test_id)} (${(data.steps as string[] | undefined)?.join(', ') ?? ''}): ${String(data.command)} → exit ${String(data.exit_code)}, ${String(data.outcome)}`;
    case 'test.started': return `${String(data.test_id)} начат для ${(data.steps as string[] | undefined)?.join(', ') ?? ''}`;
    case 'plan.published': return `Working Plan v${String(data.revision)} (${String(data.steps)} шагов)${data.reason ? ` — ${String(data.reason)}` : ''}`;
    case 'blocker.recorded': return `${String(data.blocker_id)}: ${String(data.summary)}`;
    case 'blocker.resolved': return `${String(data.blocker_id)} снят: ${String(data.note)}`;
    case 'checkpoint.saved': return `Checkpoint${data.current_step ? ` на ${String(data.current_step)}` : ''}${data.note ? ` — ${String(data.note)}` : ''}`;
    case 'note.recorded': return `${String(data.kind)}: ${String(data.text)}`;
    case 'report.finalized': return `Report финализирован: ${String(data.outcome)}`;
    case 'run.finished': return `Journal закрыт: ${String(data.outcome)}`;
    case 'run.started': return 'Исполнение начато (E1)';
    default: return event.type;
  }
};

/** «Журнал выполнения»: bounded imported event history (read-only side drawer). */
export function RunJournalDrawer({ run, onClose }: { run: TaskRunControls; onClose: () => void }) {
  const [events, setEvents] = useState<RunJournalEvent[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [failure, setFailure] = useState('');
  const [loading, setLoading] = useState(true);
  const more = async (after: number) => {
    setLoading(true);
    const page = await run.onLoadJournal(after);
    setLoading(false);
    if (typeof page === 'string') { setFailure(page); return; }
    setFailure('');
    setEvents(prev => after === 0 ? page.events : [...prev, ...page.events]);
    setNext(page.next_after);
  };
  useEffect(() => { void more(0); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="rosc-draft-overlay" onClick={onClose}>
    <aside className="rosc-draft-drawer" role="dialog" aria-modal="true" aria-labelledby="rosc-journal-title" onClick={event => event.stopPropagation()}>
      <div className="rosc-draft-head">
        <div><h2 id="rosc-journal-title">Журнал выполнения</h2><p>Импортированные события Worker — заявления Worker, не проверка.</p></div>
        <div className="rosc-draft-head__actions"><button type="button" className="rosc-btn rosc-btn--small" aria-label="Закрыть журнал" onClick={onClose}>×</button></div>
      </div>
      <div className="rosc-draft-body">
        {failure && <p className="rosc-task-agents__err" role="alert">{failure}</p>}
        {!loading && !failure && events.length === 0 && <p className="rosc-hint">Событий пока нет: Core ещё не импортировал Journal этого Run.</p>}
        <ol className="rosc-run-journal" aria-label="События журнала">
          {events.map(event => <li key={event.seq}><span className="rosc-run-journal__seq">#{event.seq}</span>
            <span className="rosc-run-journal__at">{formatTime(event.at)}</span> <strong>{event.type}</strong> {eventSummary(event)}</li>)}
        </ol>
        {loading && <p className="rosc-hint">Загрузка…</p>}
        {next !== null && !loading && <button type="button" className="rosc-btn rosc-btn--small" onClick={() => void more(next)}>Показать ещё</button>}
      </div>
    </aside>
  </div>;
}

/** The registered Worker Run Report (digest re-verified by Core on every read; content is an unverified claim). */
export function RunReportDrawer({ run, onClose }: { run: TaskRunControls; onClose: () => void }) {
  const [page, setPage] = useState<RunReportPage | null>(null);
  const [failure, setFailure] = useState('');
  useEffect(() => {
    let live = true;
    void run.onLoadReport().then(result => { if (!live) return; if (typeof result === 'string') setFailure(result); else setPage(result); });
    return () => { live = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="rosc-draft-overlay" onClick={onClose}>
    <aside className="rosc-draft-drawer rosc-draft-drawer--wide" role="dialog" aria-modal="true" aria-labelledby="rosc-report-title" onClick={event => event.stopPropagation()}>
      <div className="rosc-draft-head">
        <div><h2 id="rosc-report-title">Run Report</h2>
          <p>{page ? `${OUTCOME_TEXT[page.outcome] ?? page.outcome} · sha256 ${page.sha256.slice(0, 16)}… · заявление Worker, ожидает Architect Review` : 'Отчёт Worker'}</p></div>
        <div className="rosc-draft-head__actions"><button type="button" className="rosc-btn rosc-btn--small" aria-label="Закрыть Report" onClick={onClose}>×</button></div>
      </div>
      <div className="rosc-draft-body">
        {failure && <p className="rosc-task-agents__err" role="alert">Report недоступен: {failure}</p>}
        {!page && !failure && <p className="rosc-hint">Загрузка…</p>}
        {page && <pre className="rosc-run-report">{page.content}</pre>}
        {page?.next_offset != null && <p className="rosc-hint">Показана первая часть отчёта ({page.content.length} из {page.chars} символов).</p>}
      </div>
    </aside>
  </div>;
}

export const outcomeText = (value: string | null | undefined) => (value ? OUTCOME_TEXT[value] ?? value : '');
