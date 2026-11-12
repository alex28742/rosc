import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { PromptRevision, TaskAuthoring } from '../core/models';
import { project, projectWorker, stubApi, task, taskAuthoring, workerSession } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const approved: PromptRevision = {
  revision: 1, source_spec_revision: 1, summary: '1. x', body: 'Do', digest: 'p'.repeat(64), rules_ref: null, state: 'approved',
  actor: { kind: 'owner', id: 'local-owner' }, created_at: '2026-10-31T00:00:00Z',
  approval: { spec_revision: 1, prompt_revision: 1, digest: 'p'.repeat(64), actor: { kind: 'owner', id: 'local-owner' }, approved_at: '2026-10-31T00:00:00Z' } as unknown as PromptRevision['approval'],
};
const authoring = (): TaskAuthoring => taskAuthoring({ prompt: approved, prompt_history: [approved],
  next: { action: 'authorize_run', label: 'Запустить Run', enabled: false, next_actor: 'Owner', hint: 'Запуск Run станет доступен после подключения Worker и передачи задания (P042)', secondary: 'Просмотреть утверждённый Draft' } });
const open = (workers: ReturnType<typeof projectWorker>[], extra = {}) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getTaskAuthoring: vi.fn(async () => authoring()),
  listProjectWorkers: vi.fn(async () => workers), ...extra });
const context = async () => within(await screen.findByLabelText('Контекст и действия'));
const card = async () => within((await context()).getByLabelText('Worker этой Task'));
const choose = async (id: string) => {
  const ctx = await context();
  const select = await waitFor(() => {
    const el = ctx.getByLabelText('Worker для T0001') as HTMLSelectElement;
    expect(el.options.length).toBeGreaterThan(1);
    return el;
  });
  fireEvent.change(select, { target: { value: id } });
};

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Worker CLI session in the Task context (P042A)', () => {
  it('lists Project Workers without starting anything and keeps Run disabled', async () => {
    const api = open([projectWorker('w-1')]);
    render(<App api={api} />);
    expect(await (await card()).findByText('Не выбран')).toBeTruthy();
    expect(((await card()).getByRole('button', { name: 'Активировать сессию' }) as HTMLButtonElement).disabled).toBe(true);
    await choose('w-1');
    expect(await (await card()).findByText('Нет сессии')).toBeTruthy();
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
    expect(((await context()).getByRole('button', { name: 'Запустить Run' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.activateTaskArchitect).not.toHaveBeenCalled();
  });

  it('activates only on the explicit click and shows «CLI-сессия активна», never READY', async () => {
    let state = workerSession({ agent_id: 'w-1' });
    const api = open([projectWorker('w-1')], {
      listProjectWorkers: vi.fn(async () => [{ ...projectWorker('w-1'), session: state }]),
      activateWorkerSession: vi.fn(async () => { state = workerSession({ agent_id: 'w-1', state: 'active', reason: 'initialised', can_activate: false, initialised_at: '2026-11-12T00:00:01Z' }); return state; }),
    });
    render(<App api={api} />);
    await choose('w-1');
    fireEvent.click(await (await card()).findByRole('button', { name: 'Активировать сессию' }));
    expect(await (await card()).findByText('CLI-сессия активна')).toBeTruthy();
    expect(api.activateWorkerSession).toHaveBeenCalledTimes(1);
    expect(api.activateWorkerSession).toHaveBeenCalledWith('P0001', 'w-1');
    expect(screen.queryByText(/READY/)).toBeNull();
    expect(await (await card()).findByRole('button', { name: 'Проверить сессию' })).toBeTruthy();
    expect(((await context()).getByRole('button', { name: 'Запустить Run' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('check re-reads status only and never activates', async () => {
    const api = open([projectWorker('w-1', { state: 'active', reason: 'initialised', can_activate: false })]);
    render(<App api={api} />);
    await choose('w-1');
    fireEvent.click(await (await card()).findByRole('button', { name: 'Проверить сессию' }));
    await waitFor(() => expect(api.getWorkerSession).toHaveBeenCalledWith('P0001', 'w-1'));
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
  });

  it.each([
    ['unknown', 'no_ack', 'Неизвестно', /не подтвердил инициализацию/],
    ['unavailable', 'tmux_missing', 'Недоступен', /tmux не установлен/],
    ['not_connected', 'vanished', 'Не на связи', /завершилась/],
    ['connecting', 'awaiting_ack', 'Подключение…', /ожидаем подтверждение/],
  ] as const)('shows %s/%s truthfully', async (state, reason, chip, note) => {
    const api = open([projectWorker('w-1', { state, reason, can_activate: state === 'not_connected' })]);
    render(<App api={api} />);
    await choose('w-1');
    const c = await card();
    expect((await c.findAllByText(chip)).length).toBeGreaterThan(0);
    expect(c.getByText(note)).toBeTruthy();
    if (state === 'unknown' || state === 'unavailable' || state === 'connecting') {
      expect(c.queryByRole('button', { name: 'Активировать сессию' }) == null || (c.getByRole('button', { name: /Активировать сессию|Подключение/ }) as HTMLButtonElement).disabled).toBe(true);
    } else {
      expect((c.getByRole('button', { name: 'Активировать сессию' }) as HTMLButtonElement).disabled).toBe(false);
    }
  });

  it('shows an activation failure inside the card', async () => {
    const api = open([projectWorker('w-1')], { activateWorkerSession: vi.fn(async () => { throw new Error('boom'); }) });
    render(<App api={api} />);
    await choose('w-1');
    fireEvent.click(await (await card()).findByRole('button', { name: 'Активировать сессию' }));
    expect(await (await card()).findByRole('alert')).toBeTruthy();
  });

  it('exposes no tmux, pane or process identifier and no prototype demo controls', async () => {
    const api = open([projectWorker('w-1', { state: 'active', reason: 'initialised' })]);
    render(<App api={api} />);
    await choose('w-1');
    await (await card()).findByText('CLI-сессия активна');
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/tmux-сессия|pane|incarnation|%\d|COLD|WARM|Симуляция|симуляция/);
  });
});
