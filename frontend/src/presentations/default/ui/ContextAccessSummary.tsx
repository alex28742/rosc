import type { DisclosureCategory } from '../../../core/models';
import type { ConversationControls } from '../../../app/presentation/contracts';

const MODE_LABEL = { read_only: 'только чтение', read_write: 'чтение и запись', denied: 'запрещено' } as const;
const TRANSPORT_LABEL = {
  active: 'ROSC Tools подключены к активному Architect',
  inactive: 'ROSC Tools не подключены к этой сессии (например, после перезапуска ROSC) — активируйте Architect заново',
  not_running: 'ROSC Tools подключатся при явной активации Architect',
  not_available: 'Runtime не поддерживает ROSC Tools',
} as const;
const DISCLOSURE: { category: DisclosureCategory; label: string }[] = [
  { category: 'project_docs_own', label: 'Документы этого проекта (ROSC-private)' },
  { category: 'project_docs_other', label: 'Документы других проектов (только чтение)' },
  { category: 'project_source', label: 'Исходники привязанного checkout этого проекта (только чтение)' },
];

type Props = Pick<ConversationControls, 'contextAccess' | 'onSetDisclosure'>;

/**
 * Compact, read-mostly summary of what the Project Architect can reach through ROSC Tools and which private content
 * the Owner has allowed to be sent to the selected provider/model. Opening it changes nothing; each checkbox is an
 * explicit Owner decision for that one category.
 */
export function ContextAccessSummary({ contextAccess, onSetDisclosure }: Props) {
  const { info, status, busy, error } = contextAccess;
  if (status === 'loading') return null;
  if (status === 'failed' || info === null) return <p className="rosc-conversation__hint" role="alert">
    Не удалось получить сводку доступа Architect. {error}</p>;
  const enabled = (category: DisclosureCategory) =>
    info.disclosure.scopes.some(scope => scope.category === category && scope.covered === 'all' && scope.enabled);
  return <details className="rosc-access">
    <summary>Доступ Architect к контексту</summary>
    <ul className="rosc-access__matrix" aria-label="Матрица доступа">
      {info.matrix.map(row => <li key={row.resource} className={`rosc-access__row rosc-access__row--${row.mode}`}>
        <strong>{MODE_LABEL[row.mode]}</strong> · {row.resource}
        {row.mode !== 'denied' && <small> — {row.operations}</small>}
      </li>)}
    </ul>
    <p className="rosc-conversation__hint">{TRANSPORT_LABEL[info.tools.transport]}.</p>
    <fieldset className="rosc-access__disclosure" disabled={busy || info.disclosure.model_id === null}>
      <legend>Передача модели {info.disclosure.model_id ? `${info.disclosure.provider_id}/${info.disclosure.model_id}` : '— Architect не назначен'}</legend>
      {DISCLOSURE.map(item => <label key={item.category}>
        <input type="checkbox" role="switch" checked={enabled(item.category)}
          onChange={event => void onSetDisclosure(item.category, event.target.checked)} />
        {item.label}
      </label>)}
      <small>По умолчанию ничего не передаётся. Без разрешения Architect получает ответ «consent_required» без содержимого.
        Содержимое, разрешённое здесь, покидает этот компьютер и попадает к провайдеру модели.</small>
    </fieldset>
    <p className="rosc-conversation__hint">Приватные документы не публикуются и не синхронизируются автоматически; Architect не выполняет
      Git-операции и не меняет исходники — это делают Worker.</p>
    {error && <p className="rosc-conversation__error" role="alert">{error}</p>}
  </details>;
}
