import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { PromptRevision, TaskAuthoring } from '../core/models';
import { deferred, project, stubApi, task, taskAuthoring } from './testing';
import { RichView } from '../presentations/default/ui/RichText';

const at = (path: string) => history.replaceState(null, '', path);
const P1 = project('P0001');
const T1 = task('T0001', 'P0001');
const prompt = (over: Partial<PromptRevision> = {}): PromptRevision => ({
  revision: 1, source_spec_revision: 1, summary: '1. Initial', body: 'Do the work', digest: 'p'.repeat(64), rules_ref: null,
  state: 'unapproved', actor: { kind: 'owner', id: 'local-owner' }, created_at: '2026-10-31T00:00:00Z', approval: null, ...over,
});
const withPrompt = (p: PromptRevision, next: TaskAuthoring['next']): TaskAuthoring => taskAuthoring({ prompt: p, prompt_history: [p], next });
const open = (authoring: TaskAuthoring, extra = {}) => stubApi({
  listProjects: vi.fn(async () => [P1]), listTasks: vi.fn(async () => [T1]), getTaskAuthoring: vi.fn(async () => authoring), ...extra });
const specPanel = async () => within(await screen.findByLabelText('Спецификация задачи'));
const context = async () => within(await screen.findByLabelText('Контекст и действия'));

beforeEach(() => at('/projects/P0001/tasks/T0001'));
afterEach(() => { cleanup(); document.body.style.overflow = ''; });

describe('Task Spec editor (P040)', () => {
  it('saves the edited rich text as a new revision against the shown revision', async () => {
    const api = open(taskAuthoring());
    render(<App api={api} />);
    const spec = await specPanel();
    expect(await spec.findByTestId('spec-version')).toHaveProperty('textContent', 'Spec v1');
    expect((spec.getByRole('button', { name: 'Сохранить Spec' }) as HTMLButtonElement).disabled).toBe(true);
    expect((spec.getByLabelText(/Прикрепить файл/) as HTMLButtonElement).disabled).toBe(true);
    const editor = spec.getByLabelText('Описание задачи', { selector: '[role="textbox"]' });
    editor.innerHTML = '<p>Новое <strong>описание</strong></p>';
    fireEvent.input(editor);
    const save = spec.getByRole('button', { name: 'Сохранить Spec' }) as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(api.saveSpec).toHaveBeenCalledTimes(1));
    const [, , input] = api.saveSpec.mock.calls[0] as unknown as [string, string, { expected_revision: number; description: string; acceptance_criteria: string }];
    expect(input.expected_revision).toBe(1);
    expect(input.description).toContain('<strong>описание</strong>');
    expect(input.acceptance_criteria).toContain('Done T0001');
  });

  it('shows a version conflict from Core and keeps the unsaved text', async () => {
    const { ApiError } = await import('../core/api');
    const api = open(taskAuthoring(), { saveSpec: vi.fn(async () => { throw new ApiError('The Spec is at revision 2, not 1', 'conflict', 409, 'spec_conflict'); }) });
    render(<App api={api} />);
    const spec = await specPanel();
    const editor = spec.getByLabelText('Критерии приёмки', { selector: '[role="textbox"]' });
    editor.innerHTML = '<p>Мои правки</p>';
    fireEvent.input(editor);
    fireEvent.click(spec.getByRole('button', { name: 'Сохранить Spec' }));
    expect(await spec.findByRole('alert')).toBeTruthy();
    expect(editor.textContent).toBe('Мои правки');
  });

  it('opens an earlier revision read-only', async () => {
    const old = { ...taskAuthoring().spec, revision: 1, description_html: '<p>Старая версия</p>', current: false };
    const head = taskAuthoring({ spec: { ...taskAuthoring().spec, revision: 2, description_html: '<p>Новая версия</p>' } });
    const hist = [1, 2].map(revision => ({ revision, title: 't', scope: '', digest: 'd', provenance: 'owner' as const, actor: { kind: 'owner', id: 'x' }, created_at: '', current: revision === 2 }));
    const api = open(head, { listSpecRevisions: vi.fn(async () => hist), getSpecRevision: vi.fn(async () => old) });
    render(<App api={api} />);
    const spec = await specPanel();
    fireEvent.click(await spec.findByRole('button', { name: 'v1' }));
    expect(await spec.findByText(/только чтение/)).toBeTruthy();
    expect(spec.getByText('Старая версия')).toBeTruthy();
    expect(spec.queryByRole('textbox')).toBeNull();
  });

  it('renders rich text through a whitelist and never creates a script element', () => {
    const { container } = render(<RichView html={'<p>a<script>window.x=1</script><img src=x onerror=alert(1)><strong>b</strong></p>'} />);
    expect(container.querySelector('script, img')).toBeNull();
    expect(container.querySelector('strong')?.textContent).toBe('b');
  });
});

