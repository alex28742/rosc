import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { architectStatus, agentDefinition, contextAccess, project, roleAssignment, task } from '../../app/testing';
import { DashboardView } from './views/DashboardView';
import { ProjectView } from './views/ProjectView';
import { TaskView } from './views/TaskView';
import { NewTaskView } from './views/NewTaskView';
import { RegisterProjectView } from './views/RegisterProjectView';
import { SettingsView } from './views/SettingsView';
import { Shell } from './Shell';

// Presentation-layer checks: the package must render the real view model truthfully, expose a
// labelled close affordance for every layer, and never grow a control that pretends a backend
// capability exists. Layout/contrast is verified in the browser; this suite guards the contract.
const feedback = { error: '', notice: '' };
const scroll = { region: () => () => {} };
const workspace = { busy: false, onReconcile: vi.fn(), onRepair: vi.fn() };
const textScale = { scale: 100 as const, onChange: vi.fn() };
const taskConversation = () => ({
  state: { status: 'ready' as const, conversationId: null, messages: [], hasMore: false, nextBefore: null,
    error: '', sending: false, olderLoading: false },
  architect: { status: 'ready' as const, info: null },
  onReload: vi.fn(), onLoadOlder: vi.fn(), onSend: vi.fn(async () => null), onSendToArchitect: vi.fn(async () => null),
  onReconcile: vi.fn(async () => null), onRetryDelivery: vi.fn(async () => null),
});

const conversation = () => ({
  state: { status: 'ready' as const, conversationId: null, messages: [], hasMore: false, nextBefore: null,
    error: '', sending: false, olderLoading: false },
  architect: { status: 'ready' as const, info: architectStatus(), busy: false, error: '' },
  contextAccess: { status: 'ready' as const, info: contextAccess(), busy: false, error: '' },
  onActivate: vi.fn(async () => null), onDeactivate: vi.fn(async () => null),
  onRefreshArchitect: vi.fn(),
  onReconcile: vi.fn(async () => null), onRetryDelivery: vi.fn(async () => null),
  onReload: vi.fn(), onLoadOlder: vi.fn(), onSend: vi.fn(async () => null),
});
const assignments = () => ({
  state: { status: 'ready' as const, agents: [], assignments: [], architect: null, architectKnown: true,
    error: '', notice: '', busy: false, unresolved: [] },
  onRetry: vi.fn(), onSave: vi.fn(async () => null), onOpenRegistry: vi.fn(),
});
const P1 = project('P0001', { name: 'ROSC Platform', description: 'Ядро платформы', root_path: '/tmp/rosc', docs_entrypoint: 'README.md' });
const P2 = project('P0002', { name: 'Telemetry Lab', workspace_state: 'needs_reconciliation', workspace_problem: 'drift' });
const T1 = task('T0001', 'P0001', { title: 'Срез работ', description: 'Описание', acceptance_criteria: 'Критерии' });

afterEach(cleanup);

describe('Dashboard presentation', () => {
  const props = { projects: [P1, P2], projectsStatus: 'ready' as const, feedback, inert: false, onOpenProject: vi.fn(), onRegisterProject: vi.fn(), onRetry: vi.fn() };

  it('renders each Project as one real card with its id, description, path and workspace state', () => {
    render(<DashboardView {...props} />);
    const cards = screen.getAllByRole('button', { name: /Открыть проект/ });
    expect(cards).toHaveLength(2);
    expect(within(cards[0]!).getByText('ROSC Platform')).toBeTruthy();
    expect(within(cards[0]!).getByText('P0001 · подключённый проект')).toBeTruthy();
    expect(within(cards[0]!).getByText('Ядро платформы')).toBeTruthy();
    expect(within(cards[0]!).getByText('/tmp/rosc')).toBeTruthy();
    expect(within(cards[0]!).getByText('Workspace готов')).toBeTruthy();
    expect(within(cards[1]!).getByText('Нужна сверка Workspace')).toBeTruthy();
    expect(screen.getByText('2')).toBeTruthy();
  });

  it('offers no agent or Run action on the Dashboard', () => {
    render(<DashboardView {...props} />);
    for (const label of [/Запустить/, /Активировать/, /Создать Agent/]) expect(screen.queryByRole('button', { name: label })).toBeNull();
  });

  it('tells loading, failure and empty apart instead of showing the empty copy early', () => {
    const { rerender } = render(<DashboardView {...props} projects={[]} projectsStatus="loading" />);
    expect(screen.getByText('Загрузка проектов…')).toBeTruthy();
    expect(screen.queryByText(/Проектов пока нет/)).toBeNull();
    rerender(<DashboardView {...props} projects={[]} projectsStatus="failed" />);
    expect(screen.getByText('Не удалось загрузить проекты.')).toBeTruthy();
    expect(screen.queryByText(/Проектов пока нет/)).toBeNull();
    rerender(<DashboardView {...props} projects={[]} projectsStatus="ready" />);
    expect(screen.getByText(/Проектов пока нет/)).toBeTruthy();
  });
});

