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
    case 'confirmed': return { line: `✓ Контекст проекта подготовлен: Worker прочитал ${files} (подтверждено hook-ом CLI).`, tone: 'ok', button: null };
    case 'pending': return { line: 'Ожидаем подтверждение Worker: он читает entry point проекта…', tone: 'neutral', button: null };
    case 'failed': return { line: b.reason === 'reads_missing'
      ? 'Контекст не подготовлен: hook CLI не зафиксировал чтение всех файлов.'
      : 'Контекст не подготовлен: Worker сообщил, что не смог прочитать файлы.', tone: 'warn', button: 'Повторить подготовку' };
    case 'unknown': return { line: 'Подтверждение не получено. Повторно не отправляем: проверьте терминал Worker.', tone: 'warn', button: null };
    case 'stale': return { line: 'Файлы проекта изменились после подготовки: контекст нужно подготовить заново.', tone: 'warn', button: 'Подготовить контекст' };
    default:
      if (b.reason === 'no_sources') return { line: 'В проекте не найден entry point (AGENTS.md, CLAUDE.md или docs entrypoint): читать нечего.', tone: 'neutral', button: null };
      return { line: 'Контекст проекта не подготовлен. Отдельный набор правил Worker в ROSC не определён.', tone: 'neutral', button: b.can_bootstrap ? 'Подготовить контекст' : null };
  }
}

/** The terminal help is only worth showing where Owner may need the CLI (trust prompt, unclear state) and a session exists. */
export const showsTerminalHelp = (session: WorkerSession) =>
  session.terminal !== null || session.state === 'unknown';
