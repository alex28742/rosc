import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GroupRosterEntry, GroupStatus, GroupTurn } from '../core/models';
import { App } from './App';
import { conversationMessage, conversationPage, groupStatus, ownerParticipant, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001'), T2 = task('T0002', 'P0001');
const CLAUDE = '11111111-1111-4111-8111-111111111111';
const CODEX = '22222222-2222-4222-8222-222222222222';
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;

const roster: GroupRosterEntry[] = [
  { agent_id: CLAUDE, display_name: 'Claude', icon_label: null, role: 'worker', runtime_adapter: 'claude-code', model_id: 'haiku',
    group_supported: true, unsupported_reason: null },
  { agent_id: CODEX, display_name: 'Codex', icon_label: null, role: 'worker', runtime_adapter: 'codex-cli', model_id: 'm',
    group_supported: false, unsupported_reason: 'adapter_not_supported' },
];
const names = { [CLAUDE]: 'Claude', [CODEX]: 'Codex' };
const turn = (over: Partial<GroupTurn> = {}): GroupTurn => ({
  turn_id: '33333333-3333-4333-8333-333333333333', round_id: 'r1', agent_id: CLAUDE, state: 'submitted', pass: 0, position: 0,
  attempt: 1, lane_id: 'l1', error_code: null, catchup_choice: null, input_after_ordinal: 0, input_through_ordinal: 1,
  reply_message_id: null, ...over,
});
const room = (over: Partial<GroupStatus> = {}) => groupStatus({ conversation_id: 'c1', roster, names, has_owner_message: true, ...over });
const activeClaude = { agent_id: CLAUDE, role: 'worker' as const, state: 'active' as const, epoch: 1, applied_through: 0,
  considered_through: 0, omitted_ranges: [], missed: 0, release_offer: null, lane: null };
const openRound = { round_id: 'r1', round_no: 1, state: 'open' as const, per_agent_quota: 1, current_pass: 0,
  grants: { [CLAUDE]: { quota: 1, published: 0, failed: false } }, turns: [] };

const api = (status: GroupStatus, extra = {}) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]),
  getGroupStatus: vi.fn(async () => status), ...extra,
});

afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('P046 Project Conversation group UI', () => {
  beforeEach(() => at('/projects/P0001'));

  it('addresses a real Worker through the group bridge, never through the Architect path, and launches nothing', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    const claude = await within(pcPanel()).findByLabelText('Адресовать Claude') as HTMLInputElement;
    const codex = within(pcPanel()).getByLabelText('Адресовать Codex') as HTMLInputElement;
    expect(codex.disabled).toBe(true);
    expect(codex.closest('label')?.title).toMatch(/только Claude Code/);
    fireEvent.click(claude);
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'Привет, Claude' } });
    const send = within(pcPanel()).getByRole('button', { name: 'Отправить · 1 адресат(а/ов)' });
    fireEvent.click(send);
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ body: 'Привет, Claude', to: [CLAUDE] })));
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
    expect(stub.authorizeGroupRound).not.toHaveBeenCalled();
    expect(await within(pcPanel()).findByText('Привет, Claude')).toBeTruthy();
  });

  it('refuses a mixed Architect + Worker post with a visible reason', async () => {
    render(<App api={api(room())} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Architect'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'x' } });
    expect((within(pcPanel()).getByRole('button', { name: /Отправить/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(pcPanel()).getByRole('alert').textContent).toMatch(/Architect пока не интегрированы/);
  });

  it('grants N only by an explicit click and starts only by another explicit click', async () => {
    const stub = api(room({ members: [activeClaude] }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Групповой чат' }));
    fireEvent.change(within(pcPanel()).getByLabelText('Ответов на агента'), { target: { value: '1' } });
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Разрешить 1 ответ: Claude' }));
    await waitFor(() => expect(stub.authorizeGroupRound).toHaveBeenCalledWith('P0001', null, { agents: [CLAUDE], quota: 1, renew: false }));
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
  });

  it('starts an authorized round and reports a waiting turn honestly without the typing animation', async () => {
    const stub = api(room({ members: [activeClaude], round: openRound }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Групповой чат' }));
    expect(within(pcPanel()).getByText('Раунд разрешён · ход не запущен')).toBeTruthy();
    stub.getGroupStatus.mockImplementation(async () => room({ members: [activeClaude], round: openRound, holding_turn: turn(),
      driver: { running: true, last: null } }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Запустить ход' }));
    await waitFor(() => expect(stub.startGroupTurns).toHaveBeenCalledWith('P0001', null));
    expect(await within(pcPanel()).findByText('Claude: запущено, ожидает подтверждения CLI')).toBeTruthy();
    expect(within(pcPanel()).queryByRole('img', { name: 'формирует ответ' })).toBeNull();
  });

  it('animates only for provider-confirmed activity and renders the published reply on the left with #ordinal', async () => {
    const reply = conversationMessage('Ответ Claude', { ordinal: 2, sender: ownerParticipant({ kind: 'worker', display_name: 'Worker',
      agent_id: CLAUDE, agent_display_name: 'Claude', participant_id: 'p-claude' }),
      group: { to: ['owner'], audience: ['owner', CLAUDE], reply_to: null, reply_to_ordinal: null, round_id: 'r1', turn_id: 't', sender_agent_id: CLAUDE } });
    const stub = api(room({ members: [activeClaude], round: openRound, holding_turn: turn(), activity: 'cli_confirmed' }), {
      getConversation: vi.fn(async () => conversationPage({ messages: [conversationMessage('Вопрос', { ordinal: 1 }), reply] })),
    });
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    expect((await within(pcPanel()).findAllByRole('img', { name: 'формирует ответ' })).length).toBeGreaterThan(0);
    const bubble = (await within(pcPanel()).findByText('Ответ Claude')).closest('article') as HTMLElement;
    expect(bubble.className).toContain('rosc-chat-message--worker');
    expect(within(bubble).getByText('Claude · Worker')).toBeTruthy();
    expect(within(bubble).getByText('→ @Owner')).toBeTruthy();
    expect(within(bubble).getByRole('button', { name: 'Скопировать ссылку на сообщение 2' }).textContent).toBe('#2');
  });

  it('shows UNKNOWN as an honest blocker whose only action is evidence reconciliation', async () => {
    const stub = api(room({ members: [activeClaude], round: openRound, holding_turn: turn({ state: 'unknown', error_code: 'evidence_missing' }) }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Групповой чат' }));
    expect(within(pcPanel()).getByText(/принятие хода Claude не подтверждено \(evidence отсутствует\)/)).toBeTruthy();
    expect((within(pcPanel()).getByRole('button', { name: 'Запустить ход' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(pcPanel()).queryByText(/отметить отвеч/i)).toBeNull();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Запросить сверку' }));
    await waitFor(() => expect(stub.reconcileGroup).toHaveBeenCalledWith('P0001', null));
  });

  it('reports an Owner reconciliation that found no evidence without pretending recovery', async () => {
    const held = room({ members: [activeClaude], round: openRound, holding_turn: turn({ state: 'unknown', error_code: 'no_result' }) });
    const stub = api(held);
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Групповой чат' }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Запросить сверку' }));
    expect(await within(pcPanel()).findByText(/Сверка выполнена: доказательств принятия или отказа нет — ход остаётся UNKNOWN/)).toBeTruthy();
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
  });

  it('offers a new round for newly addressed Workers when they differ from the current grants', async () => {
    const second = '44444444-4444-4444-8444-444444444444';
    const withSecond = room({
      roster: [...roster, { ...roster[0], agent_id: second, display_name: 'Claude-2' }], names: { ...names, [second]: 'Claude-2' },
      members: [{ ...activeClaude, state: 'passive' }, { ...activeClaude, agent_id: second }],
      round: { ...openRound, state: 'awaiting_owner', grants: { [CLAUDE]: { quota: 1, published: 1, failed: false } } },
    });
    const stub = api(withSecond);
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Групповой чат' }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: /Новый раунд \(3\): Claude-2/ }));
    await waitFor(() => expect(stub.authorizeGroupRound).toHaveBeenCalledWith('P0001', null, { agents: [second], quota: 3, renew: true }));
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
  });

  it('asks the real Send-full/Skip question with Core counts and continues the authorized queue after Skip', async () => {
    const catchup = { turn_id: turn().turn_id, agent_id: CLAUDE, from: 3, through: 20, chars: 5896, limit: 2000, count: 18 };
    const stub = api(room({ members: [activeClaude], round: openRound, catchup }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    const dialog = await screen.findByRole('dialog', { name: 'Передать пропущенный контекст?' });
    expect(within(dialog).getByText(/18 сообщений · 5896 символов · #3–#20/)).toBeTruthy();
    expect(within(dialog).getByText(/порог 2000 символов/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Пропустить историю' }));
    await waitFor(() => expect(stub.chooseGroupCatchup).toHaveBeenCalledWith('P0001', null, catchup.turn_id, 'skip_backlog'));
    await waitFor(() => expect(stub.startGroupTurns).toHaveBeenCalledTimes(1));
  });

  it('offers Keep / Release for a long-Passive member as a notice, not a chat message', async () => {
    const passive = { ...activeClaude, state: 'passive' as const, missed: 11, release_offer: { missed: 11, step: 1, threshold: 10 } };
    const stub = api(room({ members: [passive] }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    expect(await within(pcPanel()).findByText('ROSC предлагает освободить Claude')).toBeTruthy();
    expect(within(pcPanel()).getByText(/пропустил 11 сообщений \(порог 10/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Оставить в группе' }));
    await waitFor(() => expect(stub.keepGroupMember).toHaveBeenCalledWith('P0001', null, CLAUDE));
  });

  it('confirms Release in a dialog and toggles Pause / Emergency Brake through Core', async () => {
    const stub = api(room({ members: [activeClaude] }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: /Участники/ }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Освободить' }));
    const dialog = screen.getByRole('dialog', { name: 'Освободить Claude от обсуждения?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Перевести в Released' }));
    await waitFor(() => expect(stub.releaseGroupMember).toHaveBeenCalledWith('P0001', null, CLAUDE));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Пауза доставки' }));
    await waitFor(() => expect(stub.setGroupControls).toHaveBeenCalledWith('P0001', null, { paused: true }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Emergency Brake' }));
    await waitFor(() => expect(stub.setGroupControls).toHaveBeenCalledWith('P0001', null, { emergency_brake: true }));
  });

  it('keeps the plain Architect composer when no Worker is in scope', async () => {
    render(<App api={api(groupStatus())} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    await screen.findByText('Сообщений пока нет.');
    expect(within(pcPanel()).queryByLabelText('Адресовать Architect')).toBeNull();
    expect(within(pcPanel()).getByLabelText('@ Адресаты — пока недоступно')).toBeTruthy();
  });
});

describe('P046 Task Conversation group UI', () => {
  beforeEach(() => at('/projects/P0001/tasks/T0001'));

  it('reads and posts the TC room by Task scope and never shows another Task\'s room state', async () => {
    const stub = api(room(), {
      getGroupStatus: vi.fn(async (_p: string, taskId: string | null) => taskId === 'T0001'
        ? room({ round: { ...openRound, round_no: 7 } }) : room()),
    });
    render(<App api={stub} />);
    await waitFor(() => expect(tcPanel()).not.toBeNull());
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(tcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.change(within(tcPanel()).getByLabelText('Сообщение в Task Conversation'), { target: { value: 'Вопрос по T1' } });
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить · 1 адресат(а/ов)' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', 'T0001', expect.objectContaining({ to: [CLAUDE] })));
    expect(stub.sendTaskConversationMessage).not.toHaveBeenCalled();
    expect(stub.getGroupStatus).toHaveBeenCalledWith('P0001', 'T0001');
    expect(stub.getGroupStatus).toHaveBeenCalledWith('P0001', null);
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Групповой чат' }));
    expect(within(tcPanel()).getByText(/Раунд #7/)).toBeTruthy();
    expect(within(pcPanel()).queryByText(/Раунд #7/)).toBeNull();
  });

  it('keeps Architect addressing on the established P038/P039 path', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await waitFor(() => expect(tcPanel()).not.toBeNull());
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(tcPanel()).findByLabelText('Адресовать Architect'));
    fireEvent.change(within(tcPanel()).getByLabelText('Сообщение в Task Conversation'), { target: { value: 'Для Architect' } });
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendTaskConversationMessage).toHaveBeenCalledWith('P0001', 'T0001',
      expect.objectContaining({ body: 'Для Architect', recipient: 'architect' })));
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });
});
