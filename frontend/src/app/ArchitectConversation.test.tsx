import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import type { MessageDelivery } from '../core/models';
import { App } from './App';
import { architectStatus, conversationMessage, conversationPage, deferred, ownerParticipant, project, stubApi } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001'), P2 = project('P0002');
const panel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const delivery = (over: Partial<MessageDelivery> = {}): MessageDelivery => ({
  state: 'not_sent', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: false, ...over,
});
const architectParticipant = ownerParticipant({ participant_id: 'participant-architect', kind: 'architect',
  display_name: 'Architect', actor_id: 'agent-1', assurance: 'adapter_verified', agent_id: 'agent-1' });
const open = (over = {}) => stubApi({ listProjects: vi.fn(async () => [P1, P2]), ...over });

beforeEach(() => at('/projects/P0001'));
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('Architect activation control', () => {
  it('shows an inactive Architect and starts nothing by opening the Project or sending text', async () => {
    const api = open();
    render(<App api={api} />);
    expect(await screen.findByText('Architect: не активирован')).toBeTruthy();
    expect(api.getArchitect).toHaveBeenCalledWith('P0001');
    const box = within(panel()).getByLabelText('Сообщение в Project Conversation');
    fireEvent.change(box, { target: { value: 'Привет' } });
    fireEvent.click(within(panel()).getByText('Отправить'));
    await waitFor(() => expect(api.sendConversationMessage).toHaveBeenCalled());
    expect(api.activateArchitect).not.toHaveBeenCalled();
    expect(within(panel()).getAllByText(/Architect не активирован — доставки и ответа не будет/).length).toBeGreaterThan(0);
  });

  it('activates only on the explicit button, shows activating, then verified active', async () => {
    const pending = deferred<ReturnType<typeof architectStatus>>();
    const api = open({ activateArchitect: vi.fn(() => pending.promise) });
    render(<App api={api} />);
    fireEvent.click(await screen.findByText('Активировать Architect'));
    expect(await screen.findByText('Architect: запускается…')).toBeTruthy();
    expect(screen.queryByText('Активировать Architect')).toBeNull();
    pending.resolve(architectStatus({ state: 'ready', can_activate: false, can_deactivate: true }));
    expect(await screen.findByText('Architect: активен (проверен)')).toBeTruthy();
    expect(api.activateArchitect).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Остановить Architect')).toBeTruthy();
    expect(screen.getByText(/Ранее сохранённые сообщения не пересылаются/)).toBeTruthy();
  });

  it('never presents an unconfigured, unavailable or failed runtime as active', async () => {
    const api = open({ getArchitect: vi.fn(async () => architectStatus({
      state: 'not_configured', detail: 'runtime_credential_missing', can_activate: false })) });
    render(<App api={api} />);
    expect(await screen.findByText('Architect: не настроен')).toBeTruthy();
    expect(screen.getByText(/OPENCODE_PASSWORD/)).toBeTruthy();
    expect(screen.queryByText('Активировать Architect')).toBeNull();
  });

  it('reports a failed activation with a recovery action and keeps the Owner conversation usable', async () => {
    const activateArchitect = vi.fn()
      .mockResolvedValueOnce(architectStatus({ state: 'error', detail: 'start_timeout' }))
      .mockResolvedValueOnce(architectStatus({ state: 'ready', can_activate: false, can_deactivate: true }));
    render(<App api={open({ activateArchitect })} />);
    fireEvent.click(await screen.findByText('Активировать Architect'));
    expect(await screen.findByText('Architect: ошибка')).toBeTruthy();
    expect(screen.getByText('Runtime не запустился вовремя.')).toBeTruthy();
    fireEvent.click(screen.getByText('Повторить активацию Architect'));
    expect(await screen.findByText('Architect: активен (проверен)')).toBeTruthy();
  });

  it('surfaces an API failure of activation without claiming activity', async () => {
    const activateArchitect = vi.fn().mockRejectedValue(new ApiError('Не удалось связаться с ядром ROSC.', 'network', null));
    render(<App api={open({ activateArchitect })} />);
    fireEvent.click(await screen.findByText('Активировать Architect'));
    expect(await screen.findByText(/Не удалось связаться с ядром ROSC/)).toBeTruthy();
    expect(screen.getByText('Architect: не активирован')).toBeTruthy();
  });

  it('keeps the Architect state per Project when switching', async () => {
    const getArchitect = vi.fn(async (id: string) => id === 'P0001'
      ? architectStatus({ state: 'ready', can_activate: false, can_deactivate: true })
      : architectStatus({ project_id: 'P0002', state: 'not_configured', detail: 'no_active_architect_assignment', can_activate: false }));
    render(<App api={open({ getArchitect })} />);
    expect(await screen.findByText('Architect: активен (проверен)')).toBeTruthy();
    at('/projects/P0002');
    window.dispatchEvent(new PopStateEvent('popstate'));
    expect(await screen.findByText('Architect: не настроен')).toBeTruthy();
    expect(screen.queryByText('Architect: активен (проверен)')).toBeNull();
  });
});

