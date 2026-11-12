import { useCallback, useRef, useState } from 'react';
import type { SpecRevision } from '../../../core/models';
import { describeTaskArchitect } from '../ui/architectView';
import { PromptDraftDrawer } from '../ui/PromptDraftDrawer';
import { RichEditor, RichToolbar, RichView, type RichEditorHandle } from '../ui/RichText';
import type { TaskViewProps } from '../../../app/presentation/contracts';
import { Drawer } from '../ui/Drawer';
import { Feedback } from '../ui/Feedback';
import { TextViewControl } from '../ui/TextViewControl';
import { TaskConversationPanel } from '../ui/TaskConversationPanel';
import { WorkspaceActions } from '../ui/WorkspaceActions';
import { useApplyTextScale } from '../ui/useApplyTextScale';

const LATER = 'Будет доступно позже';

/** A visible control for a capability Core does not have yet: natively disabled, no handler, nothing simulated. */
function Later({ children, label }: { children: string; label?: string }) {
  return <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER} aria-label={label ?? `${children} — ${LATER.toLowerCase()}`}>{children}</button>;
}

/**
 * Task Work Area. The structure follows the approved prototype (Blocker slot, Task heading, Specification,
 * Working Plan | Context & Actions, full-height Task Conversation, full-width head/footer). Core data fills the
 * regions it has; the rest are honest empty states with disabled controls, never simulated state.
 */
