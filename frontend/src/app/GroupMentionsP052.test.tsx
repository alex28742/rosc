import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { GroupRosterEntry, GroupStatus } from '../core/models';
import { findMentions } from '../presentations/default/ui/mentions';
import { App } from './App';
import { groupStatus, project, stubApi, task } from './testing';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const CLAUDE = '11111111-1111-4111-8111-111111111111';
const CODEX = '22222222-2222-4222-8222-222222222222';
const DEEP = '33333333-3333-4333-8333-333333333333';
const ARCH = '55555555-5555-4555-8555-555555555555';
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const worker = (id: string, name: string, supported = true): GroupRosterEntry => ({ agent_id: id, display_name: name,
  icon_label: null, role: 'worker', runtime_adapter: supported ? 'claude-code' : 'codex-cli', model_id: 'm',
  group_supported: supported, unsupported_reason: supported ? null : 'adapter_not_supported' });
const architect: GroupRosterEntry = { agent_id: ARCH, display_name: 'Architect', icon_label: null, role: 'architect',
  runtime_adapter: 'opencode', model_id: 'm', group_supported: true, unsupported_reason: null };
const roster = [worker(CLAUDE, 'Claude'), worker(DEEP, 'DeepSeek'), worker(CODEX, 'Codex', false), architect];
const names = { [CLAUDE]: 'Claude', [DEEP]: 'DeepSeek', [CODEX]: 'Codex', [ARCH]: 'Architect' };
const room = (over: Partial<GroupStatus> = {}) => groupStatus({ conversation_id: 'c1', roster, names, has_owner_message: true, ...over });
const api = (status: GroupStatus) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getGroupStatus: vi.fn(async () => status),
});
const ready = (find: () => HTMLElement) => waitFor(() => {
  const element = find();
  if (!element) throw new Error('panel not rendered yet');
  return element;
});
const type = (panel: HTMLElement, label: string, text: string) =>
  fireEvent.change(within(panel).getByLabelText(label), { target: { value: text } });

afterEach(() => cleanup());

describe('P052 mention rule (UI preview mirror)', () => {
  it('matches exact names with boundaries, longest first, Unicode case, and flags duplicates', () => {
    const c = { a: ['Claude'], b: ['Claude-2'], c: ['Claude Opus'], d: ['Ärchi'] };
    expect(findMentions('@claude, и @CLAUDE-2', c).refs).toEqual(['a', 'b']);
    expect(findMentions('@Claude Opus', c).refs).toEqual(['c']);
    expect(findMentions('@Claudette a@Claude @Claude-3', c).refs).toEqual([]);
    expect(findMentions('@äRCHI!', c).refs).toEqual(['d']);
    expect(findMentions('@claude', { a: ['Claude'], z: ['CLAUDE'] })).toEqual({ refs: [], ambiguous: ['claude'], unknown: [] });
  });
});

