import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RunReview, RunView, TaskAuthoring } from '../core/models';
import { App } from './App';
import { project, runView, stubApi, task, taskAuthoring } from './testing';

/** P056 Owner Decision and the focused «Контекст и действия» panel (synthetic: stubbed Core API only). */
const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const RUN = '99999999-9999-4999-8999-999999999999';
const LONG = 'Первый абзац: критерии выполнены, тесты проходят.\n\nВторой абзац с подробным разбором каждого шага, который в свёрнутом виде не показывается.';
const passed: RunReview = { review_id: 'rv-1', run_id: RUN, task_id: 'T0001', version: 1, supersedes_review_id: null,
  verdict: 'review_passed', summary: LONG, findings: [], limitations: ['Только логи Worker', 'Тесты не перезапускались'],
  continue_by: null, report_path: 'reports/final.md', report_sha256: '798c15da3ea9'.padEnd(64, '0'),
  architect: { agent_id: 'a-1', display_name: 'DeepSeek' }, recorded_at: '2026-11-19T06:26:00Z', source: 'rosc_review_submit' };
const report = { path: 'reports/final.md', sha256: passed.report_sha256, bytes: 10, outcome: 'completed' as const, registered_at: '2026-11-19T06:00:00Z', note: '' };
const progress = { plan_revision: 1, steps: [], progress: { total: 0, produced: 0, test_passed: 0, note: '' }, tests: [], blockers: [],
  started: true, journal: { events: 5, last_event_at: null, last_seq: 5 },
  import: { imported_at: null, checked_at: null, journal_observed_at: null, problem: null, problem_detail: null, torn_tail_bytes: 0 },
  checkpoint: null, report, handoff: null };
const reported = (review: RunReview | null = passed): RunView => runView({ run_id: RUN, state: 'reported', reason: 'worker_reported', holding: false,
  report, progress, review: review ? { state: review.verdict, next_actor: 'Owner', latest: review, note: '' }
    : { state: 'awaiting_architect_review', next_actor: 'Architect', latest: null, note: '' } });
const hint = 'Worker завершил работу. Architect проверил результат и рекомендовал принять его. Это не VERIFIED и не приёмка Task — решение остаётся за Owner.';
const deciding = (): TaskAuthoring => taskAuthoring({ task_status: 'Active', step: 'awaiting_owner_decision',
  run: { authorized: true, label: 'Run: отчёт Worker получен', worker: 'w', run_id: RUN, state: 'reported', holding: false,
    review: { state: 'review_passed', next_actor: 'Owner', latest: passed, note: '' }, decision: null },
  next: { action: 'owner_decision', label: 'Ожидает решения Owner', enabled: true, next_actor: 'Owner', hint, decisions: ['accept', 'request_rework'] } });
const completed = (): TaskAuthoring => ({ ...deciding(), task_status: 'Completed', step: 'completed', editable: false,
  next: { action: 'completed', label: 'Задача принята Owner', enabled: false, next_actor: '—', hint: 'Owner принял результат. Это решение Owner, а не VERIFIED.' } });
const ctx = async () => within(await screen.findByLabelText('Контекст и действия'));
const order = (a: Element, b: Element) => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING;

afterEach(() => { cleanup(); document.body.style.overflow = ''; vi.restoreAllMocks(); });

function setup(authoring: () => TaskAuthoring, run: RunView = reported(), over = {}) {
  at('/projects/P0001/tasks/T0001');
  let current = authoring();
  const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [task('T0001', 'P0001')]),
    listTaskRuns: vi.fn(async () => [run]), getRun: vi.fn(async () => run), getTaskAuthoring: vi.fn(async () => current), ...over });
  render(<App api={stub} />);
  return { stub, set: (next: () => TaskAuthoring) => { current = next(); } };
}

