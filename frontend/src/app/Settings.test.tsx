import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import type { RoleAssignment } from '../core/models';
import { App } from './App';
import { agentDefinition, architectStatus, deferred, project, roleAssignment, stubApi } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001', { name: 'Platform' });
const P2 = project('P0002', { name: 'Lab' });

const architectCaps = ['project.context.read', 'architect.document.read', 'architect.document.write'];
const workerCaps = ['project.context.read', 'task.context.read'];

const architectAgent = (id = 'a1', name = 'Architect Agent') =>
  agentDefinition(id, { display_name: name });
const workerAgent = (id = 'w1', name = 'Worker Agent') =>
  agentDefinition(id, {
    display_name: name,
    capabilities: workerCaps.map(capability => ({ capability, status: 'declared' as const })),
  });

beforeEach(() => at('/'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Settings route and loading', () => {
  it('opens /settings/agents directly, from the header, and closes to the Dashboard', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listAgents: vi.fn(async () => [architectAgent()]) });
    at('/settings/agents');
    render(<App api={api} />);
    expect(await screen.findByLabelText('Настройки ROSC')).toBeTruthy();
    expect(await screen.findByText('Architect Agent')).toBeTruthy();

    fireEvent.click(screen.getByLabelText('Закрыть настройки'));
    expect(location.pathname).toBe('/');
    expect(screen.queryByLabelText('Настройки ROSC')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Настройки' }));
    expect(await screen.findByLabelText('Настройки ROSC')).toBeTruthy();
    expect(location.pathname).toBe('/settings/agents');
  });

  it('follows browser Back and Forward between Settings and a Project without agent activation', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listAgents: vi.fn(async () => []) });
    render(<App api={api} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Настройки' }));
    await screen.findByLabelText('Настройки ROSC');
    fireEvent.click(screen.getByLabelText('Закрыть настройки'));
    fireEvent.click(await screen.findByText('Platform'));
    expect(await screen.findByLabelText('Project Work Area')).toBeTruthy();

    act(() => history.back());
    await waitFor(() => expect(location.pathname).toBe('/'));
    act(() => history.back());
    await waitFor(() => expect(location.pathname).toBe('/settings/agents'));
    expect(await screen.findByLabelText('Настройки ROSC')).toBeTruthy();
    act(() => history.forward());
    await waitFor(() => expect(location.pathname).toBe('/'));
    act(() => history.forward());
    await waitFor(() => expect(location.pathname).toBe('/projects/P0001'));

    expect(api.activateArchitect).not.toHaveBeenCalled();
    expect(api.deactivateArchitect).not.toHaveBeenCalled();
  });

  it('shows a real load failure with Retry and recovers without inventing rows', async () => {
    const listAgents = vi.fn()
      .mockRejectedValueOnce(new ApiError('backend down', 'network', null))
      .mockResolvedValue([architectAgent('a1', 'Recovered Agent')]);
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listAgents });
    at('/settings/agents');
    render(<App api={api} />);
    expect(await screen.findByText('Не удалось загрузить Agent Registry.')).toBeTruthy();
    expect(screen.queryByText('Recovered Agent')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(await screen.findByText('Recovered Agent')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('Agent Registry mutations', () => {
  it('registers the full valid payload, then shows a server-confirmed notice', async () => {
    const registered = agentDefinition('a2', { display_name: 'New Agent' });
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn().mockResolvedValueOnce([]).mockResolvedValue([registered]),
      registerAgent: vi.fn(async () => registered),
    });
    at('/settings/agents');
    render(<App api={api} />);
    await screen.findByText('Нет Agent Definitions.');
    fireEvent.click(screen.getByRole('button', { name: '＋ Добавить' }));
    fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: 'New Agent' } });
    fireEvent.change(screen.getByLabelText('Provider ID'), { target: { value: 'deepseek' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'deepseek-v4.1-flash' } });
    fireEvent.change(screen.getByLabelText('Набор declared capabilities'), { target: { value: 'both' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Зарегистрировать' }).closest('form')!);

    expect(api.registerAgent).toHaveBeenCalledWith({
      display_name: 'New Agent', icon_label: 'robot', runtime_adapter: 'opencode',
      provider_id: 'deepseek', model_id: 'deepseek-v4.1-flash',
      capabilities: ['project.context.read', 'architect.document.read', 'architect.document.write', 'task.context.read'],
    });
    expect((await screen.findByRole('status')).textContent).toBe('Agent Definition зарегистрирован. Runtime не проверялся.');
    expect(await screen.findByText('New Agent')).toBeTruthy();
  });

  it('edits with the loaded expected_version and never claims a connected runtime', async () => {
    const current = agentDefinition('a1', { display_name: 'Old Name', version: 3 });
    const updated = { ...current, display_name: 'New Name', version: 4 };
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn().mockResolvedValueOnce([current]).mockResolvedValue([updated]),
      updateAgent: vi.fn(async () => updated),
    });
    at('/settings/agents');
    render(<App api={api} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Изменить Old Name' }));
    fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: 'New Name' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Сохранить изменения' }).closest('form')!);

    expect(api.updateAgent).toHaveBeenCalledWith('a1', {
      display_name: 'New Name', icon_label: 'robot', runtime_adapter: 'opencode', provider_id: 'prov', model_id: 'model-a',
      capabilities: [...architectCaps], expected_version: 3,
    });
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.queryByText(/Подключен/)).toBeNull();
  });

  it('keeps the draft after a 409 and never shows a success notice', async () => {
    const current = agentDefinition('a1', { display_name: 'Old Name', version: 2 });
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [current]),
      updateAgent: vi.fn(async () => { throw new ApiError('Agent was changed by another operation; reload and retry', 'conflict', 409); }),
    });
    at('/settings/agents');
    render(<App api={api} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Изменить Old Name' }));
    fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: 'Draft Name' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Сохранить изменения' }).closest('form')!);

    await waitFor(() => expect(screen.getAllByRole('alert').some(node => node.textContent?.includes('Agent was changed'))).toBe(true));
    expect((screen.getByLabelText('Отображаемое имя') as HTMLInputElement).value).toBe('Draft Name');
    expect(screen.queryByText('Agent Definition сохранён. Runtime не проверялся.')).toBeNull();
  });

  it('rejects an invalid or credential-like draft before calling the API', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listAgents: vi.fn(async () => []) });
    at('/settings/agents');
    render(<App api={api} />);
    await screen.findByText('Нет Agent Definitions.');
    fireEvent.click(screen.getByRole('button', { name: '＋ Добавить' }));
    fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: 'Agent' } });
    fireEvent.change(screen.getByLabelText('Provider ID'), { target: { value: 'sk-secret-token' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'model-a' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Зарегистрировать' }).closest('form')!);

    expect(api.registerAgent).not.toHaveBeenCalled();
    expect(screen.getByRole('alert').textContent).toContain('короткие идентификаторы');
  });
});