describe('Architect reply and delivery status', () => {
  const ready = architectStatus({ state: 'ready', can_activate: false, can_deactivate: true });

  it('renders the persisted Architect reply as Architect, not Owner, as plain text', async () => {
    const getConversation = vi.fn(async () => conversationPage({ messages: [
      conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state: 'delivered', reply_message_id: 'message-2' }) }),
      conversationMessage('<b>Ответ</b> Architect', { ordinal: 2, sender: architectParticipant }),
    ] }));
    render(<App api={open({ getArchitect: vi.fn(async () => ready), getConversation })} />);
    const reply = await waitFor(() => {
      const el = panel().querySelector('.rosc-chat-message--architect');
      if (!el) throw new Error('not rendered');
      return el as HTMLElement;
    });
    expect(within(reply).getByText('Architect')).toBeTruthy();
    expect(reply.querySelector('.rosc-chat-body')?.textContent).toBe('<b>Ответ</b> Architect');
    expect(reply.querySelector('b')).toBeNull();
    expect(panel().querySelectorAll('.rosc-chat-message--owner').length).toBe(1);
    expect(within(panel()).getByText('Architect ответил')).toBeTruthy();
  });

  it('polls while a reply is pending and then shows the reply without a reload', async () => {
    let calls = 0;
    const getConversation = vi.fn(async () => {
      calls += 1;
      return calls < 2
        ? conversationPage({ messages: [conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state: 'pending' }) })] })
        : conversationPage({ messages: [
          conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state: 'delivered', reply_message_id: 'message-2' }) }),
          conversationMessage('Готов ответ', { ordinal: 2, sender: architectParticipant })] });
    });
    render(<App api={open({ getArchitect: vi.fn(async () => ready), getConversation })} />);
    expect(await screen.findByText(/ожидается ответ/)).toBeTruthy();
    expect(await screen.findByText('Готов ответ', {}, { timeout: 4000 })).toBeTruthy();
    expect(screen.queryByText(/ожидается ответ/)).toBeNull();
  });

  it('shows failed delivery with retry only when Core allows it, and never a fake answer', async () => {
    const message = (state: MessageDelivery['state'], over: Partial<MessageDelivery>) =>
      conversationPage({ messages: [conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state, ...over }) })] });
    const getConversation = vi.fn()
      .mockResolvedValueOnce(message('failed', { error_code: 'session_not_ready', can_retry: true }))
      .mockResolvedValue(message('pending', { can_reconcile: true }));
    const api = open({ getArchitect: vi.fn(async () => ready), getConversation });
    render(<App api={api} />);
    expect(await screen.findByText(/Не доставлено Architect/)).toBeTruthy();
    expect(panel().querySelector('.rosc-chat-message--architect')).toBeNull();
    fireEvent.click(screen.getByText('Повторить отправку'));
    await waitFor(() => expect(api.retryDelivery).toHaveBeenCalledWith('P0001', 'message-1'));
    expect(await screen.findByText(/ожидается ответ/)).toBeTruthy();
  });

  it('offers a lookup, not a re-send, for an unknown delivery', async () => {
    const getConversation = vi.fn(async () => conversationPage({ messages: [
      conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state: 'unknown', error_code: 'timeout', can_reconcile: true }) })] }));
    const api = open({ getArchitect: vi.fn(async () => ready), getConversation });
    render(<App api={api} />);
    expect(await screen.findByText(/повторная отправка не выполнялась/)).toBeTruthy();
    expect(screen.queryByText('Повторить отправку')).toBeNull();
    fireEvent.click(screen.getByText('Проверить результат'));
    await waitFor(() => expect(api.reconcileDelivery).toHaveBeenCalledWith('P0001', 'message-1'));
    expect(api.retryDelivery).not.toHaveBeenCalled();
  });

  it('reports a delivery action failure without hiding the conversation', async () => {
    const reconcileDelivery = vi.fn().mockRejectedValue(new ApiError('Не удалось связаться с ядром ROSC.', 'network', null));
    const getConversation = vi.fn(async () => conversationPage({ messages: [
      conversationMessage('Вопрос', { ordinal: 1, delivery: delivery({ state: 'unknown', can_reconcile: true }) })] }));
    render(<App api={open({ getArchitect: vi.fn(async () => ready), getConversation, reconcileDelivery })} />);
    fireEvent.click(await screen.findByText('Проверить результат'));
    expect(await screen.findByText(/Не удалось связаться с ядром ROSC/)).toBeTruthy();
    expect(screen.getByText('Вопрос')).toBeTruthy();
  });
});
