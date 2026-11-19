import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { ApiError } from '../core/api';
import type { PromptRevision, RunProgress, RunView, TaskAuthoring } from '../core/models';
import { RUN_ID, deferred, project, projectWorker, runView, stubApi, task, taskAuthoring } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const approved: PromptRevision = {
  revision: 1, source_spec_revision: 1, summary: '1. x', body: 'Do', digest: 'p'.repeat(64), rules_ref: null, state: 'approved',
  actor: { kind: 'owner', id: 'local-owner' }, created_at: '2026-10-31T00:00:00Z', approval: null,
};
const launchable = (): TaskAuthoring => taskAuthoring({ prompt: approved, prompt_history: [approved],
  next: { action: 'authorize_run', label: 'Запустить Run', enabled: true, next_actor: 'Owner', hint: 'Выберите Worker.' } });
const ready = (id: string, name: string) => projectWorker(id, { state: 'active', reason: 'initialised', can_activate: false,
  bootstrap: { state: 'confirmed', reason: 'confirmed', can_bootstrap: false, sources: ['AGENTS.md'], confirmed_at: '2026-11-17T09:00:00Z' } }, name);

const progress = (over: Partial<RunProgress> = {}): RunProgress => ({
  plan_revision: 2,
  steps: [
    { id: 'S01', title: 'Schema', description: null, depends_on: [], expected_outcome: null, test_intent: null, implementation: 'PRODUCED', test: 'PASSED', tests: ['T01'] },
    { id: 'S02', title: 'API', description: null, depends_on: ['S01'], expected_outcome: null, test_intent: null, implementation: 'PRODUCED', test: 'PASSED', tests: ['T01'] },
    { id: 'S03', title: 'UI', description: null, depends_on: ['S02'], expected_outcome: null, test_intent: null, implementation: 'IN_PROGRESS', test: 'NOT_TESTED', tests: [] },
  ],
  progress: { total: 3, produced: 2, test_passed: 2, note: '' },
  tests: [{ test_id: 'T01', steps: ['S01', 'S02'], command: 'pytest -k api', exit_code: 0, outcome: 'passed', limitations: null, at: '2026-11-17T10:05:00Z', evidence: null }],
  blockers: [{ blocker_id: 'B01', summary: 'Нет доступа к схеме', step_id: 'S03', next_action: 'Owner решает', owner_decision: true, open: true, at: '2026-11-17T10:06:00Z' }],
  started: true, journal: { events: 9, last_event_at: '2026-11-17T10:06:00Z', last_seq: 9 },
  import: { imported_at: '2026-11-17T10:06:05Z', checked_at: '2026-11-17T10:06:05Z', journal_observed_at: null, problem: null, problem_detail: null, torn_tail_bytes: 0 },
  checkpoint: null, report: null, handoff: null, ...over,
});
const running = (over: Partial<RunView> = {}) => runView({ state: 'running', reason: 'exec_submitted', progress: progress(), work_started: true, ...over });

const open = (extra: Record<string, ReturnType<typeof vi.fn>> = {}, workers = [ready('w-1', 'claude')], authoring = launchable()) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getTaskAuthoring: vi.fn(async () => authoring),
  listProjectWorkers: vi.fn(async () => workers), ...extra });
const context = async () => within(await screen.findByLabelText('Контекст и действия'));
const plan = async () => within(await screen.findByLabelText('Рабочий план Worker'));
const launchButton = async () => (await context()).findByRole('button', { name: 'Запустить Run' }) as Promise<HTMLButtonElement>;
const enabledLaunch = async () => {
  const button = await launchButton();
  await waitFor(() => expect(button.disabled).toBe(false));
  return button;
};

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; vi.restoreAllMocks(); });