describe('Work area presentation', () => {
  const projectProps = {
    projectId: 'P0001', project: P1, projectStatus: 'ready' as const, tasks: [T1], tasksStatus: 'ready' as const,
    feedback, inert: false, scroll, workspace, conversation: conversation(), assignments: assignments(), textScale,
    tab: 'tasks' as const, onSelectTab: vi.fn(), onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
  };

  it('labels the Project layer and keeps one visible, named close control', () => {
    render(<ProjectView {...projectProps} />);
    const drawer = screen.getByLabelText('Project Work Area');
    expect(within(drawer).getByLabelText('Закрыть проект')).toBeTruthy();
    expect(within(drawer).getByText('Tasks (1)')).toBeTruthy();
    expect(within(drawer).getByText('T0001 · Срез работ')).toBeTruthy();
    expect(within(drawer).getByText('Draft', { selector: '.rosc-tag' })).toBeTruthy();
  });

  it('shows the Project description, path and workspace facts from the view model', () => {
    render(<ProjectView {...projectProps} />);
    expect(screen.getByText('Контекст проекта')).toBeTruthy();
    expect(screen.getByText('Ядро платформы')).toBeTruthy();
    expect(screen.getByText('/tmp/rosc')).toBeTruthy();
    expect(screen.getByText('README.md')).toBeTruthy();
  });

  it('renders the approved "Агенты проекта" overview card without duplicating its facts in the first card', () => {
    render(<ProjectView {...projectProps} />);
    expect(screen.getByText('Агенты проекта', { selector: 'h3' })).toBeTruthy();
    expect(screen.queryByText('Состояние Workspace')).toBeNull();
    expect(screen.queryByText('Architect', { selector: 'dt' })).toBeNull();
    expect(screen.queryByText('Workers', { selector: 'dt' })).toBeNull();
    expect(screen.getByText('Workers в работе')).toBeTruthy();
    expect(screen.getByText('Режим')).toBeTruthy();
    expect(screen.getByText('Подробнее о назначениях — в настройках проекта')).toBeTruthy();
    expect(screen.getByText('Документация', { selector: 'dt' })).toBeTruthy();
  });

  it('renders a nested Task layer that reads the Task without offering a Run', () => {
    render(<TaskView projectName="ROSC Platform" taskId="T0001" task={T1} tasksStatus="ready" feedback={feedback} inert={false} scroll={scroll} workspace={workspace} conversation={taskConversation()} textScale={textScale} onClose={vi.fn()} onRetry={vi.fn()} />);
    const drawer = screen.getByLabelText('Task Work Area');
    expect(within(drawer).getByLabelText('Закрыть задачу')).toBeTruthy();
    expect(within(drawer).getByLabelText('Спецификация задачи')).toBeTruthy();
    expect(within(drawer).getByText('Описание задачи', { selector: 'h4' })).toBeTruthy();
    expect(within(drawer).getByText('Критерии приёмки', { selector: 'h4' })).toBeTruthy();
    expect(within(drawer).getByText(/чтение Task не запускает агента/)).toBeTruthy();
    expect(within(drawer).queryByRole('button', { name: /Запустить|Run/ })).toBeNull();
  });

  it('assembles every Task Work Area region without backend data, with disabled future controls and a working rail', () => {
    render(<TaskView projectName="ROSC Platform" taskId="T0001" task={T1} tasksStatus="ready" feedback={feedback} inert={false} scroll={scroll} workspace={workspace} conversation={taskConversation()} textScale={textScale} onClose={vi.fn()} onRetry={vi.fn()} />);
    const drawer = screen.getByLabelText('Task Work Area');
    const main = drawer.querySelector('.rosc-task-workspace')!;
    expect(Array.from(main.children).map(child => child.className.split(' ')[0])).toEqual(['rosc-task-left', 'rosc-conversation']);
    const lower = drawer.querySelector('.rosc-task-lower')!;
    expect(lower.children.length).toBe(2);
    for (const name of ['Блокеры', 'Спецификация задачи', 'Рабочий план Worker', 'Контекст и действия', 'Task Conversation']) {
      expect(within(drawer).getByLabelText(name)).toBeTruthy();
    }
    expect(within(drawer).getByText('Рабочий план пока недоступен.')).toBeTruthy();
    expect(drawer.querySelector('.rosc-drawer__footer')).toBeTruthy();
    expect(main.contains(drawer.querySelector('.rosc-drawer__footer'))).toBe(false);
    for (const label of ['Сохранить Spec', 'Ответить на блокер', 'Architect в Task', 'Выбрать Worker', 'Prompt Draft']) {
      const button = within(drawer).getByRole('button', { name: new RegExp(label) }) as HTMLButtonElement;
      expect(button.disabled, label).toBe(true);
    }
    expect(within(drawer).queryByText(/B-014|S0\d|R5002/)).toBeNull();
    fireEvent.click(within(drawer).getByLabelText('Свернуть Task Conversation'));
    expect(main.className).toContain('rosc-task-workspace--collapsed');
    fireEvent.click(within(drawer).getByLabelText('Развернуть Task Conversation'));
    expect(main.className).not.toContain('collapsed');
    fireEvent.click(within(drawer).getByText('Свернуть спецификацию ⌃'));
    expect(within(drawer).queryByText('Описание задачи', { selector: 'h4' })).toBeNull();
  });

  it('keeps the Task Draft form labelled and submits through the typed callback only', () => {
    const onSubmit = vi.fn();
    render(<NewTaskView projectName="ROSC Platform" feedback={feedback} busy={false} inert={false} onSubmit={onSubmit} onClose={vi.fn()} />);
    const drawer = screen.getByLabelText('Новая Task');
    fireEvent.change(within(drawer).getByLabelText('Название задачи *'), { target: { value: 'New task' } });
    fireEvent.change(within(drawer).getByLabelText('Описание и контекст *'), { target: { value: 'Context' } });
    fireEvent.change(within(drawer).getByLabelText('Критерии приёмки *'), { target: { value: 'Reviewed' } });
    fireEvent.submit(drawer.querySelector('form')!);
    expect(onSubmit).toHaveBeenCalledWith({ title: 'New task', description: 'Context', acceptance_criteria: 'Reviewed' });
  });
});

