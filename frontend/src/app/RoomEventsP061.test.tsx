import { act, cleanup, fireEvent, render, renderHook, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { openRoomEvents } from '../core/api/conversations';
import type { ConversationPage, GroupRosterEntry, GroupStatus, MessageAssessment, MessageGroup } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';
import { useGroupConversation } from './useGroupConversation';
import { FALLBACK_POLL_MS, OFFLINE_GRACE_MS, REVALIDATE_GAP_MS } from './useRoomEvents';

// P061: Core's read-only room SSE is an invalidation channel; the UI re-reads the ordinary GETs on hello/invalidate,
// keeps message objects by id (no duplicates, no remount), and never derives a business status from the stream.

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
  emit(type: string) {
    if (type === 'hello') this.readyState = 1;
    for (const listener of this.listeners.get(type) ?? []) listener(new MessageEvent(type, { data: '{}' }));
  }
  fail(gaveUp: boolean) { this.readyState = gaveUp ? 2 : 0; this.onerror?.(new Event('error')); }
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
const env = (over: Partial<MessageGroup>): MessageGroup => ({ to: [ARCH], audience: ['owner', ARCH, WORKER], reply_to: null,
  reply_to_ordinal: null, round_id: 'r1', turn_id: 't1', sender_agent_id: WORKER, next: 'finish', ...over });
const assessment = (over: Partial<MessageAssessment>): MessageAssessment => ({ state: 'awaiting', obligation_id: 'o1',
  expectation: 'informational', outcome: null, escalation_reason: null, next_actor: null, reply_message_id: null,
  reply_ordinal: null, error: null, offer_count: 0, completed_at: null, ...over });
/** A finished, idle room: no driver, no held turn — nothing would poll it. */
const idle = (): GroupStatus => groupStatus({ conversation_id: 'c1', roster, names: { [WORKER]: 'Claude', [ARCH]: 'Architect' },
  has_owner_message: true, engagement: 'group', round_active: false, stop_reason: 'finished', unanswered: [] });
const worker = (body: string, ordinal: number, group: MessageGroup, id = `m-${ordinal}`) => conversationMessage(body, {
  message_id: id, ordinal, sender: { kind: 'worker', agent_id: WORKER, agent_display_name: 'Claude' } as never, group });
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });

