import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import { App } from './App';
import { deferred, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001'), P2 = project('P0002');
const T1 = task('T0001', 'P0001');

beforeEach(() => at('/'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('navigation', () => {
  it('walks Dashboard -> Project -> Task, closes up, and follows Back/Forward', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]) });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('Project P0001'));
    expect(location.pathname).toBe('/projects/P0001');
    fireEvent.click(await screen.findByText('T0001 · Task T0001'));
    expect(location.pathname).toBe('/projects/P0001/tasks/T0001');
    expect(await screen.findByLabelText('Task Work Area')).toBeTruthy();
    expect(screen.getByLabelText('Project Work Area')).toBeTruthy();

    fireEvent.click(screen.getByLabelText('Закрыть задачу'));
    expect(location.pathname).toBe('/projects/P0001');
    expect(screen.queryByLabelText('Task Work Area')).toBeNull();
    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    expect(location.pathname).toBe('/');
    expect(screen.queryByLabelText('Project Work Area')).toBeNull();

    act(() => history.back());
    await waitFor(() => expect(location.pathname).toBe('/projects/P0001'));
    expect(await screen.findByLabelText('Project Work Area')).toBeTruthy();
    act(() => history.back());
    await waitFor(() => expect(screen.getByLabelText('Task Work Area')).toBeTruthy());
    act(() => history.forward());
    await waitFor(() => expect(screen.queryByLabelText('Task Work Area')).toBeNull());
  });

  it('opens deep links directly, including the Task Draft form, without any mutating call', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]) });
    at('/projects/P0001/new-task');
    render(<App api={api} />);
    expect(await screen.findByLabelText('Новая Task')).toBeTruthy();
    for (const mutation of [api.createProject, api.createTask, api.reconcileProject, api.repairProject, api.reconcileTask, api.repairTask]) expect(mutation).not.toHaveBeenCalled();
  });

  it('does not coerce an unsupported path into a Project view', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]) });
    at('/projects/P0001/bogus');
    render(<App api={api} />);
    await screen.findByText('Project P0001');
    expect(screen.queryByLabelText('Project Work Area')).toBeNull();
    expect(api.listTasks).not.toHaveBeenCalled();
  });

  it('makes the page behind an overlay inert and locks body scroll', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]) });
    at('/projects/P0001');
    const { container } = render(<App api={api} />);
    await screen.findByLabelText('Project Work Area');
    expect(container.querySelector('main.dashboard')?.hasAttribute('inert')).toBe(true);
    expect(document.body.style.overflow).toBe('hidden');
  });
});

