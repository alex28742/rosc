import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError, type CoreApi } from '../core/api';
import type { ConversationDiagnostics } from '../core/models';
import type { DiagnosticsControls, LoadStatus } from './presentation/contracts';

/** P052 r03: the Owner's Conversation Diagnostics switches, read from Core while ROSC Settings is open. */
export function useConversationDiagnostics(api: CoreApi, open: boolean): DiagnosticsControls {
  const [status, setStatus] = useState<LoadStatus>('loading');
  const [value, setValue] = useState<ConversationDiagnostics | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const next = await api.getConversationDiagnostics();
      if (mine !== seq.current) return;
      setValue(next); setStatus('ready'); setError('');
    } catch (e) {
      if (mine !== seq.current) return;
      setStatus('failed'); setError(describeError(e));
    }
  }, [api]);

  useEffect(() => { if (open) void load(); else seq.current++; }, [open, load]);

  const onChange = useCallback(async (change: { enabled?: boolean; include_bodies?: boolean }) => {
    if (busyRef.current) return 'Предыдущее изменение ещё сохраняется.';
    busyRef.current = true;
    setBusy(true);
    seq.current++;
    try {
      const next = await api.updateConversationDiagnostics(change);
      setValue(next); setStatus('ready'); setError('');
      return null;
    } catch (e) {
      const message = describeError(e);
      setError(message);
      void load();
      return message;
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }, [api, load]);

  return { status, value, error, busy, onReload: () => void load(), onChange };
}