describe('Project assignments', () => {
  const openSettings = async (api: ReturnType<typeof stubApi>, path = '/projects/P0001') => {
    at(path);
    render(<App api={api} />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Настройки проекта' }));
    await screen.findByText('Агенты проекта · назначения');
  };

  it('enters from the left tab, keeps the Project Conversation, and creates an eligible Worker', async () => {
    let current: RoleAssignment[] = [];
    const created = roleAssignment('as-1', { agent_id: 'w1' });
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments: vi.fn(async () => current),
      createAssignment: vi.fn(async () => { current = [created]; return created; }),
    });
    await openSettings(api);
    expect(location.pathname).toBe('/projects/P0001/settings');
    expect(screen.getByRole('heading', { name: 'Project Conversation' })).toBeTruthy();

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));
    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledWith('P0001', {
      agent_id: 'w1', role: 'worker', task_id: null, capabilities: [...workerCaps],
    }));
    expect((await screen.findByRole('status')).textContent).toBe('Назначения сохранены.');
    for (const call of [api.activateArchitect, api.deactivateArchitect]) expect(call).not.toHaveBeenCalled();
  });

  it('treats an unchanged save as a no-op and never claims a mutation', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments: vi.fn(async () => [roleAssignment('as-1', { agent_id: 'w1' })]),
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));
    expect((await screen.findByRole('status')).textContent).toBe('Назначения не изменились.');
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
  });

  it('removes a Worker with its expected_version and keeps a blocked current Worker removable', async () => {
    let current: RoleAssignment[] = [roleAssignment('as-1', { agent_id: 'w1', readiness: 'blocked', version: 4 })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments: vi.fn(async () => current),
      revokeAssignment: vi.fn(async () => { current = []; return roleAssignment('as-1', { state: 'revoked', version: 5 }); }),
    });
    await openSettings(api);
    const box = screen.getByRole('checkbox') as HTMLInputElement;
    expect(box.disabled).toBe(false);
    expect(box.checked).toBe(true);
    fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));
    await waitFor(() => expect(api.revokeAssignment).toHaveBeenCalledWith('P0001', 'as-1', 4));
    expect(await screen.findByRole('status')).toBeTruthy();
  });

  it('requires explicit confirmation before replacing the current Architect and then mutates sequentially', async () => {
    let current: RoleAssignment[] = [roleAssignment('arch-1', { agent_id: 'a1', role: 'architect', capabilities: [...architectCaps] })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1', 'Architect One'), architectAgent('a2', 'Architect Two')]),
      listAssignments: vi.fn(async () => current),
      revokeAssignment: vi.fn(async () => { current = []; return roleAssignment('arch-1', { state: 'revoked', version: 2 }); }),
      createAssignment: vi.fn(async (_projectId: string, input: { agent_id: string; role: 'architect' | 'worker'; capabilities: string[] }) => {
        const made = roleAssignment('arch-2', { agent_id: input.agent_id, role: input.role, capabilities: input.capabilities });
        current = [made];
        return made;
      }),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a2' } });
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));

    expect(screen.getByText('Подтвердить замену Architect?')).toBeTruthy();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(api.createAssignment).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }));
    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledTimes(1));
    expect(api.revokeAssignment).toHaveBeenCalledWith('P0001', 'arch-1', 1);
    expect(api.createAssignment).toHaveBeenCalledWith('P0001', {
      agent_id: 'a2', role: 'architect', task_id: null, capabilities: [...architectCaps],
    });
    expect(api.activateArchitect).not.toHaveBeenCalled();
    expect(api.deactivateArchitect).not.toHaveBeenCalled();
  });

  it('blocks an Architect change when the read-only status is active or unavailable', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1')]),
      listAssignments: vi.fn(async () => []),
      getArchitect: vi.fn(async () => architectStatus({ state: 'ready', can_activate: false, can_deactivate: true })),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a1' } });
    expect(screen.getByText(/Architect не подтверждённо остановлен/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Сохранить назначения' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.createAssignment).not.toHaveBeenCalled();
  });

  it('reports a partial failure truthfully, reloads, and never rolls back or retries', async () => {
    const w2 = workerAgent('w2', 'Worker Two');
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), w2]),
      listAssignments: vi.fn(async () => []),
      createAssignment: vi.fn()
        .mockResolvedValueOnce(roleAssignment('as-1', { agent_id: 'w1' }))
        .mockRejectedValueOnce(new ApiError('A conflicting assignment already exists', 'conflict', 409)),
    });
    await openSettings(api);
    const boxes = screen.getAllByRole('checkbox');
    for (const box of boxes) fireEvent.click(box);
    fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));

    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledTimes(2));
    const alert = (await screen.findAllByRole('alert')).map(node => node.textContent).join(' ');
    expect(alert).toContain('Не все изменения подтверждены');
    expect(alert).toContain('Не подтверждено');
    expect(alert).toContain('Требуется решение Owner');
    expect(screen.queryByText('Назначения сохранены.')).toBeNull();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
  });

  it('ignores duplicate save clicks while the first request is in flight', async () => {
    const pending = deferred<RoleAssignment>();
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments: vi.fn(async () => []),
      createAssignment: vi.fn(() => pending.promise),
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('checkbox'));
    const save = screen.getByRole('button', { name: 'Сохранить назначения' });
    fireEvent.click(save);
    fireEvent.click(save);
    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledTimes(1));
    await act(async () => { pending.resolve(roleAssignment('as-1', { agent_id: 'w1' })); await pending.promise; });
    expect(api.createAssignment).toHaveBeenCalledTimes(1);
  });

  it('never exposes Project A assignments after navigating to Project B', async () => {
    const a = deferred<RoleAssignment[]>();
    const bWorker = roleAssignment('b-1', { project_id: 'P0002', agent_id: 'w2' });
    const api = stubApi({
      listProjects: vi.fn(async () => [P1, P2]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), workerAgent('w2', 'Worker Two')]),
      listAssignments: vi.fn((id: string) => id === 'P0001' ? a.promise : Promise.resolve([bWorker])),
    });
    at('/projects/P0001/settings');
    render(<App api={api} />);
    await screen.findByLabelText('Project Work Area');
    await waitFor(() => expect(location.pathname).toBe('/projects/P0001/settings'));

    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(await screen.findByText('Lab'));
    fireEvent.click(await screen.findByRole('tab', { name: 'Настройки проекта' }));
    await screen.findByText('Агенты проекта · назначения');
    await act(async () => { a.resolve([roleAssignment('a-1', { agent_id: 'w1' })]); await a.promise; });

    const panel = screen.getByText('Агенты проекта · назначения').closest('.rosc-settings__panel') as HTMLElement;
    expect(within(panel).getByRole('checkbox', { name: /Worker Two/ })).toBeTruthy();
    expect((within(panel).getByRole('checkbox', { name: /Worker One/ }) as HTMLInputElement).checked).toBe(false);
  });
});

