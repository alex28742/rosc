import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationMessage, GroupRosterEntry, GroupStatus, RunReview, RunView, TaskAuthoring } from '../core/models';
import { App } from './App';
import { describeBootstrap } from '../presentations/default/ui/workerView';
import { architectStatus, conversationMessage, conversationPage, groupStatus, project, runView, stubApi, task, taskAuthoring,
  workerSession } from './testing';

/** P055 Live QA corrections (synthetic: stubbed Core API only; nothing real is called). */
const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001'), T2 = task('T0002', 'P0001');
const CLAUDE = '11111111-1111-4111-8111-111111111111';
const ARCH = '55555555-5555-4555-8555-555555555555';
const pcPanel = () => document.querySelector('aside[aria-label="Project Conversation"]') as HTMLElement;
const tcPanel = () => document.querySelector('aside[aria-label="Task Conversation"]') as HTMLElement;
const openTc = () => waitFor(() => { const el = tcPanel(); expect(el).not.toBeNull(); return el; });
const worker: GroupRosterEntry = { agent_id: CLAUDE, display_name: 'Claude', icon_label: null, role: 'worker',
  runtime_adapter: 'claude-code', model_id: 'haiku', group_supported: true, unsupported_reason: null };
const architect = (supported: boolean): GroupRosterEntry => ({ agent_id: ARCH, display_name: 'Architect', icon_label: null,
  role: 'architect', runtime_adapter: 'opencode', model_id: 'm', group_supported: supported,
  unsupported_reason: supported ? null : 'architect_not_active' });
const names = { [CLAUDE]: 'Claude', [ARCH]: 'Architect' };
/** A brand-new room: no Conversation row yet, zero messages. */
const freshRoom = (supported: boolean, over: Partial<GroupStatus> = {}) => groupStatus({ conversation_id: null,
  roster: [worker, architect(supported)], names, has_owner_message: false, ...over });
const readyArchitect = architectStatus({ state: 'ready', can_activate: false, can_deactivate: true,
  agent: { agent_id: ARCH, display_name: 'Architect', provider_id: 'p', model_id: 'm' },
  session: { session_id: 'sess-1', state: 'ready', verified_at: '2026-11-18T10:00:00Z' } });

afterEach(() => { cleanup(); document.body.style.overflow = ''; vi.restoreAllMocks(); });

describe('P055 QA-ARCH-STATUS-UI: fresh room, first multi-agent Send', () => {
  it('a zero-message room whose roster snapshot predates the verified Architect is re-read: no false refusal, one group Send', async () => {
    at('/projects/P0001');
    // The room status was read before the Architect became verified-ready; Core reports it eligible on the next read.
    const getGroupStatus = vi.fn().mockResolvedValueOnce(freshRoom(false)).mockResolvedValue(freshRoom(true));
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getGroupStatus,
      getArchitect: vi.fn(async () => readyArchitect) });
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    await waitFor(() => expect(getGroupStatus.mock.calls.length).toBeGreaterThanOrEqual(2));
    fireEvent.click(await within(pcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Architect'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'обсудите' } });
    await waitFor(() => expect(within(pcPanel()).queryByText(/Architect не активен/)).toBeNull());
    fireEvent.click(within(pcPanel()).getByRole('button', { name: 'Отправить' }));
    await waitFor(() => expect(stub.sendGroupMessage).toHaveBeenCalledTimes(1));
    expect(stub.sendGroupMessage.mock.calls[0][2].to.sort()).toEqual([ARCH, CLAUDE].sort());
    expect(stub.sendConversationMessage).not.toHaveBeenCalled();
  });

  it('a genuinely inactive Architect is still refused accurately (nothing is fabricated)', async () => {
    at('/projects/P0001');
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getGroupStatus: vi.fn(async () => freshRoom(false)), getArchitect: vi.fn(async () => architectStatus()) });
    render(<App api={stub} />);
    await waitFor(() => expect(pcPanel()).not.toBeNull());
    fireEvent.click(await within(pcPanel()).findByLabelText('Адресовать Claude'));
    fireEvent.click(within(pcPanel()).getByLabelText('Адресовать Architect'));
    fireEvent.change(within(pcPanel()).getByLabelText('Сообщение в Project Conversation'), { target: { value: 'обсудите' } });
    expect(await within(pcPanel()).findByText(/Architect не активен/)).toBeTruthy();
    expect(stub.sendGroupMessage).not.toHaveBeenCalled();
  });
});

const HANDOFF_ID = 'handoff-msg-4';
const handoffMessage = (): ConversationMessage => conversationMessage('Worker-отчёт по Run …', { message_id: HANDOFF_ID, ordinal: 4,
  sender: { participant_id: 'pw', kind: 'worker', display_name: 'Claude', actor_id: CLAUDE, assurance: 'rosc:managed_run',
    agent_id: CLAUDE, agent_display_name: 'Claude' } as ConversationMessage['sender'] });
