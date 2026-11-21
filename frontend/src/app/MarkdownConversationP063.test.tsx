import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationPage, DraftFrame, GroupRosterEntry, GroupStatus, MessageGroup } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';

// P063: real PC/TC composition — published Markdown from Owner/Architect/Worker, growing provisional Markdown on the
// P061/P062 room SSE, single replacement by the canonical message, and visible-text search. Synthetic EventSource
// only (no provider, no real stream, no backend).

class FakeSource {
  static all: FakeSource[] = [];
  readonly url: string;
  readyState = 0;
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  private listeners = new Map<string, Array<(event: MessageEvent) => void>>();
  constructor(url: string) { this.url = url; FakeSource.all.push(this); }
  addEventListener(type: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close() { this.closed = true; this.readyState = 2; }
  emit(type: string, data: unknown = {}) {
    if (type === 'hello') this.readyState = 1;
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data: JSON.stringify(data) }));
  }
  static open() { return FakeSource.all.filter(source => !source.closed); }
}
const opener = () => vi.fn((pid: string, tid: string | null) =>
  new FakeSource(`/api/projects/${pid}${tid ? `/tasks/${tid}` : ''}/conversation/group/events`) as unknown as EventSource);

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const WORKER = '11111111-1111-4111-8111-111111111111';
const ARCH = '55555555-5555-4555-8555-555555555555';
const roster: GroupRosterEntry[] = [
  { agent_id: WORKER, display_name: 'Claude', icon_label: null, role: 'worker', runtime_adapter: 'claude-code', model_id: 'm', group_supported: true, unsupported_reason: null },
  { agent_id: ARCH, display_name: 'Architect', icon_label: null, role: 'architect', runtime_adapter: 'opencode', model_id: 'm', group_supported: true, unsupported_reason: null },
];
const status = (): GroupStatus => groupStatus({ conversation_id: 'c1', roster, names: { [WORKER]: 'Claude', [ARCH]: 'Architect' },
  has_owner_message: true, engagement: 'direct', round_active: false, unanswered: [] });
const env = (over: Partial<MessageGroup>): MessageGroup => ({ to: ['owner'], audience: ['owner', WORKER], reply_to: null,
  reply_to_ordinal: null, round_id: 'r1', turn_id: 't1', sender_agent_id: WORKER, next: 'finish', ...over });
const frame = (over: Partial<DraftFrame>): DraftFrame => ({ turn: 't1', agent: WORKER, offset: 0, text: '', state: 'streaming',
  message: null, truncated: false, ...over });
const architect = { kind: 'architect', agent_id: ARCH, agent_display_name: 'Architect' } as never;
const worker = { kind: 'worker', agent_id: WORKER, agent_display_name: 'Claude' } as never;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });
const drafts = (panel: HTMLElement) => [...panel.querySelectorAll<HTMLElement>('[data-provisional-turn]')];
const bubble = (panel: HTMLElement, id: string) => panel.querySelector(`[data-message-id="${id}"] .rosc-chat-body`) as HTMLElement;
const openSearch = (panel: HTMLElement, term: string) => {
  fireEvent.click(within(panel).getByRole('button', { name: 'Поиск по сообщениям' }));
  fireEvent.change(within(panel).getByRole('searchbox'), { target: { value: term } });
};

