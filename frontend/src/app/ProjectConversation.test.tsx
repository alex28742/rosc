import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '../core/api';
import { App } from './App';
import { conversationMessage, conversationPage, deferred, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001'), P2 = project('P0002');
const T1 = task('T0001', 'P0001');
const panel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;

beforeEach(() => at('/projects/P0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Project Conversation readback', () => {
  it('loads the persisted history of the open Project and renders ordered Owner messages', async () => {
    const getConversation = vi.fn(async () => conversationPage({
      messages: [conversationMessage('Первый вопрос', { ordinal: 1 }), conversationMessage('Второе уточнение', { ordinal: 2 })],
    }));
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getConversation })} />);
    await screen.findByText('Первый вопрос');
    const view = panel();
    expect(within(view).getByText('Второе уточнение')).toBeTruthy();
    expect(within(view).getAllByText('Owner').length).toBe(2);
    const bodies = [...view.querySelectorAll('.rosc-chat-body')].map(node => node.textContent);
    expect(bodies).toEqual(['Первый вопрос', 'Второе уточнение']);
    expect(getConversation).toHaveBeenCalledWith('P0001');
    expect(within(view).getByLabelText('Сообщение в Project Conversation')).toBeTruthy();
  });

  it('shows a truthful empty state and creates nothing just by opening the Project', async () => {
    const api = stubApi({ listProjects: vi.fn(async () => [P1]) });
    render(<App api={api} />);
    expect(await screen.findByText('Сообщений пока нет.')).toBeTruthy();
    expect(screen.getByText(/Первое сообщение Owner сохранится в ROSC/)).toBeTruthy();
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
    expect(api.createProject).not.toHaveBeenCalled();
    expect(api.createTask).not.toHaveBeenCalled();
  });

  it('reloads the persisted history after a browser refresh (fresh application mount)', async () => {
    const page = () => conversationPage({ messages: [conversationMessage('Пережило перезагрузку')] });
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(async () => page()) });
    const first = render(<App api={api} />);
    await screen.findByText('Пережило перезагрузку');
    first.unmount();
    render(<App api={api} />);
    expect(await screen.findByText('Пережило перезагрузку')).toBeTruthy();
    expect(api.getConversation).toHaveBeenCalledTimes(2);
  });

  it('fails honestly with Retry when history cannot be loaded, without inventing messages', async () => {
    const getConversation = vi.fn()
      .mockRejectedValueOnce(new ApiError('Не удалось связаться с ядром ROSC. Проверьте, что backend запущен, и повторите.', 'network', null))
      .mockResolvedValue(conversationPage({ messages: [conversationMessage('Загружено после повтора')] }));
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), getConversation })} />);
    const view = await waitFor(() => {
      const element = panel();
      if (!element) throw new Error('Project Conversation is not mounted yet');
      return element;
    });
    const alert = await within(view).findByRole('alert');
    expect(alert.textContent).toContain('Не удалось загрузить переписку.');
    expect(view.querySelectorAll('.rosc-chat-message').length).toBe(0);
    fireEvent.click(within(view).getByText('Повторить'));
    expect(await screen.findByText('Загружено после повтора')).toBeTruthy();
    expect(getConversation).toHaveBeenCalledTimes(2);
  });

  it('pages towards older messages on demand and keeps them in stable order', async () => {
    const latest = conversationPage({
      messages: [conversationMessage('четвёртое', { ordinal: 4 }), conversationMessage('пятое', { ordinal: 5 })],
      page: { limit: 50, has_more: true, next_before: 4 },
    });
    const older = conversationPage({
      messages: [conversationMessage('второе', { ordinal: 2 }), conversationMessage('третье', { ordinal: 3 })],
      page: { limit: 50, has_more: false, next_before: null },
    });
    const getConversation = vi.fn().mockResolvedValueOnce(latest).mockResolvedValueOnce(older);
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), getConversation })} />);
    await screen.findByText('пятое');
    expect(screen.getByText('Поиск охватывает загруженные сообщения')).toBeTruthy();
    fireEvent.click(screen.getByText('Загрузить более ранние'));
    await screen.findByText('второе');
    expect(getConversation).toHaveBeenLastCalledWith('P0001', 4);
    const order = [...panel().querySelectorAll('.rosc-chat-message')].map(node => node.getAttribute('data-message-id'));
    expect(order).toEqual(['message-2', 'message-3', 'message-4', 'message-5']);
    expect(screen.queryByText('Загрузить более ранние')).toBeNull();
  });
});