describe('P052 typed @name addressing in the Project Conversation', () => {
  beforeEach(() => at('/projects/P0001'));

  it('a typed @Claude alone is a real direct addressee (not decoration)', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Claude, глянь');
    expect(within(pcPanel()).getByText(/Адресаты из текста: @Claude/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ body: '@Claude, глянь', to: [] })));  // P052 r02: Core parses the typed name
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('typed names are united with chips in a stable order and include the Architect', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    fireEvent.click(await within(await ready(pcPanel)).findByLabelText('Адресовать Claude'));
    type(pcPanel(), 'Сообщение в Project Conversation', '@deepseek и @Architect, обсудите');
    expect(within(pcPanel()).getByText(/Адресаты из текста: @DeepSeek, @Architect/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ to: [CLAUDE] })));  // P052 r02: explicit ids = chips; Core unites @deepseek/@Architect
  });

  it('a typed @Architect alone keeps the established direct Architect path; @Owner adds nobody', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Architect и @Owner: план?');
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });

  it('an unsupported typed Agent blocks the Send with a truthful reason', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Codex проверь');
    expect(within(pcPanel()).getByRole('alert').textContent).toMatch(/Codex: ответы недоступны/);
    expect((within(pcPanel()).getByRole('button', { name: 'Отправить' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('shows an exhausted @addressee honestly and offers the Owner-only continuation', async () => {
    const status = room({
      engagement: 'group', stop_reason: 'quota_exhausted',
      round: { round_id: 'r1', round_no: 1, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 5, turns: [],
        grants: { [ARCH]: { quota: 3, published: 3, failed: false }, [CLAUDE]: { quota: 3, published: 3, failed: false } } },
      unanswered: [{ agent_id: ARCH, from_agent_id: CLAUDE, message_id: 'm7', ordinal: 7, reason: 'quota_exhausted' }],
    });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getGroupStatus: vi.fn(async () => status), continueGroup: vi.fn(async () => ({ round: {}, conversation_id: 'c1', started: true })) });
    render(<App api={stub} />);
    const panel = await ready(pcPanel);
    expect(await within(panel).findByText(/Лимит ответов исчерпан/)).toBeTruthy();
    expect(within(panel).getByText(/#7: Claude → @Architect — без ответа: у него не осталось ответов/)).toBeTruthy();
    expect(within(panel).getByText(/Модель не вызывалась/)).toBeTruthy();
    fireEvent.click(within(panel).getByRole('button', { name: 'Продолжить обсуждение' }));
    await waitFor(() => expect(stub.continueGroup).toHaveBeenCalledWith('P0001', null));
    expect(screen.queryByText(/Обсуждение завершилось само/)).toBeNull();
  });

  it('a natural stop is not shown as an exhausted limit and offers no continuation', async () => {
    const status = room({ engagement: 'group', stop_reason: 'natural', unanswered: [],
      round: { round_id: 'r1', round_no: 1, state: 'awaiting_owner', per_agent_quota: 3, current_pass: 1, turns: [],
        grants: { [ARCH]: { quota: 3, published: 1, failed: false }, [CLAUDE]: { quota: 3, published: 1, failed: false } } } });
    const stub = api(status);
    render(<App api={stub} />);
    const panel = await ready(pcPanel);
    await within(panel).findByLabelText('Адресовать Claude');  // the room state has been read
    // P052 r03: a naturally finished round is a past discussion: no group bar, no stale quota cards, no continuation.
    expect(stub.getGroupStatus).toHaveBeenCalled();
    expect(within(panel).queryByLabelText('Групповой чат')).toBeNull();
    expect(within(panel).queryByText(/Обсуждение завершилось само/)).toBeNull();
    expect(within(panel).queryByRole('button', { name: 'Продолжить обсуждение' })).toBeNull();
  });
});

describe('P052 typed @name addressing in the Task Conversation', () => {
  beforeEach(() => at('/projects/P0001/tasks/T0001'));

  it('a typed @Claude in a Task room routes to the group Send for THIS Task', async () => {
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(tcPanel)).findByLabelText('Адресовать Claude');
    const label = within(tcPanel()).getByRole('textbox', { name: /Task Conversation|Сообщение/ }).getAttribute('aria-label')!;
    type(tcPanel(), label, '@Claude что осталось?');
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', 'T0001',
      expect.objectContaining({ to: [] })));
  });
});

describe('P052 r02 @Owner-only, unknown names and the authoritative route', () => {
  it('PC: @Owner alone is an Owner note on the room route (no Architect send, no Agent id)', async () => {
    at('/projects/P0001');
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Owner Напоминание: проверить отчёт');
    expect(within(pcPanel()).getByText(/Заметка для @Owner/)).toBeTruthy();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ body: '@Owner Напоминание: проверить отчёт', to: [] })));
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('TC: @Owner alone is an Owner note on THIS Task room route, never the Architect save/send path', async () => {
    at('/projects/P0001/tasks/T0001');
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(tcPanel)).findByLabelText('Адресовать Claude');
    const label = within(tcPanel()).getByRole('textbox', { name: /Task Conversation|Сообщение/ }).getAttribute('aria-label')!;
    type(tcPanel(), label, '@owner не забыть');
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', 'T0001',
      expect.objectContaining({ to: [] })));
    expect(stub.sendTaskConversationMessage).not.toHaveBeenCalled();
  });

  it('an unknown explicit @name blocks the Send with a clear reason (never the default Architect route)', async () => {
    at('/projects/P0001');
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Claud, проверь');
    expect(within(pcPanel()).getByRole('alert').textContent).toMatch(/@Claud — нет такого участника/);
    const send = within(pcPanel()).getByRole('button', { name: 'Отправить' }) as HTMLButtonElement;
    expect(send.disabled).toBe(true);
    fireEvent.click(send);
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });

  it('an e-mail or a backticked @fragment stays the plain legacy Architect message', async () => {
    at('/projects/P0001');
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', 'пиши на ops@Claude.dev, декоратор `@dataclass`');
    expect(within(pcPanel()).queryByRole('alert')).toBeNull();
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendConversationMessage).toHaveBeenCalledTimes(1));
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });

  it('mixed @Owner + @Claude goes to the room route; Core adds both addressees', async () => {
    at('/projects/P0001');
    const stub = api(room());
    render(<App api={stub} />);
    await within(await ready(pcPanel)).findByLabelText('Адресовать Claude');
    type(pcPanel(), 'Сообщение в Project Conversation', '@Owner @Straße? нет: @CLAUDE, глянь');
    expect(within(pcPanel()).getByRole('alert').textContent).toMatch(/@Straße/);
    type(pcPanel(), 'Сообщение в Project Conversation', '@Owner и @CLAUDE, глянь');
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledWith('P0001', null,
      expect.objectContaining({ to: [] })));
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
  });
});
