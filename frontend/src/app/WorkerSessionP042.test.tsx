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
const workers = async () => within((await context()).getByLabelText('Workers проекта'));
const row = async (id: string) => {
  const list = await workers();
  const el = await waitFor(() => { const found = document.querySelector(`[data-worker-row="${id}"]`); expect(found).toBeTruthy(); return found as HTMLElement; });
  void list;
  return within(el);
};

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; vi.restoreAllMocks(); });

describe('Project Workers in the Task context (P042A r02)', () => {
  it('lists every Project Worker even with NO Prompt, starts nothing, keeps Run disabled', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getTaskAuthoring: vi.fn(async () => taskAuthoring()),
      listProjectWorkers: vi.fn(async () => [projectWorker('w-1', {}, 'claude'), projectWorker('w-2', {}, 'codex')]) });
    render(<App api={api} />);
    expect(await (await row('w-1')).findByText('@claude')).toBeTruthy();
    expect((await row('w-2')).getByText('@codex')).toBeTruthy();
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
    expect(api.bootstrapWorkerSession).not.toHaveBeenCalled();
    expect(api.activateTaskArchitect).not.toHaveBeenCalled();
    const run = (await context()).queryByRole('button', { name: 'Запустить Run' }) as HTMLButtonElement | null;
    expect(run === null || run.disabled).toBe(true);
  });

  it('keeps the same list after Prompt approval and has no shared Worker select', async () => {
    const api = open([projectWorker('w-1', {}, 'ivan'), projectWorker('w-2', {}, 'maria')]);
    render(<App api={api} />);
    expect(await (await row('w-1')).findByText('@ivan')).toBeTruthy();
    expect((await row('w-2')).getByText('@maria')).toBeTruthy();
    expect(screen.queryByLabelText(/Worker для T0001/)).toBeNull();
  });

  it('shows a configuration hint when no Worker is assigned', async () => {
    render(<App api={open([])} />);
    expect(await (await workers()).findByText(/нет назначенных Workers/)).toBeTruthy();
  });

  it('activates exactly the Worker whose row was clicked, concurrently with another, and never mixes results', async () => {
    const gates: Record<string, (v: ReturnType<typeof workerSession>) => void> = {};
    const api = open([projectWorker('w-1', {}, 'claude'), projectWorker('w-2', {}, 'codex')], {
      activateWorkerSession: vi.fn((_p: string, id: string) => new Promise<ReturnType<typeof workerSession>>(resolve => { gates[id] = resolve; })),
    });
    render(<App api={api} />);
    fireEvent.click((await row('w-1')).getByRole('button', { name: 'Активировать сессию' }));
    fireEvent.click((await row('w-2')).getByRole('button', { name: 'Активировать сессию' }));
    await waitFor(() => expect(api.activateWorkerSession).toHaveBeenCalledTimes(2));
    expect(api.activateWorkerSession).toHaveBeenCalledWith('P0001', 'w-1');
    expect(api.activateWorkerSession).toHaveBeenCalledWith('P0001', 'w-2');
    expect(await (await row('w-1')).findByRole('button', { name: 'Активация…' })).toBeTruthy();
    expect((await row('w-2')).getByRole('button', { name: 'Активация…' })).toBeTruthy();
    // answers arrive in the opposite order: each row takes only its own
    gates['w-2'](workerSession({ agent_id: 'w-2', state: 'active', reason: 'initialised' }));
    expect(await (await row('w-2')).findByText('CLI-сессия активна')).toBeTruthy();
    expect((await row('w-1')).queryByText('CLI-сессия активна')).toBeNull();
    expect((await row('w-1')).getByRole('button', { name: 'Активация…' })).toBeTruthy();
    gates['w-1'](workerSession({ agent_id: 'w-1', state: 'unknown', reason: 'no_ack' }));
    expect(await (await row('w-1')).findByText('Неизвестно')).toBeTruthy();
    expect((await row('w-2')).getByText('CLI-сессия активна')).toBeTruthy();
  });

  it('a second click on the same Worker while its request runs does nothing', async () => {
    let release: (v: ReturnType<typeof workerSession>) => void = () => undefined;
    const api = open([projectWorker('w-1')], { activateWorkerSession: vi.fn(() => new Promise<ReturnType<typeof workerSession>>(r => { release = r; })) });
    render(<App api={api} />);
    const button = (await row('w-1')).getByRole('button', { name: 'Активировать сессию' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(api.activateWorkerSession).toHaveBeenCalledTimes(1);
    release(workerSession({ agent_id: 'w-1', state: 'active', reason: 'initialised' }));
  });

  it('an activation failure stays inside its own row', async () => {
    const api = open([projectWorker('w-1', {}, 'claude'), projectWorker('w-2', {}, 'codex')], {
      activateWorkerSession: vi.fn(async () => { throw new Error('boom'); }) });
    render(<App api={api} />);
    fireEvent.click((await row('w-1')).getByRole('button', { name: 'Активировать сессию' }));
    expect(await (await row('w-1')).findByRole('alert')).toBeTruthy();
    expect((await row('w-2')).queryByRole('alert')).toBeNull();
  });

  it('check re-reads one Worker only and never activates or bootstraps', async () => {
    const api = open([projectWorker('w-1', { state: 'active', reason: 'initialised', can_activate: false })]);
    render(<App api={api} />);
    fireEvent.click((await row('w-1')).getByRole('button', { name: 'Проверить сессию' }));
    await waitFor(() => expect(api.getWorkerSession).toHaveBeenCalledWith('P0001', 'w-1'));
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
    expect(api.bootstrapWorkerSession).not.toHaveBeenCalled();
  });

  it.each([
    ['unknown', 'no_ack', 'Неизвестно', /не подтвердил инициализацию/],
    ['unavailable', 'tmux_missing', 'Недоступен', /tmux не установлен/],
    ['not_connected', 'vanished', 'Не на связи', /завершилась/],
    ['connecting', 'awaiting_ack', 'Подключение…', /ожидаем подтверждение/],
  ] as const)('shows %s/%s truthfully', async (state, reason, chip, note) => {
    const api = open([projectWorker('w-1', { state, reason, can_activate: state === 'not_connected' })]);
    render(<App api={api} />);
    const r = await row('w-1');
    expect((await r.findAllByText(chip)).length).toBeGreaterThan(0);
    expect(r.getByText(note)).toBeTruthy();
    expect(r.queryByText(/Контекст проекта подготовлен/)).toBeNull();
    const activate = r.queryByRole('button', { name: 'Активировать сессию' });
    expect(activate !== null).toBe(state === 'not_connected');
  });

  it('separates «CLI-сессия активна» from «контекст подготовлен» and never claims chat- or Run-readiness', async () => {
    let session = workerSession({ agent_id: 'w-1', state: 'active', reason: 'initialised',
      bootstrap: { state: 'none', reason: 'not_requested', can_bootstrap: true, sources: ['AGENTS.md'], confirmed_at: null } });
    const api = open([projectWorker('w-1')], {
      listProjectWorkers: vi.fn(async () => [{ ...projectWorker('w-1'), session }]),
      bootstrapWorkerSession: vi.fn(async () => { session = { ...session, bootstrap: { ...session.bootstrap, state: 'pending', reason: 'awaiting_worker', can_bootstrap: false } }; return session; }) });
    render(<App api={api} />);
    const r = await row('w-1');
    expect(await r.findByText('CLI-сессия активна')).toBeTruthy();
    expect(r.getByText(/Контекст проекта не подготовлен/)).toBeTruthy();
    expect(api.bootstrapWorkerSession).not.toHaveBeenCalled();
    fireEvent.click(r.getByRole('button', { name: 'Подготовить контекст' }));
    expect(await r.findByText(/Ожидаем подтверждение Worker/)).toBeTruthy();
    expect(api.bootstrapWorkerSession).toHaveBeenCalledWith('P0001', 'w-1');
    expect(r.queryByRole('button', { name: /Подготовить контекст|Повторить/ })).toBeNull();
    expect(document.body.textContent ?? '').not.toMatch(/READY|готов к диалогу|Готов к Run|Run выполняется|отправлено Worker/i);
  });

  it.each([
    ['confirmed', 'context_read', /Контекст проекта подготовлен: Worker прочитал AGENTS.md/, null],
    ['failed', 'reads_missing', /не зафиксировал чтение/, 'Повторить подготовку'],
    ['unknown', 'no_confirmation', /Повторно не отправляем/, null],
    ['stale', 'binding_changed', /изменились/, 'Подготовить контекст'],
  ] as const)('shows bootstrap %s honestly', async (state, reason, line, button) => {
    const api = open([projectWorker('w-1', { state: 'active', reason: 'initialised',
      bootstrap: { state, reason, can_bootstrap: button !== null, sources: ['AGENTS.md'], confirmed_at: null } })]);
    render(<App api={api} />);
    const r = await row('w-1');
    expect(await r.findByText(line)).toBeTruthy();
    expect(r.queryByRole('button', { name: button ?? /^$/ }) !== null).toBe(button !== null);
  });

  it('terminal help shows the server-provided command, copies it and offers nothing when unverifiable', async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    const command = "tmux -L rosc attach-session -t '=rosc-p0001-abcd1234'";
    const api = open([projectWorker('w-1', { state: 'unknown', reason: 'no_ack', terminal: { attach_command: command } }, 'claude'),
      projectWorker('w-2', { state: 'unknown', reason: 'process_mismatch' }, 'codex')]);
    render(<App api={api} />);
    const first = await row('w-1');
    fireEvent.click(await first.findByRole('button', { name: 'Как открыть терминал' }));
    expect(first.getByText(command)).toBeTruthy();
    expect(first.getByText(/подтвердите это в терминале/)).toBeTruthy();
    fireEvent.click(first.getByRole('button', { name: 'Копировать команду' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(command));
    expect(await first.findByText('Команда скопирована.')).toBeTruthy();
    const second = await row('w-2');
    fireEvent.click(second.getByRole('button', { name: 'Как открыть терминал' }));
    expect(second.getByText(/Команда подключения недоступна/)).toBeTruthy();
    expect(second.queryByRole('button', { name: 'Копировать команду' })).toBeNull();
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
  });

  it('shows no demo controls, no internal identifiers, and only a disabled addressee checkbox', async () => {
    const api = open([projectWorker('w-1', { state: 'active', reason: 'initialised' }, 'Мария')]);
    render(<App api={api} />);
    const r = await row('w-1');
    await r.findByText('CLI-сессия активна');
    const box = r.getByRole('checkbox') as HTMLInputElement;
    expect(box.disabled).toBe(true);
    expect(box.title).toMatch(/появится позже/);
    expect(r.getByText('@Мария')).toBeTruthy();
    expect(document.body.textContent ?? '').not.toMatch(/pane|incarnation|%\d|COLD|WARM|Симуляция|симуляция|только демо/);
  });
});
