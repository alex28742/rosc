import defaultPresentation from '../../presentations/default';
import type { Presentation } from './contracts';

/** The one place that chooses the active presentation package. */
export const ACTIVE_PRESENTATION = 'default';

export const presentations: Readonly<Record<string, Presentation>> = {
  default: defaultPresentation,
};

export function resolvePresentation(key: string = ACTIVE_PRESENTATION, available: Readonly<Record<string, Presentation>> = presentations): Presentation {
  const found = Object.hasOwn(available, key) ? available[key] : undefined;
  if (!found) throw new Error(`Unknown presentation "${key}". Registered: ${Object.keys(available).join(', ') || 'none'}`);
  if (found.id !== key) throw new Error(`Presentation registered as "${key}" declares id "${found.id}"`);
  return found;
}
