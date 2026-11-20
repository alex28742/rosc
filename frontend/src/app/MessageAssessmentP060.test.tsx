import { cleanup, render, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationPage, GroupRosterEntry, GroupStatus, MessageAssessment, MessageGroup } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, project, stubApi, task } from './testing';

// P060: the final marker, the sender's expectation and the Core assessment of a Worker -> Architect message are shown
// SEPARATELY and only from Core facts; a message without an assessment event never shows a false "assessed".
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
const status = (): GroupStatus => groupStatus({ conversation_id: 'c1', roster, names: { [WORKER]: 'Claude', [ARCH]: 'Architect' },
  has_owner_message: true, engagement: 'group', round_active: false, stop_reason: 'finished', unanswered: [] });
const worker = (body: string, ordinal: number, group: MessageGroup) => conversationMessage(body, { ordinal,
  sender: { kind: 'worker', agent_id: WORKER, agent_display_name: 'Claude' } as never, group });
const tc = (): HTMLElement => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const ready = (find: () => HTMLElement) => waitFor(() => { const el = find(); if (!el) throw new Error('not yet'); return el; });

async function bubbles(messages: ReturnType<typeof worker>[]) {
  at('/projects/P0001/tasks/T0001');
  const page = conversationPage({ task_id: 'T0001', messages } as Partial<ConversationPage>);
  const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
    getConversation: vi.fn(async () => conversationPage({})), getTaskConversation: vi.fn(async () => page),
    getGroupStatus: vi.fn(async () => status()) });
  render(<App api={stub} />);
  const panel = await ready(tc);
  return async (text: string) => (await within(panel).findByText(text)).closest('article') as HTMLElement;
}

afterEach(() => cleanup());

describe('P060 Worker -> Architect assessment in the TC chat', () => {
  it('final marker, expectation and pending/assessed state are independent Core facts', async () => {
    const find = await bubbles([
      worker('Tests are green.', 1, env({ expectation: 'informational', assessment: assessment({}) })),
      worker('All set.', 2, env({ expectation: 'informational',
        assessment: assessment({ state: 'assessed', outcome: 'no_reply_needed', obligation_id: 'o2' }) })),
    ]);
    const pending = await find('Tests are green.');
    expect(within(pending).getByText('✓ финальный ответ')).toBeTruthy();
    expect(within(pending).getByText('ожидание: к сведению')).toBeTruthy();
    expect(within(pending).getByText('Architect: ожидает оценки')).toBeTruthy();
    expect(within(pending).queryByText(/оценено/)).toBeNull();
    const done = await find('All set.');
    expect(within(done).getByText('✓ финальный ответ')).toBeTruthy();
    expect(within(done).getByText('Architect: оценено · ответ не требуется')).toBeTruthy();
  });

  it('an escalation names the reason and the Owner; a legacy message shows no assessment at all', async () => {
    const find = await bubbles([
      worker('Which port?', 1, env({ expectation: 'reply_requested', assessment: assessment({ state: 'owner_action',
        expectation: 'reply_requested', outcome: 'escalated', escalation_reason: 'no_reply_permission', next_actor: 'Owner' }) })),
      worker('Old final message.', 2, env({ expectation: null, assessment: null })),
      worker('Continuing.', 3, env({ next: 'continue', expectation: 'reply_requested', assessment: assessment({ state: 'assessing' }) })),
    ]);
    const escalated = await find('Which port?');
    expect(within(escalated).getByText('ожидание: нужен ответ')).toBeTruthy();
    expect(within(escalated).getByText('Нужно действие Owner: нет разрешённого ответа · next_actor=Owner')).toBeTruthy();
    const legacy = await find('Old final message.');
    expect(within(legacy).getByText('✓ финальный ответ')).toBeTruthy();
    expect(within(legacy).queryByText(/Architect:|ожидание:|Owner:/)).toBeNull();
    const continuing = await find('Continuing.');
    expect(within(continuing).queryByText('✓ финальный ответ')).toBeNull();
    expect(within(continuing).getByText('Architect: оценивается')).toBeTruthy();
  });
});
