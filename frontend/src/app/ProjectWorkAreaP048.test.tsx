import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { project, stubApi, task } from './testing';

// P048 (UX v0.8): compact «О проекте», the «Агенты» tab and the narrow-width pane switch for Project and Task.
// Pane geometry itself is CSS (verified in the browser); these tests pin the behaviour: tab routing, honest
// «О проекте» data, and that switching panes never unmounts the Conversation or the Task list state.

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001', { description: 'Первая строка описания\nВторая строка', docs_entrypoint: 'docs/README.md' });
const open = (over = {}) => stubApi({
  listProjects: vi.fn(async () => [P1]),
  listTasks: vi.fn(async () => [task('T0001', 'P0001'), task('T0002', 'P0001', { title: 'Другая' })]),
  ...over,
});
const projectDrawer = () => screen.getByLabelText('Project Work Area');
const projectWorkspace = () => projectDrawer().querySelector('.rosc-project-workspace') as HTMLElement;

beforeEach(() => at('/projects/P0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('P048 Project «О проекте»', () => {
  it('is collapsed by default, shows the real description, and expands to honest «Нет данных» progress', async () => {
    render(<App api={open()} />);
    const toggle = await screen.findByRole('button', { name: /О проекте/ });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(within(toggle).getByText(/Первая строка описания/)).toBeTruthy();
    const details = document.getElementById(toggle.getAttribute('aria-controls')!) as HTMLElement;
    expect(details.hidden).toBe(true);
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(details.hidden).toBe(false);
    expect(within(details).getByText('docs/README.md')).toBeTruthy();
    for (const label of ['Последнее достижение', 'Где остановились', 'Следующий шаг']) expect(within(details).getByText(label)).toBeTruthy();
    expect(within(details).getAllByText('Нет данных')).toHaveLength(3);
    expect(within(details).queryByText('Draft')).toBeNull();  // never borrows a Task status as progress
    expect(screen.getByRole('tab', { name: /Tasks/ }).getAttribute('aria-selected')).toBe('true');
  });
});

describe('P048 «Агенты» tab', () => {
  it('keeps the default Tasks view free of runtime controls and opens them in «Агенты» without starting anything', async () => {
    const api = open();
    render(<App api={api} />);
    await screen.findByText('Task T0001', { exact: false });
    expect(screen.queryByRole('region', { name: 'Агенты проекта' })).toBeNull();
    expect(screen.queryByText('Активировать Architect')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Агенты' }));
    expect(await screen.findByRole('region', { name: 'Агенты проекта' })).toBeTruthy();
    expect(location.pathname).toBe('/projects/P0001/agents');
    expect(screen.getByRole('tab', { name: 'Агенты' }).getAttribute('aria-selected')).toBe('true');
    expect(api.activateArchitect).not.toHaveBeenCalled();
    expect(api.activateWorkerSession).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Настройки назначений' }));
    expect(await screen.findByText('Агенты проекта · назначения')).toBeTruthy();
    expect(location.pathname).toBe('/projects/P0001/settings');
  });
});

describe('P048 narrow-width pane switch', () => {
  it('moves only the outer Project panes: the Conversation draft and the Task search survive both ways', async () => {
    render(<App api={open()} />);
    await screen.findByText('Task T0001', { exact: false });
    const search = screen.getByLabelText('Поиск по задачам') as HTMLInputElement;
    fireEvent.change(search, { target: { value: 'T0001' } });
    const box = screen.getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'черновик' } });

    const toChat = screen.getByRole('button', { name: 'Открыть Project Conversation' });
    expect(toChat.getAttribute('aria-pressed')).toBe('false');
    fireEvent.click(toChat);
    expect(projectWorkspace().className).toContain('rosc-panes--chat');
    const back = screen.getByRole('button', { name: 'Вернуться к задачам проекта' });
    expect(back.getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(back);
    expect(projectWorkspace().className).not.toContain('rosc-panes--chat');

    expect(screen.getByLabelText('Сообщение в Project Conversation')).toBe(box);  // same element: never remounted
    expect(box.value).toBe('черновик');
    expect((screen.getByLabelText('Поиск по задачам') as HTMLInputElement).value).toBe('T0001');
  });

  it('expands a desktop-collapsed chat when opened as a pane, and a desktop collapse returns to the work pane', async () => {
    render(<App api={open()} />);
    await screen.findByText('Task T0001', { exact: false });
    fireEvent.click(screen.getByLabelText('Свернуть Project Conversation'));
    expect(projectWorkspace().className).toContain('rosc-project-workspace--collapsed');
    fireEvent.click(screen.getByRole('button', { name: 'Открыть Project Conversation' }));
    expect(projectWorkspace().className).not.toContain('rosc-project-workspace--collapsed');
    expect(projectWorkspace().className).toContain('rosc-panes--chat');
    fireEvent.click(screen.getByLabelText('Свернуть Project Conversation'));
    expect(projectWorkspace().className).not.toContain('rosc-panes--chat');
  });

  it('keeps the Task pane state separate from the Project pane underneath', async () => {
    at('/projects/P0001/tasks/T0001');
    render(<App api={open()} />);
    const taskDrawer = await screen.findByLabelText('Task Work Area');
    await waitFor(() => expect(taskDrawer.querySelector('.rosc-task-workspace')).toBeTruthy());
    const taskWorkspace = taskDrawer.querySelector('.rosc-task-workspace') as HTMLElement;
    const box = within(taskDrawer).getByLabelText('Сообщение в Task Conversation') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'по задаче' } });
    fireEvent.click(within(taskDrawer).getByRole('button', { name: 'Открыть Task Conversation' }));
    expect(taskWorkspace.className).toContain('rosc-panes--chat');
    expect(projectWorkspace().className).not.toContain('rosc-panes--chat');
    fireEvent.click(within(taskDrawer).getByRole('button', { name: 'Вернуться к рабочей области задачи' }));
    expect(taskWorkspace.className).not.toContain('rosc-panes--chat');
    expect(within(taskDrawer).getByLabelText('Сообщение в Task Conversation')).toBe(box);
    expect(box.value).toBe('по задаче');
  });
});
