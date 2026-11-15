type Pane = 'work' | 'chat';

type Props = {
  pane: Pane;
  /** Vertical strip label while the Conversation is open, e.g. TASKS (Project) or TASK (Task). */
  workLabel: string;
  /** Screen-reader names of the two panes. */
  workName: string;
  chatName: string;
  onSwitch: (next: Pane) => void;
};

/**
 * P048 narrow-width pane switch (UX v0.8): a ~40px strip on the edge of the Work Area. It only moves the two outer
 * panes sideways via CSS; both stay mounted, so drafts, history, filters and scroll positions survive. On a wide
 * screen it is not displayed and the desktop two-column layout (with its own chat collapse) is unchanged.
 */
export function MobilePaneSwitch({ pane, workLabel, workName, chatName, onSwitch }: Props) {
  const chatOpen = pane === 'chat';
  return <button type="button" className="rosc-pane-switch" aria-pressed={chatOpen}
    aria-label={chatOpen ? `Вернуться к ${workName}` : `Открыть ${chatName}`}
    onClick={() => onSwitch(chatOpen ? 'work' : 'chat')}>
    <span className="rosc-pane-switch__arrow" aria-hidden="true">{chatOpen ? '›' : '‹'}</span>
    <span className="rosc-pane-switch__label" aria-hidden="true">{chatOpen ? workLabel : 'CHAT'}</span>
  </button>;
}
