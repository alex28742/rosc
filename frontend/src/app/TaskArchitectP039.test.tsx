import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import type { ConversationMessage, MessageDelivery } from '../core/models';
import { App } from './App';
import { architectStatus, conversationMessage, conversationPage, deferred, ownerParticipant, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001'), T2 = task('T0002', 'P0001');
const panel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const live = () => architectStatus({ state: 'ready', can_activate: false, can_deactivate: true });
const delivery = (over: Partial<MessageDelivery> = {}): MessageDelivery =>
  ({ state: 'pending', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: true, ...over });
const architectReply = (body: string): ConversationMessage => ({
  ...conversationMessage(body), message_id: `reply-${body}`, ordinal: 2,
  sender: ownerParticipant({ kind: 'architect', display_name: 'Architect', agent_id: 'agent-1' }), delivery: undefined, recipient: null,
});
const open = (extra = {}) => stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]), ...extra });

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Task Conversation with a live Project Architect (P039)', () => {
  it('shows the real Architect availability, hands an addressed message over and shows its reply in the same chat', async () => {
    const owner = conversationMessage('Что делаем в T1?', { recipient: 'architect', ordinal: 1, delivery: delivery({ state: 'not_sent', can_reconcile: false }) });
    let phase: 'saved' | 'pending' | 'done' = 'saved';
    const getTaskConversation = vi.fn(async () => conversationPage({ task_id: 'T0001', messages:
      phase === 'saved' ? [] : phase === 'pending' ? [{ ...owner, delivery: delivery() }]
        : [{ ...owner, delivery: delivery({ state: 'delivered', reply_message_id: 'r1', can_reconcile: false }) }, architectReply('Отвечаю по T1')] }));
    const send = vi.fn(async () => { phase = 'pending'; setTimeout(() => { phase = 'done'; }, 50); return { delivery: delivery() }; });
    const api = open({ getArchitect: vi.fn(async () => live()), getTaskConversation,
      sendTaskConversationMessage: vi.fn(async () => ({ message: owner, replayed: false })), sendTaskMessageToArchitect: send });
    render(<App api={api} />);
    const view = within(await screen.findByLabelText('Task Conversation'));
    await view.findByText('Architect: активен (проверен)');
    fireEvent.change(view.getByLabelText('Адресат сообщения'), { target: { value: 'architect' } });
    fireEvent.change(view.getByLabelText('Сообщение в Task Conversation'), { target: { value: 'Что делаем в T1?' } });
    fireEvent.click(view.getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('P0001', 'T0001', owner.message_id));
    expect(await view.findByText('Отправлено Architect, ожидается ответ…')).toBeTruthy();
    // the history is re-read without a manual refresh and the real reply appears
    expect(await view.findByText('Отвечаю по T1', {}, { timeout: 6000 })).toBeTruthy();
    expect(view.getByText('Architect ответил в этой задаче')).toBeTruthy();
    expect(panel().querySelector('.rosc-chat-message--architect')).toBeTruthy();
    expect(api.sendConversationMessage).not.toHaveBeenCalled(); // nothing was written into the Project Conversation
  }, 15000);

  it('keeps an addressed message not_sent while the Architect is inactive and sends it only on the explicit action', async () => {
    const saved = conversationMessage('Сохранено заранее', { recipient: 'architect', ordinal: 1, delivery: delivery({ state: 'not_sent', can_reconcile: false }) });
    let status = architectStatus();
    const send = vi.fn(async () => ({ delivery: delivery() }));
    const api = open({ getArchitect: vi.fn(async () => status), sendTaskMessageToArchitect: send,
      getTaskConversation: vi.fn(async () => conversationPage({ task_id: 'T0001', messages: [saved] })) });
    render(<App api={api} />);
    const view = within(await screen.findByLabelText('Task Conversation'));
    await view.findByText('Сохранено · не отправлено Architect');
    expect(view.getByText('Architect: не активирован')).toBeTruthy();
    const button = view.getByRole('button', { name: 'Отправить Architect' }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(send).not.toHaveBeenCalled(); // opening the Task or an old saved message never sends anything
    status = live();
    await waitFor(() => expect((view.getByRole('button', { name: 'Отправить Architect' }) as HTMLButtonElement).disabled).toBe(false),
      { timeout: 12000 });
    fireEvent.click(view.getByRole('button', { name: 'Отправить Architect' }));
    await waitFor(() => expect(send).toHaveBeenCalledWith('P0001', 'T0001', saved.message_id));
  }, 20000);

  it('reports a refused send honestly and leaves the message not_sent', async () => {
    const saved = conversationMessage('Не уйдёт', { recipient: 'architect', ordinal: 1, delivery: delivery({ state: 'not_sent', can_reconcile: false }) });
    const send = vi.fn(async () => { throw new ApiError('Activate the Architect before sending', 'conflict', 409); });
    const api = open({ getArchitect: vi.fn(async () => live()), sendTaskMessageToArchitect: send,
      getTaskConversation: vi.fn(async () => conversationPage({ task_id: 'T0001', messages: [saved] })) });
    render(<App api={api} />);
    const view = within(await screen.findByLabelText('Task Conversation'));
    await view.findByText('Architect: активен (проверен)');
    fireEvent.click(view.getByRole('button', { name: 'Отправить Architect' }));
    expect((await view.findByRole('alert')).textContent).toContain('Activate the Architect');
    expect(view.getByText('Сохранено · не отправлено Architect')).toBeTruthy();
  });

  it('shows unknown and failed states with their explicit actions and never an invented reply', async () => {
    const unknown = conversationMessage('Неясно', { recipient: 'architect', ordinal: 1, delivery: delivery({ state: 'unknown', error_code: 'timeout' }) });
    const failed = conversationMessage('Сбой', { recipient: 'architect', ordinal: 2, message_id: 'm-failed',
      delivery: delivery({ state: 'failed', error_code: 'session_not_ready', can_retry: true, can_reconcile: false }) });
    const reconcile = vi.fn(async () => ({ delivery: delivery({ state: 'unknown', error_code: 'timeout' }) }));
    const retry = vi.fn(async () => ({ delivery: delivery() }));
    const api = open({ getArchitect: vi.fn(async () => live()), reconcileDelivery: reconcile, retryDelivery: retry,
      getTaskConversation: vi.fn(async () => conversationPage({ task_id: 'T0001', messages: [unknown, failed] })) });
    render(<App api={api} />);
    const view = within(await screen.findByLabelText('Task Conversation'));
    await view.findByText(/Результат доставки неизвестен/);
    expect(view.queryByText('Architect ответил в этой задаче')).toBeNull();
    fireEvent.click(view.getByRole('button', { name: 'Проверить результат' }));
    await waitFor(() => expect(reconcile).toHaveBeenCalledWith('P0001', unknown.message_id));
    fireEvent.click(view.getByRole('button', { name: 'Повторить отправку' }));
    await waitFor(() => expect(retry).toHaveBeenCalledWith('P0001', 'm-failed'));
  });

  it('never paints a late Architect reply of the previous Task into the open Task', async () => {
    const slow = deferred<ReturnType<typeof conversationPage>>();
    const getTaskConversation = vi.fn(async (_p: string, taskId: string) =>
      taskId === 'T0001' ? slow.promise : conversationPage({ task_id: 'T0002', messages: [conversationMessage('Только T2')] }));
    render(<App api={open({ getArchitect: vi.fn(async () => live()), getTaskConversation })} />);
    await screen.findByLabelText('Task Work Area');
    history.pushState(null, '', '/projects/P0001/tasks/T0002');
    window.dispatchEvent(new PopStateEvent('popstate'));
    await screen.findByText('Только T2');
    slow.resolve(conversationPage({ task_id: 'T0001', messages: [architectReply('Ответ для T1')] }));
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(screen.queryByText('Ответ для T1')).toBeNull();
  });
});
