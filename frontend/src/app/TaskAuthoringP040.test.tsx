import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import type { PromptRevision, TaskAuthoring } from '../core/models';
import { project, stubApi, task, taskAuthoring } from './testing';
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

  it('after approval offers a disabled "Проверить и разрешить Run" that never starts anything', async () => {
    const approved = prompt({ state: 'approved', approval: { spec_revision: 1, prompt_revision: 1, digest: 'p'.repeat(64),
      actor: { kind: 'owner', id: 'local-owner' }, created_at: '2026-10-31T00:00:00Z' } });
    const api = open(withPrompt(approved, { action: 'authorize_run', label: 'Проверить и разрешить Run', enabled: false, next_actor: 'Owner',
      hint: 'Разрешение Run появится в P041', secondary: 'Просмотреть утверждённый Draft' }));
    render(<App api={api} />);
    const ctx = await context();
    const cta = await ctx.findByRole('button', { name: 'Проверить и разрешить Run' }) as HTMLButtonElement;
    expect(cta.disabled).toBe(true);
    expect(cta.title).toContain('P041');
    expect(ctx.getByTestId('draft-fact').textContent).toContain('утверждён');
    expect(ctx.getByText('Run не авторизован')).toBeTruthy();
    expect(ctx.getByText('Worker не запускался')).toBeTruthy();
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

  it('offers the existing Project activation from the Task and shows the active state afterwards', async () => {
    const { architectStatus } = await import('./testing');
    let live = false;
    const api = open(taskAuthoring(), {
      getArchitect: vi.fn(async () => architectStatus(live ? { state: 'ready', can_activate: false, can_deactivate: true } : { state: 'stopped', can_activate: true })),
      activateArchitect: vi.fn(async () => { live = true; return architectStatus({ state: 'ready', can_activate: false, can_deactivate: true }); }),
    });
    render(<App api={api} />);
    const ctx = await context();
    fireEvent.click(await ctx.findByRole('button', { name: 'Активировать Project Architect' }));
    await waitFor(() => expect(api.activateArchitect).toHaveBeenCalledWith('P0001'));
    expect(await ctx.findByText(/Architect активен — можно обратиться через @ Architect/)).toBeTruthy();
    expect(api.sendTaskConversationMessage).not.toHaveBeenCalled();
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
