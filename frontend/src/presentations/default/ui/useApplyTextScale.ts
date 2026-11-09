import { useCallback } from 'react';
import type { TextScale } from '../../../app/presentation/contracts';

/**
 * Applies the App-owned scale as a CSS custom property on the Work Area root only. A callback ref
 * re-applies it whenever the root or the chosen step changes, so no global or Dashboard style is touched.
 */
export function useApplyTextScale(scale: TextScale) {
  return useCallback((node: HTMLElement | null) => {
    if (node) node.style.setProperty('--rosc-text-scale', String(scale / 100));
  }, [scale]);
}