describe('Context & actions and the Prompt Draft drawer (P040)', () => {
  it('leads from creating a Draft to saving it as a new unapproved revision', async () => {
    const api = open(taskAuthoring());
    render(<App api={api} />);
    const ctx = await context();
    fireEvent.click(await ctx.findByRole('button', { name: 'Создать Prompt Draft' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect((dialog.getByRole('button', { name: 'Утвердить Draft' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText('Резюме изменений по пунктам'), { target: { value: '1. Всё' } });
    fireEvent.change(dialog.getByLabelText('Полный текст Prompt Draft'), { target: { value: 'Сделай работу' } });
    fireEvent.click(dialog.getByRole('button', { name: 'Сохранить новую версию' }));
    await waitFor(() => expect(api.savePrompt).toHaveBeenCalledWith('P0001', 'T0001',
      { expected_revision: null, source_spec_revision: 1, body: 'Сделай работу', summary: '1. Всё' }));
  });

  it('approves only the saved revision and disables approval while there are unsaved edits', async () => {
    const api = open(withPrompt(prompt(), { action: 'review_prompt', label: 'Просмотреть и утвердить Prompt', enabled: true, next_actor: 'Owner', hint: 'h' }));
    render(<App api={api} />);
    fireEvent.click(await (await context()).findByRole('button', { name: 'Просмотреть и утвердить Prompt' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText('Prompt Draft v1', { selector: 'h2' })).toBeTruthy();
    const approve = dialog.getByRole('button', { name: 'Утвердить Draft v1' }) as HTMLButtonElement;
    expect(approve.disabled).toBe(false);
    fireEvent.change(dialog.getByLabelText('Полный текст Prompt Draft'), { target: { value: 'Правка' } });
    expect(approve.disabled).toBe(true);
    fireEvent.change(dialog.getByLabelText('Полный текст Prompt Draft'), { target: { value: 'Do the work' } });
    expect(approve.disabled).toBe(false);
    fireEvent.click(approve);
    await waitFor(() => expect(api.approvePrompt).toHaveBeenCalledWith('P0001', 'T0001', 1, 'p'.repeat(64)));
  });

  it('marks a stale Draft, blocks approval and offers to update it', async () => {
    const api = open(withPrompt(prompt({ state: 'stale', source_spec_revision: 1 }),
      { action: 'update_prompt', label: 'Обновить Prompt Draft', enabled: true, next_actor: 'Owner', hint: 'h' }));
    render(<App api={api} />);
    expect(await within(await screen.findByLabelText('Спецификация задачи')).findByText('Prompt устарел')).toBeTruthy();
    fireEvent.click(await (await context()).findByRole('button', { name: 'Обновить Prompt Draft' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect((dialog.getByRole('button', { name: /Утвердить Draft v1/ }) as HTMLButtonElement).disabled).toBe(true);
    expect(dialog.getByText(/утвердить устаревшую версию нельзя/)).toBeTruthy();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(screen.getByLabelText('Task Conversation')).toBeTruthy(); // closing returns to the Task with the chat intact
  });

  it('after approval offers a disabled "Запустить Run" that never starts anything', async () => {
    const approved = prompt({ state: 'approved', approval: { spec_revision: 1, prompt_revision: 1, digest: 'p'.repeat(64),
      actor: { kind: 'owner', id: 'local-owner' }, created_at: '2026-10-31T00:00:00Z' } });
    const api = open(withPrompt(approved, { action: 'authorize_run', label: 'Запустить Run', enabled: false, next_actor: 'Owner',
      hint: 'Запуск Run станет доступен после подключения Worker и передачи задания (P042)', secondary: 'Просмотреть утверждённый Draft' }));
    render(<App api={api} />);
    const ctx = await context();
    const cta = await ctx.findByRole('button', { name: 'Запустить Run' }) as HTMLButtonElement;
    expect(cta.disabled).toBe(true);
    expect(cta.title).toContain('P042');
    expect(ctx.getByTestId('draft-fact').textContent).toContain('утверждён');
    expect(ctx.getByText('Run не запускался')).toBeTruthy();
    const worker = within(ctx.getByLabelText('Worker этой Task'));
    expect(worker.getByText('Не активен')).toBeTruthy();
    expect((worker.getByRole('button', { name: 'Активировать' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(ctx.getByRole('button', { name: 'Prompt / контракт' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect((dialog.getByRole('button', { name: 'Draft v1 утверждён' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.approvePrompt).not.toHaveBeenCalled();
  });
});

describe('P040 r02 UX pass', () => {
  it('saves a changed title and scope, passes them to Core and CAS-guards against the loaded revision', async () => {
    const api = open(taskAuthoring());
    render(<App api={api} />);
    const spec = await specPanel();
    fireEvent.change(await spec.findByLabelText('Название Task'), { target: { value: 'Новое название' } });
    fireEvent.change(spec.getByLabelText('Границы (scope)'), { target: { value: 'Только API' } });
    fireEvent.click(spec.getByRole('button', { name: 'Сохранить Spec' }));
    await waitFor(() => expect(api.saveSpec).toHaveBeenCalledTimes(1));
    expect(api.saveSpec.mock.calls[0][2]).toMatchObject({ expected_revision: 1, title: 'Новое название', scope: 'Только API' });
  });

  it('shows no blocker banner, no "Агенты не запущены" and no stale footer promise', async () => {
    render(<App api={open(taskAuthoring())} />);
    const drawer = await screen.findByLabelText('Task Work Area');
    await within(drawer).findByLabelText('Контекст и действия');
    expect(within(drawer).queryByText('Активных блокеров нет')).toBeNull();
    expect(within(drawer).queryByText('Агенты не запущены')).toBeNull();
    expect(within(drawer).queryByText(/появятся в следующих срезах$/)).toBeNull();
    expect(drawer.querySelector('.rosc-task-notice')).toBeNull();
  });

  it('puts the primary action first, then Architect/Worker, then the facts, then documents', async () => {
    render(<App api={open(taskAuthoring())} />);
    const panel = (await screen.findByLabelText('Контекст и действия')) as HTMLElement;
    await within(panel).findByRole('button', { name: 'Создать Prompt Draft' });
    const order = ['.rosc-task-primary', '.rosc-task-agents', '.rosc-task-facts', '.rosc-task-actions'].map(sel => panel.querySelector(sel)!);
    for (let i = 0; i < order.length - 1; i++) expect(order[i].compareDocumentPosition(order[i + 1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(panel).queryByText('Architect', { selector: 'dt' })).toBeNull();
  });

  it('shows THIS Task\'s Architect state, not the Project host, and activates only this Task by the button', async () => {
    const { architectStatus, taskArchitectStatus } = await import('./testing');
    const live = architectStatus({ state: 'ready', can_activate: false, can_deactivate: true });
    let state: 'inactive' | 'active' = 'inactive';
    const api = open(taskAuthoring(), {
      getArchitect: vi.fn(async () => live),  // Project host is ready the whole time
      getTaskArchitect: vi.fn(async () => taskArchitectStatus({ state, can_activate: state !== 'active' })),
      activateTaskArchitect: vi.fn(async () => { state = 'active'; return taskArchitectStatus({ state, can_activate: false }); }),
    });
    render(<App api={api} />);
    const ctx = await context();
    const card = within(await ctx.findByLabelText('Architect этой Task'));
    expect(await card.findByText('Не активен')).toBeTruthy();  // host ready is NOT Task Architect active
    expect(ctx.queryByText(/Project Architect/)).toBeNull();
    expect(card.queryByText(/сессии|Task-диалог/)).toBeNull();  // no long explanation in the normal state
    const press = card.getByRole('button', { name: 'Активировать' });
    fireEvent.click(press); fireEvent.click(press);  // a second quick click does nothing more
    await waitFor(() => expect(api.activateTaskArchitect).toHaveBeenCalledWith('P0001', 'T0001'));
    expect(await card.findByText('Активен')).toBeTruthy();
    expect((card.getByRole('button', { name: 'Завершить сессию' }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.activateTaskArchitect).toHaveBeenCalledTimes(1);
    expect(api.activateArchitect).not.toHaveBeenCalled();  // the Project action is not used by the Task button
    expect(api.sendTaskConversationMessage).not.toHaveBeenCalled();  // no message, no model turn
    expect(api.sendTaskMessageToArchitect).not.toHaveBeenCalled();
  });

  it('shows only a short factual reason in the error state and offers an explicit retry', async () => {
    const { taskArchitectStatus } = await import('./testing');
    let state: 'error' | 'active' = 'error';
    const api = open(taskAuthoring(), {
      getTaskArchitect: vi.fn(async () => taskArchitectStatus(state === 'error' ? { state: 'error', detail: 'host_not_running' } : { state: 'active', can_activate: false })),
      activateTaskArchitect: vi.fn(async () => { state = 'active'; return taskArchitectStatus({ state: 'active', can_activate: false }); }),
    });
    render(<App api={api} />);
    const card = within(await (await context()).findByLabelText('Architect этой Task'));
    expect(await card.findByText('Ошибка')).toBeTruthy();
    expect(card.getByRole('alert').textContent).toBe('Сессия потеряна: host Architect остановлен.');
    fireEvent.click(card.getByRole('button', { name: 'Повторить активацию' }));
    expect(await card.findByText('Активен')).toBeTruthy();
    expect(card.queryByRole('alert')).toBeNull();
  });

  it('keeps each Task\'s Architect status separate when switching Tasks and drops a late result', async () => {
    const { taskArchitectStatus } = await import('./testing');
    const late = deferred<ReturnType<typeof taskArchitectStatus>>();
    const api = open(taskAuthoring(), {
      listTasks: vi.fn(async () => [T1, task('T0002', 'P0001')]),
      getTaskArchitect: vi.fn((_p: string, taskId: string) => taskId === 'T0001' ? late.promise
        : Promise.resolve(taskArchitectStatus({ task_id: taskId, state: 'inactive' }))),
    });
    render(<App api={api} />);
    await context();
    history.pushState(null, '', '/projects/P0001/tasks/T0002'); window.dispatchEvent(new PopStateEvent('popstate'));
    await screen.findByText(/^T0002 ·/);
    const card = within(await (await context()).findByLabelText('Architect этой Task'));
    expect(await card.findByText('Не активен')).toBeTruthy();
    late.resolve(taskArchitectStatus({ task_id: 'T0001', state: 'active', can_activate: false }));
    await new Promise(resolve => setTimeout(resolve, 30));
    expect(card.queryByText('Активен')).toBeNull();  // T0001's late answer never repaints T0002
  });

  it('keeps Prompt history selectable and shows a past revision read-only', async () => {
    const v1 = prompt({ revision: 1, summary: 'старое резюме', body: 'старый текст', state: 'superseded' });
    const v2 = prompt({ revision: 2, summary: 'новое резюме', body: 'новый текст', digest: 'q'.repeat(64) });
    const head = taskAuthoring({ prompt: v2, prompt_history: [{ ...v1, body: undefined, current: false }, { ...v2, body: undefined, current: true }],
      next: { action: 'review_prompt', label: 'Просмотреть и утвердить Prompt', enabled: true, next_actor: 'Owner', hint: 'h' } });
    const api = open(head, { getPromptRevision: vi.fn(async () => v1) });
    render(<App api={api} />);
    fireEvent.click(await (await context()).findByRole('button', { name: 'Prompt / контракт' }));
    const dialog = within(await screen.findByRole('dialog'));
    fireEvent.click(dialog.getByRole('button', { name: /^v1/ }));
    expect(await dialog.findByText(/Просмотр Prompt v1; текущая v2/)).toBeTruthy();
    const body = dialog.getByLabelText('Полный текст Prompt Draft') as HTMLTextAreaElement;
    expect(body.value).toBe('старый текст');
    expect(body.readOnly).toBe(true);
    expect((dialog.getByRole('button', { name: 'Утвердить Draft v2' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(dialog.getByRole('button', { name: 'К текущей версии' }));
    expect((dialog.getByLabelText('Полный текст Prompt Draft') as HTMLTextAreaElement).value).toBe('новый текст');
  });
});
