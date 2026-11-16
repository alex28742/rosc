import { cleanup, fireEvent, render, waitFor, within, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationPage, GroupRosterEntry, GroupStatus } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const CLAUDE = '11111111-1111-4111-8111-111111111111';
const ARCH = '55555555-5555-4555-8555-555555555555';
const roster: GroupRosterEntry[] = [
  { agent_id: CLAUDE, display_name: 'Claude', icon_label: null, role: 'worker', runtime_adapter: 'claude-code', model_id: 'm', group_supported: true, unsupported_reason: null },
  { agent_id: ARCH, display_name: 'Architect', icon_label: null, role: 'architect', runtime_adapter: 'opencode', model_id: 'm', group_supported: true, unsupported_reason: null },
];
const names = { [CLAUDE]: 'Claude', [ARCH]: 'Architect' };
const grants = (arch: number, claude: number) => ({ [ARCH]: { quota: 3, published: arch, failed: false }, [CLAUDE]: { quota: 3, published: claude, failed: false } });
const room = (over: Partial<GroupStatus> = {}) => groupStatus({ conversation_id: 'c1', roster, names, has_owner_message: true, ...over });
const finished = (over: Partial<GroupStatus> = {}) => room({ engagement: 'group', round_active: false, stop_reason: 'natural', unanswered: [],
  round: { round_id: 'r7', round_no: 7, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 0, turns: [], grants: grants(1, 1) }, ...over });
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });

afterEach(() => cleanup());

describe('P052 r03 group lifecycle in the conversation panels', () => {
  it.each([['PC', '/projects/P0001', pcPanel], ['TC', '/projects/P0001/tasks/T0001', tcPanel]] as const)(
    '%s: a naturally finished round #7 is not shown as an active conversation', async (_label, path, panel) => {
      at(path);
      const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getGroupStatus: vi.fn(async () => finished()) });
      render(<App api={stub} />);
      const el = await ready(panel);
      await within(el).findByLabelText('Адресовать Claude');
      expect(within(el).queryByLabelText('Групповой чат')).toBeNull();
      expect(within(el).queryByText('1/3')).toBeNull();  // no stale quota cards
      expect(within(el).queryByLabelText('Состояние доставки')).toBeNull();
    });

  it('a running fresh round #8 shows its fresh quotas, then disappears on the natural end', async () => {
    at('/projects/P0001');
    let status = room({ engagement: 'group', round_active: true, waiting: [CLAUDE], driver: { running: true, last: null },
      round: { round_id: 'r8', round_no: 8, state: 'open', per_agent_quota: 3, current_pass: 0, turns: [], grants: grants(1, 0) } });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getGroupStatus: vi.fn(async () => status) });
    render(<App api={stub} />);
    const el = await ready(pcPanel);
    const bar = await within(el).findByLabelText('Групповой чат');
    fireEvent.click(within(bar).getByRole('button', { name: /Группа: Owner/ }));
    expect(within(bar).getByText('Обсуждение #8 · каждое новое групповое сообщение Owner открывает новое обсуждение')).toBeTruthy();
    expect(within(bar).getByText('0/3')).toBeTruthy();
    status = room({ engagement: 'group', round_active: false, stop_reason: 'natural', unanswered: [],
      round: { round_id: 'r8', round_no: 8, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 0, turns: [], grants: grants(1, 1) } });
    await waitFor(() => expect(within(el).queryByLabelText('Групповой чат')).toBeNull(), { timeout: 5000 });
  });

  it('an exhausted limit is a compact continuation strip, not a misleading active round', async () => {
    at('/projects/P0001');
    const status = finished({ stop_reason: 'quota_exhausted', round: { round_id: 'r7', round_no: 7, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 2, turns: [], grants: grants(3, 3) },
      unanswered: [{ agent_id: ARCH, from_agent_id: CLAUDE, message_id: 'm9', ordinal: 9, reason: 'quota_exhausted' }] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getGroupStatus: vi.fn(async () => status) });
    render(<App api={stub} />);
    const el = await ready(pcPanel);
    const strip = await within(el).findByLabelText('Состояние доставки');
    expect(within(strip).getByRole('button', { name: 'Продолжить обсуждение' })).toBeTruthy();
    expect(within(el).queryByLabelText('Групповой чат')).toBeNull();
    expect(within(el).queryByText('3/3')).toBeNull();
  });

  it('a refused Architect invitation is explained without a body or trace', async () => {
    at('/projects/P0001');
    const status = room({ engagement: 'group', round_active: false,
      invitation: { message_id: 'm85', ordinal: 85, decision: 'declined', reason: 'pending_owner_request', round_id: null, round_no: null, owner_message_id: 'm84' },
      unanswered: [{ agent_id: CLAUDE, from_agent_id: ARCH, message_id: 'm85', ordinal: 85, reason: 'pending_owner_request' }] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getGroupStatus: vi.fn(async () => status) });
    render(<App api={stub} />);
    const el = await ready(pcPanel);
    expect(await within(el).findByText(/#85: Architect → @Claude — без ответа: приглашение не открыло обсуждение: незавершённый запрос Owner/)).toBeTruthy();
  });

  it('PC: when a direct Architect delivery finishes, the room state and history are re-read once (bounded follow)', async () => {
    at('/projects/P0001');
    const owner = conversationMessage('посчитай', { ordinal: 84, delivery: { state: 'pending', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: false } });
    let page: ConversationPage = conversationPage({ messages: [owner] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(async () => page), getGroupStatus: vi.fn(async () => finished()) });
    render(<App api={stub} />);
    await ready(pcPanel);
    await waitFor(() => expect(stub.getGroupStatus).toHaveBeenCalled());
    const statusReads = stub.getGroupStatus.mock.calls.length;
    const claude = conversationMessage('391', { ordinal: 86, sender: { kind: 'worker', agent_id: CLAUDE } as never });
    page = conversationPage({ messages: [{ ...owner, delivery: { state: 'delivered', error_code: null, reply_message_id: 'm85', can_retry: false, can_reconcile: false } },
      conversationMessage('@Claude ещё', { ordinal: 85, sender: { kind: 'architect', agent_id: ARCH } as never }), claude] });
    await waitFor(() => expect(stub.getGroupStatus.mock.calls.length).toBeGreaterThan(statusReads), { timeout: 6000 });
    await waitFor(() => expect(within(pcPanel()).getByText('391')).toBeTruthy(), { timeout: 6000 });
    // Idle afterwards: no indefinite polling of a finished room.
    const settled = stub.getGroupStatus.mock.calls.length;
    await new Promise(resolve => setTimeout(resolve, 2200));
    expect(stub.getGroupStatus.mock.calls.length).toBe(settled);
  }, 15000);
});

