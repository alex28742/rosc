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

  it('reports inactive Tools of a live ready session as a Tools warning, never as a broken native session', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      getArchitect: vi.fn(async () => architectStatus({
        state: 'ready', can_activate: false, can_deactivate: true,
        session: { session_id: 'session-1', state: 'ready', verified_at: null },
        tools: { state: 'inactive', detail: 'tools_unavailable' },
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

describe('Engine popups: Tools warning survives leaving and reopening the Project', () => {
  const outage = () => stubApi({
    listProjects: vi.fn(async () => [P1]),
    listTasks: vi.fn(async () => []),
    getArchitect: vi.fn(async () => readyArchitect),
    getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'inactive', names: ['rosc_briefing'] } })),
  });

  it('shows the still-current warning again after Project -> Dashboard -> Project', async () => {
    at('/projects/P0001');
    render(<App api={outage()} />);
    expect(await screen.findByText('Инструменты временно недоступны')).toBeTruthy();

    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    await waitFor(() => expect(screen.queryByText('Инструменты временно недоступны')).toBeNull());
    fireEvent.click(await screen.findByText('Project P0001'));

    expect(await screen.findByText('Инструменты временно недоступны')).toBeTruthy();
    expect(screen.getAllByText('Инструменты временно недоступны')).toHaveLength(1);
    expect(screen.queryByText('Инструменты снова доступны')).toBeNull();
  });

  it('does not bring back a warning the Owner closed by hand during the same outage', async () => {
    at('/projects/P0001');
    render(<App api={outage()} />);
    expect(await screen.findByText('Инструменты временно недоступны')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Закрыть уведомление'));

    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(await screen.findByText('Project P0001'));
    await screen.findByLabelText('Project Work Area');
    expect(screen.queryByText('Инструменты временно недоступны')).toBeNull();
  });
});

describe('Engine popups: Architect onboarding (P029)', () => {
  const onboarded = (state: 'initializing' | 'ready' | 'blocked' | 'unknown' | 'legacy_unverified', detail: string | null = null) =>
    architectStatus({
      state: 'ready', can_activate: false, can_deactivate: true,
      session: { session_id: 'session-1', state: 'ready', verified_at: '2026-11-08T10:00:00Z' },
      tools: { state: 'active', detail: null }, onboarding: { state, detail },
    });
  const apiFor = (getArchitect: () => Promise<ReturnType<typeof onboarded>>) => stubApi({
    listProjects: vi.fn(async () => [P1]),
    listTasks: vi.fn(async () => []),
    getArchitect: vi.fn(getArchitect),
    getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'active', names: ['rosc_briefing'] } })),
  });

  it('shows real progress, then one readiness card only after the onboarding was observed', async () => {
    vi.useFakeTimers();
    let state: 'initializing' | 'ready' = 'initializing';
    at('/projects/P0001');
    render(<App api={apiFor(async () => onboarded(state))} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const card = screen.getByText('Architect знакомится с проектом').closest('.rosc-engine-notice');
    expect(card?.classList.contains('rosc-engine-notice--progress')).toBe(true);

    state = 'ready';
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(screen.queryByText('Architect знакомится с проектом')).toBeNull();
    expect(screen.getAllByText('Architect готов к работе')).toHaveLength(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(6600); });
    expect(screen.queryByText('Architect готов к работе')).toBeNull();
  });

  it('does not announce readiness (or anything) when an onboarded or legacy Project is merely opened', async () => {
    for (const state of ['ready', 'legacy_unverified'] as const) {
      at('/projects/P0001');
      const { container, unmount } = render(<App api={apiFor(async () => onboarded(state))} />);
      await screen.findByLabelText('Project Work Area');
      expect(container.querySelector('.rosc-engine-notice-stack')).toBeNull();
      unmount();
    }
  });

  it('reports blocked and unknown outcomes honestly, never as readiness, and respects manual dismiss', async () => {
    at('/projects/P0001');
    render(<App api={apiFor(async () => onboarded('blocked', 'evidence_missing'))} />);
    const card = (await screen.findByText('Знакомство Architect не подтверждено')).closest('.rosc-engine-notice');
    expect(card?.getAttribute('role')).toBe('alert');
    expect(screen.queryByText('Architect готов к работе')).toBeNull();
    fireEvent.click(screen.getByLabelText('Закрыть уведомление'));
    expect(screen.queryByText('Знакомство Architect не подтверждено')).toBeNull();

    cleanup();
    at('/projects/P0001');
    render(<App api={apiFor(async () => onboarded('unknown', 'interrupted'))} />);
    expect(await screen.findByText('Итог знакомства Architect неизвестен')).toBeTruthy();
    expect(screen.queryByText('Architect готов к работе')).toBeNull();
  });
});