beforeEach(() => { FakeSource.all = []; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

const ANSWER = [
  'Итог: **важно** проверить [отчёт](https://example.com/hidden-target).', '',
  '| Шаг | Статус |', '| --- | --- |', '| сборка | `ok` |', '', '```sh', 'npm run build', '```',
].join('\n');

describe('P063 PC: published Markdown from every participant through one renderer', () => {
  it('renders Owner, Architect and Worker Markdown alike; HTML is text; search hits visible text only', async () => {
    at('/projects/P0001');
    const page = conversationPage({ messages: [
      conversationMessage('Owner: *курсив* <b>не тег</b>', { message_id: 'o', ordinal: 1 }),
      conversationMessage(ANSWER, { message_id: 'a', ordinal: 2, sender: architect }),
      conversationMessage('- **важно**: пункт\n- второй', { message_id: 'w', ordinal: 3, sender: worker, group: env({}) }),
    ] } as Partial<ConversationPage>);
    render(<App api={stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(async () => page),
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() })} />);
    const panel = await ready(pcPanel);
    await waitFor(() => expect(bubble(panel, 'a')).toBeTruthy());
    expect(bubble(panel, 'o').querySelector('em')?.textContent).toBe('курсив');
    expect(bubble(panel, 'o').querySelector('b')).toBeNull();
    expect(bubble(panel, 'o').textContent).toContain('<b>не тег</b>');
    const a = bubble(panel, 'a');
    expect(a.tagName).toBe('DIV'); // a block container: never <p><table>
    expect(a.querySelector('strong')?.textContent).toBe('важно');
    expect(a.querySelector('.rosc-md-table-wrap table td code')?.textContent).toBe('ok');
    expect(a.querySelector('pre')?.textContent).toBe('npm run build');
    expect(a.querySelector('a')?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(a.textContent).not.toMatch(/\*\*|```/);
    expect(bubble(panel, 'w').querySelector('ul li strong')?.textContent).toBe('важно');
    // The same structure for Architect and Worker: only the participant metadata differs.
    expect(a.className).toBe(bubble(panel, 'w').className);

    openSearch(panel, 'важно');
    expect(within(panel).getByText('1 / 2')).toBeTruthy();
    const marks = [...panel.querySelectorAll<HTMLElement>('mark.rosc-chat-mark')];
    expect(marks.map(mark => mark.parentElement?.tagName)).toEqual(['STRONG', 'STRONG']);
    expect(marks[0].classList.contains('rosc-chat-mark--current')).toBe(true);
    fireEvent.click(within(panel).getByRole('button', { name: 'Следующее совпадение' }));
    expect(marks[1].classList.contains('rosc-chat-mark--current')).toBe(true);
    // The hidden link destination and markdown delimiters are not part of the searchable text.
    fireEvent.change(within(panel).getByRole('searchbox'), { target: { value: 'hidden-target' } });
    expect(panel.querySelectorAll('mark')).toHaveLength(0);
    fireEvent.change(within(panel).getByRole('searchbox'), { target: { value: 'npm run' } });
    expect(panel.querySelector('pre mark')?.textContent).toBe('npm run');
  });
});

describe('P063 streaming Markdown drafts and canonical replacement', () => {
  it('TC direct Architect: the draft formats as it grows, search skips it, the committed reply replaces it once', async () => {
    at('/projects/P0001/tasks/T0001');
    const owner = conversationMessage('Вопрос про **важно**', { message_id: 'm-1', ordinal: 1 });
    let page = conversationPage({ task_id: 'T0001', messages: [owner] } as Partial<ConversationPage>);
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getConversation: vi.fn(async () => conversationPage({})), getTaskConversation: vi.fn(async () => page),
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(tcPanel);
    await waitFor(() => expect(bubble(panel, 'm-1')).toBeTruthy());
    const [source] = FakeSource.open();
    act(() => source.emit('hello', { v: 1, drafts: true }));
    let sent = '';
    const push = (text: string) => { act(() => source.emit('draft', frame({ turn: 'direct-m-1', agent: ARCH, offset: sent.length, text }))); sent += text; };
    push('Гипотеза: **важ');
    await waitFor(() => expect(drafts(panel)).toHaveLength(1));
    const draft = drafts(panel)[0];
    expect(draft.textContent).toContain('Гипотеза: **важ'); // unclosed: literal until closed, nothing lost
    expect(draft.querySelector('.rosc-chat-caret')).toBeTruthy();
    push('но**\n\n```js\nconst a');
    await waitFor(() => expect(drafts(panel)[0]?.querySelector('strong')?.textContent).toBe('важно'));
    expect(drafts(panel)[0].querySelector('pre')?.textContent).toBe('const a');
    expect(drafts(panel)[0].querySelector('pre .rosc-chat-caret')).toBeNull();
    expect(drafts(panel)[0]).toBe(draft); // updated in place
    openSearch(panel, 'важно');
    expect(within(panel).getByText('1 / 1')).toBeTruthy(); // only the published Owner message, never the draft
    expect(draft.querySelector('mark')).toBeNull();
    fireEvent.change(within(panel).getByRole('searchbox'), { target: { value: '' } });
    page = conversationPage({ task_id: 'T0001', messages: [owner,
      conversationMessage('Итог: **важно** — подтверждено.', { message_id: 'm-2', ordinal: 2, sender: architect })] } as Partial<ConversationPage>);
    act(() => source.emit('draft', frame({ turn: 'direct-m-1', agent: ARCH, offset: sent.length, state: 'published', message: 'm-2' })));
    await waitFor(() => expect(bubble(panel, 'm-2')).toBeTruthy());
    await waitFor(() => expect(drafts(panel)).toHaveLength(0));
    expect(panel.querySelectorAll('[data-message-id="m-2"]')).toHaveLength(1);
    expect(bubble(panel, 'm-2').querySelector('strong')?.textContent).toBe('важно'); // canonical stays formatted
    expect(within(panel).queryByText(/Гипотеза/)).toBeNull();
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
  });

  it('PC Worker group turn: a streamed table is replaced by the canonical message of the same turn, no duplicate', async () => {
    at('/projects/P0001');
    const owner = conversationMessage('@Claude таблицу', { message_id: 'm-1', ordinal: 1 });
    let page = conversationPage({ messages: [owner] } as Partial<ConversationPage>);
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(async () => page),
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(pcPanel);
    const [source] = FakeSource.open();
    act(() => source.emit('hello', { v: 1, drafts: true }));
    act(() => source.emit('draft', frame({ text: '| a | b |\n| - | - |\n| 1 | ' })));
    await waitFor(() => expect(drafts(panel)[0]?.querySelector('table td')?.textContent).toBe('1'));
    expect(drafts(panel)[0].querySelector('table .rosc-chat-caret')).toBeNull();
    // A reset (replace from offset 0) rebuilds the draft without keeping stale text.
    act(() => source.emit('draft', frame({ text: '> новая *версия*' })));
    await waitFor(() => expect(drafts(panel)[0]?.querySelector('blockquote em')?.textContent).toBe('версия'));
    expect(drafts(panel)[0].querySelector('table')).toBeNull();
    page = conversationPage({ messages: [owner, conversationMessage('| a | b |\n| - | - |\n| 1 | 2 |',
      { message_id: 'm-2', ordinal: 2, sender: worker, group: env({}) })] } as Partial<ConversationPage>);
    act(() => source.emit('draft', frame({ offset: '> новая *версия*'.length, state: 'published', message: 'm-2' })));
    await waitFor(() => expect(bubble(panel, 'm-2')?.querySelector('td:last-child')?.textContent).toBe('2'));
    await waitFor(() => expect(drafts(panel)).toHaveLength(0));
    expect(panel.querySelectorAll('table')).toHaveLength(1);
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });
});
