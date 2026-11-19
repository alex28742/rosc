import type { LoadStatus } from '../../../app/presentation/contracts';
import type { ProjectWorker, WorkerBootstrap, WorkerSession } from '../../../core/models';

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
export type WorkerView = { chip: string; tone: WorkerTone; note: string; button: 'activate' | 'check' | 'none'; activating: boolean };

export const workerReason = (code: string) => WORKER_REASONS[code] ?? `Код: ${code}`;

/** Never shows «активна» unless Core verified a live process incarnation AND its CLI-side acknowledgement. */
export function describeWorker(worker: ProjectWorker, busy: 'check' | 'activate' | 'bootstrap' | undefined): WorkerView {
  const s: WorkerSession = worker.session;
  const note = workerReason(s.reason);
  switch (s.state) {
    case 'active': return { chip: 'CLI-сессия активна', tone: 'ok', note, button: 'check', activating: false };
    case 'connecting': return { chip: 'Подключение…', tone: 'neutral', note, button: 'none', activating: true };
    case 'not_connected': return { chip: s.reason === 'vanished' ? 'Не на связи' : 'Нет сессии', tone: 'neutral', note, button: 'activate', activating: busy === 'activate' };
    case 'unknown': return { chip: 'Неизвестно', tone: 'warn', note, button: s.can_check ? 'check' : 'none', activating: false };
    case 'unavailable': return { chip: 'Недоступен', tone: 'error', note, button: 'none', activating: false };
    default: return { chip: 'Не назначен', tone: 'neutral', note, button: 'none', activating: false };
  }
}

export type BootstrapView = { line: string; tone: 'ok' | 'warn' | 'neutral'; button: string | null };

/** Separate, honest wording for the project-context step. «Подготовлен» is never «готов к диалогу» or «готов к Run». */
export function describeBootstrap(session: WorkerSession): BootstrapView {
  const b: WorkerBootstrap = session.bootstrap;
  const files = b.sources.join(', ');
  if (session.state !== 'active') return { line: 'Контекст проекта готовится после подтверждения CLI-сессии.', tone: 'neutral', button: null };
  switch (b.state) {
    case 'confirmed': return { line: b.reason === 'identity_acknowledged'
      ? '✓ Worker подтвердил данные проекта ROSC (имя, ID, корень).'
      : `✓ Контекст проекта подготовлен: Worker прочитал ${files} (подтверждено hook-ом CLI).`, tone: 'ok', button: null };
    case 'pending': return { line: 'Ожидаем подтверждение Worker: он читает entry point проекта…', tone: 'neutral', button: null };
    case 'failed': return { line: b.reason === 'reads_missing'
      ? 'Контекст не подготовлен: hook CLI не зафиксировал чтение всех файлов.'
      : 'Контекст не подготовлен: Worker сообщил, что не смог прочитать файлы.', tone: 'warn', button: 'Повторить подготовку' };
    case 'unknown': return { line: 'Подтверждение не получено. Повторно не отправляем: проверьте терминал Worker.', tone: 'warn', button: null };
    case 'stale': return { line: 'Файлы проекта изменились после подготовки: контекст нужно подготовить заново.', tone: 'warn', button: 'Подготовить контекст' };
    default:
      if (b.reason === 'identity_missing') return { line: 'Эта CLI-сессия запущена до P047 и не получила проверенные данные проекта ROSC (имя, ID, корень; Git не обязателен). Представьте проект один раз — без чтения файлов и изменений.', tone: 'warn', button: b.can_bootstrap ? 'Представить проект' : null };
      // P055: an empty new Project needs no documentation, only the Worker's native acknowledgement of the ROSC identity.
      if (b.reason === 'no_sources') return { line: 'В проекте нет entry point (AGENTS.md, CLAUDE.md или docs entrypoint) — создавать их не нужно. Для формального Run Worker должен один раз подтвердить данные проекта ROSC (имя, ID, корень), без чтения файлов и изменений.', tone: 'warn', button: b.can_bootstrap ? 'Подтвердить данные проекта' : null };
      return { line: 'Контекст проекта не подготовлен. Отдельный набор правил Worker в ROSC не определён.', tone: 'neutral', button: b.can_bootstrap ? 'Подготовить контекст' : null };
  }
}

/** The terminal help is only worth showing where Owner may need the CLI (trust prompt, unclear state) and a session exists. */
export const showsTerminalHelp = (session: WorkerSession) =>
  session.terminal !== null || session.state === 'unknown';

export type WorkerSummaryTone = 'on' | 'off' | 'warn' | 'unknown';

/**
 * P047: one truthful Project-level summary of the interactive (tmux) Worker CLI sessions, derived ONLY from the verified
 * /workers read. Never a placeholder `0`: while loading it is «проверяется», on a failed read «неизвестно».
 */
export function summarizeWorkers(status: LoadStatus, workers: ProjectWorker[]): { text: string; tone: WorkerSummaryTone } {
  if (status === 'loading') return { text: 'Worker CLI: проверяется', tone: 'unknown' };
  if (status === 'failed') return { text: 'Worker CLI: неизвестно', tone: 'warn' };
  if (workers.length === 0) return { text: 'Workers не назначены', tone: 'off' };
  const active = workers.filter(item => item.session.state === 'active').length;
  const unclear = workers.some(item => item.session.state === 'unknown' || item.session.state === 'unavailable');
  return { text: `Worker CLI ${active}/${workers.length} ${active > 0 ? 'активен' : 'не активен'}`,
    tone: active > 0 ? 'on' : unclear ? 'warn' : 'off' };
}
