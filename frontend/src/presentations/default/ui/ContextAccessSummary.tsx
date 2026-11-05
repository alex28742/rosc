import type { ConversationControls } from '../../../app/presentation/contracts';

const MODE_LABEL = { read_only: 'только чтение', read_write: 'чтение и запись', denied: 'запрещено' } as const;
const TRANSPORT_LABEL = {
  active: 'ROSC Tools подключены к активному Architect',
  inactive: 'ROSC Tools не подключены к этой сессии (например, после перезапуска ROSC) — активируйте Architect заново',
  not_running: 'ROSC Tools подключатся при явной активации Architect',
  not_available: 'Runtime не поддерживает ROSC Tools',
} as const;

type Props = Pick<ConversationControls, 'contextAccess'>;

/**
 * Compact, read-only summary of what the Project Architect can reach through ROSC Tools. It states the effective
 * access and the Tool transport state; it offers no switches. Opening it changes nothing and sends nothing.
 */
export function ContextAccessSummary({ contextAccess }: Props) {
  const { info, status, error } = contextAccess;
  if (status === 'loading') return null;
  if (status === 'failed' || info === null) return <p className="rosc-conversation__hint" role="alert">
    Не удалось получить сводку доступа Architect. {error}</p>;
  const model = info.provider.model_id ? `${info.provider.provider_id}/${info.provider.model_id}` : null;
  return <details className="rosc-access">
    <summary>Доступ Architect к контексту</summary>
    <ul className="rosc-access__matrix" aria-label="Матрица доступа">
      {info.matrix.map(row => <li key={row.resource} className={`rosc-access__row rosc-access__row--${row.mode}`}>
        <strong>{MODE_LABEL[row.mode]}</strong> · {row.resource}
        {row.mode !== 'denied' && <small> — {row.operations}</small>}
      </li>)}
    </ul>
    <p className="rosc-conversation__hint">{TRANSPORT_LABEL[info.tools.transport]}.</p>
    <p className="rosc-conversation__hint">
      {model ? `Провайдер/модель: ${model}. ` : 'Architect не назначен. '}
      Во время активной беседы результаты разрешённых Tools (фрагменты документов и исходников) могут отправляться
      выбранному вами провайдеру/модели. При открытии проекта и при активации ничего не отправляется.</p>
    <p className="rosc-conversation__hint">Приватные документы не публикуются и не синхронизируются автоматически; Architect не выполняет
      Git-операции и не меняет исходники — это делают Worker.</p>
    {error && <p className="rosc-conversation__error" role="alert">{error}</p>}
  </details>;
}
