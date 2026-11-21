import { act, cleanup, fireEvent, render, renderHook, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ConversationPage, DraftFrame, GroupRosterEntry, GroupStatus, MessageGroup, ProvisionalDraft } from '../core/models';
import { App } from './App';
import { applyDraftFrame, DRAFT_RENDER_MS, parseDraftFrame } from './provisionalDrafts';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';
import { useGroupConversation } from './useGroupConversation';

// P062: provisional agent text arrives as typed `draft` frames on the P061 room SSE.  The UI shows it as a temporary
// plain-text bubble — one renderer for PC and TC, whatever the Agent's role, runtime or model — and replaces it exactly
// once by the canonical message of the same turn.  Synthetic EventSource only (no provider, no real stream).

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
    const raw = typeof data === 'string' ? data : JSON.stringify(data);
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data: raw }));
  }
  static open() { return FakeSource.all.filter(source => !source.closed); }
}
const opener = () => vi.fn((pid: string, tid: string | null) =>
  new FakeSource(`/api/projects/${pid}${tid ? `/tasks/${tid}` : ''}/conversation/group/events`) as unknown as EventSource);

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const T2 = task('T0002', 'P0001');
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
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });
const drafts = (panel: HTMLElement) => [...panel.querySelectorAll<HTMLElement>('[data-provisional-turn]')];

beforeEach(() => { FakeSource.all = []; });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('P062 draft frames', () => {
  const apply = (frames: DraftFrame[]) => frames.reduce((map, f) => applyDraftFrame(map, f), new Map<string, ProvisionalDraft>());

  it('appends at the exact offset, replaces at 0, ignores an out-of-sync frame, drops on end/gone', () => {
    let map = apply([frame({ text: 'Hel' }), frame({ offset: 3, text: 'lo' })]);
    expect(map.get('t1')?.text).toBe('Hello');
    map = applyDraftFrame(map, frame({ offset: 9, text: 'GAP' }));
    expect(map.get('t1')?.text).toBe('Hello');
    map = applyDraftFrame(map, frame({ offset: 0, text: 'Help' }));
    expect(map.get('t1')?.text).toBe('Help');
    map = applyDraftFrame(map, frame({ offset: 4, text: '', state: 'published', message: 'm-9' }));
    expect(map.get('t1')).toMatchObject({ state: 'published', messageId: 'm-9', text: 'Help' });
    expect(applyDraftFrame(map, frame({ state: 'gone' })).size).toBe(0);
    expect(apply([frame({ text: 'x' }), frame({ state: 'ended' })]).size).toBe(0);
  });

  it('a newer streaming turn supersedes an older streaming one; invalid frames are rejected', () => {
    const map = apply([frame({ turn: 't1', text: 'one' }), frame({ turn: 't2', agent: ARCH, text: 'two' })]);
    expect([...map.keys()]).toEqual(['t2']);
    expect(parseDraftFrame('{"turn":"t","offset":-1,"text":"","state":"streaming"}')).toBeNull();
    expect(parseDraftFrame('{"turn":"t","offset":0,"text":"","state":"hacked"}')).toBeNull();
    expect(parseDraftFrame('not json')).toBeNull();
    expect(parseDraftFrame(JSON.stringify(frame({ text: 'ok' })))?.text).toBe('ok');
  });
});