describe('loading and retry', () => {
  it('shows a retry for a failed Project load and recovers', async () => {
    const listProjects = vi.fn()
      .mockRejectedValueOnce(new ApiError('Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.', 'network', null))
      .mockResolvedValue([P1]);
    const api = stubApi({ listProjects });
    at('/projects/P0001');
    render(<App api={api} />);
    expect(await screen.findByText('Не удалось загрузить проект.')).toBeTruthy();
    const drawer = screen.getByLabelText('Project Work Area');
    expect(within(drawer).getByRole('alert').textContent).toContain('Не удалось связаться с ядром ROSC');
    fireEvent.click(within(drawer).getByText('Повторить'));
    expect(await screen.findByText('Контекст проекта')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('never shows tasks of Project A under Project B after a quick switch', async () => {
    const a = deferred<ReturnType<typeof task>[]>(), b = deferred<ReturnType<typeof task>[]>();
    const listTasks = vi.fn((id: string) => (id === 'P0001' ? a.promise : b.promise));
    const api = stubApi({ listProjects: vi.fn(async () => [P1, P2]), listTasks });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('Project P0001'));
    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(screen.getByText('Project P0002'));
    await screen.findByText('Контекст проекта');
    await act(async () => { b.resolve([]); await b.promise; });
    await act(async () => { a.resolve([task('T0009', 'P0001', { title: 'Belongs to A' })]); await a.promise; });
    expect(screen.queryByText(/Belongs to A/)).toBeNull();
    expect(await screen.findByText('Задач пока нет.')).toBeTruthy();
  });

  it('does not show a late error from the previous Project in the next layer', async () => {
    const a = deferred<never>();
    const api = stubApi({ listProjects: vi.fn(async () => [P1, P2]), listTasks: vi.fn((id: string) => (id === 'P0001' ? a.promise : Promise.resolve([]))) });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('Project P0001'));
    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(screen.getByText('Project P0002'));
    await screen.findByText('Задач пока нет.');
    await act(async () => { a.reject(new ApiError('late A failure', 'server', 500)); await a.promise.catch(() => undefined); });
    expect(screen.queryByText('late A failure')).toBeNull();
  });
});

describe('Project registration', () => {
  const fill = (dialog: HTMLElement) => {
    fireEvent.change(within(dialog).getByLabelText('Название'), { target: { value: 'New one' } });
    fireEvent.change(within(dialog).getByLabelText('Описание'), { target: { value: 'About it' } });
    fireEvent.change(within(dialog).getByLabelText('Корневая директория'), { target: { value: '/tmp/new-one' } });
  };

  it('keeps the error inside the modal and the typed values after a failure, then succeeds on retry', async () => {
    const createProject = vi.fn()
      .mockRejectedValueOnce(new ApiError('Project root overlaps another registered Project', 'conflict', 409))
      .mockResolvedValue(project('P0003', { name: 'New one' }));
    const api = stubApi({ createProject });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    fill(form);
    fireEvent.submit(form);
    await waitFor(() => expect(within(form).getByRole('alert').textContent).toBe('Project root overlaps another registered Project'));
    expect((within(form).getByLabelText('Корневая директория') as HTMLInputElement).value).toBe('/tmp/new-one');
    expect(location.pathname).toBe('/');

    fireEvent.submit(form);
    await waitFor(() => expect(location.pathname).toBe('/projects/P0003'));
    expect(createProject).toHaveBeenLastCalledWith({ name: 'New one', description: 'About it', root_path: '/tmp/new-one', mode: 'existing', docs_entrypoint: null, no_external_agent_confirmed: false });
    expect(screen.queryByText('Добавить проект', { selector: 'h2' })).toBeNull();
  });

  it('ignores a second submit while the first is in flight', async () => {
    const pending = deferred<ReturnType<typeof project>>();
    const api = stubApi({ createProject: vi.fn(() => pending.promise) });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    fill(form);
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(api.createProject).toHaveBeenCalledTimes(1);
    await act(async () => { pending.resolve(project('P0003')); await pending.promise; });
  });

  const openForm = async () => {
    fireEvent.click(await screen.findByText('＋ Добавить проект'));
    return screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
  };
  const formGone = () => expect(screen.queryByText('Добавить проект', { selector: 'h2' })).toBeNull();

  it.each([
    ['the close button', () => fireEvent.click(screen.getByLabelText('Закрыть'))],
    ['the backdrop', () => fireEvent.click(document.querySelector('.modal-back')!)],
  ])('does not leak a dismissed modal error to the Dashboard or a reopened form (closed via %s)', async (_name, dismiss) => {
    const createProject = vi.fn().mockRejectedValue(new ApiError('Project root overlaps another registered Project', 'conflict', 409));
    render(<App api={stubApi({ createProject })} />);
    const form = await openForm();
    fill(form);
    fireEvent.submit(form);
    await waitFor(() => expect(within(form).getByRole('alert').textContent).toBe('Project root overlaps another registered Project'));

    dismiss();
    formGone();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Project root overlaps another registered Project')).toBeNull();

    const reopened = await openForm();
    expect(within(reopened).queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Project root overlaps another registered Project')).toBeNull();
  });

  it('lets a stale in-flight registration finish without closing or navigating a replacement modal', async () => {
    const pending = deferred<ReturnType<typeof project>>();
    const createProject = vi.fn(() => pending.promise);
    const listProjects = vi.fn(async () => [P1]);
    render(<App api={stubApi({ createProject, listProjects })} />);
    const formA = await openForm();
    fill(formA);
    fireEvent.submit(formA);
    expect(createProject).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByLabelText('Закрыть'));
    formGone();
    const formB = await openForm();
    fireEvent.change(within(formB).getByLabelText('Название'), { target: { value: 'Second draft' } });

    await act(async () => { pending.resolve(project('P0003', { name: 'New one' })); await pending.promise; });
    await waitFor(() => expect(listProjects.mock.calls.length).toBeGreaterThan(1));

    expect(screen.getByText('Добавить проект', { selector: 'h2' })).toBeTruthy();
    expect((within(formB).getByLabelText('Название') as HTMLInputElement).value).toBe('Second draft');
    expect(within(formB).queryByRole('alert')).toBeNull();
    expect(location.pathname).toBe('/');
  });

  it('ignores a stale failure of a dismissed registration in a replacement modal', async () => {
    const pending = deferred<ReturnType<typeof project>>();
    render(<App api={stubApi({ createProject: vi.fn(() => pending.promise) })} />);
    const formA = await openForm();
    fill(formA);
    fireEvent.submit(formA);
    fireEvent.click(screen.getByLabelText('Закрыть'));
    const formB = await openForm();

    await act(async () => { pending.reject(new ApiError('late A failure', 'conflict', 409)); await pending.promise.catch(() => undefined); });

    expect(within(formB).queryByRole('alert')).toBeNull();
    expect(screen.queryByText('late A failure')).toBeNull();
  });
});

describe('Task Draft creation', () => {
  const fillTask = () => {
    fireEvent.change(screen.getByLabelText('Название'), { target: { value: 'Write docs' } });
    fireEvent.change(screen.getByLabelText('Описание и контекст'), { target: { value: 'Context' } });
    fireEvent.change(screen.getByLabelText('Критерии приёмки'), { target: { value: 'Reviewed' } });
  };

  it('shows a failure in the Task layer, keeps the form, and only the project layer stays clean', async () => {
    const createTask = vi.fn()
      .mockRejectedValueOnce(new ApiError('Project Workspace requires reconciliation', 'conflict', 409))
      .mockResolvedValue(task('T0005', 'P0001', { title: 'Write docs' }));
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), createTask });
    at('/projects/P0001/new-task');
    render(<App api={api} />);
    await screen.findByLabelText('Новая Task');
    fillTask();
    fireEvent.submit(screen.getByLabelText('Новая Task').querySelector('form')!);
    const drawer = screen.getByLabelText('Новая Task');
    await waitFor(() => expect(within(drawer).getByRole('alert').textContent).toBe('Project Workspace requires reconciliation'));
    expect((screen.getByLabelText('Название') as HTMLInputElement).value).toBe('Write docs');
    expect(within(screen.getByLabelText('Project Work Area')).queryByRole('alert')).toBeNull();

    fireEvent.submit(drawer.querySelector('form')!);
    await waitFor(() => expect(location.pathname).toBe('/projects/P0001/tasks/T0005'));
    expect(createTask).toHaveBeenLastCalledWith('P0001', { title: 'Write docs', description: 'Context', acceptance_criteria: 'Reviewed' });
  });

  it('does not yank the user to the new Task when they already left the form', async () => {
    const pending = deferred<ReturnType<typeof task>>();
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), createTask: vi.fn(() => pending.promise) });
    at('/projects/P0001/new-task');
    render(<App api={api} />);
    await screen.findByLabelText('Новая Task');
    fillTask();
    fireEvent.submit(screen.getByLabelText('Новая Task').querySelector('form')!);
    fireEvent.click(screen.getByLabelText('Закрыть новую задачу'));
    await act(async () => { pending.resolve(task('T0006', 'P0001')); await pending.promise; });
    expect(location.pathname).toBe('/projects/P0001');
  });
});