describe('P056 «Контекст и действия»: heading → short text → two actions → Report → collapsed Review', () => {
  it('renders the approved order with a status heading (not a button) and the actions before the details', async () => {
    setup(deciding);
    const panel = await ctx();
    const heading = await panel.findByRole('heading', { name: 'Ожидает решения Owner' });
    expect(panel.queryByRole('button', { name: 'Ожидает решения Owner' })).toBeNull();
    expect(panel.getByText('Следующий ход: Owner')).toBeTruthy();
    const text = panel.getByText(hint);
    const accept = panel.getByRole('button', { name: 'Принять результат' });
    const rework = panel.getByRole('button', { name: 'Вернуть на доработку' });
    const reportRow = await panel.findByTestId('run-report-ref');
    const review = panel.getByTestId('run-review');
    expect(order(heading, text) && order(text, accept) && order(accept, rework) && order(rework, reportRow) && order(reportRow, review)).toBeTruthy();
    expect(reportRow.textContent).toContain('Report: завершён · sha256 798c15da3ea9… · заявление Worker');
    expect(within(reportRow).getByRole('button', { name: 'Открыть Report' })).toBeTruthy();
    // collapsed by default: short summary only, no multi-paragraph text or limitation list visible
    expect(review.textContent).toContain('Architect Review пройден');
    expect(review.textContent).toContain('DeepSeek · v1');
    expect(within(review).getByTestId('run-review-short').textContent).toBe('Первый абзац: критерии выполнены, тесты проходят.');
    const more = within(review).getByRole('button', { name: 'Подробнее ⌄' });
    expect(more.getAttribute('aria-expanded')).toBe('false');
    expect((document.getElementById(more.getAttribute('aria-controls')!) as HTMLElement).hidden).toBe(true);
    fireEvent.click(more);
    const details = document.getElementById(more.getAttribute('aria-controls')!) as HTMLElement;
    expect(details.hidden).toBe(false);
    expect(details.textContent).toContain('Второй абзац');
    expect(within(details).getByRole('list', { name: 'Ограничения evidence' }).textContent).toContain('Тесты не перезапускались');
    expect(order(rework, details)).toBeTruthy(); // the actions stay above the details when expanded
    fireEvent.click(within(review).getByRole('button', { name: 'Свернуть ⌃' }));
    expect(details.hidden).toBe(true);
    // obsolete placeholders are gone
    expect(screen.queryByRole('button', { name: 'Принять задачу' })).toBeNull();
    expect(screen.queryByText(/Утверждение Draft не запускает Worker: Owner запускает Run/)).toBeNull();
  });

  it('«Принять результат» records the decision in Core and the panel follows Core (Completed)', async () => {
    const { stub, set } = setup(deciding);
    const panel = await ctx();
    fireEvent.click(await panel.findByRole('button', { name: 'Принять результат' }));
    set(completed);
    await waitFor(() => expect(stub.submitOwnerDecision).toHaveBeenCalledTimes(1));
    expect(stub.submitOwnerDecision.mock.calls[0]).toEqual(['P0001', 'T0001', RUN, { decision: 'accept', review_id: 'rv-1',
      review_version: 1, request_key: `owner-decision:${RUN}:accept`, reason: null }]);
    expect(await panel.findByRole('heading', { name: 'Задача принята Owner' })).toBeTruthy();
    expect(panel.queryByRole('button', { name: 'Принять результат' })).toBeNull();
    expect(screen.getAllByText('Task: Completed').length).toBeGreaterThan(0);
    expect(stub.launchRun).not.toHaveBeenCalled();
  });

  it('«Вернуть на доработку» needs a short reason; cancelling submits nothing', async () => {
    const { stub } = setup(deciding);
    const panel = await ctx();
    fireEvent.click(await panel.findByRole('button', { name: 'Вернуть на доработку' }));
    const form = panel.getByRole('form', { name: 'Вернуть на доработку' });
    const send = within(form).getByRole('button', { name: 'Отправить на доработку' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    expect((panel.getByRole('button', { name: 'Принять результат' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(within(form).getByRole('button', { name: 'Отмена' }));
    expect(panel.queryByRole('form', { name: 'Вернуть на доработку' })).toBeNull();
    expect(stub.submitOwnerDecision).not.toHaveBeenCalled();
    fireEvent.click(panel.getByRole('button', { name: 'Вернуть на доработку' }));
    fireEvent.change(panel.getByLabelText('Что нужно доработать'), { target: { value: '  Нет теста S02  ' } });
    fireEvent.click(panel.getByRole('button', { name: 'Отправить на доработку' }));
    await waitFor(() => expect(stub.submitOwnerDecision).toHaveBeenCalledTimes(1));
    expect(stub.submitOwnerDecision.mock.calls[0][3]).toMatchObject({ decision: 'request_rework', reason: 'Нет теста S02' });
  });

  it('a Core refusal is shown and nothing is faked locally', async () => {
    const { ApiError } = await import('../core/api');
    const refusal = vi.fn(async () => { throw new ApiError('The Architect Review changed since you opened it; reload the Task', 'conflict', 409, 'review_stale'); });
    setup(deciding, reported(), { submitOwnerDecision: refusal });
    const panel = await ctx();
    fireEvent.click(await panel.findByRole('button', { name: 'Принять результат' }));
    expect((await panel.findByRole('alert')).textContent).toContain('Review changed');
    expect(panel.getByRole('heading', { name: 'Ожидает решения Owner' })).toBeTruthy();
  });

  it('changes_requested offers only rework; awaiting review and inconclusive offer no decision', async () => {
    const changes: RunReview = { ...passed, verdict: 'changes_requested', findings: ['S02 падает'] };
    setup(() => ({ ...deciding(), next: { action: 'review_changes_requested', label: 'Architect запросил изменения — нужно решение Owner',
      enabled: true, next_actor: 'Owner', hint: 'Принять результат при таком Review нельзя.', decisions: ['request_rework'] } }), reported(changes));
    let panel = await ctx();
    expect(await panel.findByRole('button', { name: 'Вернуть на доработку' })).toBeTruthy();
    expect(panel.queryByRole('button', { name: 'Принять результат' })).toBeNull();
    cleanup();
    setup(() => ({ ...deciding(), next: { action: 'awaiting_review', label: 'Ожидает Architect Review', enabled: false, next_actor: 'Architect',
      hint: 'Отчёт Worker получен.' } }), reported(null));
    panel = await ctx();
    expect(await panel.findByRole('heading', { name: 'Ожидает Architect Review' })).toBeTruthy();
    expect(panel.queryByRole('button', { name: /Принять|Вернуть|Ожидает/ })).toBeNull();
  });

  it('rework state stays actionable with ordinary authoring and shows the Owner reason', async () => {
    setup(() => taskAuthoring({ task_status: 'Active', step: 'rework_needed',
      next: { action: 'update_prompt', label: 'Обновить Prompt Draft', enabled: true, next_actor: 'Owner', hint: 'Spec изменилась.',
        rework: { reason: 'Нет теста S02', decided_at: '2026-11-19T07:00:00Z', run_id: RUN } } }));
    const panel = await ctx();
    expect((await panel.findByTestId('rework-note')).textContent).toContain('Нет теста S02');
    expect((panel.getByRole('button', { name: 'Обновить Prompt Draft' }) as HTMLButtonElement).disabled).toBe(false);
  });
});
