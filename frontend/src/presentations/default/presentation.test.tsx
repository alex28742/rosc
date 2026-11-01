import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { project, task } from '../../app/testing';
import { DashboardView } from './views/DashboardView';
import { ProjectView } from './views/ProjectView';
import { TaskView } from './views/TaskView';
import { NewTaskView } from './views/NewTaskView';
import { RegisterProjectView } from './views/RegisterProjectView';
import { Shell } from './Shell';

// Presentation-layer checks: the package must render the real view model truthfully, expose a
// labelled close affordance for every layer, and never grow a control that pretends a backend
// capability exists. Layout/contrast is verified in the browser; this suite guards the contract.
const feedback = { error: '', notice: '' };
const scroll = { region: () => () => {} };
const workspace = { busy: false, onReconcile: vi.fn(), onRepair: vi.fn() };
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
    feedback, inert: false, scroll, workspace, onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
  };

  it('labels the Project layer and keeps one visible, named close control', () => {
    render(<ProjectView {...projectProps} />);
    const drawer = screen.getByLabelText('Project Work Area');
    expect(within(drawer).getByLabelText('Закрыть проект')).toBeTruthy();
    expect(within(drawer).getByText('Tasks (1)')).toBeTruthy();
    expect(within(drawer).getByText('T0001 · Срез работ')).toBeTruthy();
    expect(within(drawer).getByText('Draft')).toBeTruthy();
  });

  it('shows the Project description, path and workspace facts from the view model', () => {
    render(<ProjectView {...projectProps} />);
    expect(screen.getByText('Контекст проекта')).toBeTruthy();
    expect(screen.getByText('Ядро платформы')).toBeTruthy();
    expect(screen.getByText('/tmp/rosc')).toBeTruthy();
    expect(screen.getByText('README.md')).toBeTruthy();
  });

  it('renders a nested Task layer that reads the Task without offering a Run', () => {
    render(<TaskView projectName="ROSC Platform" taskId="T0001" task={T1} tasksStatus="ready" feedback={feedback} inert={false} scroll={scroll} workspace={workspace} onClose={vi.fn()} onRetry={vi.fn()} />);
    const drawer = screen.getByLabelText('Task Work Area');
    expect(within(drawer).getByLabelText('Закрыть задачу')).toBeTruthy();
    expect(within(drawer).getByText('Спецификация задачи · Draft')).toBeTruthy();
    expect(within(drawer).getByText('Описание', { selector: 'dt' })).toBeTruthy();
    expect(within(drawer).getByText('Критерии приёмки', { selector: 'dt' })).toBeTruthy();
    expect(within(drawer).getByText(/чтение Task не запускает агента/)).toBeTruthy();
    expect(within(drawer).queryByRole('button', { name: /Запустить|Run/ })).toBeNull();
  });

  it('keeps the Task Draft form labelled and submits through the typed callback only', () => {
    const onSubmit = vi.fn();
    render(<NewTaskView projectName="ROSC Platform" feedback={feedback} busy={false} inert={false} onSubmit={onSubmit} onClose={vi.fn()} />);
    const drawer = screen.getByLabelText('Новая Task');
    fireEvent.change(within(drawer).getByLabelText('Название'), { target: { value: 'New task' } });
    fireEvent.change(within(drawer).getByLabelText('Описание и контекст'), { target: { value: 'Context' } });
    fireEvent.change(within(drawer).getByLabelText('Критерии приёмки'), { target: { value: 'Reviewed' } });
    fireEvent.submit(drawer.querySelector('form')!);
    expect(onSubmit).toHaveBeenCalledWith({ title: 'New task', description: 'Context', acceptance_criteria: 'Reviewed' });
  });
});

describe('Workspace presentation', () => {
  const baseProject = {
    projectId: 'P0001', projectStatus: 'ready' as const, tasks: [], tasksStatus: 'ready' as const,
    feedback, inert: false, scroll, onClose: vi.fn(), onRetry: vi.fn(), onOpenTask: vi.fn(), onNewTask: vi.fn(),
  };
  const controls = () => ({ busy: false, onReconcile: vi.fn(), onRepair: vi.fn() });

  it('offers no reconcile or repair control for a healthy Workspace in the Project or Task layer', () => {
    const workspace = controls();
    const { unmount } = render(<ProjectView {...baseProject} project={project('P0001')} workspace={workspace} />);
    expect(screen.queryByRole('button', { name: 'Проверить Workspace' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Восстановить манифест' })).toBeNull();
    expect(screen.getByText('Манифест Workspace соответствует записи в базе.')).toBeTruthy();
    unmount();

    render(<TaskView projectName="ROSC Platform" taskId="T0001" task={task('T0001', 'P0001')} tasksStatus="ready" feedback={feedback} inert={false} scroll={scroll} workspace={workspace} onClose={vi.fn()} onRetry={vi.fn()} />);
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

describe('Shell', () => {
  it('states the Engine slice truthfully and renders the view as its only content', () => {
    render(<Shell><main data-testid="view" /></Shell>);
    expect(screen.getByText(/Engine Slice 01 · агенты не запущены/)).toBeTruthy();
    expect(screen.getByTestId('view')).toBeTruthy();
  });
});