describe('Workspace repair', () => {
  const corrupt = project('P0001', { workspace_state: 'needs_reconciliation', workspace_problem: 'corrupt' });

  it('requires an explicit confirmation before calling repair, then shows the quarantine notice', async () => {
    const repairProject = vi.fn(async () => ({ ...P1, repair: { backup_id: 'B-1' } }));
    const listProjects = vi.fn().mockResolvedValueOnce([corrupt]).mockResolvedValue([P1]);
    const api = stubApi({ listProjects, repairProject });
    at('/projects/P0001');
    render(<App api={api} />);
    const button = await screen.findByText('Восстановить манифест');
    fireEvent.click(button);
    expect(repairProject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText(/Подтверждаю замену манифеста/));
    fireEvent.click(button);
    expect(await screen.findByRole('status')).toBeTruthy();
    expect(screen.getByRole('status').textContent).toBe('Манифест восстановлен. Копия сохранена в карантин: B-1');
    expect(repairProject).toHaveBeenCalledTimes(1);
    expect(repairProject).toHaveBeenCalledWith('P0001');
  });

  it('never shows a success notice when the repair outcome is uncertain (503)', async () => {
    const detail = 'Workspace repair outcome is uncertain: fsync failed';
    const api = stubApi({ listProjects: vi.fn(async () => [corrupt]), repairProject: vi.fn(async () => { throw new ApiError(detail, 'server', 503); }) });
    at('/projects/P0001');
    render(<App api={api} />);
    fireEvent.click(await screen.findByLabelText(/Подтверждаю замену манифеста/));
    fireEvent.click(screen.getByText('Восстановить манифест'));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(detail));
    expect(screen.queryByRole('status')).toBeNull();
  });

  it.each(['oversized', 'foreign', 'unsafe'] as const)('offers no automatic action for %s manifests', async problem => {
    const api = stubApi({ listProjects: vi.fn(async () => [project('P0001', { workspace_state: 'needs_reconciliation', workspace_problem: problem })]) });
    at('/projects/P0001');
    render(<App api={api} />);
    expect((await screen.findAllByText(/нужно ручное разрешение/)).length).toBe(1);
    expect(screen.queryByText('Восстановить манифест')).toBeNull();
    expect(screen.queryByText('Проверить Workspace')).toBeNull();
  });

  it('keeps a failed Task reconcile error visible in the Task layer', async () => {
    const pendingTask = task('T0001', 'P0001', { workspace_state: 'needs_reconciliation', workspace_problem: 'missing' });
    const api = stubApi({
      listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [pendingTask]),
      reconcileTask: vi.fn(async () => { throw new ApiError('Workspace is not writable; retry reconciliation', 'server', 503); }),
    });
    at('/projects/P0001/tasks/T0001');
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('Проверить Workspace', { selector: '.task-drawer button' }));
    const drawer = screen.getByLabelText('Task Work Area');
    await waitFor(() => expect(within(drawer).getByRole('alert').textContent).toBe('Workspace is not writable; retry reconciliation'));
    expect(api.reconcileTask).toHaveBeenCalledWith('P0001', 'T0001');
  });
});