describe('P062 TC: provisional text while the Agent is generating', () => {
  it('shows text as it streams, keeps composer/history, then is replaced exactly once by the canonical message', async () => {
    at('/projects/P0001/tasks/T0001');
    const owner = conversationMessage('Owner question', { message_id: 'm-1', ordinal: 1 });
    let page = conversationPage({ task_id: 'T0001', messages: [owner] } as Partial<ConversationPage>);
    const getTaskConversation = vi.fn(async () => page);
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getConversation: vi.fn(async () => conversationPage({})), getTaskConversation,
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(tcPanel);
    const ownerBubble = (await within(panel).findByText('Owner question')).closest('article');
    const composer = within(panel).getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: 'half-typed' } });
    const [source] = FakeSource.open();
    act(() => source.emit('hello', { v: 1, drafts: true }));
    act(() => source.emit('draft', frame({ text: 'Привет, <b>это</b>' })));
    await waitFor(() => expect(drafts(panel)).toHaveLength(1));
    const bubble = drafts(panel)[0];
    expect(bubble.textContent).toContain('Привет, <b>это</b>'); // plain text, never HTML
    expect(bubble.querySelector('b')).toBeNull();
    expect(within(bubble).getByText('печатает · временный ответ')).toBeTruthy();
    expect(bubble.querySelector('.rosc-chat-caret')).toBeTruthy();
    act(() => source.emit('draft', frame({ offset: 'Привет, <b>это</b>'.length, text: ' поток' })));
    await waitFor(() => expect(drafts(panel)[0]?.textContent).toContain('Привет, <b>это</b> поток'));
    expect(drafts(panel)[0]).toBe(bubble); // updated in place, not re-mounted
    // Core committed the reply: the canonical message of the same turn replaces the draft once.
    page = conversationPage({ task_id: 'T0001', messages: [owner, conversationMessage('Final canonical', {
      message_id: 'm-2', ordinal: 2, sender: { kind: 'worker', agent_id: WORKER, agent_display_name: 'Claude' } as never,
      group: env({}) })] } as Partial<ConversationPage>);
    act(() => source.emit('draft', frame({ offset: 'Привет, <b>это</b> поток'.length, state: 'published', message: 'm-2' })));
    await within(panel).findByText('Final canonical');
    await waitFor(() => expect(drafts(panel)).toHaveLength(0));
    expect(within(panel).getAllByText('Final canonical')).toHaveLength(1);
    expect(within(panel).queryByText(/Привет/)).toBeNull();
    expect(within(panel).getByText('Owner question').closest('article')).toBe(ownerBubble);
    expect(composer.value).toBe('half-typed');
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
    expect(stub.reconcileGroup).not.toHaveBeenCalled();
  });

  it('a failed/UNKNOWN turn ends the draft without any message; a fresh connection resets stale drafts', async () => {
    at('/projects/P0001/tasks/T0001');
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getConversation: vi.fn(async () => conversationPage({})),
      getTaskConversation: vi.fn(async () => conversationPage({ task_id: 'T0001', messages: [] } as Partial<ConversationPage>)),
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(tcPanel);
    const [source] = FakeSource.open();
    act(() => source.emit('draft', frame({ text: 'partial' })));
    await waitFor(() => expect(drafts(panel)).toHaveLength(1));
    act(() => source.emit('draft', frame({ offset: 7, state: 'ended' })));
    await waitFor(() => expect(drafts(panel)).toHaveLength(0));
    act(() => source.emit('draft', frame({ turn: 't2', text: 'second' })));
    await waitFor(() => expect(drafts(panel)).toHaveLength(1));
    act(() => source.emit('hello', { v: 1, drafts: true })); // reconnect: its own snapshot (none here) follows
    await waitFor(() => expect(drafts(panel)).toHaveLength(0));
  });
});

describe('P062 PC: the same renderer for every role, runtime and model', () => {
  it('a Worker draft and an Architect draft render with the same structure; only the participant label differs', async () => {
    at('/projects/P0001');
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]),
      getConversation: vi.fn(async () => conversationPage({ messages: [] } as Partial<ConversationPage>)),
      getGroupStatus: vi.fn(async () => status()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(pcPanel);
    const [source] = FakeSource.open();
    expect(source.url).toBe('/api/projects/P0001/conversation/group/events');
    act(() => source.emit('draft', frame({ turn: 'tw', agent: WORKER, text: 'Same text' })));
    await waitFor(() => expect(drafts(panel)).toHaveLength(1));
    const workerShape = drafts(panel)[0].querySelector('.rosc-chat-body')?.outerHTML;
    expect(drafts(panel)[0].textContent).toContain('Claude');
    act(() => source.emit('draft', frame({ turn: 'ta', agent: ARCH, text: 'Same text' })));
    await waitFor(() => expect(drafts(panel)[0]?.dataset.provisionalTurn).toBe('ta'));
    expect(drafts(panel)).toHaveLength(1); // the newer turn superseded the older streaming one
    expect(drafts(panel)[0].querySelector('.rosc-chat-body')?.outerHTML).toBe(workerShape);
    expect(drafts(panel)[0].textContent).toContain('Architect');
  });
});

describe('P062 shared hook: room isolation and batching', () => {
  it('frames of a previous room never reach the new room; deltas are batched', async () => {
    vi.useFakeTimers();
    const api = stubApi({ openRoomEvents: opener(), listTasks: vi.fn(async () => [T1, T2]) });
    const hook = renderHook(({ tid }) => useGroupConversation(api, 'P0001', tid, vi.fn(), vi.fn()),
      { initialProps: { tid: 'T0001' as string | null } });
    const [first] = FakeSource.open();
    act(() => { for (const piece of ['a', 'b', 'c']) first.emit('draft', frame({ offset: 'abc'.indexOf(piece), text: piece })); });
    expect(hook.result.current.drafts).toEqual([]); // nothing rendered before the batch window
    await act(async () => { vi.advanceTimersByTime(DRAFT_RENDER_MS + 1); });
    expect(hook.result.current.drafts?.map(d => d.text)).toEqual(['abc']);
    hook.rerender({ tid: 'T0002' });
    expect(first.closed).toBe(true);
    act(() => first.emit('draft', frame({ turn: 'old', text: 'LEAK' })));
    await act(async () => { vi.advanceTimersByTime(DRAFT_RENDER_MS + 1); });
    expect(hook.result.current.drafts).toEqual([]);
    const second = FakeSource.open()[0];
    expect(second.url).toContain('/tasks/T0002/');
    act(() => second.emit('draft', frame({ turn: 'new', text: 'mine' })));
    await act(async () => { vi.advanceTimersByTime(DRAFT_RENDER_MS + 1); });
    expect(hook.result.current.drafts?.map(d => d.text)).toEqual(['mine']);
    hook.unmount();
  });
});
