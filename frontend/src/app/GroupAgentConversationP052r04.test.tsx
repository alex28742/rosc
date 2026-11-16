import { cleanup, render, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationPage, GroupRosterEntry, GroupStatus, MessageGroup } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';

// P052 r04 (R-15.26): an Agent -> Agent reply shows its real recipient and the message it answers (not the Owner), an
// explicit final answer is marked, a finished round is history (no active group bar), and a spent Owner authorization
// is explained honestly with the Owner action that can renew it.
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
const env = (over: Partial<MessageGroup>): MessageGroup => ({ to: [], audience: ['owner', ARCH, CLAUDE], reply_to: null, reply_to_ordinal: null,
  round_id: 'r1', turn_id: null, sender_agent_id: null, next: null, ...over });
const finished = (over: Partial<GroupStatus> = {}) => groupStatus({ conversation_id: 'c1', roster, names, has_owner_message: true,
  engagement: 'group', round_active: false, stop_reason: 'finished', unanswered: [],
  round: { round_id: 'r1', round_no: 1, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 1, turns: [],
    grants: { [ARCH]: { quota: 3, published: 1, failed: false }, [CLAUDE]: { quota: 3, published: 1, failed: false } } }, ...over });
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });

const page = (taskId?: string): ConversationPage => conversationPage({ ...(taskId ? { task_id: taskId } : {}), messages: [
  conversationMessage('Architect, спроси Claude', { ordinal: 1, group: env({ to: [ARCH] }) }),
  conversationMessage('@Claude проверь отступы?', { ordinal: 2, sender: { kind: 'architect', agent_id: ARCH } as never,
    group: env({ to: [CLAUDE], reply_to: 'message-1', reply_to_ordinal: 1, sender_agent_id: ARCH }) }),
  conversationMessage('Проверил: 16px, всё ровно.', { ordinal: 3, sender: { kind: 'worker', agent_id: CLAUDE, agent_display_name: 'Claude' } as never,
    group: env({ to: [ARCH], reply_to: 'message-2', reply_to_ordinal: 2, turn_id: 't3', sender_agent_id: CLAUDE, next: 'finish' }) }),
] } as Partial<ConversationPage>);

afterEach(() => cleanup());

describe('P052 r04 Agent <-> Agent conversation in the panels', () => {
  it.each([['PC', '/projects/P0001', pcPanel, undefined], ['TC', '/projects/P0001/tasks/T0001', tcPanel, 'T0001']] as const)(
    '%s: a reply without @mention is shown to its requester with its causal message and the final marker', async (_l, path, panel, taskId) => {
      at(path);
      const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
        getConversation: vi.fn(async () => page()), getTaskConversation: vi.fn(async () => page(taskId)),
        getGroupStatus: vi.fn(async () => finished()) });
      render(<App api={stub} />);
      const el = await ready(panel);
      const reply = await within(el).findByText('Проверил: 16px, всё ровно.');
      const bubble = reply.closest('article') as HTMLElement;
      expect(within(bubble).getByText('→ @Architect')).toBeTruthy();  // the requester, never @Owner
      expect(within(bubble).queryByText(/@Owner/)).toBeNull();
      expect(within(bubble).getByText('↩ #2')).toBeTruthy();  // the specific message it answers
      expect(within(bubble).getByText('✓ финальный ответ')).toBeTruthy();
      expect(within(bubble).queryByText(/rosc-control/)).toBeNull();  // control never reaches the visible body
      // A finished round is history: no active group bar, no stale quota cards.
      expect(within(el).queryByLabelText('Групповой чат')).toBeNull();
      expect(within(el).queryByText('1/3')).toBeNull();
    });

  it('a continuing reply carries no final marker', async () => {
    at('/projects/P0001');
    const cont = conversationPage({ messages: [
      conversationMessage('Проверил. А мобильная?', { ordinal: 3, sender: { kind: 'worker', agent_id: CLAUDE, agent_display_name: 'Claude' } as never,
        group: env({ to: [ARCH], reply_to: 'message-2', reply_to_ordinal: 2, turn_id: 't3', sender_agent_id: CLAUDE, next: 'continue' }) })] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getConversation: vi.fn(async () => cont), getGroupStatus: vi.fn(async () => finished({ stop_reason: 'natural' })) });
    render(<App api={stub} />);
    const el = await ready(pcPanel);
    const bubble = (await within(el).findByText('Проверил. А мобильная?')).closest('article') as HTMLElement;
    expect(within(bubble).getByText('→ @Architect')).toBeTruthy();
    expect(within(bubble).queryByText('✓ финальный ответ')).toBeNull();
  });

  it('a spent Owner authorization is explained with the honest Owner action; no model was called', async () => {
    at('/projects/P0001');
    const status = finished({ stop_reason: 'natural',
      invitation: { message_id: 'm9', ordinal: 9, decision: 'declined', reason: 'authorization_spent', round_id: null, round_no: null, owner_message_id: 'm1' },
      unanswered: [{ agent_id: CLAUDE, from_agent_id: ARCH, message_id: 'm9', ordinal: 9, reason: 'authorization_spent' }] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), getGroupStatus: vi.fn(async () => status) });
    render(<App api={stub} />);
    const el = await ready(pcPanel);
    expect(await within(el).findByText(/#9: Architect → @Claude — без ответа: разрешение Owner на обсуждение уже израсходовано — нужно новое сообщение Owner\. Модель не вызывалась\./)).toBeTruthy();
    expect(within(el).queryByRole('button', { name: 'Продолжить обсуждение' })).toBeNull();  // not a quota stop
  });
});
