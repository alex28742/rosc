import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ArchitectStatus, OnboardingStatus } from '../core/models';
import { App } from './App';
import { architectStatus, contextAccess, project, stubApi } from './testing';

// P053: the compact onboarding row inside the existing Project Architect block of the «Агенты» tab.
const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');

const steps = (overrides: Record<string, string> = {}) => [
  { id: 'briefing', tool: 'rosc_briefing', path: null },
  { id: 'role', tool: 'rosc_global_read', path: 'agents/architect/rules/role.md' },
  { id: 'onboarding_rules', tool: 'rosc_global_read', path: 'agents/architect/rules/onboarding.md' },
  { id: 'docs_entrypoint', tool: 'rosc_source_read', path: 'README.md' },
].map(s => ({ ...s, state: (overrides[s.id] ?? 'done') as 'done', code: null }));

const withOnboarding = (onboarding: OnboardingStatus): ArchitectStatus => architectStatus({
  state: 'ready', can_activate: false, can_deactivate: true,
  session: { session_id: 'session-1', state: 'ready', verified_at: '2026-11-16T10:00:00Z' },
  tools: { state: 'active', detail: null }, onboarding,
});

const blocked = withOnboarding({
  state: 'blocked', detail: 'missing_evidence', attempt: 1, final_status: 'ready',
  steps: steps({ onboarding_rules: 'missing', docs_entrypoint: 'missing' }), can_check: true, can_complete: true,
});

const apiWith = (over: Record<string, unknown>) => stubApi({
  listProjects: vi.fn(async () => [P1]),
  listTasks: vi.fn(async () => []),
  getContextAccess: vi.fn(async () => contextAccess({ tools: { transport: 'active', names: ['rosc_briefing'] } })),
  ...over,
});

const row = async () => screen.findByLabelText('Знакомство Architect с проектом');

beforeEach(() => at('/projects/P0001/agents'));
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('P053 onboarding readiness row', () => {
  it('shows the Core reason and each required read, and sends nothing on open or polling', async () => {
    const api = apiWith({ getArchitect: vi.fn(async () => blocked) });
    render(<App api={api} />);
    const box = await row();
    expect(within(box).getByText('Знакомство: не подтверждено')).toBeTruthy();
    expect(within(box).getByText(/не подтвердил все обязательные чтения/)).toBeTruthy();
    expect(within(box).getByText('Протокол знакомства (agents/architect/rules/onboarding.md) — не подтверждено')).toBeTruthy();
    expect(within(box).getByText('Правила роли (agents/architect/rules/role.md) — прочитано')).toBeTruthy();
    expect(api.checkArchitectOnboarding).not.toHaveBeenCalled();
    expect(api.completeArchitectOnboarding).not.toHaveBeenCalled();
  });

  it('«Проверить» calls only the read-only check and shows its answer', async () => {
    const api = apiWith({
      getArchitect: vi.fn(async () => blocked),
      checkArchitectOnboarding: vi.fn(async () => withOnboarding({ ...blocked.onboarding, check: 'busy' })),
    });
    render(<App api={api} />);
    fireEvent.click(within(await row()).getByRole('button', { name: 'Проверить' }));
    expect(await screen.findByText(/показаны сохранённые данные/)).toBeTruthy();
    expect(api.checkArchitectOnboarding).toHaveBeenCalledTimes(1);
    expect(api.completeArchitectOnboarding).not.toHaveBeenCalled();
  });

  it('«Завершить знакомство» calls Core once, then follows the real progress to the result', async () => {
    vi.useFakeTimers();
    let current: ArchitectStatus = blocked;
    let release: (value: ArchitectStatus) => void = () => undefined;
    const api = apiWith({
      getArchitect: vi.fn(async () => current),
      completeArchitectOnboarding: vi.fn(() => new Promise<ArchitectStatus>(resolve => { release = resolve; })),
    });
    render(<App api={api} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const button = within(screen.getByLabelText('Знакомство Architect с проектом'))
      .getByRole('button', { name: 'Завершить знакомство' });
    fireEvent.click(button);
    fireEvent.click(button);  // a second click while the request runs sends nothing
    expect(api.completeArchitectOnboarding).toHaveBeenCalledTimes(1);
    current = withOnboarding({ ...blocked.onboarding, state: 'initializing', detail: 'completing', can_complete: false,
      steps: steps({ onboarding_rules: 'pending', docs_entrypoint: 'pending' }) });
    await act(async () => { release(current); await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('Знакомство: выполняется…')).toBeTruthy();
    current = withOnboarding({ state: 'ready', detail: null, attempt: 2, final_status: 'ready', steps: steps(),
      can_check: true, can_complete: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    expect(screen.getByText('Знакомство: подтверждено')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Завершить знакомство' })).toBeNull();
    expect(api.completeArchitectOnboarding).toHaveBeenCalledTimes(1);
  });

  it('shows a refused completion without blocking the conversation', async () => {
    const api = apiWith({
      getArchitect: vi.fn(async () => blocked),
      completeArchitectOnboarding: vi.fn(async () => { throw new Error('ROSC Tools are not connected to this Architect session'); }),
    });
    render(<App api={api} />);
    fireEvent.click(within(await row()).getByRole('button', { name: 'Завершить знакомство' }));
    expect(await within(await row()).findByRole('alert')).toBeTruthy();
    at('/projects/P0001');
    cleanup();
    render(<App api={api} />);
    const composer = await screen.findByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement;
    expect(composer.disabled).toBe(false);
  });
});
