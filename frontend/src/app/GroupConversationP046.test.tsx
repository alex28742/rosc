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
const ARCH = '55555555-5555-4555-8555-555555555555';
const architectEntry = (supported: boolean): GroupRosterEntry => ({ agent_id: ARCH, display_name: 'Architect', icon_label: null,
  role: 'architect', runtime_adapter: 'opencode', model_id: 'm', group_supported: supported,
  unsupported_reason: supported ? null : 'architect_not_active' });
const second = '44444444-4444-4444-8444-444444444444';
const groupRound = { round_id: 'r1', round_no: 1, state: 'open' as const, per_agent_quota: 3, current_pass: 0,
  grants: { [CLAUDE]: { quota: 3, published: 0, failed: false }, [second]: { quota: 3, published: 0, failed: false } }, turns: [] };
const activeClaude = { agent_id: CLAUDE, role: 'worker' as const, state: 'active' as const, epoch: 1, applied_through: 0,
  considered_through: 0, omitted_ranges: [], missed: 0, release_offer: null, lane: null };
const openRound = { round_id: 'r1', round_no: 1, state: 'open' as const, per_agent_quota: 1, current_pass: 0,
  grants: { [CLAUDE]: { quota: 1, published: 0, failed: false } }, turns: [] };

const api = (status: GroupStatus, extra = {}) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]),
  getGroupStatus: vi.fn(async () => status), ...extra,
});

afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('P046/P047 Project Conversation group UI', () => {
  beforeEach(() => at('/projects/P0001'));

  it('sends a direct message to one Worker with ONE Send: no Authorize/Start ritual and no group panel', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    const claude = await within(pcPanel()).findByLabelText('Адресовать Claude') as HTMLInputElement;
    const codex = within(pcPanel()).getByLabelText('Адресовать Codex') as HTMLInputElement;
    expect(codex.disabled).toBe(true);
    expect(codex.closest('label')?.title).toMatch(/только Claude Code/);
    fireEvent.click(claude);
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'ты тут?' } });
    expect(within(pcPanel()).getByText(/Личное сообщение: адресат ответит один раз/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ body: 'ты тут?', to: [CLAUDE] })));
    expect(stub.sendGroupMessage).toHaveBeenCalledTimes(1);
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
    expect(stub.authorizeGroupRound).not.toHaveBeenCalled();
    expect(await within(pcPanel()).findByText('ты тут?')).toBeTruthy();
    expect(within(pcPanel()).queryByRole('region', { name: 'Групповой чат' })).toBeNull();
    expect(within(pcPanel()).queryByText(/Разрешить|Запустить ход|Продлить/)).toBeNull();
  });

  it('sends Architect + Worker as ONE group message when the Architect is active, and explains when it is not', async () => {
    const stub = api(room({ roster: [...roster, architectEntry(true)] }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Architect'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'обсудите' } });
    expect(within(pcPanel()).getByText(/Групповое сообщение: каждый адресат ответит один раз/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ body: 'обсудите', to: [CLAUDE, ARCH] })));
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('refuses Architect + Worker with a truthful reason while the Architect is not active', async () => {
    render(<App api={api(room({ roster: [...roster, architectEntry(false)] }))} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Architect'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'x' } });
    expect((within(pcPanel()).getByRole('button', { name: /Отправить/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(pcPanel()).getByRole('alert').textContent).toMatch(/Architect не активен/);
    // The selection is shown exactly as made: nothing is silently unchecked.
    expect((within(pcPanel()).getByLabelText('Адресовать Architect') as HTMLInputElement).checked).toBe(true);
  });

  it('keeps a direct 1:1 room free of the group panel even with an old round and passive roster', async () => {
    const direct = room({ engagement: 'direct', members: [activeClaude, { ...activeClaude, agent_id: second, state: 'passive' }],
      round: { ...openRound, state: 'awaiting_owner', grants: { [CLAUDE]: { quota: 1, published: 1, failed: false } } } });
    render(<App api={api(direct)} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    await within(pcPanel()).findByLabelText('Адресовать Claude');
    expect(within(pcPanel()).queryByRole('region', { name: 'Групповой чат' })).toBeNull();
    expect(within(pcPanel()).queryByRole('region', { name: 'Состояние доставки' })).toBeNull();
  });

  it('shows a compact group panel only for a true group, with N as a ceiling and no Authorize/Start', async () => {
    const stub = api(room({ engagement: 'group', members: [activeClaude, { ...activeClaude, agent_id: second }], round: groupRound,
      holding_turn: turn(), driver: { running: true, last: null },
      roster: [...roster, { ...roster[0], agent_id: second, display_name: 'Claude-2' }], names: { ...names, [second]: 'Claude-2' } }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    const bar = await within(pcPanel()).findByRole('region', { name: 'Групповой чат' });
    expect(within(bar).getByRole('button', { name: /Группа: Owner, Claude, Claude-2/ })).toBeTruthy();
    expect(within(bar).getByText(/до/).textContent).toMatch(/до 3 на агента/);
    expect(within(bar).getByText('Claude: запущено, ожидает подтверждения CLI')).toBeTruthy();
    expect(within(bar).queryByRole('img', { name: 'формирует ответ' })).toBeNull();
    expect(within(bar).queryByText(/Разрешить|Запустить ход|Продлить|Новый раунд/)).toBeNull();
    fireEvent.click(within(bar).getByRole('button', { name: /Группа: Owner/ }));
    expect(within(bar).getByText(/Обсуждение #1/)).toBeTruthy();
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

  it('shows UNKNOWN as a visible blocker even in 1:1 chatting; its only action is evidence reconciliation', async () => {
    const stub = api(room({ engagement: 'direct', members: [activeClaude], round: openRound,
      holding_turn: turn({ state: 'unknown', error_code: 'evidence_missing' }) }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    const strip = await within(pcPanel()).findByRole('region', { name: 'Состояние доставки' });
    expect(within(pcPanel()).queryByRole('region', { name: 'Групповой чат' })).toBeNull();
    expect(within(strip).getByText(/принятие хода Claude не подтверждено \(evidence отсутствует\)/)).toBeTruthy();
    expect(within(strip).queryByText(/Запустить|отметить отвеч/i)).toBeNull();
    fireEvent.click(within(strip).getByRole('button', { name: 'Запросить сверку' }));
    await waitFor(() => expect(stub.reconcileGroup).toHaveBeenCalledWith('P0001', null));
  });

  it('reports an Owner reconciliation that found no evidence without pretending recovery', async () => {
    const held = room({ engagement: 'direct', members: [activeClaude], round: openRound,
      holding_turn: turn({ state: 'unknown', error_code: 'no_result' }) });
    const stub = api(held);
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: 'Запросить сверку' }));
    expect(await within(pcPanel()).findByText(/Сверка выполнена: доказательств принятия или отказа нет — ход остаётся UNKNOWN/)).toBeTruthy();
    expect(stub.startGroupTurns).not.toHaveBeenCalled();
  });

  it('surfaces a held Send (pause) truthfully instead of an apparent model invocation', async () => {
    const stub = api(room({ engagement: 'direct', controls: { paused: true, emergency_brake: false } }), {
      sendGroupMessage: vi.fn(async () => ({ message: conversationMessage('после паузы', { ordinal: 5 }), replayed: false,
        dispatch: { mode: 'direct', agents: [CLAUDE], started: false, round: null, blocked: 'paused' } })),
    });
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    expect(await within(pcPanel()).findByText('Доставка на паузе')).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Claude'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'после паузы' } });
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    expect(await within(pcPanel()).findByText(/доставка на паузе — снимите паузу/)).toBeTruthy();
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

  it('confirms Release in a dialog and toggles Pause / Brake through Core inside the group panel', async () => {
    const stub = api(room({ engagement: 'group', members: [activeClaude, { ...activeClaude, agent_id: second }], round: groupRound,
      roster: [...roster, { ...roster[0], agent_id: second, display_name: 'Claude-2' }], names: { ...names, [second]: 'Claude-2' } }));
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByRole('button', { name: /Группа: Owner/ }));
    fireEvent.click(within(pcPanel()).getAllByRole('button', { name: 'Освободить' })[0]);
    const dialog = screen.getByRole('dialog', { name: 'Освободить Claude от обсуждения?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Перевести в Released' }));
    await waitFor(() => expect(stub.releaseGroupMember).toHaveBeenCalledWith('P0001', null, CLAUDE));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Пауза' }));
    await waitFor(() => expect(stub.setGroupControls).toHaveBeenCalledWith('P0001', null, { paused: true }));
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Brake' }));
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

describe('P046/P047 Task Conversation group UI', () => {
  beforeEach(() => at('/projects/P0001/tasks/T0001'));

  it('reads and posts the TC room by Task scope and never shows another Task\'s room state', async () => {
    const stub = api(room(), {
      getGroupStatus: vi.fn(async (_p: string, taskId: string | null) => taskId === 'T0001'
        ? room({ engagement: 'group', round: { ...groupRound, round_no: 7 } }) : room()),
    });
    render(<App api={stub} />);
    await waitFor(() => expect(tcPanel()).not.toBeNull());
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(tcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.change(within(tcPanel()).getByLabelText('Сообщение в Task Conversation'), { target: { value: 'Вопрос по T1' } });
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', 'T0001', expect.objectContaining({ to: [CLAUDE] })));
    expect(stub.sendTaskConversationMessage).not.toHaveBeenCalled();
    expect(stub.getGroupStatus).toHaveBeenCalledWith('P0001', 'T0001');
    expect(stub.getGroupStatus).toHaveBeenCalledWith('P0001', null);
    fireEvent.click(within(tcPanel()).getByRole('button', { name: /Группа: Owner/ }));
    expect(within(tcPanel()).getByText(/Обсуждение #7/)).toBeTruthy();
    expect(within(pcPanel()).queryByText(/Обсуждение #7/)).toBeNull();
    expect(within(pcPanel()).queryByRole('region', { name: 'Групповой чат' })).toBeNull();
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
