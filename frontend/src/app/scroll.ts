import { useMemo } from 'react';

/** What a presentation sees: tag a scroll container with a stable key and its position is remembered. */
export type ScrollRegions = { region: (key: string) => (element: HTMLElement | null) => void };

const SETTLE_MS = 2000;

/**
 * Keeps scroll positions for the session, keyed by logical region ("dashboard", "project:P0001", ...).
 * A region that is unmounted (closed overlay) gets its position back when it is mounted again. Content that
 * arrives after mount (async task list) re-applies the saved position until the user scrolls or SETTLE_MS passes.
 * Pure DOM; no knowledge of routes, Core or presentations.
 */
export class ScrollMemory {
  private positions = new Map<string, number>();
  private cleanups = new Map<string, () => void>();

  read(key: string): number { return this.positions.get(key) ?? 0; }

  attach(key: string, element: HTMLElement): () => void {
    const target = this.positions.get(key) ?? 0;
    let settling = target > 0;
    let applying = false;
    const apply = () => {
      if (!settling) return;
      applying = true;
      element.scrollTop = target;
      applying = false;
    };
    const onScroll = () => {
      if (applying) return;
      settling = false;
      this.positions.set(key, element.scrollTop);
    };
    const stop = () => { settling = false; };
    apply();
    const observer = new MutationObserver(apply);
    observer.observe(element, { childList: true, subtree: true });
    const timer = window.setTimeout(stop, SETTLE_MS);
    element.addEventListener('scroll', onScroll, { passive: true });
    element.addEventListener('wheel', stop, { passive: true });
    element.addEventListener('touchstart', stop, { passive: true });
    element.addEventListener('keydown', stop);
    return () => {
      observer.disconnect();
      window.clearTimeout(timer);
      element.removeEventListener('scroll', onScroll);
      element.removeEventListener('wheel', stop);
      element.removeEventListener('touchstart', stop);
      element.removeEventListener('keydown', stop);
    };
  }

  private refs = new Map<string, (element: HTMLElement | null) => void>();

  /** Stable per key, so React does not detach and reattach the region on every render. */
  region = (key: string): ((element: HTMLElement | null) => void) => {
    let ref = this.refs.get(key);
    if (!ref) {
      ref = (element) => {
        this.cleanups.get(key)?.();
        this.cleanups.delete(key);
        if (element) this.cleanups.set(key, this.attach(key, element));
      };
      this.refs.set(key, ref);
    }
    return ref;
  };
}

export function useScrollRegions(): ScrollRegions {
  return useMemo(() => new ScrollMemory(), []);
}
