import { useEffect, useState } from 'react';
import { TEXT_SCALE_STEPS, type TextScale } from './presentation/contracts';

const STORAGE_PREFIX = 'rosc.text-scale.';
const isTextScale = (value: number): value is TextScale => (TEXT_SCALE_STEPS as readonly number[]).includes(value);

function read(scope: string | null): TextScale {
  if (!scope || typeof window === 'undefined') return 100;
  try {
    const raw = window.localStorage.getItem(`${STORAGE_PREFIX}${scope}`);
    if (raw === null) return 100;
    const parsed = Number(raw);
    if (!isTextScale(parsed)) {
      window.localStorage.removeItem(`${STORAGE_PREFIX}${scope}`);
      return 100;
    }
    return parsed;
  } catch {
    return 100;
  }
}

function write(scope: string | null, scale: TextScale) {
  if (!scope) return;
  try {
    window.localStorage.setItem(`${STORAGE_PREFIX}${scope}`, String(scale));
  } catch {
    // Storage can be unavailable; the in-memory choice still applies for this visit.
  }
}

/**
 * App-owned local text scale for one scope (`project:<id>` or `task:<id>`). The presentation receives the
 * value through its view contract, so it never touches storage itself. An invalid stored value is cleared.
 */
export function useTextScale(scope: string | null) {
  const [scale, setScaleState] = useState<TextScale>(() => read(scope));

  useEffect(() => {
    setScaleState(read(scope));
  }, [scope]);

  const setScale = (next: TextScale) => {
    setScaleState(next);
    write(scope, next);
  };

  return { scale, onChange: setScale };
}
