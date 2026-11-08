import type { EngineNotice, EngineNoticeKind } from '../../../app/presentation/contracts';

const GLYPH: Record<EngineNoticeKind, string> = {
  progress: '', success: '✓', warning: '!', error: '!',
};

/** The approved v0.7 stack never shows more than three cards at once. */
const MAX_VISIBLE = 3;

/**
 * Approved v0.7 Engine system popups. Presentation only: it renders typed notices and reports a dismiss
 * intent. It never talks to Core, starts a demo scenario or creates a conversation message.
 */
export function EngineNoticeStack({ notices, onDismiss }: {
  notices: readonly EngineNotice[];
  onDismiss: (key: string) => void;
}) {
  if (notices.length === 0) return null;
  const visible = notices.slice(-MAX_VISIBLE).reverse();
  return <div className="rosc-engine-notice-stack" aria-label="Всплывающие системные уведомления ROSC">
    {visible.map(notice => {
      const role = notice.kind === 'error' ? 'alert' : 'status';
      return <section key={notice.key} className={`rosc-engine-notice rosc-engine-notice--${notice.kind}`} role={role}>
        <div className="rosc-engine-notice__icon" aria-hidden="true">
          {notice.kind === 'progress'
            ? <span className="rosc-engine-notice__spin" />
            : GLYPH[notice.kind]}
        </div>
        <div className="rosc-engine-notice__body">
          <p className="rosc-engine-notice__kicker">{notice.kicker}</p>
          <h3 className="rosc-engine-notice__title">{notice.title}</h3>
          <p className="rosc-engine-notice__desc">{notice.description}</p>
        </div>
        <button type="button" className="rosc-engine-notice__dismiss" title="Закрыть уведомление"
          aria-label="Закрыть уведомление" onClick={() => onDismiss(notice.key)}>×</button>
      </section>;
    })}
  </div>;
}