describe('Dashboard loading, failure and retry', () => {
  const down = () => new ApiError('Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.', 'network', null);
  const EMPTY = /Проектов пока нет/;
  const dashboard = () => document.querySelector('main.dashboard') as HTMLElement;

  it('shows a loading state, not the empty copy, before the first list arrives', async () => {
    const pending = deferred<ReturnType<typeof project>[]>();
    render(<App api={stubApi({ listProjects: vi.fn(() => pending.promise) })} />);
    expect(within(dashboard()).getByText('Загрузка проектов…')).toBeTruthy();
    expect(screen.queryByText(EMPTY)).toBeNull();
    await act(async () => { pending.resolve([]); await pending.promise; });
    expect(await screen.findByText(EMPTY)).toBeTruthy();
  });

  it('shows failure with Retry (never the empty copy) and recovers on Retry', async () => {
    const listProjects = vi.fn().mockRejectedValueOnce(down()).mockResolvedValue([P1]);
    const api = stubApi({ listProjects });
    render(<App api={api} />);
    expect(await within(dashboard()).findByText('Не удалось загрузить проекты.')).toBeTruthy();
    expect(within(dashboard()).getByRole('alert').textContent).toContain('Не удалось связаться с ядром ROSC');
    expect(screen.queryByText(EMPTY)).toBeNull();

    fireEvent.click(within(dashboard()).getByText('Повторить'));
    expect(await screen.findByText('Project P0001')).toBeTruthy();
    expect(listProjects).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByText('Не удалось загрузить проекты.')).toBeNull();
    for (const m of [api.createProject, api.createTask, api.reconcileProject, api.repairProject]) expect(m).not.toHaveBeenCalled();
  });

  it('recovers from a failed load into a legitimate empty state', async () => {
    const listProjects = vi.fn().mockRejectedValueOnce(down()).mockResolvedValue([]);
    render(<App api={stubApi({ listProjects })} />);
    fireEvent.click(await within(dashboard()).findByText('Повторить'));
    expect(await screen.findByText(EMPTY)).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('keeps cached Projects visible and the failure/Retry available after a failed background refresh', async () => {
    const createProject = vi.fn().mockRejectedValue(new ApiError('Project root overlaps another registered Project', 'conflict', 409));
    const listProjects = vi.fn().mockResolvedValueOnce([P1]).mockRejectedValueOnce(down()).mockResolvedValue([P1, P2]);
    render(<App api={stubApi({ createProject, listProjects })} />);
    await screen.findByText('Project P0001');
    fireEvent.click(screen.getByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    fireEvent.change(within(form).getByLabelText('Название'), { target: { value: 'N' } });
    fireEvent.change(within(form).getByLabelText('Описание'), { target: { value: 'D' } });
    fireEvent.change(within(form).getByLabelText('Корневая директория'), { target: { value: '/tmp/n' } });
    fireEvent.submit(form);
    await waitFor(() => expect(within(form).getByRole('alert').textContent).toBe('Project root overlaps another registered Project'));
    await waitFor(() => expect(listProjects).toHaveBeenCalledTimes(2));
    expect(within(form).getByRole('alert').textContent).toBe('Project root overlaps another registered Project');
    fireEvent.click(screen.getByLabelText('Закрыть'));

    expect(within(dashboard()).getByText('Project P0001')).toBeTruthy();
    expect(within(dashboard()).getByText('Не удалось загрузить проекты.')).toBeTruthy();
    fireEvent.click(within(dashboard()).getByText('Повторить'));
    expect(await screen.findByText('Project P0002')).toBeTruthy();
    expect(screen.queryByText('Не удалось загрузить проекты.')).toBeNull();
    expect(createProject).toHaveBeenCalledTimes(1);
  });

  it.each([
    ['the close button', () => fireEvent.click(screen.getByLabelText('Закрыть'))],
    ['the backdrop', () => fireEvent.click(document.querySelector('.modal-back')!)],
  ])('keeps the Dashboard failure and Retry after opening and dismissing the modal (%s); reopened form is clean', async (_n, dismiss) => {
    const listProjects = vi.fn().mockRejectedValueOnce(down()).mockResolvedValue([P1]);
    render(<App api={stubApi({ listProjects })} />);
    await within(dashboard()).findByText('Не удалось загрузить проекты.');

    fireEvent.click(screen.getByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    expect(within(form).queryByRole('alert')).toBeNull();
    dismiss();
    expect(screen.queryByText('Добавить проект', { selector: 'h2' })).toBeNull();
    expect(within(dashboard()).getByText('Не удалось загрузить проекты.')).toBeTruthy();
    expect(within(dashboard()).getByRole('alert').textContent).toContain('Не удалось связаться с ядром ROSC');
    expect(screen.queryByText(EMPTY)).toBeNull();

    fireEvent.click(screen.getByText('＋ Добавить проект'));
    expect(within(screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!).queryByRole('alert')).toBeNull();
    fireEvent.click(screen.getByLabelText('Закрыть'));

    fireEvent.click(within(dashboard()).getByText('Повторить'));
    expect(await screen.findByText('Project P0001')).toBeTruthy();
  });

  it('assigns a list failure that arrives while the modal is open to the Dashboard, not the form', async () => {
    const late = deferred<ReturnType<typeof project>[]>();
    const listProjects = vi.fn().mockReturnValueOnce(late.promise).mockResolvedValue([P1]);
    render(<App api={stubApi({ listProjects })} />);
    fireEvent.click(screen.getByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    await act(async () => { late.reject(down()); await late.promise.catch(() => undefined); });
    expect(within(form).queryByRole('alert')).toBeNull();

    fireEvent.click(screen.getByLabelText('Закрыть'));
    expect(within(dashboard()).getByText('Не удалось загрузить проекты.')).toBeTruthy();
    expect(within(dashboard()).getByRole('alert').textContent).toContain('Не удалось связаться с ядром ROSC');
    expect(screen.queryByText(EMPTY)).toBeNull();
  });

  it('does not let a registration 409 become a Dashboard error, nor a Dashboard failure a modal error, across close/reopen', async () => {
    const createProject = vi.fn().mockRejectedValue(new ApiError('Project root overlaps another registered Project', 'conflict', 409));
    const listProjects = vi.fn().mockRejectedValue(down());
    render(<App api={stubApi({ createProject, listProjects })} />);
    await within(dashboard()).findByText('Не удалось загрузить проекты.');
    fireEvent.click(screen.getByText('＋ Добавить проект'));
    const form = screen.getByText('Добавить проект', { selector: 'h2' }).closest('form')!;
    fireEvent.change(within(form).getByLabelText('Название'), { target: { value: 'N' } });
    fireEvent.change(within(form).getByLabelText('Описание'), { target: { value: 'D' } });
    fireEvent.change(within(form).getByLabelText('Корневая директория'), { target: { value: '/tmp/n' } });
    fireEvent.submit(form);
    await waitFor(() => expect(within(form).getByRole('alert').textContent).toBe('Project root overlaps another registered Project'));
    fireEvent.click(screen.getByLabelText('Закрыть'));
    expect(screen.queryByText('Project root overlaps another registered Project')).toBeNull();
    expect(within(dashboard()).getByRole('alert').textContent).toContain('Не удалось связаться с ядром ROSC');
    expect(createProject).toHaveBeenCalledTimes(1);
  });
});
