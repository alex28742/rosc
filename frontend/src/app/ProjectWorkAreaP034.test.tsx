import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001', { root_path: '/tmp/P0001' });
const P2 = project('P0002', { root_path: '/tmp/P0002' });

const tasks = (count: number) => Array.from({ length: count }, (_, index) => {
  const number = String(index + 1).padStart(4, '0');
  return task(`T${number}`, 'P0001', {
    title: index === 0 ? 'Alpha release' : `Task ${number}`,
    description: index === 0 ? 'alpha description' : `Description ${number}`,
    status: index % 3 === 0 ? 'Draft' : 'Accepted',
  });
});

const open = (over = {}) => stubApi({ listProjects: vi.fn(async () => [P1, P2]), ...over });
const drawer = () => screen.getByLabelText('Project Work Area') as HTMLElement;
const panel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;

beforeEach(() => { window.localStorage.clear(); at('/projects/P0001'); });
afterEach(() => { cleanup(); document.body.style.overflow = ''; window.localStorage.clear(); });

describe('P034 Aa · Вид text scale', () => {
  it('changes the Project size, persists it locally and keeps the Task size independent', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => [task('T0001', 'P0001')]) })} />);
    await screen.findByText('Задачи проекта');

    fireEvent.click(within(drawer()).getByRole('button', { name: 'Aa · Вид' }));
    const projectMenu = screen.getByRole('menu', { name: 'Размер текста: Project Work Area' });
    fireEvent.click(within(projectMenu).getByRole('menuitemradio', { name: '110%' }));

    expect(drawer().style.getPropertyValue('--rosc-text-scale')).toBe('1.1');
    expect(window.localStorage.getItem('rosc.text-scale.project:P0001')).toBe('110');

    fireEvent.click(within(drawer()).getByText('T0001 · Task T0001'));
    const taskDrawer = await screen.findByLabelText('Task Work Area');
    expect((taskDrawer as HTMLElement).style.getPropertyValue('--rosc-text-scale')).toBe('1');

    fireEvent.click(within(taskDrawer).getByRole('button', { name: 'Aa · Вид' }));
    const taskMenu = screen.getByRole('menu', { name: 'Размер текста: Task Work Area' });
    fireEvent.click(within(taskMenu).getByRole('menuitemradio', { name: '130%' }));

    expect((taskDrawer as HTMLElement).style.getPropertyValue('--rosc-text-scale')).toBe('1.3');
    expect(drawer().style.getPropertyValue('--rosc-text-scale')).toBe('1.1');
    expect(window.localStorage.getItem('rosc.text-scale.task:T0001')).toBe('130');
  });

  it('discards an unsupported stored size instead of applying it', async () => {
    window.localStorage.setItem('rosc.text-scale.project:P0001', '42');
    render(<App api={open({ listTasks: vi.fn(async () => [task('T0001', 'P0001')]) })} />);
    await screen.findByText('Задачи проекта');
    expect(window.localStorage.getItem('rosc.text-scale.project:P0001')).toBeNull();
    expect(drawer().style.getPropertyValue('--rosc-text-scale')).toBe('1');
  });
});

describe('P034 Task list header, filters, views and pagination', () => {
  it('counts the real loaded list, filters locally, switches views and resets the page', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => tasks(12)) })} />);
    await screen.findByText('Задачи проекта');
    expect(screen.getByText('12 из 12')).toBeTruthy();
    expect(document.querySelectorAll('.rosc-task-item').length).toBe(10);
    expect(screen.getByText('Страница 1 из 2')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: 'Вперёд ›' }));
    expect(screen.getByText('Страница 2 из 2')).toBeTruthy();
    expect(document.querySelectorAll('.rosc-task-item').length).toBe(2);

    const search = screen.getByLabelText('Поиск по задачам');
    fireEvent.change(search, { target: { value: 'Alpha release' } });
    expect(screen.getByText('1 из 12')).toBeTruthy();
    expect(document.querySelectorAll('.rosc-task-item').length).toBe(1);
    expect(screen.queryByText('Страница 1 из 2')).toBeNull();

    fireEvent.change(search, { target: { value: '' } });
    const filter = screen.getByLabelText('Фильтр задач');
    fireEvent.change(filter, { target: { value: 'Draft' } });
    expect(screen.getByText('4 из 12')).toBeTruthy();

    fireEvent.click(screen.getByRole('button', { name: /Карточки/ }));
    expect(document.querySelectorAll('.rosc-task-card').length).toBe(4);
    expect(document.querySelectorAll('.rosc-task-item').length).toBe(0);
    expect(screen.getByRole('button', { name: /Карточки/ }).getAttribute('aria-pressed')).toBe('true');

    fireEvent.change(search, { target: { value: 'nothing matches' } });
    expect(screen.getByText('По выбранным условиям задач нет.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Сбросить фильтры' }));
    expect(screen.getByText('12 из 12')).toBeTruthy();
    expect(document.querySelectorAll('.rosc-task-card').length).toBe(10);
  });

  it('does not pretend archive or trash collections exist', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => tasks(2)) })} />);
    await screen.findByText('Задачи проекта');
    const collection = screen.getByLabelText('Раздел задач') as HTMLSelectElement;
    expect(collection.disabled).toBe(false);
    const options = [...collection.options].map(option => ({ text: option.textContent, disabled: option.disabled }));
    expect(options).toEqual([
      { text: 'Текущие', disabled: false },
      { text: 'Архив — недоступно', disabled: true },
      { text: 'Корзина — недоступно', disabled: true },
    ]);
  });
});

