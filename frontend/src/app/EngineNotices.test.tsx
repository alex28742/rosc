import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import { App } from './App';
import { architectStatus, contextAccess, project, stubApi } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const readyArchitect = architectStatus({
  state: 'ready', can_activate: false, can_deactivate: true,
  session: { session_id: 'session-1', state: 'ready', verified_at: '2026-11-08T10:00:00Z' },
});

beforeEach(() => at('/'));
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('Engine popups: Core connectivity', () => {
  it('reports a real connection failure and replaces it with one short recovery notice', async () => {
    const listProjects = vi.fn()
      .mockRejectedValueOnce(new ApiError('Не удалось связаться с ядром ROSC.', 'network', null))
      .mockResolvedValue([P1]);
    render(<App api={stubApi({ listProjects })} />);

    expect(await screen.findByText('Нет связи с ROSC Core')).toBeTruthy();
    expect(screen.getByText('Нет связи с ROSC Core').closest('.rosc-engine-notice')?.getAttribute('role')).toBe('alert');

    fireEvent.click(await screen.findByText('Повторить'));

    expect(await screen.findByText('Связь с ROSC Core восстановлена')).toBeTruthy();
    expect(screen.queryByText('Нет связи с ROSC Core')).toBeNull();
    expect(screen.getByText('Связь с ROSC Core восстановлена').closest('.rosc-engine-notice')?.getAttribute('role')).toBe('status');
  });

  it('does not treat an ordinary 4xx business error as an Engine outage', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => { throw new ApiError('HTTP 404', 'client', 404); }) });
    render(<App api={api} />);

    expect(await screen.findByText('Не удалось загрузить проекты.')).toBeTruthy();
    expect(screen.queryByText('Нет связи с ROSC Core')).toBeNull();
  });

  it('keeps one card for a repeated connection failure instead of stacking toasts', async () => {
    const listProjects = vi.fn(async () => { throw new ApiError('down', 'network', null); });
    render(<App api={stubApi({ listProjects })} />);

    expect(await screen.findByText('Нет связи с ROSC Core')).toBeTruthy();
    fireEvent.click(await screen.findByText('Повторить'));
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText('Нет связи с ROSC Core')).toHaveLength(1);
  });

  it('lets the Owner close a persistent Engine error card', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => { throw new ApiError('down', 'network', null); }) });
    render(<App api={api} />);

    expect(await screen.findByText('Нет связи с ROSC Core')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Закрыть уведомление'));
    expect(screen.queryByText('Нет связи с ROSC Core')).toBeNull();
  });

  it('auto-dismisses the recovery notice after the approved delay', async () => {
    vi.useFakeTimers();
    const listProjects = vi.fn()
      .mockRejectedValueOnce(new ApiError('down', 'network', null))
      .mockResolvedValue([P1]);
    render(<App api={stubApi({ listProjects })} />);

    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Нет связи с ROSC Core')).toBeTruthy();
    fireEvent.click(screen.getByText('Повторить'));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Связь с ROSC Core восстановлена')).toBeTruthy();

    await act(async () => { await vi.advanceTimersByTimeAsync(6500); });
    expect(screen.queryByText('Связь с ROSC Core восстановлена')).toBeNull();
  });
});

describe('Engine popups: Architect tools and session', () => {
  it('warns about unavailable Tools only for a confirmed live session and replaces it with recovery', async () => {
    let transport: 'inactive' | 'active' = 'inactive';
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listTasks: vi.fn(async () => []),
      getArchitect: vi.fn(async () => readyArchitect),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport, names: ['rosc_briefing'] } })),
    });
    at('/projects/P0001');
    render(<App api={api} />);

    expect(await screen.findByText('Инструменты временно недоступны')).toBeTruthy();
    expect(screen.queryByText('Сессия Architect не отвечает')).toBeNull();

    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    transport = 'active';
    fireEvent.click(await screen.findByText('Project P0001'));

    expect(await screen.findByText('Инструменты снова доступны')).toBeTruthy();
    expect(screen.queryByText('Инструменты временно недоступны')).toBeNull();
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('reports the P028 tools_unavailable state as a Tools warning, never as a broken native session', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      getArchitect: vi.fn(async () => architectStatus({
        state: 'error', detail: 'tools_unavailable', can_activate: false, can_deactivate: true,
        session: { session_id: 'session-1', state: 'ready', verified_at: null },
      })),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'inactive', names: [] } })),
    });
    at('/projects/P0001');
    render(<App api={api} />);

    expect(await screen.findByText('Инструменты временно недоступны')).toBeTruthy();
    expect(screen.queryByText('Сессия Architect не отвечает')).toBeNull();
  });

  it('does not call a runtime without Tool support a temporary MCP outage', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      getArchitect: vi.fn(async () => readyArchitect),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'not_available', names: [] } })),
    });
    at('/projects/P0001');
    render(<App api={api} />);

    expect(await screen.findByText(/Runtime не поддерживает ROSC Tools/)).toBeTruthy();
    expect(screen.queryByText('Инструменты временно недоступны')).toBeNull();
  });

  it('shows a separate native-session error only from a confirmed Core error state', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      getArchitect: vi.fn(async () => architectStatus({
        state: 'error', detail: 'runtime_not_alive', can_activate: true, can_deactivate: false,
        session: { session_id: 'session-1', state: 'error', verified_at: null },
      })),
      getContextAccess: vi.fn(async () => contextAccess()),
    });
    at('/projects/P0001');
    render(<App api={api} />);

    expect(await screen.findByText('Сессия Architect не отвечает')).toBeTruthy();
    expect(screen.getByText('Сессия Architect не отвечает').closest('.rosc-engine-notice')?.getAttribute('role')).toBe('alert');
  });

  it('does not greet a normal initial ready state with success cards', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      getArchitect: vi.fn(async () => readyArchitect),
      getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'active', names: ['rosc_briefing'] } })),
    });
    at('/projects/P0001');
    const { container } = render(<App api={api} />);

    await screen.findByLabelText('Project Work Area');
    expect(container.querySelector('.rosc-engine-notice-stack')).toBeNull();
  });
});
