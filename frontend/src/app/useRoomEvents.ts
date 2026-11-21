import { useEffect, useRef, useState } from 'react';
import type { DraftFrame } from '../core/models';
import { parseDraftFrame } from './provisionalDrafts';

/** P061: the live-update channel of ONE open room.  It is never a business status: `offline` only means the browser is
 * not currently receiving Core's invalidation stream, so the room is re-read on the bounded fallback instead. */
export type RoomLiveState = 'off' | 'connecting' | 'live' | 'offline';

/** Burst guard: invalidations are coalesced so that at most one revalidation starts per this interval (trailing). */
export const REVALIDATE_GAP_MS = 400;
/** A transient drop (e.g. the server's bounded stream lifetime ends and the browser reconnects) is not shown at all. */
export const OFFLINE_GRACE_MS = 5000;
/** While the stream is down and the tab is visible, the room is re-read at most this often. */
export const FALLBACK_POLL_MS = 15000;
/** A stream the browser gave up on (HTTP error) is reopened with a capped exponential backoff. */
const REOPEN_BASE_MS = 2000;
const REOPEN_MAX_MS = 60000;

/**
 * Subscribes to Core's read-only room SSE (`hello` / `invalidate` / `heartbeat`, never message text) while `open`
 * returns a source, and calls `onChange` — which must only re-read the ordinary Core GETs — on connect, on every
 * reconnect, on each (coalesced) invalidation and when the tab regains focus/visibility.  A hidden tab holds no
 * connection and issues no background reads; returning revalidates once.  `open` returning null (no EventSource in
 * this environment) leaves the caller's existing behaviour unchanged.
 *
 * P062: the same stream also carries typed `draft` frames (provisional agent text).  `onDraft` receives each valid frame
 * of the CURRENT source, and `null` whenever the previous provisional state can no longer be trusted (a fresh
 * connection — whose own snapshot follows —, a closed stream, a room change).
 */
export function useRoomEvents(key: string | null, open: () => EventSource | null, onChange: () => void,
  onDraft?: (frame: DraftFrame | null) => void): RoomLiveState {
  const [state, setState] = useState<RoomLiveState>('off');
  const openRef = useRef(open);
  const onChangeRef = useRef(onChange);
  openRef.current = open;
  onChangeRef.current = onChange;
  const onDraftRef = useRef(onDraft);
  onDraftRef.current = onDraft;

  useEffect(() => {
    if (!key) { setState('off'); return; }
    let disposed = false;
    let source: EventSource | null = null;
    let lastRun = 0;
    let failures = 0;
    let inert = false; // no EventSource here: never read on its behalf
    let throttle: ReturnType<typeof setTimeout> | undefined;
    let grace: ReturnType<typeof setTimeout> | undefined;
    let reopen: ReturnType<typeof setTimeout> | undefined;
    let fallback: ReturnType<typeof setInterval> | undefined;
    const hidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden';

    const revalidate = () => {
      if (disposed || inert || throttle !== undefined || hidden()) return;
      throttle = setTimeout(() => {
        throttle = undefined;
        if (disposed || hidden()) return;
        lastRun = Date.now();
        onChangeRef.current();
      }, Math.max(0, lastRun + REVALIDATE_GAP_MS - Date.now()));
    };
    const stopFallback = () => { if (fallback !== undefined) { clearInterval(fallback); fallback = undefined; } };
    const goOffline = () => {
      if (disposed) return;
      setState('offline');
      if (fallback === undefined) fallback = setInterval(revalidate, FALLBACK_POLL_MS);
    };
    const clearGrace = () => { if (grace !== undefined) { clearTimeout(grace); grace = undefined; } };
    /** Closes the current source and a pending reopen; the offline grace keeps running until a `hello` arrives. */
    const close = () => {
      if (source) { source.close(); source = null; onDraftRef.current?.(null); }
      if (reopen !== undefined) { clearTimeout(reopen); reopen = undefined; }
    };

    const connect = (first?: EventSource | null) => {
      close();
      if (disposed || hidden()) return;
      const es = first ?? openRef.current();
      if (!es) { inert = true; setState('off'); return; }
      source = es;
      setState(prev => (prev === 'offline' ? prev : 'connecting'));
      es.addEventListener('hello', () => {
        if (source !== es) return;
        failures = 0;
        clearGrace();
        stopFallback();
        setState('live');
        onDraftRef.current?.(null); // this connection's own draft snapshot follows
        revalidate(); // a fresh (re)connection: whatever was missed meanwhile is read from Core
      });
      es.addEventListener('draft', event => {
        if (source !== es) return;
        const frame = parseDraftFrame((event as MessageEvent).data);
        if (frame) onDraftRef.current?.(frame);
      });
      es.addEventListener('invalidate', () => { if (source === es) revalidate(); });
      es.onerror = () => {
        if (source !== es) return;
        if (grace === undefined) grace = setTimeout(() => { grace = undefined; goOffline(); }, OFFLINE_GRACE_MS);
        if (es.readyState === 2 /* CLOSED: the browser will not retry by itself */) {
          es.close();
          source = null;
          failures += 1;
          const delay = Math.min(REOPEN_MAX_MS, REOPEN_BASE_MS * 2 ** (failures - 1));
          reopen = setTimeout(() => { reopen = undefined; connect(); }, delay);
        }
        // CONNECTING: the browser reconnects after the server's `retry:` and the next `hello` revalidates.
      };
    };

    const onVisibility = () => {
      if (hidden()) { close(); clearGrace(); stopFallback(); return; }
      if (!source && reopen === undefined) connect(); // its `hello` revalidates
      else revalidate();
    };
    const onFocus = () => { if (!hidden()) revalidate(); };

    // No EventSource in this environment: stay completely inert (no listeners, no extra reads); the caller's
    // ordinary behaviour is unchanged.
    const initial = hidden() ? undefined : openRef.current();
    if (initial === null) { setState('off'); return; }
    connect(initial);
    document.addEventListener('visibilitychange', onVisibility);
    window.addEventListener('focus', onFocus);
    return () => {
      disposed = true;
      close();
      clearGrace();
      stopFallback();
      if (throttle !== undefined) clearTimeout(throttle);
      document.removeEventListener('visibilitychange', onVisibility);
      window.removeEventListener('focus', onFocus);
    };
  }, [key]);

  return key ? state : 'off';
}
