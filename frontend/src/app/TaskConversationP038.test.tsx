import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import { App } from './App';
import { conversationMessage, conversationPage, deferred, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001'), T2 = task('T0002', 'P0001');
const panel = () => document.querySelector('section[aria-label="Task Conversation"]') as HTMLElement;
const open = (extra = {}) => stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]), ...extra });

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Task Conversation in the Task drawer', () => {
  it('reads this Task\'s history from Core and shows the honest empty state', async () => {
    const api = open();
    render(<App api={api} />);
    expect(await screen.findByText('Сообщений по этой задаче пока нет.')).toBeTruthy();
    expect(api.getTaskConversation).toHaveBeenCalledWith('P0001', 'T0001');
    expect(api.sendTaskConversationMessage).not.toHaveBeenCalled();
    // The Project Conversation is not the one shown in the Task drawer.
    expect(within(panel()).queryByText(/Project Conversation/)).toBeNull();
  });

  it('saves an unaddressed message as "Сохранено" and an addressed one as not delivered', async () => {
    const api = open();
    render(<App api={api} />);
    await screen.findByText('Сообщений по этой задаче пока нет.');
    const view = within(panel());
    const box = view.getByLabelText('Сообщение в Task Conversation');
    fireEvent.change(box, { target: { value: 'Без адресата' } });
    fireEvent.click(view.getByRole('button', { name: 'Сохранить' }));
    await view.findByText('Сохранено');
    expect(api.sendTaskConversationMessage).toHaveBeenLastCalledWith('P0001', 'T0001',
      expect.objectContaining({ body: 'Без адресата', recipient: null }));

    fireEvent.change(view.getByLabelText('Адресат сообщения'), { target: { value: 'architect' } });
    fireEvent.change(box, { target: { value: 'Для Architect' } });
    fireEvent.click(view.getByRole('button', { name: 'Сохранить' }));
    await view.findByText('Сохранено · не доставлено Architect');
    expect(api.sendTaskConversationMessage).toHaveBeenLastCalledWith('P0001', 'T0001',
      expect.objectContaining({ body: 'Для Architect', recipient: 'architect' }));
    expect(view.queryByText(/Architect ответил|печатает|в сети/)).toBeNull();
  });

  it('keeps the draft and retries with the same key after a failed save, without a double save', async () => {
    const send = vi.fn()
      .mockRejectedValueOnce(new ApiError('Не удалось связаться с ядром ROSC.', 'network', null))
      .mockImplementation(async (_p: string, _t: string, input: { body: string }) => ({ message: conversationMessage(input.body, { recipient: null }), replayed: false }));
    render(<App api={open({ sendTaskConversationMessage: send })} />);
    await screen.findByText('Сообщений по этой задаче пока нет.');
    const view = within(panel());
    const box = view.getByLabelText('Сообщение в Task Conversation') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Повтор' } });
    fireEvent.click(view.getByRole('button', { name: 'Сохранить' }));
    expect((await view.findByRole('alert')).textContent).toContain('Не удалось связаться');
    expect(box.value).toBe('Повтор');
    fireEvent.click(view.getByRole('button', { name: 'Сохранить' }));
    await waitFor(() => expect(box.value).toBe(''));
    expect(send.mock.calls[0][2].client_key).toBe(send.mock.calls[1][2].client_key);
    expect(view.getAllByText('Повтор').length).toBe(1);
  });

  it('shows only the open Task\'s messages and ignores a late response from the previous Task', async () => {
    const slow = deferred<ReturnType<typeof conversationPage>>();
    const getTaskConversation = vi.fn(async (_p: string, taskId: string) =>
      taskId === 'T0001' ? slow.promise : conversationPage({ task_id: 'T0002', messages: [conversationMessage('Только T2')] }));
    render(<App api={open({ getTaskConversation })} />);
    await screen.findByLabelText('Task Work Area');
    // Navigate T0001 -> T0002 while T0001's history is still loading.
    history.pushState(null, '', '/projects/P0001/tasks/T0002');
    window.dispatchEvent(new PopStateEvent('popstate'));
    await screen.findByText('Только T2');
    slow.resolve(conversationPage({ task_id: 'T0001', messages: [conversationMessage('Чужое из T1')] }));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(screen.queryByText('Чужое из T1')).toBeNull();
    expect(screen.getByText('Только T2')).toBeTruthy();
  });

  it('re-reads saved messages from Core after a fresh mount (refresh)', async () => {
    const api = open({ getTaskConversation: vi.fn(async () => conversationPage({ task_id: 'T0001',
      messages: [conversationMessage('Пережило обновление', { recipient: 'architect' })] })) });
    const first = render(<App api={api} />);
    await screen.findByText('Пережило обновление');
    first.unmount();
    render(<App api={api} />);
    expect(await screen.findByText('Пережило обновление')).toBeTruthy();
    expect(screen.getByText('Сохранено · не доставлено Architect')).toBeTruthy();
    expect(api.getTaskConversation).toHaveBeenCalledTimes(2);
  });
});