export function TaskView({ projectName, taskId, task, tasksStatus, feedback, inert, scroll, workspace, conversation, authoring, textScale, onClose, onRetry }: TaskViewProps) {
  const scaleRef = useApplyTextScale(textScale.scale);
  const [chatCollapsed, setChatCollapsed] = useState(false);
  const [specCollapsed, setSpecCollapsed] = useState(false);
  const [draftOpen, setDraftOpen] = useState(false);
  const [specNote, setSpecNote] = useState('');
  const [specFailure, setSpecFailure] = useState('');
  const [past, setPast] = useState<SpecRevision | null>(null);
  const [richDirty, setRichDirty] = useState(false);
  const [loadedSpec, setLoadedSpec] = useState<SpecRevision | null>(null);
  const [title, setTitle] = useState('');
  const [scope, setScope] = useState('');
  const [activateFailure, setActivateFailure] = useState('');
  const descriptionEditor = useRef<RichEditorHandle | null>(null);
  const criteriaEditor = useRef<RichEditorHandle | null>(null);
  const markDirty = useCallback(() => { setRichDirty(true); setSpecNote(''); }, []);
  const data = authoring.data;
  const spec = data?.spec ?? null;
  const fieldsDirty = loadedSpec !== null && (title !== loadedSpec.title || scope !== (loadedSpec.scope ?? ''));
  const specDirty = richDirty || fieldsDirty;
  // The editor follows the saved head only while nothing is being edited; a head that moved underneath is a visible conflict.
  if (spec && (loadedSpec === null || (loadedSpec.digest !== spec.digest && !specDirty))) {
    setLoadedSpec(spec); setTitle(spec.title); setScope(spec.scope ?? ''); setRichDirty(false);
  }
  const specMoved = spec !== null && loadedSpec !== null && spec.digest !== loadedSpec.digest && specDirty;
  const canEditSpec = data?.editable === true && past === null;
  const saveSpec = async () => {
    setSpecFailure(''); setSpecNote('');
    if (!loadedSpec) return;
    if (title.trim() === '') { setSpecFailure('Название Task не может быть пустым.'); return; }
    const result = await authoring.onSaveSpec({ expected_revision: loadedSpec.revision, title: title.trim(), scope,
      description: descriptionEditor.current?.html() ?? '', acceptance_criteria: criteriaEditor.current?.html() ?? '' });
    if (result.error) { setSpecFailure(result.error); return; }
    setRichDirty(false); setLoadedSpec(null);
    setSpecNote(result.created ? 'Новая версия Spec сохранена.' : 'Изменений нет — новая версия не создана.');
  };
  const discardSpec = () => { setRichDirty(false); setLoadedSpec(null); setSpecFailure(''); setPast(null); authoring.onReload(); };
  const openPast = async (revision: number) => {
    setSpecFailure('');
    if (spec && revision === spec.revision) { setPast(null); return; }
    const result = await authoring.onLoadSpecRevision(revision);
    if (typeof result === 'string') setSpecFailure(result); else setPast(result);
  };
  const shown = past ?? spec;
  const next = data?.next ?? null;
  const prompt = data?.prompt ?? null;
  const promptText = prompt ? `v${prompt.revision} · ${prompt.state === 'stale' ? 'устарел' : prompt.state === 'approved' ? 'утверждён' : 'не утверждён'}` : 'нет';
  const primaryEnabled = next !== null && next.enabled && !authoring.busy;
  const primaryTone = specDirty ? 'positive' : next === null ? 'neutral'
    : next.action === 'update_prompt' ? 'warning' : next.enabled && (next.action === 'create_prompt' || next.action === 'review_prompt') ? 'positive' : 'neutral';
  const tca = conversation.taskArchitect;
  const architectView = describeTaskArchitect(tca.status, tca.info, tca.busy);
  const head = <>
    <div className="rosc-drawer__heading">
      <p className="rosc-drawer-eyebrow">TASK / WORK AREA</p>
      <h2>{task ? `${task.id} · ${spec?.title ?? task.title}` : taskId}</h2>
      <p>{projectName} · чтение Task не запускает агента</p>
    </div>
    <div className="rosc-drawer__controls">
      {task && <span className="rosc-tag rosc-tag--info">{task.status}</span>}
      <TextViewControl scopeLabel="Task Work Area" scale={textScale.scale} onChange={textScale.onChange} />
    </div>
  </>;
  const footer = <>
    <span className="rosc-task-foot__note">Spec и Prompt Draft хранятся версиями · утверждение Prompt не запускает Run; Run, Worker и приёмка — в следующих срезах</span>
    <span className="rosc-task-foot__actions">
      <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER}>Принять задачу</button>
      <button type="button" className="rosc-btn rosc-btn--small" onClick={onClose}>К проекту</button>
    </span>
  </>;
  const state = (task === null)
    ? (tasksStatus === 'loading'
      ? <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка…</div>
      : tasksStatus === 'failed' ? <div className="rosc-state rosc-state--failed"><p>Не удалось загрузить задачу. <button className="rosc-btn rosc-btn--small" onClick={onRetry}>Повторить</button></p></div>
        : <div className="rosc-state"><p>Задача не найдена в этом проекте.</p></div>)
    : null;
  return <Drawer variant="task" label="Task Work Area" closeLabel="Закрыть задачу" inert={inert} onClose={onClose}
    head={head} rootRef={scaleRef} fill footer={task ? footer : undefined}>
    {!task ? <div className="rosc-task-state"><Feedback {...feedback} />{state}</div>
      : <div className={chatCollapsed ? 'rosc-task-workspace rosc-task-workspace--collapsed' : 'rosc-task-workspace'}>
        <div className="rosc-task-left" ref={scroll.region(`task:${taskId}`)}>
          <Feedback {...feedback} />
          <div className="rosc-task-heading">
            <div>
              <h3>{spec?.title ?? task.title}</h3>
              <p>Task {task.id} · {spec ? `Spec v${spec.revision}` : 'Spec'} · {data?.run.label ?? 'Run не запускался'} · Исполнитель: не назначен</p>
            </div>
            <div className="rosc-task-heading__flags">
              <span className="rosc-chip">Проект: {projectName}</span>
              <span className="rosc-tag rosc-tag--info">Task: {task.status}</span>
            </div>
          </div>
          <section className={specCollapsed ? 'rosc-task-panel rosc-task-spec rosc-task-spec--collapsed' : 'rosc-task-panel rosc-task-spec'} aria-label="Спецификация задачи">
            <div className="rosc-task-panel__top"><h3>Спецификация задачи</h3>
              <span className="rosc-task-panel__chips">
                {shown && <span className="rosc-chip" data-testid="spec-version">Spec v{shown.revision}</span>}
                {shown && <span className="rosc-chip">Автор: {shown.provenance === 'architect' ? 'Architect' : shown.provenance === 'legacy_import' ? 'импорт' : 'Owner'}</span>}
                {data?.prompt?.state === 'stale' && <span className="rosc-tag rosc-tag--warning">Prompt устарел</span>}
                <span className="rosc-chip">{data?.editable ? 'Редактируется до запуска Run' : 'Зафиксирована'}</span>
              </span>
            </div>
            {authoring.status === 'failed' && <p className="rosc-task-spec__error" role="alert">Не удалось загрузить Spec. {authoring.error} <button type="button" className="rosc-task-link" onClick={authoring.onReload}>Повторить</button></p>}
            {authoring.status === 'loading' && !spec && <div className="rosc-state rosc-state--loading"><span className="rosc-spinner" aria-hidden="true" />Загрузка Spec…</div>}
            {past && <p className="rosc-task-spec__past" role="status">Просмотр прежней версии Spec v{past.revision} — только чтение. <button type="button" className="rosc-task-link" onClick={() => setPast(null)}>К текущей версии</button></p>}
            {specMoved && <p className="rosc-task-spec__error" role="alert">Spec изменилась в другом месте (теперь v{spec?.revision}). Сохранение ваших правок вернёт конфликт версий. <button type="button" className="rosc-task-link" onClick={discardSpec}>Обновить и отбросить мои правки</button></p>}
            {shown && <div className="rosc-task-spec__body" hidden={specCollapsed}>
              {canEditSpec
                ? <div className="rosc-task-spec__fields">
                  <label>Название Task<input type="text" value={title} maxLength={160} aria-label="Название Task" onChange={event => { setTitle(event.target.value); setSpecNote(''); }} /></label>
                  <label>Границы (scope)<textarea value={scope} aria-label="Границы (scope)" placeholder="Что входит и что не входит в Task" onChange={event => { setScope(event.target.value); setSpecNote(''); }} /></label>
                </div>
                : shown.scope ? <p className="rosc-task-scope"><strong>Границы (scope):</strong> {shown.scope}</p> : null}
              <div>
                <h4>Описание задачи</h4>
                {canEditSpec && <RichToolbar editor={descriptionEditor} attachLater />}
                {canEditSpec
                  ? <RichEditor ref={descriptionEditor} html={shown.description_html} resetKey={`d${loadedSpec?.revision}${loadedSpec?.digest}`} label="Описание задачи" onInput={markDirty} />
                  : <RichView html={shown.description_html} />}
              </div>
              <div>
                <h4>Критерии приёмки <small>Общие для Worker, Architect и Owner</small></h4>
                {canEditSpec && <RichToolbar editor={criteriaEditor} />}
                {canEditSpec
                  ? <RichEditor ref={criteriaEditor} html={shown.criteria_html} resetKey={`c${loadedSpec?.revision}${loadedSpec?.digest}`} label="Критерии приёмки" onInput={markDirty} />
                  : <RichView html={shown.criteria_html} />}
              </div>
            </div>}
            {(specNote || specFailure) && <p className={specFailure ? 'rosc-task-spec__error' : 'rosc-task-spec__note'} role={specFailure ? 'alert' : 'status'}>{specFailure || specNote}</p>}
            <div className="rosc-task-panel__foot">
              <span className="rosc-task-spec__versions"><strong>Одна версия критериев</strong> для исполнения, проверки и приёмки.
                {authoring.specHistory.length > 1 && <span className="rosc-task-spec__history" aria-label="Версии Spec">
                  {authoring.specHistory.map(item => <button key={item.revision} type="button" className="rosc-task-link"
                    aria-current={shown?.revision === item.revision} disabled={specDirty}
                    title={specDirty ? 'Сохраните или отмените правки' : `Открыть Spec v${item.revision}`}
                    onClick={() => void openPast(item.revision)}>v{item.revision}</button>)}
                </span>}
              </span>
              <span className="rosc-task-panel__foot-actions">
                {specDirty && <button type="button" className="rosc-btn rosc-btn--small" onClick={discardSpec}>Отменить</button>}
                {canEditSpec && <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={!specDirty || authoring.busy || title.trim() === ''}
                  onClick={() => void saveSpec()}>Сохранить Spec</button>}
                <button type="button" className="rosc-task-link" aria-expanded={!specCollapsed}
                  onClick={() => setSpecCollapsed(value => !value)}>{specCollapsed ? 'Развернуть спецификацию ⌄' : 'Свернуть спецификацию ⌃'}</button>
              </span>
            </div>
          </section>
          <div className="rosc-task-lower">
            <section className="rosc-task-panel rosc-task-plan" aria-label="Рабочий план Worker">
              <div className="rosc-task-panel__top"><h3>Рабочий план Worker</h3><span className="rosc-chip">Revision —</span></div>
              <div className="rosc-task-plan__progress" aria-label="Прогресс плана недоступен">
                <div className="rosc-task-plan__label"><strong>Прогресс не определён</strong><span>—</span></div>
                <div className="rosc-task-plan__track" aria-hidden="true" />
              </div>
              <div className="rosc-task-panel__scroll">
                <div className="rosc-state"><p>Рабочий план пока недоступен.</p><p>Шаги появятся, когда Worker подготовит план для этой Task.</p></div>
              </div>
              <div className="rosc-task-panel__foot">
                <span>Ревизии плана будут доступны позже.</span>
                <Later>История ревизий</Later>
              </div>
            </section>
            <section className="rosc-task-panel rosc-task-context" aria-label="Контекст и действия">
              <div className="rosc-task-panel__top"><h3>Контекст и действия</h3>
                <span className="rosc-chip">{next ? `Следующий ход: ${next.next_actor}` : 'Следующий ход пока не определён'}</span></div>
              <div className="rosc-task-panel__scroll">
                <div className={`rosc-task-primary rosc-task-primary--${primaryTone}`} aria-label="Основное действие">
                  <h4>{specDirty ? 'Сохраните Spec' : next?.label ?? 'Загрузка…'}</h4>
                  <p className="rosc-hint">{specDirty ? 'Есть несохранённые правки Spec. Сохраните их — каждая версия фиксируется отдельно.' : next?.hint ?? ''}</p>
                  <div className="rosc-task-actions__row">
                    {specDirty
                      ? <button type="button" className="rosc-btn rosc-btn--primary" disabled={authoring.busy || title.trim() === ''} onClick={() => void saveSpec()}>Подготовить / Сохранить Spec</button>
                      : <button type="button" className="rosc-btn rosc-btn--primary" disabled={!primaryEnabled}
                        title={next && !next.enabled ? next.hint : undefined} onClick={() => setDraftOpen(true)}>{next?.label ?? 'Загрузка…'}</button>}
                  </div>
                </div>
                <div className="rosc-task-agents">
                  <div aria-label="Architect этой Task">
                    <div className="rosc-task-agents__head"><strong>Architect</strong>
                      <span className={`rosc-tag ${architectView.active ? 'rosc-tag--success' : architectView.tone === 'error' ? 'rosc-tag--warning' : 'rosc-tag--info'}`} aria-live="polite">{architectView.chip}</span></div>
                    <div className="rosc-task-agents__actions">
                      {architectView.active
                        ? <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER}>Завершить сессию</button>
                        : tca.status === 'failed'
                          ? <button type="button" className="rosc-btn rosc-btn--small" onClick={conversation.onRefreshArchitect}>Проверить статус</button>
                          : <button type="button" className="rosc-btn rosc-btn--small rosc-btn--primary" disabled={!architectView.canActivate || tca.busy}
                            onClick={async () => setActivateFailure(await conversation.onActivateTaskArchitect() ?? '')}>
                            {tca.info?.state === 'error' ? 'Повторить активацию' : 'Активировать'}</button>}
                    </div>
                    {(architectView.reason || activateFailure) && !architectView.active
                      && <p className="rosc-task-agents__err" role="alert">{activateFailure || architectView.reason}</p>}
                  </div>
                  <div aria-label="Worker этой Task">
                    <div className="rosc-task-agents__head"><strong>Worker</strong><span className="rosc-tag rosc-tag--info">Не активен</span></div>
                    <div className="rosc-task-agents__actions">
                      <button type="button" className="rosc-btn rosc-btn--small" disabled title={LATER}>Активировать</button>
                    </div>
                  </div>
                </div>
                <dl className="rosc-task-facts">
                  <div><dt>Проект</dt><dd>{projectName}</dd></div>
                  <div><dt>Task</dt><dd>{task.status}</dd></div>
                  <div><dt>Spec</dt><dd>{spec ? `v${spec.revision}${specDirty ? ' · есть несохранённые правки' : ''}` : '—'}</dd></div>
                  <div><dt>Prompt</dt><dd data-testid="draft-fact">{promptText}</dd></div>
                  <div><dt>Workspace</dt><dd>{task.workspace_state === 'ready' ? 'готов' : 'нужна сверка'}</dd></div>
                  <div><dt>Run</dt><dd>{data?.run.label ?? 'не создан'}</dd></div>
                </dl>
                <WorkspaceActions key={`${task.id}:${task.workspace_problem}`} state={task.workspace_state} problem={task.workspace_problem} controls={workspace} />
                <div className="rosc-task-actions">
                  <h4>Документы Task</h4>
                  <div className="rosc-task-actions__row">
                    <button type="button" className="rosc-btn rosc-btn--small" disabled={!prompt}
                      title={prompt ? undefined : 'Prompt ещё не создан — начните с основного действия выше'} onClick={() => setDraftOpen(true)}>Prompt / контракт</button>
                    <Later>Журнал выполнения</Later>
                    <Later>Ревизии плана</Later>
                  </div>
                </div>
              </div>
              <div className="rosc-task-panel__foot"><span>Утверждение Draft не запускает Worker: Owner запускает Run одной командой после выбора доступного Worker.</span></div>
            </section>
          </div>
        </div>
        <TaskConversationPanel key={`${task.project_id}/${task.id}`} taskId={task.id} conversation={conversation}
          collapsed={chatCollapsed} onCollapsedChange={setChatCollapsed} />
        {draftOpen && <PromptDraftDrawer key={`${task.project_id}/${task.id}`} taskId={task.id} authoring={authoring} onClose={() => setDraftOpen(false)} />}
      </div>}
  </Drawer>;
}