beforeEach(() => { FakeSource.all = []; });
afterEach(() => { cleanup(); vi.useRealTimers(); Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' }); });

describe('P061 TC: a late P060 assessment reaches the open page without reload', () => {
  it('awaiting -> assessed after next=finish while the room is idle; same bubble, no duplicate, draft kept', async () => {
    at('/projects/P0001/tasks/T0001');
    const awaiting = worker('Tests are green.', 1, env({ expectation: 'informational', assessment: assessment({}) }));
    const assessed = worker('Tests are green.', 1, env({ expectation: 'informational',
      assessment: assessment({ state: 'assessed', outcome: 'no_reply_needed' }) }));
    let page = conversationPage({ task_id: 'T0001', messages: [awaiting] } as Partial<ConversationPage>);
    const getTaskConversation = vi.fn(async () => page);
    const getGroupStatus = vi.fn(async () => idle());
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getConversation: vi.fn(async () => conversationPage({})), getTaskConversation, getGroupStatus, openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(tcPanel);
    const bubble = (await within(panel).findByText('Tests are green.')).closest('article') as HTMLElement;
    expect(within(bubble).getByText('Architect: ожидает оценки')).toBeTruthy();
    const composer = within(panel).getByRole('textbox') as HTMLTextAreaElement;
    fireEvent.change(composer, { target: { value: 'half-typed draft' } });
    // Only the open TC room holds a stream (the PC hook is not on a Task page).
    const sources = FakeSource.open();
    expect(sources.map(s => s.url)).toEqual(['/api/projects/P0001/tasks/T0001/conversation/group/events']);
    act(() => sources[0].emit('hello'));
    await waitFor(() => expect(getTaskConversation.mock.calls.length).toBeGreaterThanOrEqual(2));
    const before = getTaskConversation.mock.calls.length;
    page = conversationPage({ task_id: 'T0001', messages: [assessed] } as Partial<ConversationPage>);
    act(() => sources[0].emit('invalidate'));
    await within(bubble).findByText('Architect: оценено · ответ не требуется');
    expect(getTaskConversation.mock.calls.length).toBeGreaterThan(before);
    expect(within(panel).getAllByText('Tests are green.')).toHaveLength(1);
    expect((within(panel).getByText('Tests are green.').closest('article'))).toBe(bubble); // updated in place
    expect(composer.value).toBe('half-typed draft');
    expect(panel.querySelector('.rosc-spinner')).toBeNull(); // no loading flash
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
    expect(stub.reconcileGroup).not.toHaveBeenCalled();
    expect(stub.retryDelivery).not.toHaveBeenCalled();
  });
});

describe('P061 PC: a late canonical reply appears without reload', () => {
  it('a new reply is merged once in ordinal order; the older bubble is the same DOM node', async () => {
    at('/projects/P0001');
    const first = conversationMessage('Owner question', { message_id: 'm-1', ordinal: 1 });
    const reply = worker('Late Architect reply', 2, env({ to: ['owner'], next: 'finish' }), 'm-2');
    let page = conversationPage({ messages: [first] } as Partial<ConversationPage>);
    const getConversation = vi.fn(async () => page);
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation,
      getGroupStatus: vi.fn(async () => idle()), openRoomEvents: opener() });
    render(<App api={stub} />);
    const panel = await ready(pcPanel);
    const owner = (await within(panel).findByText('Owner question')).closest('article');
    const sources = FakeSource.open();
    expect(sources.map(s => s.url)).toEqual(['/api/projects/P0001/conversation/group/events']);
    page = conversationPage({ messages: [first, reply] } as Partial<ConversationPage>);
    act(() => sources[0].emit('invalidate'));
    await within(panel).findByText('Late Architect reply');
    act(() => sources[0].emit('invalidate'));
    await waitFor(() => expect(getConversation.mock.calls.length).toBeGreaterThanOrEqual(3));
    expect(within(panel).getAllByText('Late Architect reply')).toHaveLength(1);
    expect(within(panel).getByText('Owner question').closest('article')).toBe(owner);
    const texts = [...panel.querySelectorAll('article')].map(a => a.textContent ?? '');
    expect(texts.findIndex(t => t.includes('Owner question'))).toBeLessThan(texts.findIndex(t => t.includes('Late Architect reply')));
  });
});