const earlier = conversationMessage('Сделай задачу', { message_id: 'm-1', ordinal: 1 });
const handoffProgress = { plan_revision: 1, steps: [], progress: { total: 0, produced: 0, test_passed: 0, note: '' }, tests: [], blockers: [],
  started: true, journal: { events: 5, last_event_at: null, last_seq: 5 },
  import: { imported_at: null, checked_at: null, journal_observed_at: null, problem: null, problem_detail: null, torn_tail_bytes: 0 },
  checkpoint: null, report: { path: 'reports/final.md', sha256: 'a'.repeat(64), bytes: 10, outcome: 'completed' as const, registered_at: '2026-11-18T10:00:00Z', note: '' },
  handoff: { message_id: HANDOFF_ID, addressed_to: ARCH, created_at: '2026-11-18T10:00:00Z' } };
const reported = (over: Partial<RunView> = {}) => runView({ state: 'reported', reason: 'worker_reported', holding: false,
  progress: handoffProgress as RunView['progress'], report: handoffProgress.report as RunView['report'],
  review: { state: 'awaiting_architect_review', next_actor: 'Architect', latest: null, note: '' }, ...over });
const running = () => runView({ state: 'running', reason: 'exec_submitted', holding: true });
const page = (messages: ConversationMessage[]) => conversationPage({ task_id: 'T0001', conversation: { conversation_id: 'c-t1' } as never, messages });

describe('P055 QA-RUN-HANDOFF-UI-REFRESH: incoming Worker handoff without a browser reload', () => {
  it('running -> reported shows exactly one handoff message in the open TC; repeated reads are idempotent', async () => {
    at('/projects/P0001/tasks/T0001');
    let run: RunView = running();
    let history = [earlier];
    const getTaskConversation = vi.fn(async () => page(history));
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]),
      listTaskRuns: vi.fn(async () => [run]), getRun: vi.fn(async () => run), getTaskConversation });
    render(<App api={stub} />);
    expect(await within(await openTc()).findByText('Сделай задачу')).toBeTruthy();
    const readsBefore = getTaskConversation.mock.calls.length;
    // Core imported the Report + stored the handoff atomically; no group discussion is active in this room.
    run = reported();
    history = [earlier, handoffMessage()];
    window.dispatchEvent(new Event('focus')); // a foreground return / offline Core catch-up read
    expect(await within(tcPanel()).findByText('Worker-отчёт по Run …')).toBeTruthy();
    expect(getTaskConversation.mock.calls.length).toBe(readsBefore + 1);
    for (let i = 0; i < 3; i++) window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(stub.getRun.mock.calls.length).toBeGreaterThanOrEqual(5));
    expect(getTaskConversation.mock.calls.length).toBe(readsBefore + 1); // known handoff: no further chat reads
    expect(within(tcPanel()).getAllByText('Worker-отчёт по Run …')).toHaveLength(1);
    expect(stub.launchRun).not.toHaveBeenCalled();
  });

  it("another Task's Run handoff never triggers this Task's conversation read", async () => {
    at('/projects/P0001/tasks/T0001');
    const foreign = reported({ task_id: 'T0002' });
    const getTaskConversation = vi.fn(async () => page([earlier]));
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1, T2]),
      listTaskRuns: vi.fn(async () => [foreign]), getRun: vi.fn(async () => foreign), getTaskConversation });
    render(<App api={stub} />);
    expect(await within(await openTc()).findByText('Сделай задачу')).toBeTruthy();
    await waitFor(() => expect(stub.getRun).toHaveBeenCalled());
    const reads = getTaskConversation.mock.calls.length;
    window.dispatchEvent(new Event('focus'));
    await waitFor(() => expect(stub.getRun.mock.calls.length).toBeGreaterThanOrEqual(2));
    expect(getTaskConversation.mock.calls.length).toBe(reads);
  });
});

describe('P055 QA-RUN-HANDOFF-UNANSWERED: the handoff is a service message', () => {
  it('labels the handoff by its durable receipt and shows no false «без ответа» warning', async () => {
    at('/projects/P0001/tasks/T0001');
    const status = groupStatus({ conversation_id: 'c-t1', roster: [worker, architect(true)], names, has_owner_message: true,
      unanswered: [], stop_reason: null,
      run_handoffs: [{ message_id: HANDOFF_ID, ordinal: 4, run_id: 'r', addressed_to: ARCH, review_state: 'awaiting_architect_review', next_actor: 'Architect' }] });
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      getGroupStatus: vi.fn(async () => status), getTaskConversation: vi.fn(async () => page([earlier, handoffMessage()])) });
    render(<App api={stub} />);
    const note = await within(await openTc()).findByTestId('run-handoff-note');
    expect(note.textContent).toContain('Отчёт Worker получен — ожидает Architect Review');
    expect(note.textContent).toContain('модель не вызывалась');
    expect(within(tcPanel()).queryByText(/без ответа/)).toBeNull();
    expect(within(tcPanel()).queryByText(/Обсуждение остановлено/)).toBeNull();
  });
});

