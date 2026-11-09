import type { LoadStatus } from '../../../app/presentation/contracts';
import type { ArchitectStatus } from '../../../core/models';

/** Human wording for a non-secret Core reason code; unknown codes are shown as-is rather than guessed at. */
export const ARCHITECT_REASONS: Record<string, string> = {
  no_active_architect_assignment: 'Для проекта не назначен активный Architect.',
  unsupported_runtime_adapter: 'Назначенный агент использует не поддерживаемый runtime.',
  runtime_binary_missing: 'Исполняемый файл OpenCode не найден.',
  runtime_credential_missing: 'Локальный пароль OpenCode недоступен; ROSC создаёт его при активации.',
  secret_provisioning_failed: 'ROSC не смог создать локальный пароль runtime — процесс не запускался.',
  runtime_secret_missing: 'Локальный пароль запущенного runtime не найден; повторный запуск заблокирован до остановки.',
  server_auth_failed: 'Локальный runtime отклонил пароль ROSC; запрос к модели не отправлялся.',
  model_not_approved: 'Модель агента не утверждена Owner для передачи данных.',
  provider_key_not_configured: 'Ключ провайдера OpenCode Go не подключён (ROSC_OPENCODE_PROVIDER_KEY_FILE).',
  provider_key_missing: 'Файл ключа провайдера OpenCode Go не найден.',
  provider_key_unsafe: 'Файл ключа провайдера небезопасен (права, владелец, ссылка или каталог) — не использован.',
  provider_key_invalid: 'Файл ключа провайдера пуст или имеет неверный формат.',
  runtime_not_alive: 'Runtime перестал отвечать.',
  restricted_mode_unconfirmed: 'Ограниченный режим runtime не подтверждён — запрос к модели не отправлялся.',
  model_unavailable: 'Модель недоступна в runtime.',
  start_timeout: 'Runtime не запустился вовремя.',
  interrupted: 'Запуск был прерван перезапуском ROSC.',
  assignment_changed: 'Назначение Architect изменилось.',
  process_not_owned: 'Процесс не принадлежит ROSC и не был остановлен.',
  runtime_status_unknown: 'Runtime не отвечает; возможно живой процесс сохранён для безопасного разрешения.',
  runtime_identity_missing: 'Недостаточно данных, чтобы безопасно остановить runtime.',
  stop_unconfirmed: 'Остановка runtime не подтверждена; повторный запуск заблокирован.',
  reply_too_large: 'Ответ был создан, но старая версия ROSC отказалась его сохранить из-за длины. Его можно восстановить без нового запроса к модели, пока сессия Architect активна.',
  reply_retrieval_resources: 'Ответ создан, но сейчас не хватает ресурсов, чтобы получить его целиком. Повторный запрос к модели не выполнялся — проверьте результат позже.',
  reply_storage_resources: 'Ответ создан, но сейчас не хватает ресурсов, чтобы сохранить его целиком. Повторный запрос к модели не выполнялся — проверьте результат позже.',
  start_cleanup_unconfirmed: 'Запуск не завершён, а остановка процесса не подтверждена.',
};

export const architectReason = (code: string | null) => (code ? ARCHITECT_REASONS[code] ?? `Код: ${code}` : '');

export type ArchitectTone = 'on' | 'off' | 'error';

export type ArchitectView = {
  /** State without the `Architect: ` prefix, for a row that is already labelled Architect. */
  state: string;
  /** Full label used by the compact conversation header chip. */
  label: string;
  live: boolean;
  tone: ArchitectTone;
  hint: string;
};

/** Never shows anything as active unless Core reported a freshly verified `ready` session. */
export function describeArchitect(info: ArchitectStatus | null, loaded: boolean, busy: boolean): ArchitectView {
  const view = (state: string, tone: ArchitectTone, hint = '', live = false): ArchitectView =>
    ({ state, label: `Architect: ${state}`, live, tone, hint });
  if (busy) return view('запускается…', 'off', 'Идёт активация и проверка runtime.');
  if (!loaded || info === null) return view('проверка…', 'off');
  switch (info.state) {
    case 'ready': return view('активен (проверен)', 'on', '', true);
    case 'starting': return view('запускается…', 'off');
    case 'error': return view('ошибка', 'error', architectReason(info.detail));
    case 'not_configured': return view('не настроен', 'off', architectReason(info.detail));
    default: return view('не активирован', 'off', architectReason(info.detail));
  }
}

/** Short Project header wording for the same state; a failed load is not presented as a known lifecycle state. */
export function architectHeadline(status: LoadStatus, info: ArchitectStatus | null, busy: boolean): string {
  if (busy) return 'запускается';
  if (status === 'failed') return 'статус не проверен';
  if (status !== 'ready' || info === null) return 'проверяется';
  switch (info.state) {
    case 'ready': return 'активен (проверен)';
    case 'starting': return 'запускается';
    case 'error': return 'требует внимания';
    case 'not_configured': return 'не настроен';
    default: return 'не активирован';
  }
}