describe('P054B one-click Run launch', () => {
  it('opening the Task only reads; one click launches once for the eligible Worker by agent_id', async () => {
    const reply = deferred<RunView & { created: boolean }>();
    const api = open({ launchRun: vi.fn(() => reply.promise) });
    render(<App api={api} />);
    const button = await enabledLaunch();
    expect(api.launchRun).not.toHaveBeenCalled();
    expect((await context()).getByTestId('run-target').textContent).toContain('@claude');
    fireEvent.click(button);
    fireEvent.click(button); // a second click while the first request runs does nothing
    expect(api.launchRun).toHaveBeenCalledTimes(1);
    const [pid, tid, agent, key] = api.launchRun.mock.calls[0];
    expect([pid, tid, agent]).toEqual(['P0001', 'T0001', 'w-1']);
    expect(key).toMatch(/^ui-/);
    reply.resolve({ ...runView(), created: true });
    expect(await (await plan()).findByTestId('run-state')).toBeTruthy();
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
  });

  it('an uncertain answer is retried with the SAME key and never as a fresh launch', async () => {
    const api = open({ launchRun: vi.fn()
      .mockRejectedValueOnce(new ApiError('Не удалось связаться с ядром ROSC.', 'network', null))
      .mockResolvedValueOnce({ ...runView(), created: false }) });
    render(<App api={api} />);
    fireEvent.click(await enabledLaunch());
    const ctx = await context();
    expect((await ctx.findByRole('alert')).textContent).toContain('Результат запуска неизвестен');
    fireEvent.click(await ctx.findByRole('button', { name: 'Повторить с тем же ключом' }));
    await waitFor(() => expect(api.launchRun).toHaveBeenCalledTimes(2));
    expect(api.launchRun.mock.calls[1][3]).toBe(api.launchRun.mock.calls[0][3]);
  });

  it('a holding conflict shows the existing Run instead of retrying with a new key', async () => {
    const existing = running();
    const api = open({
      launchRun: vi.fn(async () => { throw new ApiError('Run of this Task is still holding', 'conflict', 409, 'run_in_progress'); }),
      listTaskRuns: vi.fn().mockResolvedValueOnce([]).mockResolvedValue([existing]),
      getRun: vi.fn(async () => existing),
    });
    render(<App api={api} />);
    fireEvent.click(await enabledLaunch());
    expect((await (await context()).findByRole('alert')).textContent).toContain('Показан текущий Run');
    expect(await (await plan()).findByText('S01')).toBeTruthy();
    expect(api.launchRun).toHaveBeenCalledTimes(1);
    expect((await context()).queryByRole('button', { name: 'Повторить с тем же ключом' })).toBeNull();
  });

  it('explains why the action is disabled and requires an explicit choice among several Workers', async () => {
    render(<App api={open({}, [projectWorker('w-1', {}, 'idle')])} />);
    const button = await launchButton();
    expect(button.disabled).toBe(true);
    expect((await context()).getByTestId('run-disabled-reason').textContent).toMatch(/Нет Worker с активной CLI-сессией/);
    cleanup();
    const api = open({}, [ready('w-1', 'ivan'), ready('w-2', 'maria')]);
    render(<App api={api} />);
    const ctx = await context();
    const choice = within(await ctx.findByRole('group', { name: 'Исполнитель Run' }));
    expect((await launchButton()).disabled).toBe(true);
    fireEvent.click(choice.getByLabelText('@maria'));
    fireEvent.click(await enabledLaunch());
    await waitFor(() => expect(api.launchRun).toHaveBeenCalledTimes(1));
    expect(api.launchRun.mock.calls[0][2]).toBe('w-2');
  });

  it('a backend-disabled action keeps its reason and never offers the launch', async () => {
    const authoring = taskAuthoring({ prompt: approved, next: { action: 'observe_run', label: 'Run выполняется Worker', enabled: false,
      next_actor: 'Worker', hint: 'Run уже запущен: второй Run этой Task не запускается.' } });
    render(<App api={open({ listTaskRuns: vi.fn(async () => [running()]), getRun: vi.fn(async () => running()) }, undefined, authoring)} />);
    // P056: a status is a heading with its reason, never a disabled button repeating the label
    expect(await (await context()).findByRole('heading', { name: 'Run выполняется Worker' })).toBeTruthy();
    expect((await context()).getByText(/второй Run этой Task не запускается/)).toBeTruthy();
    expect((await context()).queryByRole('button', { name: 'Run выполняется Worker' })).toBeNull();
    expect((await context()).queryByRole('button', { name: 'Запустить Run' })).toBeNull();
  });
});