describe('Project Conversation composing', () => {
  const compose = (text: string) => {
    const textarea = within(panel()).getByLabelText('Сообщение в Project Conversation');
    fireEvent.change(textarea, { target: { value: text } });
    return textarea;
  };

  it('records an Owner message only after the server confirms it', async () => {
    const pending = deferred<{ message: ReturnType<typeof conversationMessage>; replayed: boolean }>();
    const sendConversationMessage = vi.fn(() => pending.promise);
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), sendConversationMessage });
    render(<App api={api} />);
    await screen.findByText('Сообщений пока нет.');
    const textarea = compose('Новое сообщение Owner');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));

    expect(within(panel()).getByRole('button', { name: 'Сохранение…' })).toBeTruthy();
    expect(screen.queryByText('Новое сообщение Owner', { selector: '.rosc-chat-body' })).toBeNull();
    expect(sendConversationMessage).toHaveBeenCalledTimes(1);
    const [projectId, input] = sendConversationMessage.mock.calls[0] as unknown as [string, { client_key: string; body: string }];
    expect(projectId).toBe('P0001');
    expect(input.body).toBe('Новое сообщение Owner');
    expect(input.client_key).toMatch(/^ui-[A-Za-z0-9._:-]{8,}$/);

    await act(async () => { pending.resolve({ message: conversationMessage('Новое сообщение Owner'), replayed: false }); await pending.promise; });
    expect(await screen.findByText('Новое сообщение Owner')).toBeTruthy();
    expect((textarea as HTMLTextAreaElement).value).toBe('');
  });

  it('keeps the draft and reuses the same key after a failed send, then succeeds on retry', async () => {
    const sendConversationMessage = vi.fn()
      .mockRejectedValueOnce(new ApiError('Ядро ROSC недоступно или вернуло ошибку (HTTP 503). Проверьте, что backend запущен, и повторите.', 'server', 503))
      .mockResolvedValue({ message: conversationMessage('Повторная отправка'), replayed: false });
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), sendConversationMessage })} />);
    await screen.findByText('Сообщений пока нет.');
    const textarea = compose('Повторная отправка');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));

    const alert = await within(panel()).findByRole('alert');
    expect(alert.textContent).toContain('HTTP 503');
    expect((textarea as HTMLTextAreaElement).value).toBe('Повторная отправка');
    expect(screen.queryByText('Повторная отправка', { selector: '.rosc-chat-body' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(await screen.findByText('Повторная отправка')).toBeTruthy();
    const keys = sendConversationMessage.mock.calls.map(call => (call[1] as { client_key: string }).client_key);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it('submits with Ctrl+Enter from the composer without a second mutation', async () => {
    const sendConversationMessage = vi.fn(async () => ({ message: conversationMessage('Отправлено с клавиатуры'), replayed: false }));
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), sendConversationMessage })} />);
    await screen.findByText('Сообщений пока нет.');
    const textarea = compose('Отправлено с клавиатуры');
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
    expect(await screen.findByText('Отправлено с клавиатуры')).toBeTruthy();
    expect(sendConversationMessage).toHaveBeenCalledTimes(1);
  });

  it('never renders message text as HTML', async () => {    const body = '<img src=x onerror="alert(1)"> & <b>bold</b>';
    render(<App api={stubApi({
      listProjects: vi.fn(async () => [P1]),
      getConversation: vi.fn(async () => conversationPage({ messages: [conversationMessage(body)] })),
    })} />);
    await screen.findByText(body);
    const view = panel();
    expect(view.querySelector('.rosc-chat-body')?.textContent).toBe(body);
    expect(view.querySelector('.rosc-chat-body img')).toBeNull();
    expect(view.querySelector('.rosc-chat-body b')).toBeNull();
  });

  it('labels the Architect as inactive and never claims delivery or a reply', async () => {
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]) })} />);
    await screen.findByText('Сообщений пока нет.');
    expect(screen.getByText('Project Architect · не активирован')).toBeTruthy();
    expect(screen.getByText(/доставки и ответа не будет/)).toBeTruthy();
    expect(screen.getByText('От: Owner · local_unverified')).toBeTruthy();
  });
});