describe('Workspace presentation', () => {
  const baseProject = {
    projectId: 'P0001', projectStatus: 'ready' as const, tasks: [], tasksStatus: 'ready' as const,
    feedback, inert: false, scroll, conversation: conversation(), assignments: assignments(), textScale,
    tab: 'tasks' as const, onSelectTab: vi.fn(), onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
  };
  const controls = () => ({ busy: false, onReconcile: vi.fn(), onRepair: vi.fn() });

  it('offers no reconcile or repair control for a healthy Workspace in the Project or Task layer', () => {
    const workspace = controls();
    const { unmount } = render(<ProjectView {...baseProject} project={project('P0001')} workspace={workspace} />);
    expect(screen.queryByRole('button', { name: 'Проверить Workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Восстановить манифест' })).toBeNull();
    expect(screen.getByText('Манифест Workspace соответствует записи в базе.')).toBeTruthy();
    unmount();

    render(<TaskView projectName="ROSC Platform" taskId="T0001" task={task('T0001', 'P0001')} tasksStatus="ready" feedback={feedback} inert={false} scroll={scroll} workspace={workspace} conversation={taskConversation()} textScale={textScale} onClose={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Проверить Workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Восстановить манифест' })).toBeNull();
    expect(workspace.onReconcile).not.toHaveBeenCalled();
    expect(workspace.onRepair).not.toHaveBeenCalled();
  });

  it('keeps the reconcile action for a Workspace that has not been checked yet', () => {
    const workspace = controls();
    render(<ProjectView {...baseProject} project={project('P0002', { workspace_state: 'needs_reconciliation', workspace_problem: 'missing' })} workspace={workspace} />);
    fireEvent.click(screen.getByRole('button', { name: 'Проверить Workspace' }));
    expect(workspace.onReconcile).toHaveBeenCalledTimes(1);
    expect(workspace.onRepair).not.toHaveBeenCalled();
  });

  it('requires the explicit confirmation before repairing a damaged manifest', () => {
    const workspace = controls();
    render(<ProjectView {...baseProject} project={project('P0003', { workspace_state: 'needs_reconciliation', workspace_problem: 'corrupt' })} workspace={workspace} />);
    const repair = screen.getByRole('button', { name: 'Восстановить манифест' }) as HTMLButtonElement;
    expect(repair.disabled).toBe(true);
    fireEvent.click(repair);
    expect(workspace.onRepair).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(repair.disabled).toBe(false);
    fireEvent.click(repair);
    expect(workspace.onRepair).toHaveBeenCalledWith({ confirmed: true });
  });

  it.each(['oversized', 'foreign', 'unsafe'] as const)('keeps automatic repair off for an %s manifest', problem => {
    const workspace = controls();
    render(<ProjectView {...baseProject} project={project('P0004', { workspace_state: 'needs_reconciliation', workspace_problem: problem })} workspace={workspace} />);
    expect(screen.getByText(/исправление отключено/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Проверить Workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Восстановить манифест' })).toBeNull();
  });
});

describe('Register Project dialog', () => {
  it('defaults to linking an existing directory and keeps the mode switch accessible', () => {
    const onSubmit = vi.fn();
    const { container } = render(<RegisterProjectView feedback={feedback} busy={false} onSubmit={onSubmit} onClose={vi.fn()} />);
    expect(screen.getByText('Добавить проект', { selector: 'h2' })).toBeTruthy();
    expect(screen.getByLabelText('Закрыть')).toBeTruthy();
    const existing = within(container).getByRole('radio', { name: 'Подключить существующий' }) as HTMLInputElement;
    const fresh = within(container).getByRole('radio', { name: 'Зарегистрировать новый пустой каталог' }) as HTMLInputElement;
    expect(existing.checked).toBe(true);
    expect(fresh.checked).toBe(false);
    fireEvent.click(fresh);
    fireEvent.change(screen.getByLabelText('Название'), { target: { value: 'N' } });
    fireEvent.change(screen.getByLabelText('Описание'), { target: { value: 'D' } });
    fireEvent.change(screen.getByLabelText('Корневая директория'), { target: { value: '/tmp/n' } });
    fireEvent.submit(container.querySelector('form')!);
    expect(onSubmit).toHaveBeenCalledWith({ name: 'N', description: 'D', root_path: '/tmp/n', mode: 'new', docs_entrypoint: '', no_external_agent_confirmed: false });
  });

  it('keeps a failure inside the dialog as an alert', () => {
    render(<RegisterProjectView feedback={{ error: 'Project root overlaps another registered Project', notice: '' }} busy={false} onSubmit={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole('alert').textContent).toBe('Project root overlaps another registered Project');
  });
});

describe('Settings presentation', () => {
  const base = {
    projects: [P1], onRetry: vi.fn(), onSaveAgent: vi.fn(async () => null),
    onOpenProjectSettings: vi.fn(), onClose: vi.fn(),
  };
  const registry = (over: Partial<Parameters<typeof SettingsView>[0]['state']> = {}) => ({
    status: 'ready' as const, agents: [], error: '', notice: '', busy: false, ...over,
  });

  it('renders only Core rows, labels readiness as unverified and offers Add for an empty Registry', () => {
    const { rerender } = render(<SettingsView {...base} state={registry()} />);
    expect(screen.getByText('Глобальный Agent Registry')).toBeTruthy();
    expect(screen.getByText('Нет Agent Definitions.')).toBeTruthy();
    expect(screen.getByRole('button', { name: '＋ Добавить' })).toBeTruthy();
    expect(screen.queryByText(/Подключен/)).toBeNull();

    rerender(<SettingsView {...base} state={registry({ agents: [agentDefinition('a1', { display_name: 'DeepSeek Coder' })] })} />);
    expect(screen.getByText('DeepSeek Coder')).toBeTruthy();
    expect(screen.getByText(/OpenCode · prov · model-a/)).toBeTruthy();
    expect(screen.getByText('Не проверен')).toBeTruthy();
    expect(screen.getByText('Всего Agent Definitions: 1')).toBeTruthy();
  });

  it('keeps the other Settings sections clearly deferred', () => {
    render(<SettingsView {...base} state={registry()} />);
    for (const label of ['Профиль Owner', 'Workspace и пауза', 'Авторизация', 'Статистика']) {
      expect((screen.getByRole('button', { name: label }) as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it('submits the full capability list and the selected safe icon key', () => {
    const onSaveAgent = vi.fn(async () => null);
    render(<SettingsView {...base} state={registry()} onSaveAgent={onSaveAgent} />);
    fireEvent.click(screen.getByRole('button', { name: '＋ Добавить' }));
    fireEvent.change(screen.getByLabelText('Отображаемое имя'), { target: { value: 'Architect Agent' } });
    fireEvent.change(screen.getByLabelText('Provider ID'), { target: { value: 'deepseek' } });
    fireEvent.change(screen.getByLabelText('Model ID'), { target: { value: 'deepseek-v4.1-flash' } });
    fireEvent.change(screen.getByLabelText('Набор declared capabilities'), { target: { value: 'architect' } });
    fireEvent.submit(screen.getByRole('button', { name: 'Зарегистрировать' }).closest('form')!);
    expect(onSaveAgent).toHaveBeenCalledWith(null, {
      display_name: 'Architect Agent', icon_label: 'robot', runtime_adapter: 'opencode',
      provider_id: 'deepseek', model_id: 'deepseek-v4.1-flash',
      capabilities: ['project.context.read', 'architect.document.read', 'architect.document.write'],
    });
  });

  it('shows a failed load with Retry and no invented rows', () => {
    render(<SettingsView {...base} state={registry({ status: 'failed', error: 'backend down' })} />);
    expect(screen.getByText('Не удалось загрузить Agent Registry.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Повторить' }));
    expect(base.onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('Project assignment presentation', () => {
  it('keeps the Project Conversation mounted while the left Settings tab is selected', () => {
    const props = {
      projectId: 'P0001', project: P1, projectStatus: 'ready' as const, tasks: [T1], tasksStatus: 'ready' as const,
      feedback, inert: false, scroll, workspace, conversation: conversation(), assignments: assignments(), textScale,
      tab: 'settings' as const, onSelectTab: vi.fn(), onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
    };
    render(<ProjectView {...props} />);
    expect(screen.getByText('Project Conversation')).toBeTruthy();
    expect(screen.getByText('Агенты проекта · назначения')).toBeTruthy();
    expect(screen.queryByText('Задачи проекта')).toBeNull();
  });

  it('disables an ineligible unassigned Agent and keeps a blocked current assignment removable', () => {
    const ineligible = agentDefinition('worker-ineligible', {
      display_name: 'No Worker Caps', capabilities: [{ capability: 'project.context.read', status: 'declared' }],
    });
    const blocked = agentDefinition('worker-blocked', { display_name: 'Blocked Worker' });
    const props = {
      projectId: 'P0001', project: P1, projectStatus: 'ready' as const, tasks: [], tasksStatus: 'ready' as const,
      feedback, inert: false, scroll, workspace, conversation: conversation(),
      assignments: {
        state: {
          status: 'ready' as const, agents: [ineligible, blocked], assignments: [roleAssignment('a1', { agent_id: 'worker-blocked', readiness: 'blocked' })],
          architect: architectStatus(), architectKnown: true, error: '', notice: '', busy: false, unresolved: [],
        },
        onRetry: vi.fn(), onSave: vi.fn(async () => null), onOpenRegistry: vi.fn(),
      },
      textScale,
      tab: 'settings' as const, onSelectTab: vi.fn(), onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
    };
    render(<ProjectView {...props} />);
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    expect(boxes.find(box => box.closest('label')?.textContent?.includes('No Worker Caps'))?.disabled).toBe(true);
    const assigned = boxes.find(box => box.closest('label')?.textContent?.includes('Blocked Worker'))!;
    expect(assigned.disabled).toBe(false);
    expect(assigned.checked).toBe(true);
  });
});

describe('Shell', () => {
  it('states the Engine slice truthfully and renders the view as its only content', () => {
    render(<Shell onOpenSettings={vi.fn()} settingsOpen={false} engineNotices={[]} onDismissEngineNotice={vi.fn()}><main data-testid="view" /></Shell>);
    expect(screen.getByText(/Engine Slice 01 · агенты не запущены/)).toBeTruthy();
    expect(screen.getByTestId('view')).toBeTruthy();
  });

  it('offers the Settings entry without claiming a connected agent', () => {
    const onOpenSettings = vi.fn();
    render(<Shell onOpenSettings={onOpenSettings} settingsOpen={false} engineNotices={[]} onDismissEngineNotice={vi.fn()}><main /></Shell>);
    fireEvent.click(screen.getByRole('button', { name: 'Настройки' }));
    expect(onOpenSettings).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Подключен/)).toBeNull();
  });

  it('renders Engine system notices above the views and reports their dismiss key', () => {
    const onDismissEngineNotice = vi.fn();
    render(<Shell onOpenSettings={vi.fn()} settingsOpen={false} onDismissEngineNotice={onDismissEngineNotice}
      engineNotices={[{ key: 'core', kind: 'error', kicker: 'ROSC · СИСТЕМА', title: 'Нет связи с ROSC Core', description: 'Ядро не отвечает.' }]}>
      <main data-testid="view" />
    </Shell>);
    expect(screen.getByText('Нет связи с ROSC Core')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Закрыть уведомление'));
    expect(onDismissEngineNotice).toHaveBeenCalledWith('core');
  });
});