describe('P054B Working Plan, Journal and Report', () => {
  it('shows the imported Plan with two independent axes, batch-test attribution, blockers and freshness', async () => {
    render(<App api={open({ listTaskRuns: vi.fn(async () => [running()]), getRun: vi.fn(async () => running()) })} />);
    const panel = await plan();
    expect(await panel.findByText('Revision v2')).toBeTruthy();
    expect(panel.getByText('Реализация: PRODUCED 2/3')).toBeTruthy();
    expect(panel.getByText(/не VERIFIED/)).toBeTruthy();
    const s01 = within(document.querySelector('[data-step="S01"]') as HTMLElement);
    const s02 = within(document.querySelector('[data-step="S02"]') as HTMLElement);
    const s03 = within(document.querySelector('[data-step="S03"]') as HTMLElement);
    expect([s01.getByText('Реализация · PRODUCED'), s01.getByText('Тест · PASSED'), s01.getByText('тесты: T01')]).toHaveLength(3);
    expect(s02.getByText('тесты: T01')).toBeTruthy(); // one batch test attributed to both Steps
    expect([s03.getByText('Реализация · IN_PROGRESS'), s03.getByText('Тест · NOT_TESTED')]).toHaveLength(2);
    expect(within(panel.getByRole('group', { name: 'Блокеры' })).getByText(/Нет доступа к схеме/)).toBeTruthy();
    expect(panel.getByTestId('run-freshness').textContent).toMatch(/Событие Worker: .* импорт Core: /);
    expect(screen.queryByText(/VERIFIED 2\/3|Owner Accepted|Task ACCEPTED/)).toBeNull();
  });

  it('opens the bounded Journal and the registered Report with an honest review state', async () => {
    const reported = running({ state: 'reported', holding: false, progress: progress({ report: { path: 'reports/final.md', sha256: 'b'.repeat(64), bytes: 10,
      outcome: 'completed', registered_at: '2026-11-17T10:09:00Z', note: '' } }), review: { state: 'awaiting_architect_review', next_actor: 'Architect', note: '' } });
    const api = open({ listTaskRuns: vi.fn(async () => [reported]), getRun: vi.fn(async () => reported),
      getRunJournal: vi.fn(async () => ({ run_id: RUN_ID, next_after: null, note: '', events: [
        { seq: 1, event_id: 'E000001', type: 'plan.published', step_id: null, plan_revision: 1, at: '2026-11-17T10:01:00Z', data: { revision: 1, steps: 3, reason: 'initial' }, imported_at: null },
        { seq: 2, event_id: 'E000002', type: 'test.recorded', step_id: null, plan_revision: 1, at: '2026-11-17T10:02:00Z', data: { test_id: 'T01', steps: ['S01', 'S02'], command: 'pytest -k api', exit_code: 0, outcome: 'passed' }, imported_at: null },
      ] })) });
    render(<App api={api} />);
    const ctx = await context();
    expect((await ctx.findByTestId('run-report-ref')).textContent).toContain('ожидает Architect Review');
    fireEvent.click(ctx.getByRole('button', { name: 'Журнал выполнения' }));
    const journal = within(await screen.findByRole('dialog'));
    expect(await journal.findByText(/T01 \(S01, S02\): pytest -k api → exit 0, passed/)).toBeTruthy();
    fireEvent.click(journal.getByLabelText('Закрыть журнал'));
    fireEvent.click(ctx.getByRole('button', { name: 'Открыть Report' }));
    const report = within(await screen.findByRole('dialog'));
    expect(await report.findByText('# Run Report')).toBeTruthy();
    expect(report.getByText(/заявление Worker, ожидает Architect Review/)).toBeTruthy();
    expect(api.launchRun).not.toHaveBeenCalled();
  });

  it('is honest about pre-plan, UNKNOWN, import problems and stale reads; no spinner from tmux activity', async () => {
    const noPlan = running({ state: 'unknown', reason: 'no_exec_submit_within_timeout', progress: progress({ plan_revision: 0, steps: [],
      progress: { total: 0, produced: 0, test_passed: 0, note: '' }, blockers: [],
      import: { imported_at: null, checked_at: null, journal_observed_at: null, problem: 'journal_history_changed', problem_detail: 'event 3', torn_tail_bytes: 0 } }) });
    const getRun = vi.fn().mockResolvedValueOnce(noPlan).mockRejectedValue(new ApiError('down', 'network', null));
    render(<App api={open({ listTaskRuns: vi.fn(async () => [noPlan]), getRun })} />);
    const panel = await plan();
    expect(await panel.findByText('Worker ещё не опубликовал Working Plan.')).toBeTruthy();
    expect(panel.getByText(/Состояние Run неизвестно/)).toBeTruthy();
    expect(panel.getByRole('alert').textContent).toContain('journal_history_changed');
    expect(panel.getByText('Прогресс не определён')).toBeTruthy();
    expect(document.querySelector('.rosc-task-plan .rosc-spinner')).toBeNull();
    fireEvent.focus(window);
    expect(await panel.findByText(/показаны последние полученные данные/)).toBeTruthy();
    expect(panel.getByText('Worker ещё не опубликовал Working Plan.')).toBeTruthy();
  });
});