describe('P061 shared room hook', () => {
  const setup = (taskId: string | null = 'T0001', live = true) => {
    const api = stubApi({ openRoomEvents: opener() });
    const onMessages = vi.fn();
    const hook = renderHook(({ pid, tid, on }) => useGroupConversation(api, pid, tid, onMessages, vi.fn(), null, on),
      { initialProps: { pid: 'P0001' as string | null, tid: taskId, on: live } });
    return { api, onMessages, hook };
  };
  const flush = async (ms = REVALIDATE_GAP_MS + 1) => { await act(async () => { vi.advanceTimersByTime(ms); }); };

  it('connect and every reconnect revalidate (missed events are repaired); invalidation bursts are coalesced', async () => {
    vi.useFakeTimers();
    const { api, onMessages } = setup();
    await flush();
    const status = api.getGroupStatus.mock.calls.length;
    const [source] = FakeSource.open();
    act(() => source.emit('hello'));
    await flush();
    expect(onMessages).toHaveBeenCalledTimes(1);
    act(() => { for (let i = 0; i < 10; i++) source.emit('invalidate'); });
    await flush();
    expect(onMessages).toHaveBeenCalledTimes(2); // ten events -> one bounded re-read
    act(() => { source.fail(false); source.emit('hello'); }); // the browser reconnected by itself
    await flush();
    expect(onMessages).toHaveBeenCalledTimes(3);
    expect(api.getGroupStatus.mock.calls.length).toBe(status + 3);
    act(() => source.emit('heartbeat'));
    await flush();
    expect(onMessages).toHaveBeenCalledTimes(3); // a heartbeat is not a change
  });

  it('switching room closes the old stream; its late events never touch the new room', async () => {
    vi.useFakeTimers();
    const { onMessages, hook, api } = setup('T0001');
    await flush();
    const [old] = FakeSource.open();
    hook.rerender({ pid: 'P0001', tid: 'T0002', on: true });
    await flush();
    expect(old.closed).toBe(true);
    expect(FakeSource.open().map(s => s.url)).toEqual(['/api/projects/P0001/tasks/T0002/conversation/group/events']);
    const calls = onMessages.mock.calls.length;
    const reads = api.getGroupStatus.mock.calls.length;
    act(() => { old.emit('invalidate'); old.emit('hello'); });
    await flush();
    expect(onMessages.mock.calls.length).toBe(calls);
    expect(api.getGroupStatus.mock.calls.length).toBe(reads);
    hook.rerender({ pid: 'P0002', tid: null, on: true }); // another Project
    expect(FakeSource.open().map(s => s.url)).toEqual(['/api/projects/P0002/conversation/group/events']);
    hook.unmount();
    expect(FakeSource.open()).toEqual([]);
  });

  it('a hidden tab holds no stream and reads nothing; becoming visible reconnects and revalidates', async () => {
    vi.useFakeTimers();
    const { onMessages } = setup();
    await flush();
    const [first] = FakeSource.open();
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(first.closed).toBe(true);
    act(() => { first.emit('invalidate'); window.dispatchEvent(new Event('focus')); });
    await flush(FALLBACK_POLL_MS * 2);
    expect(onMessages).not.toHaveBeenCalled();
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    const [second] = FakeSource.open();
    expect(second).not.toBe(first);
    act(() => second.emit('hello'));
    await flush();
    expect(onMessages).toHaveBeenCalledTimes(1);
  });

  it('a lost stream shows only a transport note after a grace period, re-reads at a bounded rate and reopens with backoff', async () => {
    vi.useFakeTimers();
    const { onMessages, hook, api } = setup();
    await flush();
    const [source] = FakeSource.open();
    act(() => source.emit('hello'));
    await flush();
    expect(hook.result.current.live).toBe('live');
    act(() => source.fail(true)); // HTTP error: the browser gave up
    await flush(OFFLINE_GRACE_MS - 1000);
    expect(hook.result.current.live).not.toBe('offline'); // a short drop is not shown
    await flush(1000);
    expect(hook.result.current.live).toBe('offline');
    expect(FakeSource.all.length).toBeGreaterThanOrEqual(2); // reopened after the first backoff step
    const calls = onMessages.mock.calls.length;
    await flush(FALLBACK_POLL_MS);
    expect(onMessages.mock.calls.length - calls).toBeLessThanOrEqual(2); // bounded fallback, no storm
    const latest = FakeSource.open().at(-1)!;
    act(() => latest.emit('hello'));
    await flush();
    expect(hook.result.current.live).toBe('live');
    expect(api.startGroupTurns).not.toHaveBeenCalled();
    expect(api.reconcileGroup).not.toHaveBeenCalled();
  });

  it('a room that is not visible (live=false) opens no stream', async () => {
    vi.useFakeTimers();
    const { hook } = setup(null, false);
    await flush();
    expect(FakeSource.all).toEqual([]);
    expect(hook.result.current.live).toBe('off');
  });
});

describe('P061 Core client', () => {
  it('opens the room-scoped read-only stream URL, or nothing without EventSource', () => {
    const created: string[] = [];
    vi.stubGlobal('EventSource', class { constructor(url: string) { created.push(url); } });
    openRoomEvents('P0001', 'T0009');
    openRoomEvents('P0002', null);
    expect(created).toEqual(['/api/projects/P0001/tasks/T0009/conversation/group/events', '/api/projects/P0002/conversation/group/events']);
    vi.unstubAllGlobals();
    expect(openRoomEvents('P0001', null)).toBeNull();
  });
});
