import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { architectStatus, project, projectWorker, stubApi, task, workerSession } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const card = () => screen.findByRole('region', { name: 'Агенты проекта' });
const api = (workers: ReturnType<typeof projectWorker>[], extra = {}) => stubApi({
  listProjects: vi.fn(async () => [project('P0001')]), listTasks: vi.fn(async () => [task('T0001', 'P0001')]),
  listProjectWorkers: vi.fn(async () => workers), ...extra,
});

afterEach(() => { cleanup(); document.body.style.overflow = ''; });
beforeEach(() => at('/projects/P0001/agents'));  // P048: card content lives in the «Агенты» tab

describe('P047 Project Work Area Workers', () => {
  it('offers the Task per-Worker lifecycle controls in the Project card without visiting a Task', async () => {
    const stub = api([projectWorker('w-1', { state: 'not_connected', reason: 'never_activated' }, 'Claude')], {
      activateWorkerSession: vi.fn(async () => workerSession({ agent_id: 'w-1', state: 'active', reason: 'initialised' })),
    });
    render(<App api={stub} />);
    const agents = await card();
    const row = within(agents).getByText('@Claude').closest('li') as HTMLElement;
    expect(stub.activateWorkerSession).not.toHaveBeenCalled();  // reading never starts a Worker
    fireEvent.click(within(row).getByRole('button', { name: 'Активировать сессию' }));
    await waitFor(() => expect(stub.activateWorkerSession).toHaveBeenCalledWith('P0001', 'w-1'));
    expect(await within(row).findByText('CLI-сессия активна')).toBeTruthy();
    expect(within(agents).getByText('Worker CLI 1/1 активен')).toBeTruthy();
  });

  it('shows a verified 1/1 Worker and a verified Architect as green, never "не активен/0"', async () => {
    const stub = api([projectWorker('w-1', { state: 'active', reason: 'initialised' }, 'Claude')], {
      getArchitectStatus: vi.fn(async () => architectStatus({ state: 'ready', can_deactivate: true })),
    });
    render(<App api={stub} />);
    await card();
    const chips = await screen.findAllByText('Worker CLI 1/1 активен');
    const header = chips.map(c => c.closest('.rosc-chip')).find(Boolean) as HTMLElement;
    expect(header.className).toContain('rosc-chip--on');
    await waitFor(() => expect(document.querySelector('.rosc-chip--on')?.textContent).toMatch(/Architect|Worker/));
    expect(screen.queryByText(/Worker: не активен/)).toBeNull();
    expect(screen.getByText(/НЕ требует активации tmux/)).toBeTruthy();  // chat does not depend on the tmux CLI
  });

  it('reports an unknown Worker state honestly while the read fails, not as 0/inactive', async () => {
    const stub = api([], { listProjectWorkers: vi.fn(async () => { throw new Error('down'); }) });
    render(<App api={stub} />);
    await card();
    expect((await screen.findAllByText('Worker CLI: неизвестно')).length).toBeGreaterThan(0);
  });
});
