import type { ProjectWorker, WorkerSession } from '../../../core/models';

/** Human wording for a non-secret Core reason code; unknown codes are shown as-is rather than guessed at. */
export const WORKER_REASONS: Record<string, string> = {
  no_assignment: 'Worker не назначен на этот проект.',
  unsupported_adapter: 'Runtime этого агента не поддерживается: нужен Claude Code CLI.',
  tmux_missing: 'tmux не установлен на этом компьютере.',
  cli_missing: 'Claude Code CLI не найден в PATH.',
  root_missing: 'Каталог проекта недоступен.',
  project_identity: 'Данные привязки проекта повреждены.',
  spawn_failed: 'Не удалось запустить CLI-сессию. Ничего не было отправлено.',
  never_activated: 'ROSC создаст обычную CLI-сессию в tmux.',
  vanished: 'Прежняя сессия завершилась. Можно активировать новую.',
  awaiting_ack: 'CLI запускается; ожидаем подтверждение инициализации сессии.',
  initialised: 'CLI-сессия инициализирована и жива. Принятие задания подтверждается отдельно при Run.',
  no_ack: 'CLI запущен, но не подтвердил инициализацию. Возможно, терминал ждёт ответа (например, доверия папке проекта).',
  process_mismatch: 'Процесс сессии не совпадает с тем, что запускал ROSC. Сессия не используется.',
  session_missing_process_alive: 'tmux-сессии нет, но процесс ещё жив. Новая не создаётся, чтобы не было двух исполнителей.',
  foreign_session: 'Найдена посторонняя сессия с тем же именем. ROSC её не трогает.',
  tmux_unobservable: 'Не удалось проверить tmux. Состояние неизвестно.',
};

export type WorkerTone = 'ok' | 'warn' | 'neutral' | 'error';
export type WorkerView = { chip: string; tone: WorkerTone; note: string; button: 'activate' | 'retry' | 'check' | 'none'; activating: boolean };

export const workerReason = (code: string) => WORKER_REASONS[code] ?? `Код: ${code}`;

/** Never shows «активна» unless Core verified a live process incarnation AND its CLI-side acknowledgement. */
export function describeWorker(worker: ProjectWorker | null, busy: boolean): WorkerView {
  if (!worker) return { chip: 'Не выбран', tone: 'neutral', note: 'Выберите исполнителя выше', button: 'none', activating: false };
  const s: WorkerSession = worker.session;
  const note = workerReason(s.reason);
  switch (s.state) {
    case 'active': return { chip: 'CLI-сессия активна', tone: 'ok', note, button: 'check', activating: false };
    case 'connecting': return { chip: 'Подключение…', tone: 'neutral', note, button: 'none', activating: true };
    case 'not_connected': return { chip: s.reason === 'vanished' ? 'Не на связи' : 'Нет сессии', tone: 'neutral', note, button: 'activate', activating: busy };
    case 'unknown': return { chip: 'Неизвестно', tone: 'warn', note, button: s.can_check ? 'check' : 'none', activating: false };
    case 'unavailable': return { chip: 'Недоступен', tone: 'error', note, button: 'none', activating: false };
    default: return { chip: 'Не назначен', tone: 'neutral', note, button: 'none', activating: false };
  }
}

export function workerOptionLabel(worker: ProjectWorker): string {
  const s = worker.session;
  const tail = s.state === 'active' ? 'CLI-сессия активна' : s.state === 'connecting' ? 'подключение…'
    : s.state === 'not_connected' ? (s.reason === 'vanished' ? 'нет связи' : 'нет сессии')
    : s.state === 'unknown' ? 'неизвестно' : s.state === 'unavailable' ? 'недоступен' : 'не назначен';
  return `${worker.agent.display_name} · ${tail}`;
}