const passed: RunReview = { review_id: 'rv1', run_id: 'r', task_id: 'T0001', version: 1, supersedes_review_id: null,
  verdict: 'review_passed', summary: 'Критерии выполнены по коду и логам.', findings: [], limitations: ['Только логи Worker'],
  continue_by: null, report_path: 'reports/final.md', report_sha256: 'a'.repeat(64),
  architect: { agent_id: ARCH, display_name: 'DeepSeek' }, recorded_at: '2026-11-18T11:00:00Z', source: 'rosc_review_submit' };
const ownerDecision = (): TaskAuthoring => taskAuthoring({ next: { action: 'owner_decision', label: 'Ожидает решения Owner', enabled: false,
  next_actor: 'Owner', hint: 'Architect Review пройден: не VERIFIED.' } });
const awaiting = (): TaskAuthoring => taskAuthoring({ next: { action: 'awaiting_review', label: 'Ожидает Architect Review', enabled: false,
  next_actor: 'Architect', hint: 'Отчёт Worker получен.' } });

describe('P055 QA-RUN-REVIEW-NEXT-STATE: recorded Architect Review advances the next actor', () => {
  it('a new TC message (the Architect reply after rosc_review_submit) re-reads the Run and the next-action card', async () => {
    at('/projects/P0001/tasks/T0001');
    let run = reported();
    let authoring = awaiting();
    let history = [earlier, handoffMessage()];
    const getTaskConversation = vi.fn(async () => page(history));
    const stub = stubApi({ listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]),
      listTaskRuns: vi.fn(async () => [run]), getRun: vi.fn(async () => run), getTaskAuthoring: vi.fn(async () => authoring),
      getTaskConversation, sendTaskConversationMessage: vi.fn(async (_p: string, _t: string, input: { body: string }) => ({
        message: conversationMessage(input.body, { message_id: 'm-7', ordinal: 7 }), replayed: false })) });
    render(<App api={stub} />);
    const ctx = within(await screen.findByLabelText('Контекст и действия'));
    expect(await ctx.findByText('Следующий ход: Architect')).toBeTruthy();
    // The Architect submits the Review through its Tool, then replies; only the recorded review changes the state.
    run = reported({ review: { state: 'review_passed', next_actor: 'Owner', latest: passed, note: '' } });
    authoring = ownerDecision();
    history = [earlier, handoffMessage()];
    const runReads = stub.getRun.mock.calls.length;
    // a new message lands in the open TC (here the Owner's own note; the Architect's reply arrives the same way)
    fireEvent.change(within(tcPanel()).getByLabelText('Сообщение в Task Conversation'), { target: { value: 'Спасибо' } });
    fireEvent.click(within(tcPanel()).getByRole('button', { name: 'Отправить' }));
    expect(await ctx.findByText('Следующий ход: Owner')).toBeTruthy();
    expect(stub.getRun.mock.calls.length).toBe(runReads + 1); // exactly one bounded Run re-read
    expect(ctx.getAllByText('Ожидает решения Owner').length).toBeGreaterThan(0);
    const review = await ctx.findByTestId('run-review');
    expect(review.textContent).toContain('Architect Review пройден');
    expect(review.textContent).toContain('DeepSeek');
    expect(review.textContent).toContain('не VERIFIED');
    expect(stub.launchRun).not.toHaveBeenCalled();
  });
});

describe('P055 first-run bootstrap of an empty Project', () => {
  it('no_sources offers the explicit identity-only acknowledgement instead of a dead end', () => {
    const view = describeBootstrap(workerSession({ state: 'active', reason: 'initialised',
      bootstrap: { state: 'none', reason: 'no_sources', can_bootstrap: true, sources: [], confirmed_at: null } }));
    expect(view.button).toBe('Подтвердить данные проекта');
    expect(view.line).toContain('создавать их не нужно');
    const missing = describeBootstrap(workerSession({ state: 'active', reason: 'initialised',
      bootstrap: { state: 'none', reason: 'identity_missing', can_bootstrap: true, sources: [], confirmed_at: null } }));
    expect(missing.button).toBe('Представить проект');
    const notActive = describeBootstrap(workerSession());
    expect(notActive.button).toBeNull();
  });
});