describe('Project Conversation search and collapse', () => {
  const seeded = () => {
    const getConversation = vi.fn(async () => conversationPage({
      messages: [conversationMessage('alpha beta alpha', { ordinal: 1 }), conversationMessage('только beta', { ordinal: 2 })],
    }));
    return stubApi({ listProjects: vi.fn(async () => [P1]), getConversation });
  };

  it('counts client-local matches and navigates previous/next, including empty results', async () => {
    const api = seeded();
    render(<App api={api} />);
    await screen.findByText('alpha beta alpha');

    fireEvent.click(screen.getByLabelText('Поиск по сообщениям'));
    const input = screen.getByPlaceholderText('Найти в переписке…');
    fireEvent.change(input, { target: { value: 'alpha' } });
    const view = panel();
    expect(within(view).getByText('1 / 2')).toBeTruthy();
    expect(view.querySelectorAll('mark').length).toBe(2);

    fireEvent.click(screen.getByLabelText('Следующее совпадение'));
    expect(within(view).getByText('2 / 2')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Предыдущее совпадение'));
    expect(within(view).getByText('1 / 2')).toBeTruthy();

    fireEvent.change(input, { target: { value: 'ничего' } });
    expect(within(view).getByText('0 / 0')).toBeTruthy();
    expect((screen.getByLabelText('Следующее совпадение') as HTMLButtonElement).disabled).toBe(true);
    expect(within(view).queryAllByText(/нет совпадений/).length).toBe(0);
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('collapses to a rail and restores without losing the draft or refetching', async () => {
    const api = seeded();
    render(<App api={api} />);
    await screen.findByText('alpha beta alpha');
    const textarea = within(panel()).getByLabelText('Сообщение в Project Conversation');
    fireEvent.change(textarea, { target: { value: 'черновик Owner' } });

    fireEvent.click(screen.getByLabelText('Свернуть Project Conversation'));
    expect(panel().className).toContain('rosc-conversation--collapsed');
    expect(screen.getByLabelText('Развернуть Project Conversation')).toBeTruthy();
    // Only the rail remains a direct child besides the hidden controls; the former inner bar and access summary are gone.
    expect(panel().querySelector('.rosc-conversation__architect')).toBeNull();
    expect(panel().querySelector('.rosc-access')).toBeNull();
    expect(panel().querySelector('.rosc-conversation__rail')).toBeTruthy();
    expect((within(panel()).getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement).value).toBe('черновик Owner');

    fireEvent.click(screen.getByLabelText('Развернуть Project Conversation'));
    expect(panel().className).not.toContain('rosc-conversation--collapsed');
    expect(api.getConversation).toHaveBeenCalledTimes(1);
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
  });
});

describe('Project Conversation isolation', () => {
  it('keeps the draft through nested Task navigation and never leaks it to another Project', async () => {
    const getConversation = vi.fn(async (id: string) => id === 'P0001'
      ? conversationPage({ messages: [conversationMessage('секрет P0001')] })
      : conversationPage({ project_id: 'P0002' }));
    const api = stubApi({ listProjects: vi.fn(async () => [P1, P2]), listTasks: vi.fn(async () => [T1]), getConversation });
    render(<App api={api} />);
    await screen.findByText('секрет P0001');
    fireEvent.change(within(panel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'черновик P0001' } });

    fireEvent.click(await screen.findByText('T0001 · Task T0001'));
    expect(await screen.findByLabelText('Task Work Area')).toBeTruthy();
    expect((within(panel()).getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement).value).toBe('черновик P0001');
    fireEvent.click(screen.getByLabelText('Закрыть задачу'));
    await waitFor(() => expect(screen.queryByLabelText('Task Work Area')).toBeNull());
    expect((within(panel()).getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement).value).toBe('черновик P0001');

    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(await screen.findByText('Project P0002'));
    await screen.findByText('Сообщений пока нет.');
    expect(screen.queryByText('секрет P0001')).toBeNull();
    expect((within(panel()).getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement).value).toBe('');
    expect(getConversation).toHaveBeenLastCalledWith('P0002');
  });
});

describe('Project Conversation send/reload safety (P014.1)', () => {
  type Sent = { message: ReturnType<typeof conversationMessage>; replayed: boolean };
  const mounted = () => waitFor(() => { const el = panel(); if (!el) throw new Error('not mounted'); return el; });
  const field = () => within(panel()).getByLabelText('Сообщение в Project Conversation') as HTMLTextAreaElement;
  const type = (text: string) => fireEvent.change(field(), { target: { value: text } });
  const bodies = () => [...panel().querySelectorAll('.rosc-chat-body')].map(node => node.textContent);
  const keysOf = (mock: ReturnType<typeof vi.fn>) => mock.mock.calls.map(call => (call[1] as { client_key: string }).client_key);

  it('offers no send while the initial history is loading: click and Ctrl+Enter reach no POST', async () => {
    const history = deferred<ReturnType<typeof conversationPage>>();
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(() => history.promise) });
    render(<App api={api} />);
    await waitFor(() => expect(panel()).toBeTruthy());
    type('рано');
    expect((screen.getByRole('button', { name: 'Отправить' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    fireEvent.keyDown(field(), { key: 'Enter', ctrlKey: true });
    fireEvent.submit(field().form as HTMLFormElement);
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
    await act(async () => { history.resolve(conversationPage()); await history.promise; });
    await screen.findByText('Сообщений пока нет.');
    expect(field().value).toBe('рано');
    expect((screen.getByRole('button', { name: 'Отправить' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('does not allow an ungrounded send after a failed history load, only Retry', async () => {
    const getConversation = vi.fn().mockRejectedValueOnce(new ApiError('сбой', 'network', null))
      .mockResolvedValue(conversationPage());
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation });
    render(<App api={api} />);
    await within(await mounted()).findByRole('alert');
    type('текст');
    fireEvent.keyDown(field(), { key: 'Enter', ctrlKey: true });
    expect(api.sendConversationMessage).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Повторить'));
    await screen.findByText('Сообщений пока нет.');
    fireEvent.keyDown(field(), { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(api.sendConversationMessage).toHaveBeenCalledTimes(1));
  });

  it('keeps a confirmed message exactly once and in order when a send follows an explicit reload', async () => {
    const post = deferred<Sent>();
    const reload = deferred<ReturnType<typeof conversationPage>>();
    const old = conversationMessage('старое', { ordinal: 1 });
    const getConversation = vi.fn()
      .mockRejectedValueOnce(new ApiError('сбой', 'network', null))
      .mockImplementationOnce(() => reload.promise);
    const sendConversationMessage = vi.fn(() => post.promise);
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation, sendConversationMessage });
    render(<App api={api} />);
    await within(await mounted()).findByRole('alert');
    // The reload is deferred; sending stays impossible until it is ready, then the POST merges in order.
    type('преждевременно');
    fireEvent.click(screen.getByText('Повторить'));
    expect(sendConversationMessage).not.toHaveBeenCalled();
    fireEvent.keyDown(field(), { key: 'Enter', ctrlKey: true });
    expect(sendConversationMessage).not.toHaveBeenCalled();
    await act(async () => { reload.resolve(conversationPage({ messages: [old] })); await reload.promise; });
    await screen.findByText('старое');
    type('новое');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(sendConversationMessage).toHaveBeenCalledTimes(1);
    await act(async () => { post.resolve({ message: conversationMessage('новое', { ordinal: 2 }), replayed: false }); await post.promise; });
    await waitFor(() => expect(bodies()).toEqual(['старое', 'новое']));
  });

  it('merges a message that arrives while an older page is loading, without duplicates', async () => {
    const older = deferred<ReturnType<typeof conversationPage>>();
    const post = deferred<Sent>();
    const getConversation = vi.fn()
      .mockResolvedValueOnce(conversationPage({
        messages: [conversationMessage('четыре', { ordinal: 4 })], page: { limit: 50, has_more: true, next_before: 4 } }))
      .mockImplementationOnce(() => older.promise);
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation, sendConversationMessage: vi.fn(() => post.promise) });
    render(<App api={api} />);
    await screen.findByText('четыре');
    fireEvent.click(screen.getByText('Загрузить более ранние'));
    type('пять');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await act(async () => { post.resolve({ message: conversationMessage('пять', { ordinal: 5 }), replayed: false }); await post.promise; });
    await act(async () => {
      older.resolve(conversationPage({ messages: [conversationMessage('три', { ordinal: 3 })], page: { limit: 50, has_more: false, next_before: null } }));
      await older.promise;
    });
    await waitFor(() => expect(bodies()).toEqual(['три', 'четыре', 'пять']));
    expect(screen.queryByText('Загрузить более ранние')).toBeNull();
  });

  it('does not bleed a delayed history or a delayed POST result into another Project', async () => {
    const slowP1 = deferred<ReturnType<typeof conversationPage>>();
    const getConversation = vi.fn((id: string) => id === 'P0001' ? slowP1.promise
      : Promise.resolve(conversationPage({ project_id: 'P0002', messages: [conversationMessage('чужой P2', { ordinal: 9 })] })));
    const api = stubApi({ listProjects: vi.fn(async () => [P1, P2]), getConversation });
    render(<App api={api} />);
    await waitFor(() => expect(panel()).toBeTruthy());
    fireEvent.click(screen.getByLabelText('Закрыть проект'));
    fireEvent.click(await screen.findByText('Project P0002'));
    await screen.findByText('чужой P2');
    await act(async () => {
      slowP1.resolve(conversationPage({ messages: [conversationMessage('просочилось P1', { ordinal: 1 })] })); await slowP1.promise;
    });
    expect(screen.queryByText('просочилось P1')).toBeNull();
    expect(bodies()).toEqual(['чужой P2']);
  });

  it('locks the composer during a send and clears only what was submitted', async () => {
    const post = deferred<Sent>();
    const api = stubApi({ listProjects: vi.fn(async () => [P1]), sendConversationMessage: vi.fn(() => post.promise) });
    render(<App api={api} />);
    await screen.findByText('Сообщений пока нет.');
    type('первое');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    expect(field().readOnly).toBe(true);
    // Even forced input and repeated submits during the flight cannot create a second POST or change the text.
    fireEvent.keyDown(field(), { key: 'Enter', ctrlKey: true });
    fireEvent.submit(field().form as HTMLFormElement);
    expect(api.sendConversationMessage).toHaveBeenCalledTimes(1);
    await act(async () => { post.resolve({ message: conversationMessage('первое'), replayed: false }); await post.promise; });
    await waitFor(() => expect(field().readOnly).toBe(false));
    expect(field().value).toBe('');
  });

  it('reuses the key for an identical retry, uses a new one for changed text and for independent sends', async () => {
    const send = vi.fn()
      .mockRejectedValueOnce(new ApiError('HTTP 503', 'server', 503))
      .mockRejectedValueOnce(new ApiError('HTTP 503', 'server', 503))
      .mockImplementation(async (_id: string, input: { body: string }) => ({ message: conversationMessage(input.body), replayed: false }));
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), sendConversationMessage: send })} />);
    await screen.findByText('Сообщений пока нет.');
    type('текст А');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await within(panel()).findByRole('alert');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));      // identical retry
    await waitFor(() => expect(send).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(field().readOnly).toBe(false));
    type('текст Б');                                                           // edited after an uncertain failure
    expect(screen.getByText(/Предыдущая попытка могла сохраниться/)).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await screen.findByText('текст Б');
    const [first, retry, changed] = keysOf(send);
    expect(retry).toBe(first);
    expect(changed).not.toBe(first);
    expect(screen.queryByText(/Предыдущая попытка могла сохраниться/)).toBeNull();
    type('текст В');                                                           // independent later send
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await screen.findByText('текст В');
    const all = keysOf(send);
    expect(all[3]).not.toBe(changed);
    expect(new Set([first, changed, all[3]]).size).toBe(3);
  });

  it('keeps an unsent draft and its failure through collapse and nested Task navigation', async () => {
    const send = vi.fn().mockRejectedValue(new ApiError('HTTP 503', 'server', 503));
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), sendConversationMessage: send })} />);
    await screen.findByText('Сообщений пока нет.');
    type('не отправлено');
    fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
    await within(panel()).findByRole('alert');
    fireEvent.click(screen.getByLabelText('Свернуть Project Conversation'));
    fireEvent.click(screen.getByLabelText('Развернуть Project Conversation'));
    fireEvent.click(await screen.findByText('T0001 · Task T0001'));
    await screen.findByLabelText('Task Work Area');
    expect(field().value).toBe('не отправлено');
    expect(within(panel()).getByRole('alert').textContent).toContain('HTTP 503');
    expect(screen.queryByText('не отправлено', { selector: '.rosc-chat-body' })).toBeNull();
  });
});