describe('P016.1 first Architect assignment and stale-snapshot safety', () => {
  const openSettings = async (api: ReturnType<typeof stubApi>) => {
    at('/projects/P0001');
    render(<App api={api} />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Настройки проекта' }));
    await screen.findByText('Агенты проекта · назначения');
  };
  const save = () => fireEvent.click(screen.getByRole('button', { name: 'Сохранить назначения' }));
  const archAssignment = (id: string, agent: string, over: Partial<RoleAssignment> = {}) =>
    roleAssignment(id, { agent_id: agent, role: 'architect', capabilities: [...architectCaps], ...over });
  const noRuntimeCalls = (api: ReturnType<typeof stubApi>) => {
    expect(api.activateArchitect).not.toHaveBeenCalled();
    expect(api.deactivateArchitect).not.toHaveBeenCalled();
  };

  it('F1: saves the first Architect without a replacement confirmation, with exactly one create', async () => {
    let current: RoleAssignment[] = [];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1', 'Architect One')]),
      listAssignments: vi.fn(async () => current),
      createAssignment: vi.fn(async () => { const made = archAssignment('arch-1', 'a1'); current = [made]; return made; }),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a1' } });
    save();
    expect(screen.queryByText('Подтвердить замену Architect?')).toBeNull();
    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledTimes(1));
    expect(api.createAssignment).toHaveBeenCalledWith('P0001', { agent_id: 'a1', role: 'architect', task_id: null, capabilities: [...architectCaps] });
    expect((await screen.findByRole('status')).textContent).toBe('Назначения сохранены.');
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    noRuntimeCalls(api);
  });

  it.each([
    ['starting', { state: 'starting' as const, can_activate: false, can_deactivate: false }],
    ['error', { state: 'error' as const, can_activate: false, can_deactivate: false }],
    ['can_deactivate', { state: 'stopped' as const, can_deactivate: true }],
  ])('F1: a first assignment stays blocked when the Architect probe is %s', async (_name, status) => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1')]),
      listAssignments: vi.fn(async () => []),
      getArchitect: vi.fn(async () => architectStatus(status)),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a1' } });
    expect((screen.getByRole('button', { name: 'Сохранить назначения' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.createAssignment).not.toHaveBeenCalled();
  });

  it('F1: a failed probe blocks a first assignment', async () => {
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1')]),
      listAssignments: vi.fn(async () => []),
      getArchitect: vi.fn(async () => { throw new ApiError('down', 'network', null); }),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a1' } });
    expect((screen.getByRole('button', { name: 'Сохранить назначения' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.createAssignment).not.toHaveBeenCalled();
  });

  it('F1: revoking the existing Architect needs confirmation; cancel makes zero writes, confirm revokes with version', async () => {
    let current: RoleAssignment[] = [archAssignment('arch-1', 'a1', { version: 3 })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1')]),
      listAssignments: vi.fn(async () => current),
      revokeAssignment: vi.fn(async () => { current = []; return archAssignment('arch-1', 'a1', { state: 'revoked', version: 4 }); }),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: '' } });
    save();
    expect(screen.getByText('Подтвердить отзыв Architect?')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Отмена' }));
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(api.createAssignment).not.toHaveBeenCalled();
    save();
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }));
    await waitFor(() => expect(api.revokeAssignment).toHaveBeenCalledWith('P0001', 'arch-1', 3));
    expect(api.createAssignment).not.toHaveBeenCalled();
    noRuntimeCalls(api);
  });

  it('F2: a Worker added after the form loaded is not revoked by a stale unchanged-form save', async () => {
    let current: RoleAssignment[] = [roleAssignment('as-1', { agent_id: 'w1' })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), workerAgent('w2', 'Worker Two')]),
      listAssignments: vi.fn(async () => current),
    });
    await openSettings(api);
    current = [...current, roleAssignment('as-2', { agent_id: 'w2' })];
    save();
    await waitFor(() => expect(screen.getAllByRole('alert').some(node => node.textContent?.includes('изменились'))).toBe(true));
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(screen.queryByText('Назначения сохранены.')).toBeNull();
    expect((screen.getByRole('checkbox', { name: /Worker Two/ }) as HTMLInputElement).checked).toBe(true);
    noRuntimeCalls(api);
  });

  it('F2: an intended change to Worker A plus a concurrently added Worker C produces no partial write; a fresh save sends only the delta', async () => {
    let current: RoleAssignment[] = [roleAssignment('as-1', { agent_id: 'w1' })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), workerAgent('w2', 'Worker Two'), workerAgent('w3', 'Worker Three')]),
      listAssignments: vi.fn(async () => current),
      revokeAssignment: vi.fn(async () => { current = current.filter(item => item.assignment_id !== 'as-1'); return roleAssignment('as-1', { state: 'revoked', version: 2 }); }),
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('checkbox', { name: /Worker One/ }));
    current = [...current, roleAssignment('as-3', { agent_id: 'w3' })];
    save();
    await waitFor(() => expect(screen.getAllByRole('alert').some(node => node.textContent?.includes('изменились'))).toBe(true));
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(api.createAssignment).not.toHaveBeenCalled();

    // After review the Owner reselects against the refreshed state and saves deliberately.
    fireEvent.click(screen.getByRole('checkbox', { name: /Worker One/ }));
    save();
    await waitFor(() => expect(api.revokeAssignment).toHaveBeenCalledTimes(1));
    expect(api.revokeAssignment).toHaveBeenCalledWith('P0001', 'as-1', 1);
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect((await screen.findByRole('status')).textContent).toBe('Назначения сохранены.');
  });

  it('F2: a changed Architect or an advanced version blocks a stale confirmed replacement with zero writes', async () => {
    let current: RoleAssignment[] = [archAssignment('arch-1', 'a1')];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [architectAgent('a1', 'Architect One'), architectAgent('a2', 'Architect Two'), architectAgent('a3', 'Architect Three')]),
      listAssignments: vi.fn(async () => current),
    });
    await openSettings(api);
    fireEvent.change(screen.getByLabelText('Project Architect'), { target: { value: 'a2' } });
    save();
    expect(screen.getByText('Подтвердить замену Architect?')).toBeTruthy();
    current = [archAssignment('arch-9', 'a3')]; // someone else replaced the Architect meanwhile
    fireEvent.click(screen.getByRole('button', { name: 'Подтвердить' }));
    await waitFor(() => expect(screen.getAllByRole('alert').some(node => node.textContent?.includes('изменились'))).toBe(true));
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(api.createAssignment).not.toHaveBeenCalled();
    // Refreshed state requires a fresh selection and a new confirmation.
    expect((screen.getByLabelText('Project Architect') as HTMLSelectElement).value).toBe('a3');
    expect(screen.queryByText('Подтвердить замену Architect?')).toBeNull();
  });

  it('F2: a version-only change on an existing assignment blocks the stale save', async () => {
    let current: RoleAssignment[] = [roleAssignment('as-1', { agent_id: 'w1', version: 1 })];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), workerAgent('w2', 'Worker Two')]),
      listAssignments: vi.fn(async () => current),
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('checkbox', { name: /Worker Two/ }));
    current = [roleAssignment('as-1', { agent_id: 'w1', version: 2 })];
    save();
    await waitFor(() => expect(screen.getAllByRole('alert').some(node => node.textContent?.includes('изменились'))).toBe(true));
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
  });

  it('F2: the baseline is order-independent and ignores Task-scoped assignments', async () => {
    const w1 = roleAssignment('as-1', { agent_id: 'w1' });
    const w2 = roleAssignment('as-2', { agent_id: 'w2' });
    let current: RoleAssignment[] = [w1, w2];
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent('w1', 'Worker One'), workerAgent('w2', 'Worker Two'), workerAgent('w3', 'Worker Three')]),
      listAssignments: vi.fn(async () => current),
    });
    await openSettings(api);
    current = [w2, w1, roleAssignment('t-1', { agent_id: 'w3', scope: 'task', task_id: 'T0001' })];
    fireEvent.click(screen.getByRole('checkbox', { name: /Worker Three/ }));
    save();
    await waitFor(() => expect(api.createAssignment).toHaveBeenCalledTimes(1));
    expect(api.createAssignment).toHaveBeenCalledWith('P0001', expect.objectContaining({ agent_id: 'w3', role: 'worker', task_id: null }));
    expect(api.revokeAssignment).not.toHaveBeenCalled();
  });

  it('F2: fails closed with zero writes when fresh assignments cannot be read', async () => {
    const listAssignments = vi.fn().mockResolvedValueOnce([]).mockRejectedValue(new ApiError('down', 'network', null));
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments,
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('checkbox'));
    save();
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBeGreaterThan(0));
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    expect(screen.queryByText('Назначения сохранены.')).toBeNull();
  });

  it('F2: leaving the Project during the preflight read stops before any mutation', async () => {
    const fresh = deferred<RoleAssignment[]>();
    const listAssignments = vi.fn().mockResolvedValueOnce([]).mockImplementationOnce(() => fresh.promise).mockResolvedValue([]);
    const api = stubApi({
      listProjects: vi.fn(async () => [P1, P2]),
      listAgents: vi.fn(async () => [workerAgent()]),
      listAssignments,
    });
    await openSettings(api);
    fireEvent.click(screen.getByRole('checkbox'));
    save();
    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    await act(async () => { fresh.resolve([]); await fresh.promise; });
    expect(api.createAssignment).not.toHaveBeenCalled();
    expect(api.revokeAssignment).not.toHaveBeenCalled();
    noRuntimeCalls(api);
  });
});