describe('P034 Task item menu', () => {
  it('opens a three-item menu without opening the Task; only Open is actionable', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => [task('T0001', 'P0001')]) })} />);
    await screen.findByText('T0001 · Task T0001');

    fireEvent.click(screen.getByLabelText('Действия с задачей Task T0001'));
    const menu = screen.getByRole('menu', { name: 'Действия с задачей Task T0001' });
    expect(screen.queryByLabelText('Task Work Area')).toBeNull();
    expect(within(menu).getByRole('menuitem', { name: /Открыть задачу/ })).toBeTruthy();
    expect((within(menu).getByRole('menuitem', { name: /Архивировать задачу/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((within(menu).getByRole('menuitem', { name: /Переместить в корзину/ }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu', { name: 'Действия с задачей Task T0001' })).toBeNull();

    fireEvent.click(screen.getByLabelText('Действия с задачей Task T0001'));
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('menu', { name: 'Действия с задачей Task T0001' })).toBeNull();

    fireEvent.click(screen.getByLabelText('Действия с задачей Task T0001'));
    fireEvent.click(within(screen.getByRole('menu', { name: 'Действия с задачей Task T0001' }))
      .getByRole('menuitem', { name: /Открыть задачу/ }));
    expect(await screen.findByLabelText('Task Work Area')).toBeTruthy();
  });

  it('offers the same menu in card view', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => [task('T0001', 'P0001')]) })} />);
    await screen.findByText('T0001 · Task T0001');
    fireEvent.click(screen.getByRole('button', { name: /Карточки/ }));
    expect(document.querySelectorAll('.rosc-task-card').length).toBe(1);
    fireEvent.click(screen.getByLabelText('Действия с задачей Task T0001'));
    expect(screen.getByRole('menu', { name: 'Действия с задачей Task T0001' })).toBeTruthy();
    expect(screen.queryByLabelText('Task Work Area')).toBeNull();
  });
});

describe('P034 Project footer actions', () => {
  it('stays visible, shows the real path, links to Settings and keeps archive disabled', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => []) })} />);
    await screen.findByText('Открытие Project не запускает агентов');

    fireEvent.click(screen.getByRole('button', { name: /Действия проекта/ }));
    const menu = screen.getByRole('menu', { name: 'Действия проекта' });
    expect((within(menu).getByRole('menuitem', { name: /Архивировать проект/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(menu).getByText('/tmp/P0001')).toBeTruthy();
    fireEvent.click(within(menu).getByRole('menuitem', { name: 'Настройки проекта' }));

    expect(await screen.findByText('Агенты проекта · назначения')).toBeTruthy();
    expect(screen.getByText('Открытие Project не запускает агентов')).toBeTruthy();
  });
});

describe('P034 Conversation footer and duplicate Architect chip', () => {
  it('has no duplicate Architect chip in the conversation header and keeps one in the Project header/card', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => []) })} />);
    await screen.findByText('Project Architect · не активирован');
    expect(within(panel()).queryByText(/^Architect:/)).toBeNull();
    expect(within(panel()).queryByRole('button', { name: 'Поиск по сообщениям' })).toBeTruthy();
    expect(screen.getByLabelText('Агенты проекта')).toBeTruthy();
  });

  it('shows attachment and recipient affordances as visible, disabled and honest', async () => {
    render(<App api={open({ listTasks: vi.fn(async () => []) })} />);
    await screen.findByText('Сообщений пока нет.');
    const attach = within(panel()).getByLabelText('Прикрепить файлы — пока недоступно') as HTMLButtonElement;
    const recipients = within(panel()).getByLabelText('@ Адресаты — пока недоступно') as HTMLButtonElement;
    expect(attach.disabled).toBe(true);
    expect(recipients.disabled).toBe(true);
    expect(attach.title).toBe('Вложения пока недоступны');
    expect(recipients.title).toBe('Адресация участников пока недоступна');
    const send = within(panel()).getByRole('button', { name: 'Отправить' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.change(within(panel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'текст' } });
    expect(send.disabled).toBe(false);
  });
});
