import { useEffect, useRef, useState } from 'react';
import type { TaskAuthoringControls } from '../../../app/presentation/contracts';
import type { PromptRevision } from '../../../core/models';

const STATE_TEXT: Record<string, string> = { unapproved: 'не утверждён', stale: 'устарел', approved: 'утверждён', superseded: 'заменён' };
const actorText = (kind: string) => (kind === 'architect' ? 'Architect' : kind === 'owner' ? 'Owner' : kind);
const formatTime = (iso: string) => {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
};

type Props = { taskId: string; authoring: TaskAuthoringControls; onClose: () => void };

/**
 * Prompt Draft side drawer over the Task Work Area. "What changed" comes first, then the full text. Editing never
 * counts as approval: any change disables approving until it is saved as a NEW revision. A past revision is shown
 * read-only from Core's stored copy and can never be edited or approved. Approval of an exact saved revision is a Core
 * operation and is not a Run authorization.
 */
export function PromptDraftDrawer({ taskId, authoring, onClose }: Props) {
  const data = authoring.data;
  const head = data?.prompt ?? null;
  const revisions = data?.prompt_history ?? [];
  const [summary, setSummary] = useState(head?.summary ?? '');
  const [body, setBody] = useState(head?.body ?? '');
  const [baseDigest, setBaseDigest] = useState<string | null>(head?.digest ?? null);
  // What the fields were last loaded with; "dirty" is relative to this, never to a head that moved underneath.
  const [loaded, setLoaded] = useState({ summary: head?.summary ?? '', body: head?.body ?? '' });
  const [expanded, setExpanded] = useState(false);
  const [message, setMessage] = useState('');
  const [failure, setFailure] = useState('');
  const [past, setPast] = useState<PromptRevision | null>(null);
  const [closeWarning, setCloseWarning] = useState(false);
  const dirty = summary !== loaded.summary || body !== loaded.body;
  const first = useRef<HTMLTextAreaElement | null>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  // A Draft saved elsewhere (for example by the Architect Tool) replaces the fields only while nothing is being edited.
  useEffect(() => {
    const incoming = head?.digest ?? null;
    if (incoming === baseDigest) return;
    if (!dirty) {
      const next = { summary: head?.summary ?? '', body: head?.body ?? '' };
      setSummary(next.summary); setBody(next.body); setLoaded(next); setBaseDigest(incoming);
      setMessage('Показана актуальная сохранённая версия.');
    }
  }, [head, baseDigest, dirty]);
  useEffect(() => { first.current?.focus(); }, []);

  /** Closing with unsaved edits asks once, inline; the second attempt closes without saving. */
  const requestClose = () => {
    if (dirtyRef.current && !closeWarning) { setCloseWarning(true); return; }
    onClose();
  };
  const requestCloseRef = useRef(requestClose);
  requestCloseRef.current = requestClose;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') { event.stopPropagation(); requestCloseRef.current(); } };
    document.addEventListener('keydown', onKey, true);
    return () => document.removeEventListener('keydown', onKey, true);
  }, []);
  useEffect(() => { if (!dirty) setCloseWarning(false); }, [dirty]);

  const viewing = past !== null && head !== null && past.revision !== head.revision;
  const movedElsewhere = head !== null && baseDigest !== null && baseDigest !== head.digest && dirty;
  const shown = viewing ? past : head;
  const stale = !viewing && head?.state === 'stale';
  const approved = !viewing && head?.state === 'approved';
  const canSave = !viewing && !authoring.busy && dirty && summary.trim() !== '' && body.trim() !== '' && data?.editable === true;
  const canApprove = !viewing && !authoring.busy && !dirty && head?.state === 'unapproved' && data?.editable === true;
  const title = viewing ? `Prompt v${past.revision} · просмотр` : head ? `Prompt Draft v${head.revision}` : 'Новый Prompt Draft';
  const meta = data
    ? `Task ${taskId} · Spec v${data.spec.revision}${shown ? ` · автор: ${actorText(shown.actor.kind)} · основан на Spec v${shown.source_spec_revision}` : ' · Draft ещё не создан'}`
    : taskId;
  const shownState = viewing ? 'superseded' : head?.state;

  const openRevision = async (revision: number) => {
    setFailure(''); setMessage('');
    if (head && revision === head.revision) { setPast(null); return; }
    const result = await authoring.onLoadPromptRevision(revision);
    if (typeof result === 'string') setFailure(result); else setPast(result);
  };

  const save = async () => {
    setFailure(''); setMessage('');
    const error = await authoring.onSavePrompt(body.trim(), summary.trim());
    if (error) { setFailure(error); return; }
    setLoaded({ summary: summary.trim(), body: body.trim() });
    setBaseDigest(null);
    setMessage('Новая версия сохранена. Она не утверждена — утвердите её отдельно.');
  };
  const approve = async () => {
    setFailure(''); setMessage('');
    const error = await authoring.onApprove();
    if (error) setFailure(error); else setMessage(`Draft v${head?.revision} утверждён. Разрешение Run — отдельное действие (появится в P041).`);
  };
  const copy = async () => {
    try { await navigator.clipboard.writeText(viewing ? past.body ?? '' : body); setMessage('Весь текст Draft скопирован.'); }
    catch { first.current?.blur(); setMessage('Не удалось скопировать автоматически — выделите текст вручную.'); }
  };

  const note = viewing ? `Просмотр прежней версии: изменить или утвердить её нельзя. Любая правка сохраняется только как новая версия текущего Draft.`
    : stale ? 'Spec изменилась после этого Draft. Пересмотрите текст и резюме и сохраните новую версию под текущую Spec — утвердить устаревшую версию нельзя.'
      : approved ? `Draft v${head?.revision} утверждён (${actorText(head?.approval?.actor.kind ?? 'owner')}). Любая правка сохраняется как новая неутверждённая версия.`
        : dirty ? 'Есть несохранённые правки. Сохраните новую версию — утверждать можно только сохранённую.'
          : head ? 'Сначала посмотрите резюме изменений. Полный текст доступен для просмотра и правки.'
            : 'Опишите резюме и инструкции Worker на основе Task Spec и критериев, затем сохраните Draft.';
  const shownSummary = viewing ? past.summary : summary;
  const shownBody = viewing ? past.body ?? '' : body;

  return <div className="rosc-draft-overlay" onClick={requestClose}>
    <aside className={expanded ? 'rosc-draft-drawer rosc-draft-drawer--wide' : 'rosc-draft-drawer'} role="dialog" aria-modal="true"
      aria-labelledby="rosc-draft-title" onClick={event => event.stopPropagation()}>
      <div className="rosc-draft-head">
        <div><h2 id="rosc-draft-title">{title}</h2><p>{meta}</p></div>
        <div className="rosc-draft-head__actions">
          {shown && <span className={`rosc-tag ${shownState === 'approved' ? 'rosc-tag--success' : shownState === 'stale' ? 'rosc-tag--warning' : 'rosc-tag--info'}`}>{STATE_TEXT[shownState ?? ''] ?? shownState}</span>}
          <button type="button" className="rosc-btn rosc-btn--small" aria-expanded={expanded} onClick={() => setExpanded(value => !value)}>
            {expanded ? 'Свернуть ↔' : 'Развернуть ↔'}</button>
          <button type="button" className="rosc-btn rosc-btn--small" aria-label="Закрыть Draft" onClick={requestClose}>×</button>
        </div>
      </div>
      <div className="rosc-draft-body">
        {revisions.length > 0 && <div className="rosc-draft-history" role="group" aria-label="Версии Prompt">
          <strong>Версии Prompt:</strong>
          {revisions.map(item => {
            const selected = (viewing ? past.revision : head?.revision) === item.revision;
            return <button key={item.revision} type="button" aria-current={selected} disabled={dirty}
              title={dirty ? 'Сохраните или отмените правки, чтобы открыть другую версию' : `Открыть Prompt v${item.revision}`}
              onClick={() => void openRevision(item.revision)}>v{item.revision}{item.current ? ' · текущая' : ''}</button>;
          })}
          {shown && <p className="rosc-draft-history__meta">
            v{shown.revision} · {actorText(shown.actor.kind)} · {formatTime(shown.created_at)} · Spec v{shown.source_spec_revision} · {STATE_TEXT[shownState ?? ''] ?? shownState}
          </p>}
        </div>}
        {viewing && head && <p className="rosc-draft-past" role="status">Просмотр Prompt v{past.revision}; текущая v{head.revision}.
          <button type="button" onClick={() => setPast(null)}>К текущей версии</button></p>}
        <section className="rosc-draft-summary">
          <h3>{(viewing ? past.revision : head?.revision ?? 1) > 1 ? 'Что изменилось по сравнению с предыдущей версией' : 'Что в этом Draft'}</h3>
          <p>Резюме подготовлено автором Draft и не создаётся автоматически. Полный текст — ниже.</p>
          <textarea ref={first} aria-label="Резюме изменений по пунктам" value={shownSummary} readOnly={viewing || data?.editable !== true}
            placeholder="Перечислите по пунктам, что изменено или что содержит Draft" onChange={event => { setSummary(event.target.value); setMessage(''); }} />
        </section>
        <section className="rosc-draft-full">
          <h3>Полный текст Prompt Draft</h3>
          <textarea aria-label="Полный текст Prompt Draft" value={shownBody} readOnly={viewing || data?.editable !== true}
            placeholder="Инструкции Worker на основе Task Spec и критериев" onChange={event => { setBody(event.target.value); setMessage(''); }} />
        </section>
        <p className="rosc-draft-note" role="status">{note}</p>
        {closeWarning && <p className="rosc-draft-note rosc-draft-note--warn" role="alert">Есть несохранённые правки. Сохраните новую версию или закройте ещё раз, чтобы отказаться от них.</p>}
        {movedElsewhere && <p className="rosc-draft-note rosc-draft-note--warn" role="alert">Сохранённая версия изменилась в другом месте; сохранение вашей правки вернёт конфликт версий.</p>}
        {message && <p className="rosc-draft-note" role="status">{message}</p>}
        {failure && <p className="rosc-draft-note rosc-draft-note--error" role="alert">{failure}</p>}
      </div>
      <div className="rosc-draft-foot">
        <button type="button" className="rosc-btn" onClick={() => void copy()} disabled={shownBody === ''}>Скопировать весь текст</button>
        <button type="button" className="rosc-btn" disabled={!canSave} onClick={() => void save()}>Сохранить новую версию</button>
        <button type="button" className="rosc-btn rosc-btn--primary" disabled={!canApprove} onClick={() => void approve()}
          title={viewing ? 'Прежнюю версию утвердить нельзя' : dirty ? 'Сначала сохраните новую версию' : stale ? 'Draft устарел' : approved ? 'Уже утверждён' : undefined}>
          {approved ? `Draft v${head?.revision} утверждён` : head ? `Утвердить Draft v${head.revision}` : 'Утвердить Draft'}</button>
      </div>
    </aside>
  </div>;
}