describe('P052 r03 Task Conversation follow-up', () => {
  it('TC: a finished direct Architect delivery re-reads THIS Task room state and history', async () => {
    at('/projects/P0001/tasks/T0001');
    const owner = conversationMessage('посчитай', { ordinal: 4, recipient: 'architect', delivery: { state: 'pending', error_code: null, reply_message_id: null, can_retry: false, can_reconcile: false } });
    let page: ConversationPage = conversationPage({ task_id: 'T0001', messages: [owner] } as Partial<ConversationPage>);
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getTaskConversation: vi.fn(async () => page), getGroupStatus: vi.fn(async () => finished()) });
    render(<App api={stub} />);
    await ready(tcPanel);
    await waitFor(() => expect(stub.getGroupStatus).toHaveBeenCalledWith('P0001', 'T0001'));
    const taskReads = () => stub.getGroupStatus.mock.calls.filter(call => call[1] === 'T0001').length;
    const reads = taskReads();
    page = conversationPage({ task_id: 'T0001', messages: [{ ...owner, delivery: { state: 'delivered', error_code: null, reply_message_id: 'm5', can_retry: false, can_reconcile: false } },
      conversationMessage('TC-391', { ordinal: 6, sender: { kind: 'worker', agent_id: CLAUDE } as never })] } as Partial<ConversationPage>);
    await waitFor(() => expect(taskReads()).toBeGreaterThan(reads), { timeout: 6000 });  // THIS Task's room re-read
    await waitFor(() => expect(within(tcPanel()).getByText('TC-391')).toBeTruthy(), { timeout: 6000 });
  }, 15000);
});

describe('P052 r03 Conversation Diagnostics in ROSC Settings', () => {
  it('defaults OFF, keeps the body checkbox inactive, warns honestly and applies changes through Core', async () => {
    at('/settings');
    const stub = stubApi();
    render(<App api={stub} />);
    const panel = await screen.findByRole('region', { name: 'Диагностика разговоров' });
    const main = await within(panel).findByRole('checkbox', { name: /Conversation Diagnostics/ });
    const bodies = within(panel).getByRole('checkbox', { name: /Include message bodies/ });
    expect((main as HTMLInputElement).checked).toBe(false);
    expect((bodies as HTMLInputElement).disabled).toBe(true);
    expect(within(panel).getByText(/выключена — ничего не записывается/)).toBeTruthy();
    expect(within(panel).getByRole('note').textContent).toMatch(/приватный текст проекта, промпты, секреты и\s+учётные данные/);
    expect(within(panel).getByRole('note').textContent).toMatch(/Автоматического удаления секретов из текста сообщений нет/);
    fireEvent.click(main);
    await waitFor(() => expect(stub.updateConversationDiagnostics).toHaveBeenCalledWith({ enabled: true }));
    expect(await within(panel).findByText(/только метаданные/)).toBeTruthy();
    const enabledBodies = within(panel).getByRole('checkbox', { name: /Include message bodies/ }) as HTMLInputElement;
    expect(enabledBodies.disabled).toBe(false);
  });
});
